#!/usr/bin/env python3
"""Generate a read-only source viewer for the Wi-Fi switch prototype.

The Wi-Fi switch is a separate experiment that lives beside this one in
ECHO_Project/04_wifi-switch. A judge following the shared link should be able
to read its code without cloning anything, so this copies the sources into
`extras/wifi-switch/` (they become real, downloadable files in the repository)
and renders them into a single self-contained page.

The page is written to `dashboard/assets/wifi-switch.html` for one reason: the
server mounts `/assets` from that directory, and the static build copies the
same directory into `docs/assets`. So a single relative link,
`assets/wifi-switch.html`, resolves both when you run ./start_mission.sh
locally and on the published site.

Highlighting is done here, at build time, with a small Python lexer - no
runtime script and no CDN, matching how this project vendors three.js rather
than loading it remotely.

Usage:  python3 scripts/build_wifi_viewer.py
"""
from __future__ import annotations

import html
import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.abspath(os.path.join(ROOT, "..", "04_wifi-switch"))
VENDORED = os.path.join(ROOT, "extras", "wifi-switch")
OUT = os.path.join(ROOT, "dashboard", "assets", "wifi-switch.html")

# Order matters: this is the reading order on the page.
FILES = [
    ("README.md", "What it does and how to run it"),
    ("app.py", "The interface and the switch flow"),
    ("wifi_controller.py", "Per-platform Wi-Fi control: scan, join, verify"),
    ("credentials.py", "Passwords via the macOS keychain, never on disk"),
    ("config.py", "Where the saved configuration lives"),
    ("tests/test_wifi_logic.py", "Unit tests for the switching logic"),
    ("tests/test_config.py", "Unit tests for configuration handling"),
    ("requirements.txt", "Dependencies"),
]

KEYWORDS = {
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally",
    "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
}
BUILTINS = {
    "abs", "bool", "dict", "enumerate", "float", "getattr", "hasattr", "int",
    "isinstance", "len", "list", "max", "min", "open", "print", "range",
    "repr", "return", "round", "set", "sorted", "str", "sum", "super",
    "tuple", "type", "zip", "self",
}

TOKEN = re.compile(
    r"""(?P<triple>'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\")
      | (?P<comment>\#[^\n]*)
      | (?P<string>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")
      | (?P<decorator>@[A-Za-z_][\w.]*)
      | (?P<number>\b\d+\.?\d*\b)
      | (?P<name>[A-Za-z_]\w*)
    """,
    re.X,
)


def highlight_python(src: str) -> str:
    out, last, prev = [], 0, ""
    for m in TOKEN.finditer(src):
        out.append(html.escape(src[last:m.start()]))
        kind = m.lastgroup
        text = html.escape(m.group())
        if kind in ("triple", "string"):
            cls = "s"
        elif kind == "comment":
            cls = "c"
        elif kind == "decorator":
            cls = "d"
        elif kind == "number":
            cls = "n"
        else:
            raw = m.group()
            if raw in KEYWORDS:
                cls = "k"
            elif prev in ("def", "class"):
                cls = "f"
            elif raw in BUILTINS:
                cls = "b"
            else:
                cls = ""
            prev = raw
        out.append(f'<span class="{cls}">{text}</span>' if cls else text)
        last = m.end()
    out.append(html.escape(src[last:]))
    return "".join(out)


def render_plain(src: str) -> str:
    return html.escape(src)


