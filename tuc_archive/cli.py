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

"""tuc-archive command-line interface.

Subcommands:
  discover    list crawlable categories (interactive selection)
  crawl       log in and crawl one or more category seed URLs
  resume      continue an interrupted crawl from a saved state file
  build-zim   package the on-disk store into a Kiwix ZIM
  serve       serve a ZIM with kiwix-serve (Docker) — convenience wrapper
  coordinator run the distributed coordinator (REST API)
  worker      run a distributed worker against a coordinator
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import httpx
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .config import Settings
from .crawler import Crawler
from .discovery import discover_categories
from .state import CrawlState
from .store import ContentStore
from .utils import ScopeMatcher, normalize_url

app = typer.Typer(add_completion=False, help="Archive a TYPO3 tx_tucforum forum into a ZIM.")
console = Console()


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _settings(username, password, workers, delay, output, ignore_robots) -> Settings:
    s = Settings()
    if username:
        s.username = username
    if password:
        s.password = password
    if workers:
        s.workers = workers
    if delay is not None:
        s.delay = delay
    if output:
        s.output_dir = Path(output)
        s.state_file = s.output_dir / "state.yml"
    if ignore_robots:
        s.respect_robots = False
    return s


# --------------------------------------------------------------------------- #
@app.command()
def discover(
    username: str = typer.Option(None, help="Forum username (else from .env)."),
    password: str = typer.Option(None, help="Forum password (else from .env)."),
    include: List[str] = typer.Option(None, "--include", help="Extra include regex(es)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """List accessible forum categories (sitemap + menu)."""
    _setup_logging(verbose)
    s = _settings(username, password, None, None, None, False)
    scope = ScopeMatcher(s.site, include=include)
    with httpx.Client(headers={"User-Agent": s.user_agent}, follow_redirects=True,
                      timeout=s.timeout) as client:
        from .auth import Authenticator
        Authenticator(client, s).login()
        cats = discover_categories(client, s, scope)

    table = Table(title="Discovered categories", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("URL", overflow="fold")
    for i, c in enumerate(cats, 1):
        table.add_row(str(i), c.title, c.url)
    console.print(table)
    console.print(f"[green]{len(cats)}[/] categories. Pass their URLs to `tuc-archive crawl`.")


@app.command()
def crawl(
    seeds: List[str] = typer.Argument(..., help="Category/seed URLs to archive."),
    username: str = typer.Option(None),
    password: str = typer.Option(None),
    workers: int = typer.Option(None, "--workers", "-w"),
    delay: float = typer.Option(None, "--delay", help="Seconds between requests."),
    output: str = typer.Option(None, "--output", "-o", help="Output dir."),
    include: List[str] = typer.Option(None, "--include", help="Scope regex(es)."),
    exclude: List[str] = typer.Option(None, "--exclude", help="Exclusion regex(es)."),
    category_only: bool = typer.Option(
        False, "--category-only",
        help="Restrict to the seed category's own pages + its topics "
             "(scope = /cat/<id>/ and /topic/). Avoids spidering the whole forum.",
    ),
    deobfuscate_emails: bool = typer.Option(
        False, "--deobfuscate-emails",
        help="Decode author e-mails to plaintext (default: keep obfuscated).",
    ),
    limit: int = typer.Option(0, "--limit", help="Stop after N pages (0 = unlimited). Use for tests."),
    ignore_robots: bool = typer.Option(False, "--ignore-robots"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Log in and crawl the given category seed URLs into the local store."""
    _setup_logging(verbose)
    s = _settings(username, password, workers, delay, output, ignore_robots)
    s.max_pages = limit
    s.deobfuscate_emails = deobfuscate_emails
    seeds_n = [normalize_url(u, site=s.site) for u in seeds]

    if category_only:
        include = _category_only_include(seeds_n, include)

    state = CrawlState(seeds=seeds_n, config=s.as_public_dict())
    state.add_many(seeds_n)
    _run_crawl(s, state, include, exclude)


