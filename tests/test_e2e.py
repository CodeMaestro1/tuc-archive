"""End-to-end against a real local HTTP server: login -> crawl -> ZIM.

This is the one test that actually instantiates and runs `Crawler` (the
standalone `crawl` path), exercising __init__, the run()/claim_batch loop,
robots load, store writes, snapshots, attachment download, and link rewriting
through to a libzim-readable ZIM — the surface unit tests can't reach.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tuc_archive.config import Settings, Site
from tuc_archive.crawler import Crawler
from tuc_archive.state import CrawlState
from tuc_archive.store import ContentStore
from tuc_archive.utils import ScopeMatcher, normalize_url

FIX = Path(__file__).parent / "fixtures"


class _Handler(BaseHTTPRequestHandler):
    logged_in = False  # flips once the felogin POST arrives

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    ROOT = "/el/to-polytechneio/nea-anakoinoseis-syzitiseis"

    def do_GET(self):
        if self.path == "/robots.txt":
            self._send(404, b"not found")
        elif self.path.rstrip("/") == self.ROOT and not _Handler.logged_in:
            # forum root while logged out: serve the felogin form
            self._send(200, (FIX / "forum_root.html").read_bytes())
        elif "/topic/" in self.path or "/cat/" in self.path:
            self._send(200, (FIX / "topic.html").read_bytes())
        elif self.path.startswith("/fileadmin/"):
            self._send(200, b"%PDF-1.4 fake pdf bytes", "application/pdf")
        else:
            self._send(200, b"<html><head><title>stub</title></head><body>ok</body></html>")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        _Handler.logged_in = True
        # logged-in response: no felogin form marker present
        self._send(200, b'<html><a href="?logintype=logout">out</a></html>')


@pytest.fixture
def server():
    _Handler.logged_in = False
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def test_crawl_then_build_zim(server, tmp_path):
    libzim = pytest.importorskip("libzim.reader")

    s = Settings()
    s.username, s.password = "u", "p"
    s.workers = 2
    s.delay = 0
    s.snapshot_every = 1
    s.output_dir = tmp_path
    s.state_file = tmp_path / "state.yml"
    s.respect_robots = True  # 404 robots => allowed
    # default forum_root_path points at the real tuc path; the test server
    # serves the felogin form there until the POST flips its logged_in flag.
    s.site = Site(base_url=server)

    seed = normalize_url(
        f"{server}/el/to-polytechneio/nea-anakoinoseis-syzitiseis/cat/3/page",
        site=s.site,
    )
    state = CrawlState(seeds=[seed])
    state.add(seed)
    store = ContentStore(tmp_path / "store")
    scope = ScopeMatcher(s.site)

    crawler = Crawler(s, state, store, scope)
    crawler.login()
    crawler.run()
    crawler.close()

    stats = state.stats()
    assert stats["completed"] >= 1
    assert store.has_page(seed)
    # image attachment from the topic page was downloaded
    assert store.has_asset(normalize_url(f"{server}/fileadmin/uploads/forum/shot.png", site=s.site))
    # state snapshot was persisted
    assert s.state_file.exists()

    # now package and read back
    from tuc_archive.zim import ZimBuilder
    zim_path = tmp_path / "out.zim"
    ZimBuilder(s, store).build(zim_path, title="E2E", description="d", main_url=seed)
    arch = libzim.Archive(str(zim_path))
    assert arch.entry_count >= 2
