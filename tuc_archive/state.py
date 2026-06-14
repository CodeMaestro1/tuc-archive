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

"""Crawl state: pending queue, visited set, errors — with atomic persistence.

The state file is human-readable YAML. Writes are atomic (temp file in the
same directory + ``os.replace``) so killing the process mid-write never
corrupts an existing snapshot.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class CompletedEntry:
    url: str
    status: int
    etag: str | None = None
    last_modified: str | None = None
    stored_as: str | None = None  # storage key / path of saved content


@dataclass
class CrawlState:
    seeds: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    completed: dict[str, CompletedEntry] = field(default_factory=dict)  # url -> entry
    errors: dict[str, str] = field(default_factory=dict)  # url -> last error
    in_flight: dict[str, float] = field(default_factory=dict)  # url -> claimed_at (distributed)
    last_write: float = 0.0

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    _seen: set[str] = field(default_factory=set, repr=False, compare=False)

    # ----- queue operations (thread-safe) ----------------------------------
    def __post_init__(self):
        self._seen = set(self.pending) | set(self.completed) | set(self.errors)

    def add(self, url: str) -> bool:
        """Enqueue a URL unless already seen. Returns True if newly added."""
        with self._lock:
            if url in self._seen:
                return False
            self._seen.add(url)
            self.pending.append(url)
            return True

    def add_many(self, urls) -> int:
        return sum(1 for u in urls if self.add(u))

    def next_url(self) -> str | None:
        with self._lock:
            return self.pending.pop(0) if self.pending else None

    def claim_batch(self, n: int) -> list[str]:
        """Pop up to n URLs and mark them in-flight (for distributed workers)."""
        with self._lock:
            batch = self.pending[:n]
            del self.pending[: len(batch)]
            now = time.time()
            for u in batch:
                self.in_flight[u] = now
            return batch

    def complete(self, entry: CompletedEntry) -> None:
        with self._lock:
            self.completed[entry.url] = entry
            self.in_flight.pop(entry.url, None)
            self.errors.pop(entry.url, None)

    def fail(self, url: str, msg: str) -> None:
        with self._lock:
            self.errors[url] = msg
            self.in_flight.pop(url, None)

    def requeue_in_flight(self) -> int:
        """Move ALL in-flight URLs back to pending (e.g. after a resume).

        Standalone crawls have no lease reaper, so any URL claimed but not
        completed when the process stopped would otherwise be orphaned. Called
        at the start of a run so an interrupted crawl loses nothing.
        """
        with self._lock:
            moved = 0
            for u in list(self.in_flight):
                self.in_flight.pop(u, None)
                if u not in self.completed and u not in self.pending:
                    self.pending.insert(0, u)
                    moved += 1
            return moved

    def requeue_stale(self, timeout: float) -> int:
        """Re-queue in-flight URLs older than ``timeout`` (dead worker)."""
        with self._lock:
            now = time.time()
            stale = [u for u, t in self.in_flight.items() if now - t > timeout]
            for u in stale:
                self.in_flight.pop(u, None)
                self.pending.insert(0, u)
            return len(stale)

    @property
    def remaining(self) -> int:
        return len(self.pending) + len(self.in_flight)

    def stats(self) -> dict:
        with self._lock:
            return {
                "pending": len(self.pending),
                "in_flight": len(self.in_flight),
                "completed": len(self.completed),
                "errors": len(self.errors),
            }

    # ----- persistence ------------------------------------------------------
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "last_write": time.time(),
                "seeds": self.seeds,
                "config": self.config,
                "pending": list(self.pending),
                "in_flight": dict(self.in_flight),
                "completed": {
                    u: vars(e) for u, e in self.completed.items()
                },
                "errors": dict(self.errors),
            }

    def save(self, path: Path) -> None:
        """Atomically write the state to ``path`` (temp file + os.replace)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        self.last_write = data["last_write"]
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic on POSIX and Windows
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @classmethod
    def load(cls, path: Path) -> "CrawlState":
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        completed = {
            u: CompletedEntry(**e) for u, e in (data.get("completed") or {}).items()
        }
        st = cls(
            seeds=data.get("seeds", []),
            config=data.get("config", {}),
            pending=data.get("pending", []),
            completed=completed,
            errors=data.get("errors", {}),
            in_flight=data.get("in_flight", {}),
            last_write=data.get("last_write", 0.0),
        )
        return st
