#!/usr/bin/env python3
"""Repeat paired scenarios without overwriting prior evidence.

Example: .venv/bin/python scripts/run_validation_suite.py --seeds 7 17 27
Defaults: all four scenarios, all four controllers, full scenario durations.
The aggregate is a distribution of per-run metrics, not pooled frame latencies.
"""
import argparse
import asyncio
import datetime
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from echo_sim import evidence as EV, events as E, scenarios as S
from echo_sim.config import RunConfig
from echo_sim.runner import record_run


def aggregate(rows):
    groups = {}
    for r in rows:
        key = (r['scenario'], r['mode'])
        groups.setdefault(key, []).append(r)
    out = []
    for (scenario, mode), group in groups.items():
        item = dict(scenario=scenario, mode=mode, n=len(group),
                    seeds=[r['seed'] for r in group], run_ids=[r['run_id'] for r in group])
        item['metrics'] = {}
        for name in ['late_results_pct','p95_ms','results_lost','unnecessary_reversals','failed_handovers','longest_gap_ms']:
            values = [r['summary'][name] for r in group if r['summary'].get(name) is not None]
            item['metrics'][name] = dict(mean=statistics.mean(values),
                median=statistics.median(values), minimum=min(values), maximum=max(values),
                sample_sd=statistics.stdev(values) if len(values)>1 else None) if values else None
        out.append(item)
    return out


async def run(args):
    root=Path(EV.RUNS).parent/'suites'
    root.mkdir(exist_ok=True)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination=root/(stamp+'.json')
    source=EV.source_hash()
    report=dict(status='running', source_sha256=source, seeds=args.seeds,
                note='Paired seeds; scheduler timing and transport packet patterns still vary. Small samples are descriptive, not proof of significance.',
                rows=[], comparisons=[])
    def save():
        report['aggregate']=aggregate(report['rows'])
        destination.write_text(json.dumps(report,indent=2))
    save()
    try:
        for sid in args.scenarios:
            for seed in args.seeds:
                ids=[]
                # Rotate ordering to reduce a systematic first/last-mode effect.
                modes=args.modes[seed % len(args.modes):]+args.modes[:seed % len(args.modes)]
                for mode in modes:
                    if EV.source_hash()!=source:
                        raise RuntimeError('Engine source changed during the suite; refusing to mix versions.')
                    cfg=RunConfig(mode=mode,scenario_id=sid,seed=seed,duration_s=args.duration or S.get(sid).default_duration_s, stable_link_hold_s=args.stable_link_hold)
                    manifest=await record_run(cfg)
                    if manifest['status']!='completed': raise RuntimeError('Run did not complete: '+manifest['run_id'])
                    rid=manifest['run_id']
                    if not EV.verify_run(rid)['ok']: raise RuntimeError('Evidence verification failed: '+rid)
                    summary=EV.read_json(rid,'summary.json')['summary']
                    report['rows'].append(dict(scenario=sid,mode=mode,seed=seed,run_id=rid,summary=summary))
                    ids.append(rid);save()
                    print(sid,seed,mode,'late=',summary['late_results_pct'],'reversals=',summary['unnecessary_reversals'],flush=True)
                    await asyncio.sleep(1)
                report['comparisons'].append(EV.build_comparison(ids,note='Repeated validation suite '+stamp)['comparison_id']);save()
        report['status']='completed'
    except BaseException as exc:
        report['status']='interrupted_or_failed';report['error']=str(exc);raise
    finally:
        save();print('Suite evidence:',destination,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seeds',type=int,nargs='+',default=[7,17,27])
    p.add_argument('--scenarios',nargs='+',choices=list(S.SCENARIOS),default=list(S.SCENARIOS))
    p.add_argument('--modes',nargs='+',choices=list(E.CONTROLLER_MODES),default=list(E.CONTROLLER_MODES))
    p.add_argument('--stable-link-hold', type=float, default=0, help='Experimental sustained-benefit wait in seconds; default disabled.')
    p.add_argument('--duration',type=float,help='Override seconds; shortened runs are smoke checks, not full-scenario validation.')
    args=p.parse_args()
    if len(set(args.seeds))!=len(args.seeds): p.error('Seeds must be distinct.')
    if len(set(args.modes))!=len(args.modes): p.error('Modes must be distinct.')
    if args.duration is not None and args.duration<20: p.error('Duration must be at least 20 seconds.')
    asyncio.run(run(args))
