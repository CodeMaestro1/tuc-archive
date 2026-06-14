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

"""Map archived URLs to stable in-ZIM paths and rewrite HTML links.

The same :class:`PathMapper` is used by both the ZIM builder (to place entries)
and the rewriter (to point ``href``/``src`` at those entries), guaranteeing the
two never disagree.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from .config import Site
from .utils import normalize_url, url_hash

_HTML_EXTS = {".html", ".htm", ""}


class PathMapper:
    """Deterministic URL -> ZIM entry path (no leading slash)."""

    def __init__(self, site: Site):
        self.site = site

    def page_path(self, url: str) -> str:
        p = urlparse(url)
        path = p.path.lstrip("/")
        if path == "" or path.endswith("/"):
            path = (path + "index").lstrip("/")
        # ensure an .html suffix so browsers/Kiwix serve it as a document
        root, dot, ext = path.rpartition(".")
        if not dot or ("." + ext.lower()) not in _HTML_EXTS:
            path = path + ".html"
        if p.query:
            # disambiguate query variants without illegal chars
            path = re.sub(r"\.html$", "", path) + "~" + url_hash(url)[:10] + ".html"
        return self._safe(path)

    def asset_path(self, url: str, ext: str) -> str:
        p = urlparse(url)
        path = p.path.lstrip("/")
        if not path or path.endswith("/"):
            path = "assets/" + url_hash(url) + ext
        elif p.query:
            root = re.sub(r"\.[^./]+$", "", path)
            path = root + "~" + url_hash(url)[:10] + ext
        return self._safe(path)

    @staticmethod
    def _safe(path: str) -> str:
        # collapse anything libzim/Kiwix dislikes
        path = path.replace("\\", "/").lstrip("/")
        path = re.sub(r"/{2,}", "/", path)
        return path


class LinkRewriter:
    """Rewrite in-archive links to relative ZIM paths; flag externals.

    ``in_archive(url) -> str|None`` returns the ZIM path for a URL we archived,
    else None (external / not crawled — left as an absolute link).
    """

    REWRITE_ATTRS = (("a", "href"), ("img", "src"), ("link", "href"),
                     ("script", "src"), ("source", "src"))

    def __init__(self, site: Site, resolver, redact_emails: bool = False,
                 scrub_pii: bool = False):
        self.site = site
        self.resolver = resolver  # callable(normalized_url) -> zim_path | None
        self.redact_emails = redact_emails
        self.scrub_pii = scrub_pii

    def rewrite(self, page_url: str, html: str, current_zim_path: str) -> str:
        tree = HTMLParser(html)
        depth = current_zim_path.count("/")
        up = "../" * depth

        # privacy/cleanup: drop login/logout frames (username + stale tokens)
        for sel in self.site.strip_selectors:
            for node in tree.css(sel):
                node.decompose()

        if self.redact_emails:
            self._redact_emails(tree)

        for tag, attr in self.REWRITE_ATTRS:
            for node in tree.css(f"{tag}[{attr}]"):
                val = node.attributes.get(attr)
                if not val or val.startswith(("#", "data:", "javascript:", "mailto:", "tel:")):
                    continue
                absu = urljoin(page_url, val)
                target = self.resolver(normalize_url(absu, site=self.site))
                if target:
                    node.attrs[attr] = up + target  # relative within the ZIM
                # else: external resource — leave as-is (Kiwix blocks the network)

        html_out = tree.html or html
        if self.scrub_pii:
            html_out = scrub_pii_text(html_out)
        return html_out

    _PLACEHOLDER = "[email hidden]"

    def _redact_emails(self, tree):
        """Remove author e-mails so the ZIM can't be scraped for addresses.

        Handles the structured tx_tucforum obfuscation (``data-mailto-token`` /
        ``data-mailto-vector`` elements, whose visible text is the scrambled
        address) and any real ``mailto:`` links. Replaces the whole element with
        a literal placeholder, dropping attributes and text in one step.

        Note: free-text e-mails typed into a post BODY are NOT caught (that would
        need fuzzy regex over prose, with false positives) — documented limit.
        """
        for sel in ("[data-mailto-token]", "[data-mailto-vector]", "a[href^='mailto:']"):
            for node in tree.css(sel):
                try:
                    node.replace_with(self._PLACEHOLDER)
                except Exception:  # noqa: BLE001 - fallback if backend lacks replace_with
                    for a in ("data-mailto-token", "data-mailto-vector", "href"):
                        if a in node.attributes:
                            node.attrs[a] = ""


# --------------------------------------------------------------------------- #
# Best-effort PII scrubbing (data minimisation). Masks the *structured* personal
# data that commonly appears free-typed in Greek university forum posts. Applied
# to the serialised HTML; the patterns are length/format-anchored at word
# boundaries, so they do not collide with the page's functional attributes
# (hex cHash, base64 csrf, short numeric IDs).
#
# IMPORTANT LIMIT: this catches *formats*, not meaning. Names, addresses,
# paraphrased identifiers and the contents of attachments are NOT removed. It
# reduces exposure; it does not make a ZIM safe to publish without review.
_PII_PATTERNS = (
    # IBAN first (its digit runs would otherwise be partly eaten by the id rule).
    (re.compile(r"\bGR\d{2}[\s\d]{16,30}\b"), "[redacted-iban]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r"\b69\d{8}\b"), "[redacted-phone]"),   # GR mobile
    (re.compile(r"\b2\d{9}\b"), "[redacted-phone]"),    # GR landline
    (re.compile(r"\b\d{11}\b"), "[redacted-id]"),       # AMKA-shaped
)


def scrub_pii_text(text: str) -> str:
    """Mask emails / phone numbers / AMKA / IBAN in a string. Best-effort."""
    for rx, repl in _PII_PATTERNS:
        text = rx.sub(repl, text)
    return text


_CSS_URL_RX = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")


def rewrite_css(css_text: str, css_zim_path: str, css_url: str,
                resolver, site: Site) -> str:
    """Rewrite url(...) refs inside a stylesheet to relative in-ZIM paths."""
    depth = css_zim_path.count("/")
    up = "../" * depth

    def repl(m):
        ref = m.group(1).strip()
        if ref.startswith(("data:", "#")):
            return m.group(0)
        absu = urljoin(css_url, ref)
        target = resolver(normalize_url(absu, site=site))
        return f"url({up}{target})" if target else m.group(0)

    return _CSS_URL_RX.sub(repl, css_text)
