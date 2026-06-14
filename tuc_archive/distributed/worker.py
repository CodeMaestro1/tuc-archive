"""Distributed worker: pull batches from a coordinator, crawl, report back.

The worker reuses the standalone :class:`~tuc_archive.crawler.Crawler` for the
actual fetch/parse/store logic, but instead of draining a local queue it:
  1. claims a batch from the coordinator,
  2. processes each URL (content lands in the shared store),
  3. reports completions + newly-discovered links back to the coordinator,
  4. repeats until the coordinator's queue is empty.
"""

from __future__ import annotations

import logging
import time

import httpx

from ..config import Settings
from ..crawler import Crawler
from ..state import CrawlState
from ..store import ContentStore
from ..utils import ScopeMatcher

log = logging.getLogger("tuc.worker")


def run_worker(coordinator_url: str, settings: Settings, idle_max: int = 5):
    base = coordinator_url.rstrip("/")
    headers = {"X-Auth-Token": settings.coordinator_secret}
    api = httpx.Client(base_url=base, headers=headers, timeout=30.0)

    # confirm coordinator is reachable
    api.get("/healthz").raise_for_status()

    store = ContentStore(settings.output_dir / "store")
    scope = ScopeMatcher(settings.site)
    state = CrawlState()  # local scratch queue; discovered links collected here
    crawler = Crawler(settings, state, store, scope)
    crawler.login()

    idle = 0
    log.info("Worker started against %s", base)
    try:
        while True:
            batch = api.post("/claim", json={"n": max(8, settings.workers * 4)}).json()["urls"]
            if not batch:
                idle += 1
                if idle >= idle_max:
                    log.info("Queue drained; worker exiting.")
                    break
                time.sleep(2)
                continue
            idle = 0

            before = set(state.completed)
            for url in batch:
                state.add(url)
                crawler._process(url)  # stores content; adds discovered to state.pending

            # everything newly queued locally = links discovered this round
            discovered = list(state.pending)
            state.pending.clear()
            completed = [
                vars(state.completed[u]) for u in batch if u in state.completed
            ]
            errors = {u: m for u, m in state.errors.items()}
            state.errors.clear()

            resp = api.post("/report", json={
                "completed": completed,
                "discovered": discovered,
                "errors": errors,
            }).json()
            log.info("batch=%d reported=%d discovered+=%d",
                     len(batch), resp.get("accepted", 0), resp.get("added", 0))
    finally:
        crawler.close()
        api.close()
