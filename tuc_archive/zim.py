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

"""Build a Kiwix-compatible ZIM from the on-disk content store.

Streams pages and assets out of :class:`~tuc_archive.store.ContentStore`,
rewrites internal links to relative in-ZIM paths, and writes one ZIM via
zimscraperlib's ``Creator``. Nothing is re-fetched here — the network phase is
fully decoupled from packaging, so you can rebuild the ZIM any number of times
from a single crawl.
"""

from __future__ import annotations

import datetime
import logging
import struct
import zlib
from pathlib import Path

from zimscraperlib.zim.creator import Creator
from zimscraperlib.zim.metadata import (
    CreatorMetadata,
    DateMetadata,
    DefaultIllustrationMetadata,
    DescriptionMetadata,
    LanguageMetadata,
    NameMetadata,
    PublisherMetadata,
    StandardMetadataList,
    TagsMetadata,
    TitleMetadata,
)

from .config import Settings
from .rewrite import LinkRewriter, PathMapper, rewrite_css, scrub_pii_text
from .store import ContentStore
from .utils import ScopeMatcher, normalize_url, url_hash

log = logging.getLogger("tuc.zim")


def _solid_png(size: int = 48, rgb=(0x2C, 0x3E, 0x50)) -> bytes:
    """Generate a valid solid-colour PNG without external image libraries."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit truecolour
    row = b"\x00" + bytes(rgb) * size
    raw = row * size
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


_HOMEPAGE = """<!doctype html><html lang="el"><head><meta charset="utf-8">
<title>{title}</title><style>body{{font-family:sans-serif;max-width:780px;
margin:2rem auto;padding:0 1rem;color:#222}}h1{{color:#2c3e50}}
li{{margin:.25rem 0}}</style></head><body>
<h1>{title}</h1><p>{desc}</p><p>Αρχειοθετημένες σελίδες: <b>{n}</b></p>
{authors}
<ul>{items}</ul></body></html>"""

_AUTHORS_INDEX = """<!doctype html><html lang="el"><head><meta charset="utf-8">
<title>Συντάκτες</title><style>body{{font-family:sans-serif;max-width:780px;
margin:2rem auto;padding:0 1rem;color:#222}}h1{{color:#2c3e50}}
li{{margin:.2rem 0}}small{{color:#777}}</style></head><body>
<p><a href="../index.html">← Αρχική</a></p>
<h1>Συντάκτες ({n})</h1>
<ul>{items}</ul></body></html>"""

_AUTHOR_PAGE = """<!doctype html><html lang="el"><head><meta charset="utf-8">
<title>Συντάκτης: {name}</title><style>body{{font-family:sans-serif;
max-width:780px;margin:2rem auto;padding:0 1rem;color:#222}}h1{{color:#2c3e50}}
li{{margin:.5rem 0}}small{{color:#777}}.ex{{color:#555;font-size:.9em}}</style>
</head><body>
<p><a href="{up}index.html">← Αρχική</a> · <a href="index.html">Όλοι οι συντάκτες</a></p>
<h1>{name}</h1><p>Μηνύματα: <b>{n}</b></p>
<ul>{items}</ul></body></html>"""


class ZimBuilder:
    def __init__(self, settings: Settings, store: ContentStore,
                 exclude: list[str] | None = None):
        self.settings = settings
        self.store = store
        self.site = settings.site
        self.mapper = PathMapper(self.site)
        # Build-time filter: drop already-stored pages whose URL now matches an
        # exclude rule (site defaults + any extra --exclude). Lets a second
        # build prune junk (e.g. index.php footer pages) from a 17-year store
        # WITHOUT re-crawling — the network store is untouched.
        self._filter = ScopeMatcher(self.site, exclude=exclude)

        # Build the URL -> zim-path resolver up front so the rewriter is exact.
        self._page_paths: dict[str, str] = {}
        self._asset_paths: dict[str, str] = {}
        self._skipped = 0
        self._scrub_pii = False  # set by build()
        for meta, _ in store.iter_pages():
            url = meta["url"]
            if self._filter.excluded(url):
                self._skipped += 1
                continue
            self._page_paths[url] = self.mapper.page_path(url)
        # assets are mapped lazily from the manifest below

    def _resolve(self, normalized_url: str) -> str | None:
        if normalized_url in self._page_paths:
            return self._page_paths[normalized_url]
        return self._asset_paths.get(normalized_url)

    def build(self, zim_path: Path, title: str, description: str,
              language: str = "ell", main_url: str | None = None,
              redact_emails: bool = False, author_index: bool = False,
              scrub_pii: bool = False) -> Path:
        zim_path = Path(zim_path)
        zim_path.parent.mkdir(parents=True, exist_ok=True)
        self._scrub_pii = scrub_pii

        rewriter = LinkRewriter(self.site, self._resolve,
                                redact_emails=redact_emails, scrub_pii=scrub_pii)

        # pre-map assets (need their on-disk extension) ----------------------
        asset_files = list(self.store.iter_assets())
        for f in asset_files:
            # reconstruct the URL from the manifest would be ideal; we instead
            # key assets by their stored filename and map any link whose hash
            # matches. The rewriter resolves via normalized URL, so we register
            # asset URLs as we read the manifest:
            pass
        self._index_asset_urls()

        main_path = (
            self._page_paths.get(normalize_url(main_url, site=self.site))
            if main_url else None
        )
        if not main_path:
            main_path = "index.html"

        metadata = StandardMetadataList(
            Name=NameMetadata(f"tuc-archive-{zim_path.stem}"),
            Language=LanguageMetadata(language),
            Title=TitleMetadata(title[:30]),
            Creator=CreatorMetadata("tuc-archive"),
            Publisher=PublisherMetadata("tuc-archive"),
            Date=DateMetadata(datetime.date.today()),
            Description=DescriptionMetadata(description[:80]),
            Illustration_48x48_at_1=DefaultIllustrationMetadata(_solid_png()),
            Tags=TagsMetadata(["typo3", "tucforum", "forum", "archive"]),
        )

        if self._skipped:
            log.info("Excluding %d stored page(s) from ZIM (build-time filter)",
                     self._skipped)
        log.info("Writing ZIM %s (main_path=%s)", zim_path, main_path)
        with Creator(zim_path, main_path).config_metadata(metadata) as creator:
            n_pages = self._add_pages(creator, rewriter)
            n_assets = self._add_assets(creator, asset_files)
            n_authors = self._add_author_index(creator) if author_index else 0
            self._add_homepage(creator, title, description,
                               with_authors=bool(n_authors))

        log.info("ZIM done: %d pages, %d assets, %d author page(s) -> %s",
                 n_pages, n_assets, n_authors, zim_path)
        self._verify(zim_path)
        return zim_path

    @staticmethod
    def _verify(zim_path: Path) -> None:
        """Read the freshly-written ZIM back with libzim and sanity-check it.

        ``Creator`` printing "ZIM done" is NOT proof the file is valid — a
        libzim finalization bug (e.g. the Windows >2 GB large-file path) can
        leave the header's ``checksumPos`` pointing before the cluster data,
        producing a file that opens as corrupt. Catch that here so a build can
        never report success on an unreadable archive.
        """
        try:
            from libzim.reader import Archive
        except Exception:  # noqa: BLE001 - reader optional; skip if unavailable
            log.warning("libzim reader unavailable; skipping ZIM verification")
            return
        try:
            arc = Archive(str(zim_path))
            n = arc.entry_count
            if n < 1 or not arc.has_main_entry:
                raise RuntimeError(f"ZIM opened but looks empty (entries={n})")
            log.info("ZIM verified readable: %d entries, main=%s",
                     n, arc.main_entry.path)
        except Exception as e:  # noqa: BLE001
            size = zim_path.stat().st_size if zim_path.exists() else 0
            raise RuntimeError(
                f"ZIM written but FAILED read-back ({e!r}); {size/1e9:.1f} GB file "
                f"is corrupt. On Windows this is usually the libzim >2 GB bug — "
                f"build the ZIM under Linux (Docker) to keep all attachments, or "
                f"reduce size below 2 GB."
            ) from e

    # ------------------------------------------------------------------ #
    def _index_asset_urls(self):
        """Map asset URLs -> zim paths from the manifest (type == asset)."""
        import json
        if not self.store.manifest.exists():
            return
        for line in self.store.manifest.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("type") == "asset":
                url = rec["url"]
                self._asset_paths[url] = self.mapper.asset_path(url, rec.get("ext", ".bin"))

    def _add_pages(self, creator: Creator, rewriter: LinkRewriter) -> int:
        n = 0
        for meta, html_path in self.store.iter_pages():
            url = meta["url"]
            zpath = self._page_paths.get(url)
            if zpath is None:
                continue  # filtered out by exclude rules
            try:
                html = html_path.read_text(encoding="utf-8", errors="replace")
                rewritten = rewriter.rewrite(url, html, zpath)
                creator.add_item_for(
                    path=zpath,
                    title=meta.get("title") or url,
                    content=rewritten.encode("utf-8"),
                    mimetype="text/html",
                    is_front=True,
                    duplicate_ok=True,
                )
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("skip page %s: %r", url, e)
        return n

    def _add_assets(self, creator: Creator, asset_files) -> int:
        import json
        # filename(key+ext) -> url, via manifest
        key_to_url = {}
        if self.store.manifest.exists():
            for line in self.store.manifest.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("type") == "asset":
                    key_to_url[rec["key"] + rec.get("ext", ".bin")] = rec["url"]
        n = 0
        for f in asset_files:
            url = key_to_url.get(f.name)
            if not url:
                continue
            zpath = self._asset_paths.get(url)
            if not zpath:
                continue
            try:
                if f.suffix.lower() == ".css":
                    # rewrite url(...) refs to relative in-ZIM paths
                    css = f.read_text(encoding="utf-8", errors="replace")
                    css = rewrite_css(css, zpath, url, self._resolve, self.site)
                    creator.add_item_for(path=zpath, content=css.encode("utf-8"),
                                         mimetype="text/css", duplicate_ok=True)
                else:
                    creator.add_item_for(path=zpath, fpath=f, duplicate_ok=True)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("skip asset %s: %r", url, e)
        return n

    # ------------------------------------------------------------------ #
    def _author_slug(self, name: str) -> str:
        """Stable, ZIM-safe path for an author page. ASCII-ish slug + short hash
        (the hash guarantees uniqueness even for non-Latin / colliding names)."""
        import re
        base = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "author"
        return f"authors/{base[:40]}-{url_hash(name)[:6]}.html"

    def _add_author_index(self, creator: Creator) -> int:
        """Generate static per-author pages + an A–Z index, for browse-by-author.

        Pure static HTML indexed by Kiwix, so the author's name is searchable and
        their page lists every post we archived. Privacy note: this aggregates a
        named person's whole posting history — only enable for archives you are
        authorised to build that way (``--author-index``; off by default).
        """
        import html as _html
        from collections import defaultdict

        authors: dict[str, list[dict]] = defaultdict(list)
        for meta, _ in self.store.iter_pages():
            zpath = self._page_paths.get(meta["url"])
            if zpath is None:
                continue  # page filtered out of this build
            ttl = meta.get("title") or meta["url"]
            if self._scrub_pii:
                ttl = scrub_pii_text(ttl)
            for p in (meta.get("posts") or []):
                a = (p.get("author") or "").strip()
                if not a:
                    continue
                excerpt = (p.get("text_excerpt") or "")[:200]
                if self._scrub_pii:
                    excerpt = scrub_pii_text(excerpt)
                authors[a].append({
                    "zpath": zpath, "title": ttl,
                    "ts": p.get("timestamp") or "",
                    "excerpt": excerpt,
                })
        if not authors:
            log.info("author-index requested but no post authors found; skipping")
            return 0

        slugs = {a: self._author_slug(a) for a in authors}
        # per-author pages
        for a, posts in sorted(authors.items()):
            zpath = slugs[a]
            up = "../" * zpath.count("/")
            rows = "".join(
                f'<li><a href="{up}{_html.escape(p["zpath"])}">{_html.escape(p["title"])}</a>'
                f' <small>{_html.escape(p["ts"])}</small>'
                f'<div class="ex">{_html.escape(p["excerpt"])}</div></li>'
                for p in posts
            )
            page = _AUTHOR_PAGE.format(name=_html.escape(a), n=len(posts),
                                      items=rows, up=up)
            creator.add_item_for(path=zpath, title=f"Συντάκτης: {a}",
                                 content=page.encode("utf-8"), mimetype="text/html",
                                 is_front=True, duplicate_ok=True)
        # A–Z index
        idx_items = "".join(
            f'<li><a href="{slugs[a].split("/")[-1]}">{_html.escape(a)}</a>'
            f' <small>({len(authors[a])})</small></li>'
            for a in sorted(authors)
        )
        idx = _AUTHORS_INDEX.format(n=len(authors), items=idx_items)
        creator.add_item_for(path="authors/index.html", title="Συντάκτες",
                             content=idx.encode("utf-8"), mimetype="text/html",
                             is_front=True, duplicate_ok=True)
        log.info("Author index: %d authors", len(authors))
        return len(authors)

    def _add_homepage(self, creator: Creator, title: str, description: str,
                      with_authors: bool = False):
        items = "".join(
            f'<li><a href="{p}">{(self.store.page_meta(u) or {}).get("title") or u}</a></li>'
            for u, p in sorted(self._page_paths.items(), key=lambda kv: kv[1])
        )
        authors_link = (
            '<p><a href="authors/index.html"><b>Αναζήτηση ανά συντάκτη →</b></a></p>'
            if with_authors else ""
        )
        html = _HOMEPAGE.format(
            title=title, desc=description, n=len(self._page_paths),
            items=items, authors=authors_link,
        )
        creator.add_item_for(
            path="index.html", title=title, content=html.encode("utf-8"),
            mimetype="text/html", is_front=True, duplicate_ok=True,
        )
