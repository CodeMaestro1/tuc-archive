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

"""Concurrent, polite, resumable crawler engine.

- httpx.Client connection pool shared across worker threads (cookies = session)
- exponential backoff retry on 5xx / timeouts (tenacity)
- robots.txt honoured unless disabled
- scope + exclude regex filtering (utils.ScopeMatcher)
- incremental: skips URLs already stored with matching ETag/Last-Modified
- periodic atomic state snapshots
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .auth import Authenticator
from .config import Settings
from .parser import ForumParser
from .state import CompletedEntry, CrawlState
from .store import ContentStore
from .utils import ScopeMatcher

log = logging.getLogger("tuc.crawler")

_TRANSIENT = (httpx.TimeoutException, httpx.TransportError)


class Crawler:
    def __init__(
        self,
        settings: Settings,
        state: CrawlState,
        store: ContentStore,
        scope: ScopeMatcher,
        progress_cb=None,
    ):
        self.settings = settings
        self.state = state
        self.store = store
        self.scope = scope
        self.parser = ForumParser(settings.site, deobfuscate_emails=settings.deobfuscate_emails)
        self.progress_cb = progress_cb or (lambda **k: None)

        self.client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout,
            follow_redirects=True,
        )
        self.auth = Authenticator(self.client, settings)
        self._robots = self._load_robots()
        self._processed_since_snapshot = 0
        self._snapshot_lock = threading.Lock()
        self._stop = threading.Event()

    # ----------------------------------------------------------------- #
    def _load_robots(self):
        if not self.settings.respect_robots:
            return None
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(self.settings.site.base_url, "/robots.txt")
        try:
            r = self.client.get(robots_url)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                log.info("Loaded robots.txt from %s", robots_url)
                return rp
        except Exception as e:  # noqa: BLE001
            log.warning("Could not load robots.txt (%s); proceeding without it", e)
        return None

    def _allowed_by_robots(self, url: str) -> bool:
        if self._robots is None:
            return True
        return self._robots.can_fetch(self.settings.user_agent, url)

    # ----------------------------------------------------------------- #
    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        wait=wait_exponential(multiplier=1, min=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _fetch(self, url: str, conditional: dict | None = None) -> httpx.Response:
        headers = {}
        if conditional:
            if conditional.get("etag"):
                headers["If-None-Match"] = conditional["etag"]
            if conditional.get("last_modified"):
                headers["If-Modified-Since"] = conditional["last_modified"]
        return self.client.get(url, headers=headers)

    def _polite_wait(self):
        d = self.settings.delay
        if self.settings.random_wait:
            d = random.uniform(0.5 * d, 1.5 * d)
        if d > 0:
            time.sleep(d)

    # ----------------------------------------------------------------- #
    def login(self):
        self.auth.login()

    def run(self):
        """Crawl until the queue drains or stop() is called."""
        workers = max(1, self.settings.workers)
        recovered = self.state.requeue_in_flight()
        if recovered:
            log.info("Recovered %d in-flight URL(s) from a previous run", recovered)
        log.info("Starting crawl with %d workers", workers)
        limit = self.settings.max_pages
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while not self._stop.is_set():
                if limit and len(self.state.completed) >= limit:
                    log.info("Reached --limit of %d pages; stopping.", limit)
                    break
                n = workers * 2
                if limit:  # don't claim more than we still need
                    n = min(n, max(1, limit - len(self.state.completed)))
                batch = self.state.claim_batch(n)
                if not batch:
                    break
                list(pool.map(self._process, batch))
        self._snapshot(force=True)
        log.info("Crawl finished: %s", self.state.stats())

    def stop(self):
        self._stop.set()

    # ----------------------------------------------------------------- #
    def _process(self, url: str):
        if self._stop.is_set():
            return
        # Re-check scope on dequeue so exclude-rule changes apply to URLs queued
        # by an earlier run (resume) — not just at link-discovery time.
        if self.scope.excluded(url) or not self.scope.in_scope(url):
            log.debug("out-of-scope on dequeue, dropping: %s", url)
            # status 0 = skipped; clears in_flight so requeue won't re-add it
            self.state.complete(CompletedEntry(url, 0, stored_as="skipped"))
            return
        if not self._allowed_by_robots(url):
            log.debug("robots.txt disallows %s", url)
            self.state.fail(url, "robots-disallowed")
            return

        prior = self.store.page_meta(url)
        conditional = (
            {"etag": prior.get("etag"), "last_modified": prior.get("last_modified")}
            if prior
            else None
        )

        try:
            self._polite_wait()
            resp = self._fetch(url, conditional)

            if self.auth.ensure_session(resp):
                resp = self._fetch(url)  # retry once after re-login

            if resp.status_code == 304 and prior:
                log.debug("304 Not Modified: %s", url)
                self._enqueue_known_links(url)
                self._record(CompletedEntry(url, 304, prior.get("etag"),
                                            prior.get("last_modified"), prior.get("key")))
                return

            resp.raise_for_status()
            self._handle_response(url, resp)

        except httpx.HTTPStatusError as e:
            self.state.fail(url, f"http-{e.response.status_code}")
            log.warning("HTTP %s on %s", e.response.status_code, url)
        except Exception as e:  # noqa: BLE001
            self.state.fail(url, repr(e))
            log.warning("Error on %s: %r", url, e)
        finally:
            self.progress_cb(**self.state.stats())

    def _handle_response(self, url: str, resp: httpx.Response):
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype:
            # treat as asset (e.g. a PDF reached via a normal link)
            self.store.save_asset(url, resp.content, ctype)
            self._record(CompletedEntry(url, resp.status_code, stored_as="asset"))
            return

        page = self.parser.parse(url, resp.text)
        key = self.store.save_page(
            url,
            resp.content,
            {
                "status": resp.status_code,
                "etag": resp.headers.get("etag"),
                "last_modified": resp.headers.get("last-modified"),
                "content_type": ctype,
                "title": page.topic_title or page.title,
                "breadcrumb": page.forum_breadcrumb or page.breadcrumb,
                "links": page.links,
                "ajax_endpoints": page.ajax_endpoints,
                "posts": [vars(p) for p in page.posts],
            },
        )

        # queue in-scope navigation + pagination
        added = 0
        for link in page.links:
            if self.scope.in_scope(link):
                added += int(self.state.add(link))
        if page.next_page and self.scope.in_scope(page.next_page):
            added += int(self.state.add(page.next_page))

        # AJAX fragments require the page's data-csrf token replayed as a header,
        # so fetch them inline (not via the plain-GET queue) while we hold it.
        for ep in page.ajax_endpoints:
            if self.scope.in_scope(ep):
                self._fetch_ajax(ep, page.csrf)

        # download attachments (not scope-limited; we want the files)
        for att in page.attachments:
            self._fetch_asset(att)

        # download same-origin CSS/JS/images so the archive renders offline
        if self.settings.embed_assets:
            for ref in page.subresources:
                self._fetch_asset(ref, process_css=True)

        self._record(CompletedEntry(
            url, resp.status_code, resp.headers.get("etag"),
            resp.headers.get("last-modified"), key,
        ))
        log.info("OK %s (+%d links) [%s]", url, added, page.title or "")

    def _fetch_ajax(self, url: str, csrf: str | None):
        """Fetch a tx_tucforum AJAX fragment via POST, replaying data-csrf.

        The endpoint is POST-only (a GET returns 405), so we POST with the
        token sent as a header AND a form field, plus the XHR marker. This is
        best-effort: on the live tuc.gr forum the topic/category content is
        fully server-rendered, so AJAX adds little; failures are logged at
        debug level and do NOT count as crawl errors.
        """
        if self.store.has_page(url):
            return
        try:
            self._polite_wait()
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/html, */*; q=0.01",
            }
            data = {}
            if csrf:
                headers[self.settings.site.ajax_csrf_header] = csrf
                data["csrf"] = csrf
            r = self.client.post(url, headers=headers, data=data)
            if self.auth.ensure_session(r):
                r = self.client.post(url, headers=headers, data=data)
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
                log.debug("ajax skipped %s (status %s)", url, r.status_code)
                return
            page = self.parser.parse(url, r.text)
            key = self.store.save_page(url, r.content, {
                "status": r.status_code,
                "content_type": r.headers.get("content-type", "text/html"),
                "title": page.topic_title or page.title or "AJAX fragment",
                "links": page.links,
                "ajax_fragment": True,
            })
            for link in page.links:
                if self.scope.in_scope(link):
                    self.state.add(link)
            self._record(CompletedEntry(url, r.status_code, stored_as=key))
            log.debug("ajax ok %s", url)
        except Exception as e:  # noqa: BLE001
            log.debug("ajax failed %s: %r", url, e)  # non-fatal

    def _fetch_asset(self, url: str, process_css: bool = False):
        if self.store.has_asset(url) or self.scope.excluded(url):
            return
        try:
            self._polite_wait()
            r = self._fetch(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            self.store.save_asset(url, r.content, ctype)
            log.debug("asset %s", url)
            if process_css and ("css" in ctype or url.split("?")[0].endswith(".css")):
                self._fetch_css_deps(url, r.text)
        except Exception as e:  # noqa: BLE001
            log.warning("asset failed %s: %r", url, e)

    _CSS_URL_RX = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")

    def _fetch_css_deps(self, css_url: str, css_text: str):
        """Download same-origin url(...) refs inside a stylesheet (fonts, bg images)."""
        for ref in set(self._CSS_URL_RX.findall(css_text)):
            ref = ref.strip()
            if ref.startswith(("data:", "#")):
                continue
            from .utils import normalize_url, same_site
            absu = normalize_url(ref, base=css_url, site=self.settings.site)
            if same_site(absu, self.settings.site.base_url):
                self._fetch_asset(absu)  # no recursion: deps don't pull more CSS

    def _enqueue_known_links(self, url: str):
        meta = self.store.page_meta(url) or {}
        for link in (meta.get("links") or []) + (meta.get("ajax_endpoints") or []):
            if self.scope.in_scope(link):
                self.state.add(link)

    def _record(self, entry: CompletedEntry):
        self.state.complete(entry)
        with self._snapshot_lock:
            self._processed_since_snapshot += 1
            if self._processed_since_snapshot >= self.settings.snapshot_every:
                self._processed_since_snapshot = 0
                self._snapshot()

    def _snapshot(self, force: bool = False):
        self.state.save(self.settings.state_file)
        log.debug("state snapshot written (%s)", self.settings.state_file)

    def close(self):
        self.client.close()
