"""Extbase felogin flow against the real forum_root fixture (no live network)."""

from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from tuc_archive.auth import AuthError, Authenticator
from tuc_archive.config import Settings, Site

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.tuc.gr"
ROOT = f"{BASE}/el/to-polytechneio/nea-anakoinoseis-syzitiseis"


def _settings(user="u", pw="p"):
    s = Settings()
    s.username, s.password = user, pw
    s.site = Site(base_url=BASE)
    return s


@respx.mock
def test_login_posts_to_form_action_with_extbase_tokens():
    root_html = (FIX / "forum_root.html").read_text(encoding="utf-8")
    captured = {}

    def post_handler(request):
        captured["url"] = str(request.url)
        captured["body"] = parse_qs(request.content.decode())
        # logged-in page: no felogin form marker
        return httpx.Response(200, html='<html><a href="?logintype=logout">out</a></html>')

    respx.get(ROOT).mock(return_value=httpx.Response(200, html=root_html))
    # form action carries tx_felogin_login params + cHash
    respx.post(url__startswith=ROOT).mock(side_effect=post_handler)

    with httpx.Client(follow_redirects=True) as client:
        Authenticator(client, _settings()).login()

    # posted to the form's OWN action (not the bare page)
    assert "tx_felogin_login" in captured["url"]
    assert "cHash=9192d345586ed0ae2eba7d5035387f99" in captured["url"]
    body = captured["body"]
    # Extbase integrity tokens carried through
    assert body["__RequestToken"][0].startswith("eyJ")
    assert "__trustedProperties" in body
    assert body["__referrer[@extension]"] == ["Felogin"]
    # credentials + forced fields
    assert body["user"] == ["u"] and body["pass"] == ["p"]
    assert body["logintype"] == ["login"]
    assert body["permalogin"] == ["1"]
    assert body["submit"] == ["Σύνδεση"]
    # the disabled permalogin=0 decoy must NOT be sent as the value
    assert body["permalogin"] != ["0"]


@respx.mock
def test_login_failure_when_form_persists():
    root_html = (FIX / "forum_root.html").read_text(encoding="utf-8")
    respx.get(ROOT).mock(return_value=httpx.Response(200, html=root_html))
    # POST still returns the login form => not authenticated
    respx.post(url__startswith=ROOT).mock(return_value=httpx.Response(200, html=root_html))

    with httpx.Client(follow_redirects=True) as client:
        with pytest.raises(AuthError):
            Authenticator(client, _settings()).login()


def test_login_without_credentials_raises():
    with httpx.Client() as client:
        with pytest.raises(AuthError):
            Authenticator(client, _settings(user="", pw="")).login()


def test_logged_in_page_with_logout_form_is_not_seen_as_logged_out():
    # Regression: the logout form still contains "tx_felogin_login", but has no
    # password field. Detection must key on the password input, not the string.
    s = _settings()
    auth = Authenticator(httpx.Client(), s)
    logged_in_html = (FIX / "forum_logged_in.html").read_text(encoding="utf-8")
    assert auth._login_form_present(logged_in_html) is False
    resp = httpx.Response(200, html=logged_in_html,
                          request=httpx.Request("GET", ROOT),
                          headers={"content-type": "text/html"})
    assert auth._is_logged_in(resp) is True
    auth._logged_in = True
    assert auth.is_session_expired(resp) is False


@respx.mock
def test_login_succeeds_when_post_returns_logged_in_page():
    root_html = (FIX / "forum_root.html").read_text(encoding="utf-8")
    logged_in_html = (FIX / "forum_logged_in.html").read_text(encoding="utf-8")
    respx.get(ROOT).mock(return_value=httpx.Response(200, html=root_html))
    # realistic: POST returns the page WITH the logout form (tx_felogin_login present)
    respx.post(url__startswith=ROOT).mock(return_value=httpx.Response(200, html=logged_in_html))
    with httpx.Client(follow_redirects=True) as client:
        Authenticator(client, _settings()).login()  # must not raise


def test_session_expiry_detected_on_403_and_reappearing_form():
    s = _settings()
    auth = Authenticator(httpx.Client(), s)
    auth._logged_in = True

    r403 = httpx.Response(403, request=httpx.Request("GET", ROOT))
    assert auth.is_session_expired(r403)

    form_html = (FIX / "forum_root.html").read_text(encoding="utf-8")
    r200 = httpx.Response(200, html=form_html, request=httpx.Request("GET", ROOT),
                          headers={"content-type": "text/html"})
    assert auth.is_session_expired(r200)  # login form back => expired
