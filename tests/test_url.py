"""URL canonicalisation + scope/exclude matching."""

from tuc_archive.config import Site
from tuc_archive.utils import ScopeMatcher, normalize_url, url_hash


def test_normalize_sorts_query_and_strips_session():
    site = Site(base_url="https://x.gr")
    a = normalize_url("https://X.gr/el/topic/1/?b=2&a=1&PHPSESSID=deadbeef#frag", site=site)
    b = normalize_url("https://x.gr/el/topic/1/?a=1&b=2", site=site)
    assert a == b
    assert "PHPSESSID" not in a  # session key stripped
    assert "#frag" not in a
    assert a.startswith("https://x.gr/")


def test_normalize_keeps_chash():
    # cHash must survive — TYPO3 404s parameterised URLs without it.
    site = Site(base_url="https://x.gr")
    got = normalize_url("https://x.gr/el/x?type=1&cHash=abc", site=site)
    assert "cHash=abc" in got


def test_normalize_resolves_relative():
    site = Site(base_url="https://x.gr")
    got = normalize_url("../topic/2/", base="https://x.gr/el/forum/", site=site)
    assert got == "https://x.gr/el/topic/2/"


def test_normalize_default_port_dropped():
    site = Site()
    assert normalize_url("https://x.gr:443/a", site=site) == "https://x.gr/a"


def test_url_hash_stable():
    assert url_hash("https://x.gr/a") == url_hash("https://x.gr/a")
    assert url_hash("https://x.gr/a") != url_hash("https://x.gr/b")


def test_scope_includes_category_excludes_actions():
    site = Site(base_url="https://x.gr")
    scope = ScopeMatcher(site)
    assert scope.in_scope("https://x.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis/genika/topic/1/")
    # reply action excluded
    assert not scope.in_scope("https://x.gr/el/forum/topic/1/?tx_tucforum[action]=reply")
    # logout excluded
    assert not scope.in_scope("https://x.gr/login?logintype=logout")
    # off-site excluded
    assert not scope.in_scope("https://other.com/el/forum/topic/1/")


def test_scope_custom_exclude_regex():
    site = Site(base_url="https://x.gr")
    scope = ScopeMatcher(site, exclude=[r"/topic/999/"])
    assert scope.excluded("https://x.gr/el/forum/topic/999/")
    assert not scope.excluded("https://x.gr/el/forum/topic/1/")