@app.command()
def resume(
    state_file: str = typer.Argument(..., help="Path to a saved state.yml."),
    username: str = typer.Option(None),
    password: str = typer.Option(None),
    workers: int = typer.Option(None, "--workers", "-w"),
    delay: float = typer.Option(None, "--delay", help="Seconds between requests."),
    output: str = typer.Option(None, "--output", "-o"),
    include: List[str] = typer.Option(None, "--include"),
    exclude: List[str] = typer.Option(None, "--exclude"),
    category_only: bool = typer.Option(
        False, "--category-only",
        help="Re-apply category-only scope (derived from the saved seeds). Pass "
             "this if the original crawl used --category-only, else scope widens "
             "to the whole forum on resume.",
    ),
    retry_errors: bool = typer.Option(
        False, "--retry-errors",
        help="Re-queue previously-failed URLs (e.g. transient http-500) for "
             "another attempt. Best paired with --workers 1 --delay 2 so a "
             "briefly-overloaded server gets a gentle retry; permanent failures "
             "simply error again.",
    ),
    limit: int = typer.Option(0, "--limit", help="Stop after N total pages (0 = unlimited)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Resume an interrupted crawl from a state file."""
    _setup_logging(verbose)
    s = _settings(username, password, workers, delay, output, False)
    s.max_pages = limit
    s.state_file = Path(state_file)
    state = CrawlState.load(Path(state_file))
    console.print(f"[cyan]Resuming[/]: {state.stats()}")
    if retry_errors:
        n = state.requeue_errors()
        console.print(f"[yellow]Re-queued {n} errored URL(s) for retry.[/]")
    if category_only:
        include = _category_only_include(state.seeds, include)
    _run_crawl(s, state, include, exclude)


def _category_only_include(seeds, include):
    """Derive include scope ['/cat/<id>/', '/topic/'] from seed URLs.

    Shared by `crawl` and `resume` so a resumed crawl keeps the same
    category-only scope — otherwise it would widen to the whole forum and
    follow links out of the chosen category.
    """
    import re
    cat_inc = []
    for u in seeds:
        m = re.search(r"/cat/(\d+)/", u + "/")
        if m:
            cat_inc.append(rf"/cat/{m.group(1)}/")
    if not cat_inc:
        console.print("[red]--category-only needs seeds containing /cat/<id>/.[/]")
        raise typer.Exit(1)
    out = (include or []) + cat_inc + [r"/topic/"]
    console.print(f"[cyan]Category-only scope:[/] {out}")
    return out


def _run_crawl(s: Settings, state: CrawlState, include, exclude):
    s.output_dir.mkdir(parents=True, exist_ok=True)
    store = ContentStore(s.output_dir / "store")
    scope = ScopeMatcher(s.site, include=include, exclude=exclude)

    total0 = len(state.completed)
    with Progress(
        SpinnerColumn(), TextColumn("[bold]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
        TextColumn("err={task.fields[errors]}"), console=console,
    ) as progress:
        task = progress.add_task("crawling", total=None, errors=0)

        def cb(pending, in_flight, completed, errors):
            progress.update(task, completed=completed - total0,
                            total=(completed - total0) + pending + in_flight,
                            errors=errors)

        crawler = Crawler(s, state, store, scope, progress_cb=cb)
        try:
            crawler.login()
            crawler.run()
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted — saving state…[/]")
            crawler.stop()
        finally:
            state.save(s.state_file)
            crawler.close()

    console.print(f"[green]Done.[/] {state.stats()}  state -> {s.state_file}")
    console.print(f"Build a ZIM with:  tuc-archive build-zim -o {s.output_dir}")


@app.command("build-zim")
def build_zim(
    output: str = typer.Option("./output", "--output", "-o", help="Crawl output dir."),
    zim: str = typer.Option(None, "--zim", help="Output .zim path."),
    title: str = typer.Option("TUC Forum Archive", "--title"),
    description: str = typer.Option("Offline archive of a TYPO3 tx_tucforum forum.", "--description"),
    language: str = typer.Option("ell", "--language"),
    main_url: str = typer.Option(None, "--main-url", help="URL to use as ZIM landing page."),
    exclude: List[str] = typer.Option(
        None, "--exclude",
        help="Extra exclude regex(es) applied at build time. Prunes already-"
             "stored pages from the ZIM without re-crawling (e.g. 'index\\.php').",
    ),
    redact_emails: bool = typer.Option(
        False, "--redact-emails",
        help="Remove author e-mails entirely (mailto tokens + visible text -> "
             "'[email hidden]'). Use before sharing the ZIM publicly.",
    ),
    author_index: bool = typer.Option(
        False, "--author-index",
        help="Generate browse-by-author pages (A–Z index + one page per author "
             "listing their posts), searchable in Kiwix. PRIVACY: aggregates a "
             "named person's whole posting history — only for archives you are "
             "authorised to build that way. Off by default.",
    ),
    scrub_pii: bool = typer.Option(
        False, "--scrub-pii",
        help="Best-effort data minimisation: mask emails, phone numbers, "
             "AMKA and IBANs found in post text. Catches FORMATS, not meaning — "
             "names/addresses/attachment contents survive. Reduces exposure; "
             "does NOT make a ZIM safe to publish unreviewed.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Package the on-disk store into a Kiwix-compatible ZIM."""
    _setup_logging(verbose)
    from .zim import ZimBuilder
    s = Settings()
    s.output_dir = Path(output)
    store = ContentStore(s.output_dir / "store")
    zim_path = Path(zim) if zim else s.output_dir / "archive.zim"
    if author_index:
        console.print("[yellow]--author-index:[/] building a per-author directory "
                      "(names + post history). Ensure you're authorised to publish "
                      "this if you share the ZIM.")
    if scrub_pii:
        console.print("[yellow]--scrub-pii:[/] masking emails/phones/AMKA/IBAN in "
                      "post text (format-based, best-effort — names and attachment "
                      "contents are NOT removed).")
    builder = ZimBuilder(s, store, exclude=exclude)
    out = builder.build(zim_path, title=title, description=description,
                        language=language, main_url=main_url,
                        redact_emails=redact_emails, author_index=author_index,
                        scrub_pii=scrub_pii)
    console.print(f"[green]ZIM written:[/] {out}")
    console.print(f"Open with:  kiwix-serve --port 8080 {out}")


@app.command()
def serve(
    zim: str = typer.Argument(..., help="Path to a .zim file."),
    port: int = typer.Option(8080, "--port", "-p"),
):
    """Serve a ZIM locally via kiwix-serve in Docker (convenience wrapper)."""
    import subprocess
    zpath = Path(zim).resolve()
    cmd = [
        "docker", "run", "--rm", "-p", f"{port}:8080",
        "-v", f"{zpath.parent}:/data", "ghcr.io/kiwix/kiwix-tools",
        "kiwix-serve", "--port", "8080", f"/data/{zpath.name}",
    ]
    console.print(f"[cyan]$ {' '.join(cmd)}[/]")
    subprocess.run(cmd, check=False)


@app.command()
def coordinator(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(5555, "--port"),
    output: str = typer.Option("./output", "--output", "-o"),
    seeds: List[str] = typer.Option(None, "--seed", help="Initial seed URL(s)."),
):
    """Run the distributed coordinator REST API."""
    from .distributed.coordinator import serve_coordinator
    serve_coordinator(host, port, Path(output), seeds or [])


@app.command()
def worker(
    coordinator_url: str = typer.Option(..., "--coordinator-url"),
    username: str = typer.Option(None),
    password: str = typer.Option(None),
    workers: int = typer.Option(None, "--workers", "-w"),
    output: str = typer.Option("./output", "--output", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a distributed worker that pulls jobs from a coordinator."""
    _setup_logging(verbose)
    from .distributed.worker import run_worker
    s = _settings(username, password, workers, None, output, False)
    run_worker(coordinator_url, s)


if __name__ == "__main__":
    app()
