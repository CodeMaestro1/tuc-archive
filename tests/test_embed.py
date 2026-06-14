"""CSS/asset embedding + privacy stripping of login frames."""

from pathlib import Path

from tuc_archive.config import Site
from tuc_archive.parser import ForumParser
from tuc_archive.rewrite import LinkRewriter, PathMapper, rewrite_css, scrub_pii_text

FIX = Path(__file__).parent / "fixtures"
SITE = Site(base_url="https://www.tuc.gr")
TOPIC_URL = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis/topic/56557/page"
ROOT = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis"


def test_login_frame_stripped_removes_username_and_token():
    html = (FIX / "topic.html").read_text(encoding="utf-8")
    assert "demouser" in html  # present before stripping
    rewriter = LinkRewriter(SITE, resolver=lambda u: None)
    out = rewriter.rewrite(TOPIC_URL, html, PathMapper(SITE).page_path(TOPIC_URL))
    # username + login/logout UI gone from the archived page
    assert "demouser" not in out
    assert "Αποσύνδεση" not in out
    assert "tx-felogin-input-logout" not in out
    # real post content survives
    assert "topicpostlistmessage" in out


def test_redact_emails_removes_token_and_visible_text():
    html = (FIX / "topic.html").read_text(encoding="utf-8")
    # baseline: token + scrambled text are in the source
    assert "data-mailto-token" in html
    assert "johndoe" in html
    rewriter = LinkRewriter(SITE, resolver=lambda u: None, redact_emails=True)
    out = rewriter.rewrite(TOPIC_URL, html, PathMapper(SITE).page_path(TOPIC_URL))
    # nothing left for a format-aware scraper
    assert "data-mailto-token" not in out
    assert "johndoe" not in out
    assert "[email hidden]" in out
    # post content otherwise intact
    assert "topicpostlistmessage" in out


def test_no_redaction_by_default_keeps_obfuscated_token():
    html = (FIX / "topic.html").read_text(encoding="utf-8")
    rewriter = LinkRewriter(SITE, resolver=lambda u: None)  # redact_emails=False
    out = rewriter.rewrite(TOPIC_URL, html, PathMapper(SITE).page_path(TOPIC_URL))
    assert "data-mailto-token" in out  # obfuscated token preserved (site default)


def test_scrub_pii_masks_structured_identifiers():
    txt = ("Επικοινωνία: john.doe@example.gr τηλ 6970385377 και 2821037055, "
           "ΑΜΚΑ 95405960393, IBAN GR36 0172 0000 0000 0000 0000 123.")
    out = scrub_pii_text(txt)
    assert "john.doe@example.gr" not in out and "[redacted-email]" in out
    assert "6970385377" not in out and "2821037055" not in out
    assert "[redacted-phone]" in out
    assert "95405960393" not in out and "[redacted-id]" in out
    assert "GR36 0172" not in out and "[redacted-iban]" in out
    # ordinary short numbers (e.g. a topic count) survive
    assert scrub_pii_text("Θέματα 438") == "Θέματα 438"


def test_scrub_pii_applied_in_rewrite_when_enabled():
    html = '<html><body><p>mail me at a.b@tuc.gr or 6970385377</p></body></html>'
    url = "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis/topic/1/page"
    rw = LinkRewriter(SITE, resolver=lambda u: None, scrub_pii=True)
    out = rw.rewrite(url, html, PathMapper(SITE).page_path(url))
    assert "a.b@tuc.gr" not in out and "6970385377" not in out
    assert "[redacted-email]" in out and "[redacted-phone]" in out


def test_subresources_extracted_same_origin():
    html = (FIX / "forum_root.html").read_text(encoding="utf-8")
    page = ForumParser(SITE).parse(ROOT, html)
    assert any(s.endswith(".css") or "/tucforum.css" in s for s in page.subresources)
    # all same-origin
    assert all(s.startswith("https://www.tuc.gr/") for s in page.subresources)


def test_rewrite_css_url_to_relative():
    css = "@font-face{src:url('../Fonts/icon.woff2')} .x{background:url(bg.png)}"
    css_url = "https://www.tuc.gr/typo3conf/ext/tucforum/Resources/Public/Css/tucforum.css"
    font = "https://www.tuc.gr/typo3conf/ext/tucforum/Resources/Fonts/icon.woff2"
    bg = "https://www.tuc.gr/typo3conf/ext/tucforum/Resources/Public/Css/bg.png"
    mapper = PathMapper(SITE)
    archived = {font: mapper.asset_path(font, ".woff2"), bg: mapper.asset_path(bg, ".png")}
    css_zpath = mapper.asset_path(css_url, ".css")

    out = rewrite_css(css, css_zpath, css_url, lambda u: archived.get(u), SITE)
    assert "../" in out  # relativised
    assert archived[font].split("/")[-1] in out
    assert archived[bg].split("/")[-1] in out
    # external/unknown refs would be left as-is
    assert rewrite_css("a{background:url(https://cdn.example.com/x.png)}",
                       css_zpath, css_url, lambda u: None, SITE) \
        == "a{background:url(https://cdn.example.com/x.png)}"
