#!/usr/bin/env python3
"""Targeto Lead Pipeline — email harvester (free, no paid APIs).

PERSONAL-EMAILS-ONLY POLICY (Saroj, 2026-09-29): an email is stored ONLY if
the address looks like the OWNER's personal address — the local part must
contain the owner's first and/or last name (deserie@, dbradley@,
deseriebradley@...). Generic role inboxes are NEVER stored:

    info@ contact@ hello@ sales@ admin@ office@ support@ care@ ...  → rejected

A company inbox does not convert; a wrong personal-looking guess wastes one
email. So: no name match → no email stored, and the agency stays eligible for
Apollo enrichment (which queries by owner first+last and returns the PERSON's
address).

Method (same static-DB policy as fb_find — everything pre-computed, zero
runtime dependencies at click time):
  1. Fetch the agency's own website (homepage first, then contact/about pages).
  2. Extract mailto: links + de-obfuscate "name [at] domain [dot] com" forms.
  3. Skip junk (file extensions, noreply/abuse/sentry/wixpress/webmaster…).
  4. KEEP only owner-name matches, then rank them:
       first + last both present (deserie.bradeley@)  → best
       last name (dbradley@, smith@)                  → very good
       first name (deserie@)                          → good
  5. Store agencies.email + email_source='website' (NEVER overwrite manual).

Usage:
  python -m pipeline.email_harvest --limit 100 --state FL
"""
import argparse
import html as htmllib
import re
import sys
import time
import urllib.parse

from . import get_conn
from .resolve import http_get

# Pages most likely to carry contact emails (tried after the homepage)
CONTACT_PATHS = (
    "/contact", "/contact-us", "/contacts", "/about", "/about-us",
    "/team", "/our-team", "/staff", "/leadership", "/contact.html",
    "/about.html",
)

