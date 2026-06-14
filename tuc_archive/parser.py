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

"""HTML parsing for tx_tucforum pages (links, posts, metadata, attachments).

Uses selectolax (fast, streaming-friendly C parser) instead of BeautifulSoup
to keep RAM down on very large pages. All CSS selectors come from
``config.Site`` so they can be tuned without touching this code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from .config import Site
from .utils import normalize_url, same_site

_DATE_RX = re.compile(r"(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})")


def _int(s: str | None) -> int | None:
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else None


def decode_mailto(token: str | None, shift: int) -> str | None:
    """Decode a tx_tucforum data-mailto-token (a Caesar cipher).

    Observed scheme: each character shifted by +1 on the page, so decoding
    applies ``shift`` (default -1): 'A'->'@', 'nbjmup'->'mailto', '/hs'->'.gr'.
    Returns the bare e-mail address (the leading "mailto:" is stripped).
    """
    if not token:
        return None
    dec = "".join(chr(ord(c) + shift) for c in token)
    if dec[:6].lower() == "mailto":
        dec = dec[6:].lstrip(":;+*  ")  # tolerate the colon-position cipher quirk
    return dec if "@" in dec else None


@dataclass
class PostMeta:
    post_id: str | None = None
    author: str | None = None
    timestamp: str | None = None          # "13-06-2026 16:41" as shown
    email_obfuscated: str | None = None   # data-mailto-token (raw)
    email: str | None = None              # decoded address (best-effort)
    email_visible: str | None = None      # on-page text, e.g. "johndoe<στο>example.org"
    updated: str | None = None
    department: str | None = None         # "Ιδιότητα" / role
    text_excerpt: str | None = None
    attachments: list[str] = field(default_factory=list)


@dataclass
class PageData:
    url: str
    title: str | None = None
    csrf: str | None = None
    breadcrumb: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)        # normalized, in-page navigation
    attachments: list[str] = field(default_factory=list)  # absolute URLs to download
    ajax_endpoints: list[str] = field(default_factory=list)
    posts: list[PostMeta] = field(default_factory=list)
    categories: list[dict] = field(default_factory=list)  # forum-root catlist
    topic_title: str | None = None
    forum_breadcrumb: list[str] = field(default_factory=list)
    subresources: list[str] = field(default_factory=list)  # same-origin css/js/img
    next_page: str | None = None


class ForumParser:
    def __init__(self, site: Site, deobfuscate_emails: bool = False):
        self.site = site
        # When False (default) author e-mails are left obfuscated everywhere —
        # the on-page text ("name<στο>host") and data-mailto-token are preserved
        # but no plaintext address is produced, so the archive can't be scraped
        # for an e-mail list.
        self.deobfuscate_emails = deobfuscate_emails

    def parse(self, url: str, html: str) -> PageData:
        tree = HTMLParser(html)
        data = PageData(url=url)

        title_node = tree.css_first("title")
        if title_node:
            data.title = title_node.text(strip=True)

        # forum CSRF token (data-csrf on any element)
        for node in tree.css(f"[{self.site.csrf_attr}]"):
            tok = node.attributes.get(self.site.csrf_attr)
            if tok:
                data.csrf = tok
                break

        data.breadcrumb = [
            a.text(strip=True) for a in tree.css(self.site.sel_breadcrumb) if a.text(strip=True)
        ]

        # all hyperlinks (caller decides scope)
        seen = set()
        for a in tree.css("a[href]"):
            href = a.attributes.get("href")
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absu = normalize_url(href, base=url, site=self.site)
            if absu not in seen:
                seen.add(absu)
                data.links.append(absu)

        # attachments (kept as absolute, un-normalized — we want the real file)
        for a in tree.css(self.site.sel_attachment):
            href = a.attributes.get("href")
            if href:
                # normalize so the storage key matches what LinkRewriter resolves
                data.attachments.append(normalize_url(href, base=url, site=self.site))

        # same-origin sub-resources (CSS / JS / images) for offline embedding
        seen_sub = set()
        for sel, attr in (("link[rel~=stylesheet]", "href"), ("script[src]", "src"),
                          ("img[src]", "src")):
            for node in tree.css(sel):
                ref = node.attributes.get(attr)
                if not ref or ref.startswith(("data:", "#")):
                    continue
                absu = normalize_url(ref, base=url, site=self.site)
                if same_site(absu, self.site.base_url) and absu not in seen_sub:
                    seen_sub.add(absu)
                    data.subresources.append(absu)

        # AJAX fragment endpoints (data-tucforumendpoint) — fetched separately
        for node in tree.css(f"[{self.site.ajax_endpoint_attr}]"):
            ep = node.attributes.get(self.site.ajax_endpoint_attr)
            if ep:
                data.ajax_endpoints.append(urljoin(url, ep))

        # forum-root category list (tx_tucforum catlist)
        data.categories = self._parse_categories(tree, url)

        # topic view: title + forum breadcrumb
        tnode = tree.css_first(self.site.sel_topic_title)
        if tnode:
            data.topic_title = tnode.text(strip=True)
        data.forum_breadcrumb = [
            li.text(strip=True) for li in tree.css(self.site.sel_forum_breadcrumb)
            if li.text(strip=True)
        ]

        # posts + metadata
        for node in tree.css(self.site.sel_post):
            data.posts.append(self._parse_post(node, url))

        # pagination "next"
        nxt = tree.css_first(self.site.sel_pagination_next)
        if nxt and nxt.attributes.get("href"):
            data.next_page = normalize_url(nxt.attributes["href"], base=url, site=self.site)

        return data

    def _parse_categories(self, tree, base_url: str) -> list[dict]:
        """Extract the tx_tucforum category list from the forum root page."""
        out: list[dict] = []
        for group in tree.css(self.site.sel_catgroup):
            gt = group.css_first(self.site.sel_catgroup_title)
            group_title = gt.text(strip=True) if gt else None
            for cat in group.css(self.site.sel_cat):
                link = cat.css_first(self.site.sel_cat_link)
                if not link or not link.attributes.get("href"):
                    continue
                desc = cat.css_first(self.site.sel_cat_desc)
                count = cat.css_first(self.site.sel_cat_count)
                idnode = cat.css_first(self.site.sel_cat_idnode)
                out.append({
                    "group": group_title,
                    "title": link.text(strip=True),
                    "url": normalize_url(link.attributes["href"], base=base_url, site=self.site),
                    "cat_id": idnode.attributes.get("data-catid") if idnode else None,
                    "topic_count": _int(count.text(strip=True)) if count else None,
                    "description": desc.text(strip=True) if desc else None,
                })
        return out

    def _parse_post(self, node, base_url: str) -> PostMeta:
        post = PostMeta(post_id=node.attributes.get("data-postid"))

        # header line: "Συντάχθηκε <date> από <author>"
        authored = node.css_first(self.site.sel_post_authored)
        if authored:
            htext = authored.text(separator=" ", strip=True)
            m = _DATE_RX.search(htext)
            if m:
                post.timestamp = m.group(1)
            sep = self.site.label_author_sep
            if sep in htext:
                post.author = htext.split(sep, 1)[1].strip() or None

        # author-info block: email, updated, role
        info = node.css_first(self.site.sel_post_info)
        if info:
            mail = info.css_first(self.site.sel_post_email)
            if mail:
                post.email_obfuscated = mail.attributes.get("data-mailto-token")
                post.email_visible = mail.text(strip=True) or None
                if self.deobfuscate_emails:
                    post.email = (
                        decode_mailto(post.email_obfuscated, self.site.mailto_shift)
                        or self._email_from_visible(post.email_visible)
                    )
            itext = info.text(separator="\n", strip=True)
            post.updated = self._after_label(itext, self.site.label_updated)
            post.department = self._after_label(itext, self.site.label_role)

        # message body excerpt + in-message attachments
        msg = node.css_first(self.site.sel_post_message)
        if msg:
            text = msg.text(separator=" ", strip=True)
            post.text_excerpt = (text[:280] + "…") if len(text) > 280 else text

        for a in node.css(self.site.sel_post_attachments):
            href = a.attributes.get("href") or a.attributes.get("src")
            if href and ("/fileadmin/" in href or "/uploads/" in href):
                post.attachments.append(normalize_url(href, base=base_url, site=self.site))

        return post

    @staticmethod
    def _after_label(text: str, label: str) -> str | None:
        m = re.search(re.escape(label) + r"\s*:?\s*([^\n]+)", text)
        if not m:
            return None
        val = m.group(1).strip().rstrip(".").strip()
        return val or None

    @staticmethod
    def _email_from_visible(visible: str | None) -> str | None:
        if not visible:
            return None
        # restore obfuscated separators: "name<στο>host" / "name [at] host"
        s = visible
        for at in ("<στο>", "[στο]", " στο ", "<at>", "[at]", " at "):
            s = s.replace(at, "@")
        s = s.replace("<τελεία>", ".").replace(" τελεία ", ".")
        return s if "@" in s else None
