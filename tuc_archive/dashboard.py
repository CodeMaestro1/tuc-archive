# tuc-archive — Archive a login-protected TYPO3 tx_tucforum forum into a Kiwix ZIM.
# Copyright (C) 2026 Konstantinos Pisimisis (CodeMaestro1)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Optional read-only web dashboard (FastAPI).

Shows live crawl progress by polling the state file the crawler/coordinator
writes. Intentionally read-only: start/pause/resume are driven from the CLI
(or by (re)starting the coordinator), which keeps a single source of truth for
the queue and avoids racing two writers on one state file.

Run:  tuc-archive-dashboard  (see __main__ below) or
      python -m tuc_archive.dashboard --state ./output/state.yml --port 8000
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .state import CrawlState

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5"><title>tuc-archive</title>
<style>body{{font-family:system-ui;max-width:680px;margin:2rem auto}}
.bar{{background:#eee;border-radius:6px;overflow:hidden;height:22px}}
.fill{{background:#27ae60;height:100%;width:{pct}%}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem;margin:1rem 0}}
.card{{background:#f7f7f7;border-radius:8px;padding:.8rem;text-align:center}}
.n{{font-size:1.6rem;font-weight:700}}</style></head><body>
<h1>tuc-archive</h1>
<div class="bar"><div class="fill"></div></div>
<p>{pct}% complete (auto-refresh 5s)</p>
<div class="grid">
<div class="card"><div class="n">{completed}</div>completed</div>
<div class="card"><div class="n">{pending}</div>pending</div>
<div class="card"><div class="n">{in_flight}</div>in-flight</div>
<div class="card"><div class="n">{errors}</div>errors</div>
</div>
<p>state file: <code>{state}</code></p>
</body></html>"""


def serve_dashboard(state_file: Path, host: str = "0.0.0.0", port: int = 8000):
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse
        import uvicorn
    except ImportError as e:  # noqa: BLE001
        raise SystemExit("pip install 'tuc-archive[dashboard]'") from e

    app = FastAPI(title="tuc-archive dashboard")

    def _stats():
        if not Path(state_file).exists():
            return {"completed": 0, "pending": 0, "in_flight": 0, "errors": 0}
        return CrawlState.load(state_file).stats()

    @app.get("/api/stats")
    def api_stats():
        return _stats()

    @app.get("/", response_class=HTMLResponse)
    def index():
        s = _stats()
        done = s["completed"]
        total = max(1, done + s["pending"] + s["in_flight"])
        pct = round(100 * done / total, 1)
        return _PAGE.format(pct=pct, state=state_file, **s)

    uvicorn.run(app, host=host, port=port)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="./output/state.yml")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    serve_dashboard(Path(args.state), args.host, args.port)


if __name__ == "__main__":
    main()
