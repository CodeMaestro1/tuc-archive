"""TYPO3 Extbase felogin authentication and session management.

The supplied www.tuc.gr source uses **Extbase felogin** (``tx_felogin_login``),
embedded directly in the forum root page. Flow:

  1. GET the forum root (which renders the login form when logged out).
  2. Locate the felogin ``<form>`` (the one containing ``name="pass"``) and copy
     *every* input it carries — that captures the Extbase integrity tokens
     ``__RequestToken`` (JWT CSRF), ``__trustedProperties`` and ``__referrer[*]``,
     which the backend rejects the POST without.
  3. POST to the form's OWN ``action`` URL (a parameterised URL with a cHash),
     not the plain page URL.
  4. Verify success: the login form marker disappears once authenticated.
  5. On later 401/403 or a re-appearing login form, re-login transparently.

All field names / markers live in ``config.Site`` so a different TYPO3 install
only needs that file edited.
"""

from __future__ import annotations

import logging

import httpx
from selectolax.parser import HTMLParser

from .config import Settings, Site

log = logging.getLogger("tuc.auth")


class AuthError(RuntimeError):
    pass


class Authenticator:
    def __init__(self, client: httpx.Client, settings: Settings):
        self.client = client
        self.settings = settings
        self.site: Site = settings.site
        self._logged_in = False

    # ----------------------------------------------------------------- #
    def login(self) -> None:
        if not self.settings.username or not self.settings.password:
            raise AuthError(
                "Missing credentials. Set USERNAME_FORUM/PASSWORD in .env "
                "or pass --username/--password."
            )

        page_url = self.site.resolved_login_url()
        log.info("Fetching login page %s", page_url)
        r = self.client.get(page_url)
        r.raise_for_status()

        action, payload = self._extract_login_form(r.text, page_url)
        log.info("Submitting Extbase felogin form to %s", action)
        resp = self.client.post(action, data=payload, headers={"Referer": page_url})

        if not self._is_logged_in(resp):
            # confirm against a fresh fetch of the forum root before failing
            check = self.client.get(self.site.forum_root_url())
            if not self._is_logged_in(check):
                raise AuthError(
                    "Login failed (login form still present). Verify credentials "
                    "and config.Site field names / login_form_marker."
                )
        self._logged_in = True
        log.info("Login successful; session cookies: %s", list(self.client.cookies.keys()))

    # ----------------------------------------------------------------- #
    def _extract_login_form(self, html: str, page_url: str) -> tuple[str, dict]:
        from urllib.parse import urljoin

        tree = HTMLParser(html)
        form = self._find_login_form(tree)
        if form is None:
            raise AuthError(
                "No felogin form found on the login page (no <form> with "
                f"input[name='{self.site.field_password}']). Check config.Site."
            )

        # copy every input the form carries (hidden Extbase tokens included)
        data: dict[str, str] = {}
        for inp in form.css("input"):
            name = inp.attributes.get("name")
            if not name:
                continue
            if inp.attributes.get("disabled") is not None:
                continue  # skip the disabled permalogin=0 decoy
            itype = (inp.attributes.get("type") or "text").lower()
            if itype == "checkbox" and inp.attributes.get("checked") is None:
                continue
            # last writer wins; the real permalogin checkbox (value=1) overrides
            # the empty hidden fallback that precedes it in the source
            data[name] = inp.attributes.get("value", "")

        # set credentials + force the values felogin expects
        data[self.site.field_username] = self.settings.username
        data[self.site.field_password] = self.settings.password
        data[self.site.field_logintype] = self.site.logintype_value
        data[self.site.field_submit] = self.site.submit_value
        data[self.site.field_permalogin] = "1"  # stay logged in across the crawl

        action = form.attributes.get("action") or page_url
        return urljoin(page_url, action), data

    def _find_login_form(self, tree):
        pwd = self.site.field_password
        for form in tree.css("form"):
            if form.css_first(f"input[name='{pwd}']") is not None:
                return form
        return None

    # ----------------------------------------------------------------- #
    def _login_form_present(self, html: str) -> bool:
        """True iff the page renders the felogin LOGIN form.

        Detected by the password input — the same predicate used to extract the
        form. This is robust where a string match on the extension name is not:
        Extbase felogin also renders a *logout* form when authenticated, which
        still contains "tx_felogin_login", but has no password field.
        """
        pwd = self.site.field_password
        return HTMLParser(html).css_first(f"input[name='{pwd}']") is not None

    def _is_logged_in(self, resp: httpx.Response) -> bool:
        if resp.status_code >= 400:
            return False
        return not self._login_form_present(resp.text)

    def is_session_expired(self, resp: httpx.Response) -> bool:
        if resp.status_code in self.site.reauth_status_codes:
            return True
        if self._logged_in and resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            if "html" in ct and self._login_form_present(resp.text):
                return True
        return False

    def ensure_session(self, resp: httpx.Response) -> bool:
        """If the response shows an expired session, re-login. Returns True if re-logged."""
        if self.is_session_expired(resp):
            log.warning("Session expired (status %s) — re-authenticating", resp.status_code)
            self._logged_in = False
            self.login()
            return True
        return False
