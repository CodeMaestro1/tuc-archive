"""URL canonicalisation, scope matching, and small shared helpers."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse, urlunparse

from .config import Site


def normalize_url(url: str, base: str | None = None, site: Site | None = None) -> str:
    """Return a canonical form so the same logical page maps to one key.

    - resolves relative URLs against ``base``
    - drops the fragment (#...)
    - lower-cases scheme/host
    - removes default ports
    - strips session / cache / tracking query keys (``site.strip_query_keys``)
    - sorts remaining query parameters for stable comparison
    """
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    p = urlparse(url)

    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]

    strip = set(site.strip_query_keys) if site else set()
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in strip]
    pairs.sort()
    query = urlencode(pairs)

    path = p.path or "/"
    return urlunparse((scheme, netloc, path, p.params, query, ""))


def url_hash(url: str) -> str:
    """Stable short hash of a (normalized) URL — used as a storage key."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def same_site(url: str, base: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()


class ScopeMatcher:
    """Decide whether a URL should be crawled.

    in_scope = matches site.scope_pattern (or user-supplied include patterns)
               AND does not match any exclude pattern.
    """

    def __init__(
        self,
        site: Site,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ):
        self.site = site
        inc = include or [site.scope_pattern]
        self._include = [re.compile(p) for p in inc]
        self._exclude = [re.compile(p) for p in list(site.exclude_patterns) + (exclude or [])]

    def in_scope(self, url: str) -> bool:
        if not same_site(url, self.site.base_url):
            return False
        target = self._path_query(url)
        if any(rx.search(target) for rx in self._exclude):
            return False
        return any(rx.search(target) for rx in self._include)

    def excluded(self, url: str) -> bool:
        return any(rx.search(self._path_query(url)) for rx in self._exclude)

    @staticmethod
    def _path_query(url: str) -> str:
        # URL-decode so exclude/include regexes can use literal brackets
        # (e.g. "[format]=rss") and match percent-encoded URLs (%5Bformat%5D).
        from urllib.parse import unquote

        p = urlparse(url)
        return unquote(p.path + (("?" + p.query) if p.query else ""))
