# tuc-archive

Archive a **login-protected TYPO3 forum** (custom `tx_tucforum` extension, `felogin`
authentication) into a self-contained **ZIM** file you can open offline in
[Kiwix](https://kiwix.org). Built for the "Νέα / Ανακοινώσεις / Συζητήσεις"
forum and up to ~17 years of history: category selection, polite large-scale
crawling, resumable state, and an optional coordinator/worker distributed mode.

```
felogin login ──▶ crawl category ──▶ raw store on disk ──▶ ZIM (Kiwix)
                       ▲  resume / incremental  │
                       └────────── state.yml ───┘
```

---

## What is verified vs. assumed

Configuration was calibrated against **real `www.tuc.gr` page source** (the forum
root, including the embedded felogin form and the `tx_tucforum` category list).
Behaviour is unit-tested against saved fixtures of that source, and the full
pipeline — felogin login, category-only crawl, pagination, AJAX fragments and
ZIM build — has also been run end-to-end against the live site, archiving a
real category (tens of thousands of URLs).

| Area | Status |
|------|--------|
| URL canonicalisation, scope/exclude matching | ✅ unit-tested |
| State queue, atomic save/resume | ✅ unit-tested |
| **Extbase felogin** POST — scrape the form's own action URL, carry `__RequestToken` (JWT CSRF) + `__trustedProperties` + `__referrer[*]`, `permalogin`, login-form-presence detection | ✅ tested against the **real forum_root fixture** (`respx`) |
| `tx_tucforum` **category list** parsing (groups, `cat/{id}`, topic counts, `data-csrf`, `data-tucforumendpoint`) | ✅ tested against the real fixture |
| Login → crawl → store → link-rewrite → **ZIM read back with libzim** | ✅ end-to-end against a local HTTP server |
| Logged-in vs. logged-out detection (password-input predicate, not the extension name — the logout form also contains `tx_felogin_login`) | ✅ verified against the real logged-in topic page + regression-tested |
| **Topic-/post-view** parsing — `li.topicpostlistpost`, author/date/role split, `data-mailto-token` Caesar decode, attachments, `h1.forumtopictitle`, `ul.forumbreadcrumb`, `f3-widget-paginator` | ✅ verified against the real `/topic/56557/page` source |
| **AJAX fragment** (`data-tucforumendpoint`) capture | ⚠️ wired — fetched inline replaying `data-csrf`, but the CSRF **header name** (`Site.ajax_csrf_header`) is **assumed**; confirm against a real topic page's XHR |

**Every site-specific value lives in one file — [`tuc_archive/config.py`](tuc_archive/config.py) (`Site`)** — each field tagged `VERIFIED` or `ASSUMED`.

### Calibrated to the real forum

- Base: `https://www.tuc.gr`, forum root `/el/to-polytechneio/nea-anakoinoseis-syzitiseis`.
- **Login is Extbase felogin embedded in the forum page.** The form POSTs to its
  own parameterised `action` (with a `cHash`), so `auth.py` scrapes the form and
  posts there, copying all Extbase integrity tokens — not a fixed login URL.
- **`cHash` is preserved** through URL canonicalisation (TYPO3 returns 404 for
  parameterised URLs whose `cHash` is missing). Only session/tracking keys are
  stripped.
- Scope/exclude regexes are matched against the **URL-decoded** path+query, so
  `[format]=rss` and `[action]=reply` exclusions hit percent-encoded links.
  Per-category RSS feeds, the login action, mutating actions, and the search /
  "my messages" subtree are excluded by default.
- 9 real categories discovered from the catlist (`cat/3` ≈ 11k topics, etc.).

---

## Install

Requires Python 3.10+. Native deps: `libzim` (ships in the `zimscraperlib` wheel)
and `libmagic` (Linux: `apt install libmagic1`; Windows: `pip install python-magic-bin`;
macOS: `brew install libmagic`).

```bash
pip install -e .                 # core
pip install -e ".[distributed]"  # + coordinator/worker
pip install -e ".[dashboard]"    # + web dashboard
pip install -e ".[dev]"          # + pytest/respx for the test suite
```

Or just use Docker (handles all native deps) — see below.

## Configure

```bash
cp .env.example .env
# edit USERNAME_FORUM / PASSWORD and TUC_BASE_URL
```

`USERNAME` is reserved by the OS on some systems, so credentials are read from
`USERNAME_FORUM` first, then `USERNAME`.

---

## Usage (standalone)

```bash
# 1. Discover crawlable categories (sitemap + nav menu)
tuc-archive discover

# 2. Crawl ONE category and its topics only (recommended).
#    cat/4 = "Γενικά Μηνύματα". --category-only restricts scope to that
#    category's pages + its /topic/ threads, so it never spiders the rest
#    of the forum.
tuc-archive crawl \
  "https://www.tuc.gr/el/to-polytechneio/nea-anakoinoseis-syzitiseis/cat/4/page" \
  -o ./output-genika --category-only --workers 3 --delay 1 -v

# 3. Package the crawl into a ZIM
tuc-archive build-zim -o ./output-genika --zim ./output-genika/genika.zim \
  --title "Γενικά Μηνύματα" --language ell

# 4. Open it
kiwix-serve --port 8080 ./output-genika/genika.zim
# or: tuc-archive serve ./output-genika/genika.zim   (runs kiwix in Docker)
```

On start the crawl prints `Category-only scope: ['/cat/4/', '/topic/']` — that
confirms the scope. Pages climb as `/topic/<id>/page` threads are fetched.

### Stop and resume (incl. smoke tests)

State is snapshotted atomically every `TUC_SNAPSHOT_EVERY` pages **and on
Ctrl-C**. Stopping then resuming continues exactly where it left off and runs to
completion — already-completed URLs are remembered (deduped), in-flight URLs are
recovered, and the crawl keeps discovering + following new in-scope links until
the queue drains. So you can stop any time for a quick smoke test, then resume.

```bash
# stop with Ctrl-C, inspect ./output-genika, then continue:
tuc-archive resume ./output-genika/state.yml --category-only --workers 3
```

> **Important:** scope is **not** stored in the state file — it is rebuilt from
> the command line. If the original crawl used `--category-only`, pass
> `--category-only` to `resume` too (it re-derives `/cat/<id>/` from the saved
> seeds). Omit it and the resumed crawl widens to the whole forum.

Re-running a crawl is also **incremental**: already-stored pages send
`If-None-Match` / `If-Modified-Since`; a `304` skips the re-download.

### Category selection & scope

- Seeds are explicit category URLs (positional args to `crawl`).
- `--category-only` (on `crawl` **and** `resume`) restricts scope to the seed
  category (`/cat/<id>/`) plus its topics (`/topic/`). Best way to archive one
  category without spidering the whole forum.
- `--include REGEX` adds in-scope patterns; default scope is `Site.scope_pattern`.
- `--exclude REGEX` (repeatable) drops threads/actions; destructive actions
  (`reply`, `edit`, `delete`, logout), `print=` views, per-category RSS, and
  TYPO3 `index.php?id=` CMS fallback pages (footer: accessibility / cookies /
  privacy) are excluded by default.

### Filter an existing crawl without re-crawling

Filtering also happens at **ZIM-build time**, reading the on-disk store — so you
can prune junk from a huge (17-year) crawl you already paid for, with no
re-download. The default excludes (including `index.php`) are applied
automatically; add more ad-hoc:

```bash
# drop already-stored pages matching extra patterns, no network access
tuc-archive build-zim -o ./output-genika --exclude 'index\.php' \
  --exclude 'some-other-pattern'
```

The build logs `Excluding N stored page(s) from ZIM`. The store is never
modified, so you can rebuild repeatedly with different filters.

### Author e-mails (privacy)

E-mails are kept **obfuscated** by default (the site's `data-mailto-token`
scramble is preserved as-is — no plaintext address is produced). Two opt-ins:

```bash
tuc-archive crawl ... --deobfuscate-emails     # decode to plaintext in the store
tuc-archive build-zim ... --redact-emails      # remove e-mails entirely from the
                                               # ZIM ("[email hidden]") before
                                               # sharing it publicly
```

`--redact-emails` strips structured `mailto` tokens/links; free-text addresses
typed into a post body are not caught (documented limit).

---

## Distributed mode (coordinator + workers)

A single **coordinator** owns the authoritative queue and a shared output
volume; **workers** authenticate with a shared secret (`TUC_SECRET`), lease URL
batches, crawl into the shared store, and report discovered links back. Dead
workers' leases time out and are re-queued.

```bash
# machine A — coordinator (REST API on :5555)
tuc-archive coordinator --port 5555 --output /mnt/shared \
  --seed "https://forum.example.gr/el/.../genika/"

# machines B, C, … — workers
tuc-archive worker --coordinator-url http://machineA:5555 \
  --output /mnt/shared --workers 4
```

`--output` must point at shared storage (NFS / S3-mount / same Docker volume).

### Docker Compose (coordinator + 2 workers + dashboard)

```bash
cp .env.example .env                       # set creds, TUC_SECRET, SEED_URL
SEED_URL="https://forum.example.gr/el/.../genika/" docker compose up --build
docker compose up --scale worker=4         # add workers any time

docker compose run --rm build-zim          # build /data/archive.zim
docker compose --profile serve up kiwix    # serve it on :8080
```

Read-only progress dashboard: <http://localhost:8000>.

---

## How it works (modules)

| File | Responsibility |
|------|----------------|
| `config.py` | **All site assumptions** + runtime settings (env/CLI) |
| `auth.py` | felogin POST, cookie session, re-login on 401/403/logout |
| `crawler.py` | httpx pool, retry+backoff, robots, scope, incremental, snapshots |
| `parser.py` | posts, metadata, attachments, `data-tucforumendpoint` AJAX, pagination |
| `store.py` | atomic on-disk raw store (pages + assets + manifest) |
| `state.py` | queue/visited/errors, **atomic** YAML snapshots, resume |
| `rewrite.py` | URL → ZIM path map + internal link rewriting |
| `zim.py` | streams the store into a Kiwix ZIM (`zimscraperlib`) |
| `discovery.py` | category listing from sitemap / nav menu |
| `distributed/` | FastAPI coordinator + worker |
| `dashboard.py` | optional read-only progress UI |
| `cli.py` | `tuc-archive` Typer CLI with rich progress |

### Design notes / honest limits

- **AJAX fragments** (`data-tucforumendpoint`) are detected and queued as
  separate pages so the post lists are captured. The exact endpoint shape is an
  assumption in `config.py`.
- **Email obfuscation**: `data-mailto-token` is preserved as-is (not de-obfuscated)
  — documented behaviour, avoids guessing the site's JS decode scheme.
- **JavaScript**: content is server-rendered; a plain HTTP client is used (no
  headless browser). Lightbox/Matomo JS is non-essential and left out.
- **ZIM is built offline** from the store — decoupled from crawling, so you can
  rebuild any number of times from one crawl, and a failed ZIM never costs you
  the network work.

---

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Covers URL normalisation/scope, state queue + atomic round-trip, felogin login
(fixture), HTML parsing + link rewriting, and an end-to-end ZIM build verified
by reading it back with `libzim`.

## License

**GNU AGPL-3.0-only** — see [`LICENSE`](LICENSE). Copyleft with a network clause:
if you run a modified version as a network service, you must offer its source to
users. Forks and redistributions must stay under AGPL-3.0.

## Legal / ethical use

This tool logs into and scrapes a specific institution's forum. Only run it
against a site you are authorised to archive, respect its terms of service and
`robots.txt`, and keep the polite defaults (rate limit + concurrency). Archived
pages contain other people's posts and (obfuscated) e-mail addresses, and may
embed third-party media linked from posts — review copyright and personal-data
(GDPR) obligations before redistributing a ZIM, and use `build-zim
--redact-emails` for public releases.

### Authorized use only

Like any web crawler or penetration-testing tool, this is **dual-use**: built
for lawful archival, research, and personal-backup purposes, but capable of
misuse. It is provided **for authorized use only**.

- Use it solely against systems and accounts you **own or are explicitly
  authorized to access**. It authenticates with *your* credentials — it neither
  cracks logins nor exploits vulnerabilities.
- You are **solely responsible** for complying with the target site's Terms of
  Service, `robots.txt`, applicable law, and data-protection rules (e.g. GDPR).
- Do not remove the politeness defaults to hammer a server, and do not use the
  distributed mode to evade rate limits or source-IP controls.
- The authors provide this software **as-is, without warranty**, and accept **no
  liability** for misuse or for any damage arising from its use (see `LICENSE`).