EMAIL_RE = re.compile(
    r"[a-z0-9._%+\-']+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
OBFUSCATED_RE = re.compile(
    r"([a-z0-9._%+\-']+)\s*(?:\[at\]|\(at\)|%40|&#64;|\s+at\s+)\s*"
    r"([a-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\.|&#46;|\s+dot\s+)\s*([a-z]{2,})", re.I)

JUNK_LOCAL = (
    "noreply", "no-reply", "donotreply", "abuse", "postmaster", "spam",
    "privacy", "legal", "compliance", "webmaster", "hostmaster", "dns",
    "admin@example", "user@example", "example.com", "test@",
    "sentry", "wixpress", "wix.com", "godaddy", "squarespace", "cloudflare",
    "wordpress", "sitelock", "sucuri", "hostgator", "bluehost", "ionos",
    "name@example", "email@example", "yourdomain", "domain.com", "yourname",
    "gateway", "ectest", "sdk", "api@", "support@", "billing@", "help@",
)
JUNK_DOMAIN = ("example.", "sentry", "wixpress", ".png", ".jpg", ".gif",
               ".webp", ".svg", ".jpeg", ".webp")

# Generic role inboxes — never personal, never stored (info@/sales@ etc).
GENERIC_LOCAL = (
    "info", "contact", "contactus", "hello", "sales", "admin",
    "administrator", "office", "frontdesk", "front-desk", "reception",
    "referral", "referrals", "intake", "admissions", "care", "caring",
    "support", "customerservice", "customersupport", "help", "billing",
    "accounts", "accounting", "team", "mail", "email", "enquiries",
    "enquiry", "inquiries", "inquiry", "questions", "feedback", "marketing",
    "hr", "jobs", "careers", "press", "media", "service", "services",
    "scheduling", "schedule", "appointment", "appointments", "orders",
    "general", "main", "greeting", "welcome", "coordinator", "casemanager",
    "case-manager", "case_manager", "staff", "management", "operations",
    "web", "website", "post", "box", "contactme",
)


def _text_from_html(html: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return htmllib.unescape(txt)


def _collect_emails(html: str) -> set[str]:
    found: set[str] = set()
    for m in EMAIL_RE.finditer(html):
        found.add(m.group(0).strip(".").lower())
    txt = _text_from_html(html)
    for m in OBFUSCATED_RE.finditer(txt):
        found.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower())
    return found


def _is_junk(email: str) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain or len(email) > 80:
        return True
    if any(d in domain for d in JUNK_DOMAIN):
        return True
    if any(j in local for j in JUNK_LOCAL):
        return True
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # hash-like image trackers
        return True
    return False


def _is_generic(email: str) -> bool:
    """Role inbox — the exact thing Saroj rejected. info@/sales@/hello@/…"""
    local = email.split("@")[0].replace("_", "-")
    if local in GENERIC_LOCAL:
        return True
    return any(local.startswith(g + ".") or local.startswith(g + "-")
               for g in GENERIC_LOCAL)


def _name_keys(name: str) -> set[str]:
    """Matchable keys for a person name: 'DE LA GUARDIA' → {delaguardia,
    guardia}; 'SANTOS-FLORES' → {santosflores, santos, flores}. Keys are
    ≥4 chars so short fragments can't substring-match everywhere."""
    n = (name or "").lower()
    parts = [t for t in re.split(r"[^a-z]+", n) if t]   # spaces AND hyphens split
    keys = {t for t in parts if len(t) >= 4}
    compact = re.sub(r"[^a-z]", "", n)
    if len(compact) >= 4:
        keys.add(compact)
    return keys


def name_match_strength(local: str, first: str = "", last: str = "") -> int:
    """How strongly does the address local-part look like this person?
    3 = first+last both present · 2 = last name · 1 = first name · 0 = none."""
    first, last = (first or "").strip().lower(), (last or "").strip().lower()
    hit_f = any(k in local for k in _name_keys(first))
    hit_l = any(k in local for k in _name_keys(last))
    if hit_l and hit_f:
        return 3
    if hit_l:
        return 2
    if hit_f:
        return 1
    return 0


def is_personal(email: str, first: str, last: str) -> bool:
    """True only when the address carries the owner's name (no name → False,
    regardless of how clean it looks)."""
    local = email.split("@")[0].lower()
    return name_match_strength(local, (first or "").lower(), (last or "").lower()) > 0


def _score(strength: int, homepage: bool, local: str) -> int:
    """Rank personal candidates. strength: 1=first · 2=last · 3=first+last."""
    s = 100 + strength * 30          # 130 first · 160 last · 190 full name
    if homepage:
        s += 5
    if re.search(r"\d", local):
        s -= 15                       # deserie2@ — less likely
    if local.count(".") > 2 or len(local) > 30:
        s -= 10
    return s


def harvest_one(cur, row, delay: float = 0.4) -> str:
    """row = (id, website, owner_first, owner_last). Returns found|none|skipped."""
    aid, site, first, last = row
    if not site:
        return "none"
    base = site if site.startswith("http") else "https://" + site
    host = urllib.parse.urlparse(base).netloc.replace("www.", "")
    first_n = (first or "").strip().lower()
    last_n = (last or "").strip().lower()

    pages = [base] + [base.rstrip("/") + p for p in CONTACT_PATHS]
    pool: dict[str, int] = {}
    for i, url in enumerate(pages):
        html = http_get(url, timeout=6)
        time.sleep(delay)
        if not html:
            continue
        for em in _collect_emails(html):
            if _is_junk(em) or _is_generic(em) or host not in em.split("@")[1]:
                continue  # junk / role inbox / not their own domain
            if not is_personal(em, first_n, last_n):
                continue  # PERSONAL ONLY — no owner name, no store
            pool[em] = max(pool.get(em, -99), (0 if i == 0 else 1))
    if not pool:
        return "none"
    best = max(
        pool.items(),
        key=lambda kv: (
            _score(name_match_strength(kv[0].split("@")[0], first_n, last_n),
                   kv[1] == 0, kv[0].split("@")[0]),
            -kv[1]),
    )
    email = best[0]
    cur.execute(
        """UPDATE agencies SET email = %s, email_source = 'website', updated_at = now()
           WHERE id = %s AND email IS NULL""",
        (email, aid),
    )
    return "found" if cur.rowcount else "skipped"


def main():
    ap = argparse.ArgumentParser(description="Harvest PERSONAL emails from agency websites")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--state")
    ap.add_argument("--city")
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    where, params = ["website IS NOT NULL", "email IS NULL"], []
    if args.state:
        where.append("state = %s"); params.append(args.state.upper())
    if args.city:
        where.append("city_norm = %s"); params.append(args.city.strip().lower())

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT id, website, owner_first_name, owner_last_name
                FROM agencies WHERE {' AND '.join(where)} ORDER BY id LIMIT %s""",
            params + [args.limit],
        )
        rows = cur.fetchall()
        found = 0
        for i, row in enumerate(rows, 1):
            try:
                if harvest_one(cur, row, args.delay) == "found":
                    found += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"\n  !! agency {row[0]}: {type(e).__name__}: {e}", end="")
            print(f"\r  {i}/{len(rows)} personal-found={found}", end="", flush=True)
        total = conn.cursor()
        total.execute("SELECT count(*) FROM agencies WHERE email IS NOT NULL")
        print(f"\nDone. +{found} personal emails this run · total agencies with email: {total.fetchone()[0]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
