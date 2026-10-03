#!/usr/bin/env python3
"""Targeto Lead Pipeline — personal-email pattern guesser (free, no paid APIs).

Built from GitHub research (email-permutator / pratik-dani/email_permutations
pattern sets, reacherhq-style verification philosophy), adapted to what THIS
machine can do: outbound port 25 is ISP-blocked, so live SMTP verification is
impossible — instead we MX-gate every candidate via DNS-over-HTTPS (port 443)
and store guesses in a SEPARATE table. Nothing lands in agencies.email until
a human confirms it (✅ button on the agency page → email_source='manual').

Pipeline position: the website harvester (personal-only) runs first and keeps
confirmed emails; this tool covers the rest with a ranked guess sheet:

    deserie.bradley@domain.com   #1  full first.last
    deseriebradley@domain.com    #2  compact
    deserie_bradley@domain.com   #3  underscore
    deserie@domain.com           #4  first only
    dbradley@domain.com          #5  initial+last
    d.bradley@domain.com         #6  initial.last
    bradley@domain.com           #7  last only
    bradley.deserie@domain.com   #8  last.first
    bradleydeserie@domain.com    #9  compact last-first

Usage:
  python -m pipeline.email_guess --limit 3000
  python -m pipeline.email_guess --state FL --limit 500
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

from . import get_conn
from .email_harvest import _is_generic

DOH_ENDPOINTS = (
    "https://dns.google/resolve?name={host}&type=MX",
    "https://cloudflare-dns.com/dns-query?name={host}&type=MX",
)

# (pattern-name, format-fn) — rank = position in this tuple (1-based)
PATTERNS = (
    ("first.last",   lambda f, l: f"{f}.{l}"),
    ("firstlast",    lambda f, l: f"{f}{l}"),
    ("first_last",   lambda f, l: f"{f}_{l}"),
    ("first",        lambda f, l: f),
    ("initlast",     lambda f, l: f"{f[0]}{l}"),
    ("init.last",    lambda f, l: f"{f[0]}.{l}"),
    ("last",         lambda f, l: l),
    ("last.first",   lambda f, l: f"{l}.{f}"),
    ("lastfirst",    lambda f, l: f"{l}{f}"),
)


def name_parts(first: str, last: str):
    """(first, last) compact lowercase tokens. Multi-token names compress:
    'DE LA GUARDIA' → delaguardia. Needs a ≥2-char first AND last."""
    f = re.sub(r"[^a-z]", "", (first or "").lower())
    l = re.sub(r"[^a-z]", "", (last or "").lower())
    return f, l


def candidates(first: str, last: str, domain: str):
    """Ranked (rank, pattern, email) — deduped, junk-free, lowercase."""
    f, l = name_parts(first, last)
    if len(f) < 2 or len(l) < 2 or not domain:
        return []
    out, seen = [], set()
    for i, (pname, fn) in enumerate(PATTERNS, 1):
        email = f"{fn(f, l)}@{domain}".lower()
        if email in seen or _is_generic(email):
            continue
        # first-only / last-only patterns are weak when the name is also a
        # common word — keep them, but the rank ordering already demotes them
        seen.add(email)
        out.append((i, pname, email))
    return out


def mx_has_mail(domain: str, cache: dict, timeout: float = 5.0) -> bool | None:
    """True if the domain has MX records (can receive email). DoH over 443 —
    no port 25 involved. None = lookup failed (treat as unknown, skip)."""
    if domain in cache:
        return cache[domain]
    for tpl in DOH_ENDPOINTS:
        try:
            req = urllib.request.Request(
                tpl.format(host=domain),
                headers={"Accept": "application/dns-json",
                         "User-Agent": "targeto-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            answers = [a for a in data.get("Answer", []) if a.get("type") == 15]
            ok = bool(answers) and data.get("Status", 0) == 0
            cache[domain] = ok
            return ok
        except Exception:
            continue
    cache[domain] = None
    return None


def guess_batch(limit: int, state: str | None = None, delay: float = 0.15) -> int:
    where, params = ["a.website IS NOT NULL", "a.email IS NULL",
                     "a.owner_last_name IS NOT NULL", "a.dnc IS NOT TRUE"], []
    if state:
        where.append("a.state = %s"); params.append(state.upper())
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""SELECT a.id, a.website, a.owner_first_name, a.owner_last_name
                        FROM agencies a WHERE {' AND '.join(where)}
                        ORDER BY a.id LIMIT %s""", params + [limit])
        rows = cur.fetchall()
        cache: dict = {}
        made = 0
        for n, (aid, site, first, last) in enumerate(rows, 1):
            domain = urllib.parse.urlparse(
                site if site.startswith("http") else "https://" + site).netloc
            domain = re.sub(r"^www\.", "", domain).lower()
            if ":" in domain:
                domain = domain.split(":")[0]
            if not domain or "wixsite.com" in domain or "godaddysites.com" in domain \
               or "business.site" in domain or "weebly.com" in domain:
                continue  # builder subdomains: mailbox lives elsewhere
            mx = mx_has_mail(domain, cache)
            if mx is not True:
                time.sleep(delay)
                continue
            cands = candidates(first, last, domain)
            cur.execute("DELETE FROM email_guesses WHERE agency_id = %s AND status = 'candidate'", (aid,))
            for rank, pname, email in cands:
                cur.execute("""INSERT INTO email_guesses (agency_id, email, pattern, rank, mx_ok)
                               VALUES (%s, %s, %s, %s, TRUE)
                               ON CONFLICT (agency_id, email) DO NOTHING""", (aid, email, pname, rank))
                made += 1
            conn.commit()
            if n % 25 == 0:
                print(f"\r  {n}/{len(rows)} agencies · {made} candidates", end="", flush=True)
            time.sleep(delay)
        print(f"\nDone. {made} guess candidates for {len(rows)} agencies.")
    finally:
        conn.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="Ranked personal-email guess sheet (MX-gated)")
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--state")
    ap.add_argument("--delay", type=float, default=0.15)
    args = ap.parse_args()
    return guess_batch(args.limit, args.state, args.delay)


if __name__ == "__main__":
    sys.exit(main())
