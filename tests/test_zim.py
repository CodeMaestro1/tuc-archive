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
