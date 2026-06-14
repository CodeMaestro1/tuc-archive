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

"""Central configuration and *all* site-specific assumptions.

IMPORTANT
=========
Every value in the ``Site`` section below is an assumption derived from the
project spec, NOT something verified against the live TYPO3 site (no
credentials / no network access were available at build time). When the
crawler misbehaves against the real forum, this is the *only* file you should
need to edit. Each field documents what it controls and how to discover the
real value (open the login page / a topic page in a browser and inspect the
HTML form fields and link patterns).

Runtime configuration (workers, delays, paths, ...) is layered on top via
environment variables (``.env``) and CLI flags; see :class:`Settings`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # populate os.environ from a local .env if present


# --------------------------------------------------------------------------- #
# Site assumptions (TYPO3 felogin + tx_tucforum). EDIT HERE to match reality.  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Site:
    # Base origin of the forum. Override with TUC_BASE_URL.
    # VERIFIED from the supplied page source (www.tuc.gr).
    base_url: str = os.getenv("TUC_BASE_URL", "https://www.tuc.gr")

    # Forum root (the felogin form + tx_tucforum category list live here).
    # VERIFIED. This doubles as the login page — felogin is embedded in it.
    forum_root_path: str = os.getenv(
        "TUC_FORUM_ROOT", "/el/to-polytechneio/nea-anakoinoseis-syzitiseis"
    )

    # --- felogin (Extbase) --------------------------------------------------
    # The supplied page uses **Extbase felogin** (tx_felogin_login). The form
    # POSTs to its OWN action attribute (a parameterised URL with a cHash), NOT
    # back to the plain page — so auth.py scrapes the <form action> rather than
    # using a fixed login_url. Leave login_url empty to use forum_root.
    login_url: str = os.getenv("TUC_LOGIN_URL", "")

    # felogin field names. VERIFIED against the source.
    field_username: str = "user"        # <input name="user">
    field_password: str = "pass"        # <input name="pass">
    field_logintype: str = "logintype"  # hidden, value "login"
    logintype_value: str = "login"
    field_referer: str = "referer"      # hidden, the forum root URL
    field_submit: str = "submit"        # <input type=submit name="submit" value="Σύνδεση">
    submit_value: str = "Σύνδεση"
    field_permalogin: str = "permalogin"  # checkbox; send "1" to stay logged in

    # Extbase carries a JWT CSRF token in __RequestToken plus __trustedProperties
    # and __referrer[*]. auth.py copies EVERY hidden input from the login form,
    # so these flow through automatically; listing the CSRF names here is just
    # documentation / belt-and-braces.  ("__csrf_token" kept for other TYPO3
    # felogin variants.)
    csrf_field_names: tuple[str, ...] = ("__RequestToken", "__csrf_token")

    # --- session / login detection -----------------------------------------
    # Logged-out is detected by the PASSWORD input (input[name="pass"]) being
    # present — see auth.Authenticator._login_form_present. This is deliberate:
    # Extbase felogin also renders a *logout* form when authenticated (so the
    # extension name "tx_felogin_login" appears on logged-in pages too), but
    # only the LOGIN form carries a password field.
    logged_in_marker: str = "logintype=logout"  # informational only
    reauth_status_codes: tuple[int, ...] = (401, 403)

    # --- tx_tucforum structure (VERIFIED) ----------------------------------
    # <div class="tx-tucforum" data-csrf="..." data-tucforumendpoint="...">
    csrf_attr: str = "data-csrf"
    ajax_endpoint_attr: str = "data-tucforumendpoint"
    # Header used to replay the data-csrf token on AJAX-fragment requests.
    # ASSUMED name (TYPO3/tx_tucforum convention); confirm against a real topic
    # page's XHR. Sending an unrecognised header is harmless; the wrong/missing
    # one may yield 403 on protected fragments.
    ajax_csrf_header: str = "X-Csrf-Token"

    # Category list (forum root). VERIFIED selectors.
    sel_catgroup: str = "ul.tucforumcatgrouplist > li"
    sel_catgroup_title: str = "h2"
    sel_cat: str = "ul.tucforumcatlist > li"
    sel_cat_link: str = "h3 a"
    sel_cat_desc: str = ".catdescription"
    sel_cat_count: str = ".topiccount"
    sel_cat_idnode: str = ".tucforumcatlisttopicsno"  # carries data-catid

    # Topic / post view selectors. VERIFIED against a real /topic/{id}/page.
    sel_post: str = "li.topicpostlistpost"          # carries data-postid
    sel_post_authored: str = ".topicpostlistauthored"   # "Συντάχθηκε <date> από <author>"
    sel_post_info: str = ".topicpostlistauthorinfo"     # email / updated / role block
    sel_post_email: str = "[data-mailto-token]"
    sel_post_message: str = ".topicpostlistmessage"
    sel_post_attachments: str = ".topicpostlistattachments a[href], .topicpostlistattachments img[src]"
    sel_topic_title: str = "h1.forumtopictitle"
    sel_forum_breadcrumb: str = "ul.forumbreadcrumb li"
    sel_attachment: str = (
        ".topicpostlistattachments a[href], a[href*='/fileadmin/'], "
        "a[href*='/uploads/'], a.attachment"
    )
    sel_breadcrumb: str = "ul.breadcrumb li a"

    # Nodes removed at ZIM-build time (privacy / dead UI). The felogin frame
    # carries your logged-in USERNAME and a stale __RequestToken; the login /
    # logout forms are useless offline. Stripping them keeps creds out of the ZIM.
    strip_selectors: tuple[str, ...] = (
        ".frame-type-felogin_login",
        "form[action*='logintype']",
        "form[action*='tx_felogin_login']",
    )
    # Pagination links live in the f3 widget paginator; they are ordinary <a>
    # hrefs and get queued by generic link extraction, so a dedicated "next"
    # selector is only used for metadata.
    sel_pagination_next: str = ".f3-widget-paginator li:last-child a, a[rel='next']"

    # In-text labels used to split author/date/role out of the post header
    # (Greek, VERIFIED). mailto obfuscation is a Caesar shift, see mailto_shift.
    label_author_sep: str = "από"
    label_updated: str = "Ενημερώθηκε"
    label_role: str = "Ιδιότητα"
    mailto_shift: int = -1   # data-mailto-token decode (A->@, nbjmup->mailto)

    # --- scope / exclusion (regex, matched against URL-DECODED path+query) ---
    # In-scope: anything under the forum root. VERIFIED prefix.
    scope_pattern: str = r"/el/to-polytechneio/nea-anakoinoseis-syzitiseis"
    exclude_patterns: tuple[str, ...] = (
        r"logintype=logout",
        r"tx_felogin_login",                                  # login action URLs
        r"tx_tucforum\w*\[format\]=rss",                      # per-category RSS feeds
        r"tx_tucforum\w*\[action\]=(reply|edit|delete|new|quote|create|update)",
        # search / personal pages (the slashed "nea-/-anakoinoseis-/-syzitiseis" subtree)
        r"/(anazitisi|ta-minymata-moy|eggrafi)",
        r"[?&]print=",
        # CMS fallback pages (footer: accessibility / cookies / privacy). These
        # are relative `index.php?id=NNNN` links that urljoin onto the current
        # /topic/<id>/ prefix, so they match the /topic/ scope and get crawled
        # once per topic prefix — same boilerplate, many URLs. Forum content uses
        # friendly paths (/cat/{id}/page, /topic/{id}), never index.php.
        r"index\.php",
        r"\.(zip|exe|dmg)$",
    )

    # Query keys stripped during canonicalisation (session / tracking ONLY).
    # NOTE: cHash is deliberately NOT stripped — TYPO3 validates it and returns
    # 404 for parameterised URLs without a matching cHash, so it must survive to
    # the fetch. Friendly URLs (cat/{id}/page, /topic/{id}) carry no cHash.
    strip_query_keys: tuple[str, ...] = (
        "FE_SESSION",
        "fe_typo_user",
        "PHPSESSID",
        "utm_source",
        "utm_medium",
        "utm_campaign",
    )

    def forum_root_url(self) -> str:
        return self.base_url.rstrip("/") + self.forum_root_path

    def resolved_login_url(self) -> str:
        return self.login_url or self.forum_root_url()


# --------------------------------------------------------------------------- #
# Runtime settings (env + CLI override these).                                 #
# --------------------------------------------------------------------------- #
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # credentials (from .env)
    username: str = field(default_factory=lambda: os.getenv("USERNAME_FORUM", os.getenv("USERNAME", "")))
    password: str = field(default_factory=lambda: os.getenv("PASSWORD", ""))

    # crawl behaviour
    workers: int = field(default_factory=lambda: _env_int("TUC_WORKERS", 4))
    delay: float = field(default_factory=lambda: _env_float("TUC_DELAY", 0.5))
    random_wait: bool = field(default_factory=lambda: os.getenv("TUC_RANDOM_WAIT", "0") == "1")
    max_retries: int = field(default_factory=lambda: _env_int("TUC_MAX_RETRIES", 5))
    max_pages: int = field(default_factory=lambda: _env_int("TUC_MAX_PAGES", 0))  # 0 = unlimited
    timeout: float = field(default_factory=lambda: _env_float("TUC_TIMEOUT", 30.0))
    respect_robots: bool = field(default_factory=lambda: os.getenv("TUC_RESPECT_ROBOTS", "1") == "1")
    embed_assets: bool = field(default_factory=lambda: os.getenv("TUC_EMBED_ASSETS", "1") == "1")
    # Default OFF: keep author e-mails obfuscated (no plaintext scraping list).
    deobfuscate_emails: bool = field(default_factory=lambda: os.getenv("TUC_DEOBFUSCATE_EMAILS", "0") == "1")
    user_agent: str = field(
        default_factory=lambda: os.getenv("TUC_USER_AGENT", "tuc-archive/0.1 (+offline archival)")
    )

    # paths
    output_dir: Path = field(default_factory=lambda: Path(os.getenv("TUC_OUTPUT", "./output")))
    state_file: Path = field(default_factory=lambda: Path(os.getenv("TUC_STATE", "./output/state.yml")))
    snapshot_every: int = field(default_factory=lambda: _env_int("TUC_SNAPSHOT_EVERY", 25))

    # distributed
    coordinator_secret: str = field(default_factory=lambda: os.getenv("TUC_SECRET", "change-me"))

    site: Site = field(default_factory=Site)

    def as_public_dict(self) -> dict:
        """Settings minus secrets, for state files / logs."""
        out = {}
        for f in fields(self):
            if f.name in {"password", "coordinator_secret"}:
                continue
            val = getattr(self, f.name)
            out[f.name] = str(val) if isinstance(val, Path) else val
        out.pop("site", None)
        return out
