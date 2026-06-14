"""tx_tucforum category-list parsing against the real forum_root fixture."""

from pathlib import Path

from tuc_archive.config import Site
from tuc_archive.parser import ForumParser

FIX = Path(__file__).parent / "fixtures"
SITE = Site(base_url="https://www.tuc.gr")
ROOT = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis"


def test_parse_catlist_groups_and_categories():
    html = (FIX / "forum_root.html").read_text(encoding="utf-8")
    page = ForumParser(SITE).parse(ROOT, html)

    # forum CSRF token captured from data-csrf
    assert page.csrf and page.csrf.startswith("MHwx")
    # AJAX endpoint captured
    assert any("tx_tucforum_forumdisplay" in e for e in page.ajax_endpoints)

    cats = {c["cat_id"]: c for c in page.categories}
    assert {"7", "23", "3"} <= set(cats)
    c7 = cats["7"]
    assert c7["title"] == "Ανακοινώσεις του Ιδρύματος"
    assert c7["group"] == "Δημόσιες Ανακοινώσεις"
    assert c7["topic_count"] == 438
    assert c7["url"].endswith("/cat/7/page")
    assert cats["3"]["topic_count"] == 11057
    assert cats["3"]["group"] == "Ενδοπολυτεχνικές Ανακοινώσεις και Συζητήσεις"


def test_category_only_scope_keeps_topics_excludes_other_cats():
    from tuc_archive.utils import ScopeMatcher
    # what --category-only builds for seed .../cat/4/page
    scope = ScopeMatcher(SITE, include=[r"/cat/4/", r"/topic/"])
    base = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis"
    assert scope.in_scope(base + "/cat/4/page")        # the category itself
    assert scope.in_scope(base + "/cat/4/page/2")      # its pagination
    assert scope.in_scope(base + "/topic/56557/page")  # a topic
    # the forum-root catlist and OTHER categories stay out of scope,
    # so no crawled page spiders into them
    assert not scope.in_scope(base + "/")              # forum root / catlist
    assert not scope.in_scope(base)                    # forum root (no slash)
    assert not scope.in_scope(base + "/cat/7/page")    # different category
    assert not scope.in_scope(base + "/cat/40/page")   # /cat/4/ must not match /cat/40/


def test_rss_links_are_out_of_scope():
    from tuc_archive.utils import ScopeMatcher
    scope = ScopeMatcher(SITE)
    rss = (ROOT + "/cat/7/page?tx_tucforum_forumdisplay%5Bformat%5D=rss"
           "&type=569853&cHash=e8659fb28b6a9959eb8bca264592d6e6")
    assert scope.excluded(rss)
    # the plain category page IS in scope
    assert scope.in_scope(ROOT + "/cat/7/page")
