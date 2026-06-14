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

    def __init__(self, site: Site, resolver, redact_emails: bool = False):
        self.site = site
        self.resolver = resolver  # callable(normalized_url) -> zim_path | None
        self.redact_emails = redact_emails

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
