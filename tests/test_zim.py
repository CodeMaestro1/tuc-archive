"""End-to-end: store -> ZIM -> read back with libzim."""

from pathlib import Path

import pytest

from tuc_archive.config import Settings, Site
from tuc_archive.store import ContentStore
from tuc_archive.zim import ZimBuilder, _solid_png

BASE = "https://x.gr"
PAGE_URL = f"{BASE}/el/forum/topic/1/"


def test_solid_png_is_valid_48x48():
    png = _solid_png(48)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR width/height at bytes 16..24
    import struct
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (48, 48)


def test_build_zim_roundtrip(tmp_path):
    libzim = pytest.importorskip("libzim.reader")

    s = Settings()
    s.output_dir = tmp_path
    s.site = Site(base_url=BASE)
    store = ContentStore(tmp_path / "store")

    html = (
        f'<html><head><title>Topic 1</title></head><body>'
        f'<a href="{BASE}/el/forum/topic/2/">next</a>'
        f'<a href="https://external.com/x">ext</a></body></html>'
    )
    store.save_page(PAGE_URL, html.encode("utf-8"),
                    {"status": 200, "title": "Topic 1", "content_type": "text/html",
                     "links": [], "ajax_endpoints": []})
    store.save_asset(f"{BASE}/fileadmin/a.pdf", b"%PDF-1.4 fake", "application/pdf")

    zim_path = tmp_path / "out.zim"
    ZimBuilder(s, store).build(zim_path, title="Test", description="desc")

    assert zim_path.exists()
    arch = libzim.Archive(str(zim_path))
    assert arch.entry_count >= 2
    # homepage present and reachable
    assert arch.has_entry_by_path("index.html")


def test_homepage_groups_collapses_and_cleans_titles(tmp_path):
    libzim = pytest.importorskip("libzim.reader")
    s = Settings(); s.output_dir = tmp_path; s.site = Site(base_url=BASE)
    store = ContentStore(tmp_path / "store")

    def page(url, title, breadcrumb=None, posts=1, ts=None):
        store.save_page(url, b"<html><body>x</body></html>",
                        {"status": 200, "title": title, "content_type": "text/html",
                         "links": [], "ajax_endpoints": [], "breadcrumb": breadcrumb or [],
                         "posts": [{"author": "A", "post_id": "1", "timestamp": ts}] * posts})

    # two pages of ONE topic — must collapse to a single entry; title has the
    # reads/subscriptions counter that should be stripped
    page(f"{BASE}/el/f/topic/9/page", "Καλό θέμα Αναγνώσεις: 5 / Συνδρομές: 1",
         ts="01-05-2026 10:00")
    page(f"{BASE}/el/f/topic/9/page/2", "Καλό θέμα Αναγνώσεις: 5 / Συνδρομές: 1",
         ts="03-05-2026 09:00")
    # a NEWER topic — must sort before the older one (chronological, newest first)
    page(f"{BASE}/el/f/topic/10/page", "Νεότερο θέμα", ts="10-06-2026 12:00")
    # a category page with a GENERIC <title> but a useful breadcrumb
    page(f"{BASE}/el/f/cat/3/page", "Νέα / Ανακοινώσεις - Πολυτεχνείο Κρήτης",
         breadcrumb=["Όλες", "Γενικά Μηνύματα"], posts=0)

    zim_path = tmp_path / "h.zim"
    ZimBuilder(s, store).build(zim_path, title="T", description="d")
    html = bytes(libzim.Archive(str(zim_path))
                 .get_entry_by_path("index.html").get_item().content).decode("utf-8")

    assert "<h2>Θέματα (2)</h2>" in html          # 3 topic pages -> 2 collapsed topics
    assert "· 2 σελ." in html                      # pagination count shown
    assert "Καλό θέμα" in html and "Αναγνώσεις" not in html  # counter stripped
    assert "Γενικά Μηνύματα" in html               # generic title -> breadcrumb label
    assert "Κατηγορίες" in html
    # chronological, newest first; collapsed topic keeps EARLIEST (creation) date
    assert "01-05-2026" in html
    assert html.index("Νεότερο θέμα") < html.index("Καλό θέμα")


def test_author_index_opt_in(tmp_path):
    libzim = pytest.importorskip("libzim.reader")

    s = Settings()
    s.output_dir = tmp_path
    s.site = Site(base_url=BASE)
    store = ContentStore(tmp_path / "store")

    html = f'<html><head><title>Topic 1</title></head><body>hi</body></html>'
    store.save_page(PAGE_URL, html.encode("utf-8"),
                    {"status": 200, "title": "Topic 1", "content_type": "text/html",
                     "links": [], "ajax_endpoints": [],
                     "posts": [{"author": "John Doe", "post_id": "1",
                                "timestamp": "13-06-2026", "text_excerpt": "hello"}]})

    # default: NO author index
    plain = tmp_path / "plain.zim"
    ZimBuilder(s, store).build(plain, title="T", description="d")
    assert not libzim.Archive(str(plain)).has_entry_by_path("authors/index.html")

    # opt-in: author index + a per-author page exist
    withidx = tmp_path / "auth.zim"
    ZimBuilder(s, store).build(withidx, title="T", description="d", author_index=True)
    arch = libzim.Archive(str(withidx))
    assert arch.has_entry_by_path("authors/index.html")
    # the per-author page is keyed by slug+hash; find it and check it names the author
    slug = ZimBuilder(s, store)._author_slug("John Doe")
    assert arch.has_entry_by_path(slug)
    body = bytes(arch.get_entry_by_path(slug).get_item().content).decode("utf-8")
    assert "John Doe" in body and "Topic 1" in body