CSS = """
:root{--bg:#141414;--panel:#181818;--line:#333331;--line2:#4c4b47;
  --text:#e8e5de;--muted:#aaa79f;--dim:#918e87;--accent:#e99a59;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
  font-family:Arial,Helvetica,sans-serif;font-size:14px}
.wrap{max-width:1180px;margin:0 auto;padding:16px 34px 80px}
header{padding:12px 0 20px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
.brand b{font-size:25px;letter-spacing:.15em;font-weight:600}
.brand small{display:block;font-size:10px;letter-spacing:.08em;color:var(--muted);margin-top:4px}
.back{margin-left:auto;font-size:12px;color:var(--accent);text-decoration:none;
  border:1px solid var(--line2);border-radius:3px;padding:8px 13px}
.back:hover{background:#2b2a27;border-color:#77736b}
h1{font-size:30px;font-weight:400;letter-spacing:-.02em;margin:26px 0 10px}
.lede{font-size:13px;line-height:1.75;color:var(--muted);max-width:760px;margin:0 0 6px}
.note{font-size:11px;line-height:1.7;color:var(--dim);max-width:760px;margin:14px 0 0}
.index{display:flex;flex-wrap:wrap;gap:0 22px;padding:20px 0;margin:24px 0 0;
  border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.index a{font:11px var(--mono);color:var(--muted);text-decoration:none}
.index a:hover{color:var(--accent)}
section{padding-top:44px;scroll-margin-top:12px}
.fh{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:12px}
.fh h2{font:15px var(--mono);font-weight:400;margin:0;color:var(--text)}
.fh span{font-size:11px;color:var(--dim)}
.fh em{margin-left:auto;font-style:normal;font:10px var(--mono);color:var(--dim)}
pre{margin:0;padding:18px 0 0;overflow-x:auto;font:12px/1.65 var(--mono);
  color:var(--text);tab-size:4}
pre .k{color:var(--accent)}
pre .s{color:#b9cbb9}
pre .c{color:var(--dim);font-style:italic}
pre .f{color:#d4cfc3}
pre .b{color:#a2aca7}
pre .n{color:#e8b875}
pre .d{color:#e8b875}
footer{margin-top:60px;padding-top:18px;border-top:1px solid var(--line);
  font-size:11px;color:var(--dim)}
@media(max-width:750px){.wrap{padding:10px 16px 50px}h1{font-size:24px}pre{font-size:11px}}
"""


def main() -> None:
    if not os.path.isdir(SOURCE):
        raise SystemExit(f"cannot find the Wi-Fi switch sources at {SOURCE}")

    # Vendor the sources so the repository really contains what the page shows.
    shutil.rmtree(VENDORED, ignore_errors=True)
    os.makedirs(VENDORED, exist_ok=True)
    parts, links = [], []

    for rel, blurb in FILES:
        src_path = os.path.join(SOURCE, rel)
        if not os.path.exists(src_path):
            continue
        dst = os.path.join(VENDORED, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src_path, dst)

        text = open(src_path, encoding="utf-8").read()
        anchor = re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")
        body = highlight_python(text) if rel.endswith(".py") else render_plain(text)
        lines = text.count("\n") + 1
        links.append(f'<a href="#{anchor}">{html.escape(rel)}</a>')
        parts.append(
            f'<section id="{anchor}">'
            f'<div class="fh"><h2>{html.escape(rel)}</h2>'
            f'<span>{html.escape(blurb)}</span>'
            f'<em>{lines} lines</em></div>'
            f'<pre><code>{body}</code></pre></section>'
        )

    for extra in ("run_macos.command", "run_linux.sh", "run_windows.bat"):
        p = os.path.join(SOURCE, extra)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(VENDORED, extra))
    gi = os.path.join(SOURCE, ".gitignore")
    if os.path.exists(gi):
        shutil.copy2(gi, os.path.join(VENDORED, ".gitignore"))

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wi-Fi switch prototype - source</title>
<meta name="description" content="Python source of the ECHO Wi-Fi switch prototype: a real single-radio switch between two Wi-Fi networks.">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="brand"><b>ECHO</b><small>WI-FI SWITCH PROTOTYPE / 04</small></div>
  <a class="back" href="index.html">&larr; Back to mission control</a>
</header>

<h1>Wi-Fi switch prototype</h1>
<p class="lede">A separate, smaller experiment from the same project: a real
single-radio switch between two Wi-Fi networks on macOS, Linux and Windows. It
scans, joins, and verifies the join actually took, rather than assuming it did.
Unlike the mission simulation, this one touches the real adapter - which is
also why it is manual and kept apart from the mission.</p>
<p class="note">Read-only source, generated from
<code>04_wifi-switch/</code> at build time. Passwords are never written to the
configuration: on macOS they go to the login keychain, and elsewhere they are
held in memory for the run only. Nothing on this page executes.</p>

<nav class="index">{''.join(links)}</nav>

{''.join(parts)}

<footer>Generated by scripts/build_wifi_viewer.py from the working sources.
The same files are committed under extras/wifi-switch/ in this repository.</footer>
</div>
</body>
</html>
"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(page)

    size = os.path.getsize(OUT)
    print(f"wrote {os.path.relpath(OUT, ROOT)}  ({size/1024:.0f} KB, "
          f"{len(parts)} files)")
    print(f"vendored sources into {os.path.relpath(VENDORED, ROOT)}/")


if __name__ == "__main__":
    main()
