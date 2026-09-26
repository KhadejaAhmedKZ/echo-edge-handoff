"""Local gateway integration. Credentials stay in the gateway process."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import urllib.request
from contextlib import asynccontextmanager
from fastapi import HTTPException

PROJECT = Path(__file__).resolve().parents[2]
GATEWAY = PROJECT / '06_satellite-dashboard'
BASE = 'http://127.0.0.1:3103'


def fetch_json(path, timeout=8):
    try:
        with urllib.request.urlopen(BASE+path, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        raise HTTPException(503, 'Satellite or model service unavailable. Check the local gateway and API quota; no estimated values have been substituted.') from None


@asynccontextmanager
async def lifespan(app):
    child = None
    try:
        # Reuse the existing ECHO gateway if already running.
        await asyncio.to_thread(fetch_json, '/api/ml/status?sample=0', 2)
    except HTTPException:
        node = shutil.which('node')
        if node and (GATEWAY/'server/start.mjs').exists():
            env = os.environ.copy()
            from dotenv import dotenv_values
            # Read only the needed credential; never put it in browser assets,
            # URLs returned to the user, logs, or command-line arguments.
            for path in [PROJECT/'.env', GATEWAY/'.env']:
                if path.exists():
                    value = dotenv_values(path).get('N2YO_API_KEY')
                    if value: env['N2YO_API_KEY'] = value
            env['PORT']='3103';env['ML_PORT']='3102'
            env['ECHO_PYTHON']=str(GATEWAY/'.venv/bin/python')
            env.pop('ECHO_TRACKING_URL',None);env.pop('ECHO_BACKEND_URL',None)
            child = await asyncio.create_subprocess_exec(node,'server/start.mjs',cwd=str(GATEWAY),env=env,
                        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        yield
    finally:
        if child and child.returncode is None:
            child.terminate()
            try: await asyncio.wait_for(child.wait(),5)
            except asyncio.TimeoutError: child.kill();await child.wait()
