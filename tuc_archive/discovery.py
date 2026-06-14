"""Category discovery: list crawlable forum categories from sitemap or menu.

Used by `tuc-archive discover` so a user can see/select categories before
committing to a crawl. Best-effort: tries the XML sitemap first, then falls
back to scraping the on-page navigation menu.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser

from .config import Settings
from .parser import ForumParser
from .utils import ScopeMatcher, normalize_url

log = logging.getLogger("tuc.discovery")


@dataclass
class Category:
    title: str
    url: str
    cat_id: str | None = None
    group: str | None = None
    topic_count: int | None = None


def discover_categories(client: httpx.Client, settings: Settings,
                        scope: ScopeMatcher) -> list[Category]:
    site = settings.site
    cats: dict[str, Category] = {}

    # 0) PRIMARY: the tx_tucforum category list on the forum root page.
    try:
        root_url = site.forum_root_url()
        r = client.get(root_url)
        if r.status_code == 200:
            page = ForumParser(site).parse(root_url, r.text)
            for c in page.categories:
                u = c["url"]
                if u and u not in cats:
                    cats[u] = Category(
                        title=c["title"], url=u, cat_id=c.get("cat_id"),
                        group=c.get("group"), topic_count=c.get("topic_count"),
                    )
            if cats:
                log.info("Found %d categories from the forum catlist", len(cats))
    except Exception as e:  # noqa: BLE001
        log.debug("catlist discovery failed: %r", e)

    # 1) sitemap.xml
    for sm in ("/sitemap.xml", "/sitemap_index.xml"):
        try:
            r = client.get(site.base_url.rstrip("/") + sm)
            if r.status_code == 200 and "<urlset" in r.text or "<sitemapindex" in r.text:
                for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text):
                    u = normalize_url(loc, site=site)
                    if scope.in_scope(u) and u not in cats:
                        cats[u] = Category(title=_title_from_url(u), url=u)
        except Exception as e:  # noqa: BLE001
            log.debug("sitemap %s failed: %r", sm, e)

    # 2) on-page navigation menu (fallback / supplement)
    try:
        r = client.get(site.base_url)
        if r.status_code == 200:
            tree = HTMLParser(r.text)
            for a in tree.css("nav a[href], .menu a[href], #menu a[href]"):
                href = a.attributes.get("href")
                if not href:
                    continue
                u = normalize_url(href, base=site.base_url, site=site)
                if scope.in_scope(u) and u not in cats:
                    cats[u] = Category(title=a.text(strip=True) or _title_from_url(u), url=u)
    except Exception as e:  # noqa: BLE001
        log.debug("menu scrape failed: %r", e)

    return sorted(cats.values(), key=lambda c: c.url)


def _title_from_url(url: str) -> str:
    seg = [s for s in url.split("/") if s][-1] if "/" in url else url
    return re.sub(r"[-_]+", " ", seg).strip().title() or url
