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

"""Coordinator REST API (FastAPI).

Endpoints (all but /healthz require ``X-Auth-Token: <TUC_SECRET>``):
  GET  /healthz            -> {"ok": true}
  GET  /stats              -> queue statistics
  POST /claim   {n}        -> {"urls": [...]}  (leases a batch)
  POST /report  {completed, discovered, errors}
                           -> {"accepted": k, "added": m}

State is persisted atomically after every report and a background task
re-queues leases older than ``lease_timeout`` (dead worker).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from ..config import Settings
from ..state import CompletedEntry, CrawlState
from ..utils import ScopeMatcher, normalize_url

log = logging.getLogger("tuc.coordinator")


def serve_coordinator(host: str, port: int, output: Path, seeds: list[str],
                      lease_timeout: float = 300.0):
    try:
        from fastapi import FastAPI, Header, HTTPException
        from pydantic import BaseModel
        import uvicorn
    except ImportError as e:  # noqa: BLE001
        raise SystemExit(
            "Distributed mode needs extras: pip install 'tuc-archive[distributed]'"
        ) from e

    settings = Settings()
    settings.output_dir = output
    state_file = output / "state.yml"
    scope = ScopeMatcher(settings.site)

    # load existing state or start fresh
    if state_file.exists():
        state = CrawlState.load(state_file)
        log.info("Loaded coordinator state: %s", state.stats())
    else:
        seeds_n = [normalize_url(u, site=settings.site) for u in seeds]
        state = CrawlState(seeds=seeds_n, config=settings.as_public_dict())
        state.add_many(seeds_n)

    lock = threading.Lock()

    def check(token: str | None):
        if token != settings.coordinator_secret:
            raise HTTPException(status_code=401, detail="bad token")

    class ClaimReq(BaseModel):
        n: int = 16

    class CompletedReq(BaseModel):
        url: str
        status: int
        etag: str | None = None
        last_modified: str | None = None
        stored_as: str | None = None

    class ReportReq(BaseModel):
        completed: list[CompletedReq] = []
        discovered: list[str] = []
        errors: dict[str, str] = {}

    app = FastAPI(title="tuc-archive coordinator")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/stats")
    def stats(x_auth_token: str | None = Header(default=None)):
        check(x_auth_token)
        return state.stats()

    @app.post("/claim")
    def claim(req: ClaimReq, x_auth_token: str | None = Header(default=None)):
        check(x_auth_token)
        with lock:
            batch = state.claim_batch(req.n)
        return {"urls": batch}

    @app.post("/report")
    def report(req: ReportReq, x_auth_token: str | None = Header(default=None)):
        check(x_auth_token)
        with lock:
            for c in req.completed:
                state.complete(CompletedEntry(**c.model_dump()))
            for url, msg in req.errors.items():
                state.fail(url, msg)
            added = 0
            for u in req.discovered:
                if scope.in_scope(u):
                    added += int(state.add(u))
            state.save(state_file)
        return {"accepted": len(req.completed), "added": added}

    @app.on_event("startup")
    async def _reaper():
        async def loop():
            while True:
                await asyncio.sleep(lease_timeout / 2)
                with lock:
                    n = state.requeue_stale(lease_timeout)
                    if n:
                        log.warning("re-queued %d stale lease(s)", n)
                        state.save(state_file)
        asyncio.create_task(loop())

    log.info("Coordinator listening on %s:%d (output=%s)", host, port, output)
    uvicorn.run(app, host=host, port=port, log_level="info")
