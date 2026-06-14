"""Parser + link rewriter against the real /topic/ fixture."""

from pathlib import Path

from tuc_archive.config import Site
from tuc_archive.parser import ForumParser, decode_mailto
from tuc_archive.rewrite import LinkRewriter, PathMapper

FIX = Path(__file__).parent / "fixtures"
SITE = Site(base_url="https://www.tuc.gr")
TOPIC_URL = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis/topic/56557/page"


def _topic_html():
    return (FIX / "topic.html").read_text(encoding="utf-8")


def test_topic_title_and_forum_breadcrumb():
    page = ForumParser(SITE).parse(TOPIC_URL, _topic_html())
    assert page.csrf and page.csrf.startswith("REDACTED")
    assert "αντίγραφο" in (page.topic_title or "")
    assert "Γενικά Μηνύματα" in page.forum_breadcrumb
    assert "Ενδοπολυτεχνικές Ανακοινώσεις και Συζητήσεις" in page.forum_breadcrumb


def test_parse_post_metadata():
    page = ForumParser(SITE).parse(TOPIC_URL, _topic_html())
    assert len(page.posts) == 1
    p = page.posts[0]
    assert p.post_id == "81411"
    assert p.author == "John Doe"
    assert p.timestamp == "13-06-2026 16:41"
    assert p.department == "προπτυχιακός ΗΜΜΥ"
    assert p.updated == "-"
    assert p.email_visible == "johndoe<στο>example.org"
    assert p.text_excerpt and "Πρόσφατα" in p.text_excerpt


def test_emails_obfuscated_by_default():
    # default: no plaintext address produced (privacy)
    p = ForumParser(SITE).parse(TOPIC_URL, _topic_html()).posts[0]
    assert p.email is None
    assert p.email_obfuscated == "nbjmup+kpioepfAfybnqmf/psh"  # raw token preserved


def test_emails_decoded_when_opted_in():
    p = ForumParser(SITE, deobfuscate_emails=True).parse(TOPIC_URL, _topic_html()).posts[0]
    assert p.email == "johndoe@example.org"


def test_decode_mailto_caesar():
    assert decode_mailto("nbjmup+kpioepfAfybnqmf/psh", -1) == "johndoe@example.org"
    assert decode_mailto(None, -1) is None
    assert decode_mailto("garbage", -1) is None  # no '@' => None


def test_attachments_and_ajax_and_pagination():
    page = ForumParser(SITE).parse(TOPIC_URL, _topic_html())
    assert any(a.endswith("/fileadmin/uploads/forum/shot.png") for a in page.attachments)
    assert any("tx_tucforum_forumdisplay" in e for e in page.ajax_endpoints)
    # paginator page 2 link captured for crawling
    assert any(l.endswith("/topic/56557/page/2") for l in page.links)
    # post-level attachment metadata too
    assert page.posts[0].attachments


def test_reply_and_quote_actions_excluded():
    from tuc_archive.utils import ScopeMatcher
    scope = ScopeMatcher(SITE)
    base = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis"
    assert scope.excluded(base + "?tx_tucforum_forumdisplay%5Baction%5D=reply&tx_tucforum_forumdisplay%5Btopic%5D=56557")
    assert scope.excluded(base + "?tx_tucforum_forumdisplay%5Baction%5D=quote&tx_tucforum_forumdisplay%5BquotePost%5D=81411")
    # but a normal topic page is in scope
    assert scope.in_scope(base + "/topic/56557/page")


def test_link_rewriter_internal_vs_external():
    mapper = PathMapper(SITE)
    next_topic = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis/topic/56558/page"
    archived = {next_topic: mapper.page_path(next_topic)}
    rewriter = LinkRewriter(SITE, resolver=lambda u: archived.get(u))
    current = mapper.page_path(TOPIC_URL)
    out = rewriter.rewrite(TOPIC_URL, _topic_html(), current)
    assert archived[next_topic].split("/")[-1] in out
    assert "https://github.com/openzim/zimit" in out  # external left intact
