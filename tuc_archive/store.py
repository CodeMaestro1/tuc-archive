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

"""Raw content store: intermediate on-disk layout enabling resume + ZIM build.

Layout (under <output>/store/):
    pages/<hash>.html      raw HTML
    pages/<hash>.meta.json  {url, status, etag, last_modified, content_type, title}
    assets/<hash><ext>     attachments / media
    manifest.jsonl         append-only log of stored items (one JSON per line)

The store is keyed by canonical-URL hash so re-crawling is idempotent and the
ZIM builder can stream straight from disk without re-fetching.
"""

from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from .utils import url_hash


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".w-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ContentStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.pages = self.root / "pages"
        self.assets = self.root / "assets"
        self.manifest = self.root / "manifest.jsonl"
        self.pages.mkdir(parents=True, exist_ok=True)
        self.assets.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ----- pages ------------------------------------------------------------
    def has_page(self, url: str) -> bool:
        return (self.pages / f"{url_hash(url)}.meta.json").exists()

    def page_meta(self, url: str) -> dict | None:
        p = self.pages / f"{url_hash(url)}.meta.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save_page(self, url: str, html: bytes, meta: dict) -> str:
        key = url_hash(url)
        _atomic_write_bytes(self.pages / f"{key}.html", html)
        meta = {**meta, "url": url, "key": key}
        _atomic_write_bytes(
            self.pages / f"{key}.meta.json",
            json.dumps(meta, ensure_ascii=False, indent=0).encode("utf-8"),
        )
        self._append_manifest({"type": "page", **meta})
        return key

    # ----- assets -----------------------------------------------------------
    def has_asset(self, url: str) -> bool:
        return any(self.assets.glob(f"{url_hash(url)}.*"))

    def save_asset(self, url: str, content: bytes, content_type: str | None) -> str:
        key = url_hash(url)
        ext = self._guess_ext(url, content_type)
        _atomic_write_bytes(self.assets / f"{key}{ext}", content)
        self._append_manifest(
            {"type": "asset", "url": url, "key": key, "ext": ext, "content_type": content_type}
        )
        return key + ext

    @staticmethod
    def _guess_ext(url: str, content_type: str | None) -> str:
        path = urlparse(url).path
        _, dot, ext = path.rpartition(".")
        if dot and 1 <= len(ext) <= 5 and ext.isalnum():
            return "." + ext.lower()
        if content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if guessed:
                return guessed
        return ".bin"

    # ----- manifest ---------------------------------------------------------
    def _append_manifest(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            with self.manifest.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def iter_pages(self):
        for meta_path in sorted(self.pages.glob("*.meta.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            html_path = self.pages / f"{meta['key']}.html"
            if html_path.exists():
                yield meta, html_path

    def iter_assets(self):
        for meta_path in sorted(self.assets.glob("*")):
            if meta_path.suffix in {".tmp"}:
                continue
            yield meta_path
