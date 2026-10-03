#!/usr/bin/env python3
"""Targeto Lead Pipeline — Apollo API sync (multi-token, free-plan aware).

Saroj provides one or more Apollo API keys (free plan = ~50-75 email credits
per account per month). This worker:
  1. Picks the worklist: agencies with owner+phone, no email yet, not DNC.
     Gold list first (no FB page = best cold-call targets), then by id.
  2. For each agency calls Apollo People Enrichment (organization_name +
     owner first/last). Apollo charges 1 credit ONLY when it finds an email.
  3. Stores the email (source='apollo'), updates owner_title from Apollo,
     writes an apollo_usage row (per-token accounting).
  4. Rotates to the next active token when one hits its monthly cap.
  5. Stops on: all tokens exhausted / --limit reached / repeated API errors.

Tokens live in the apollo_tokens table (add via dashboard → Apollo → Tokens)
or the TARGETO_APOLLO_TOKENS env var (comma-separated).

Usage:
  python -m pipeline.apollo_sync --limit 50
  python -m pipeline.apollo_sync --limit 10 --dry-run
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from . import get_conn

API_URL = "https://api.apollo.io/api/v1/people/match"
UA = {"User-Agent": "targeto-pipeline/1.0", "Content-Type": "application/json",
      "Accept": "application/json"}
DEFAULT_CAP = 60          # conservative monthly email-credit cap per token
CALL_DELAY = 2.0          # seconds between API calls (free plan: ~600/day)
ERROR_SLEEP = 20.0        # backoff on 4xx/5xx
MAX_CONSECUTIVE_ERRORS = 5
FLAG = "/tmp/apollo_sync_running"


def log(msg):
    print(msg, flush=True)


def http_post(url, payload, token, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={**UA, "X-Api-Key": token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_tokens(cur):
    """Active tokens with remaining credit, oldest-used first."""
    cur.execute("""
        SELECT id, token, monthly_cap, credits_used, used_reset_at
        FROM apollo_tokens WHERE active
        ORDER BY (used_reset_at IS NULL) DESC, used_reset_at NULLS FIRST, id""")
    out = []
    now = time.time()
    for tid, token, cap, used, reset in cur.fetchall():
        if cap is None:
            cap = DEFAULT_CAP
        # reset window: if used_reset_at older than ~28 days, treat as fresh month
        fresh = reset is None or (now - reset.timestamp()) > 28 * 86400
        remaining = cap - used if not fresh else cap
        if remaining > 0:
            out.append({"id": tid, "token": token, "cap": cap,
                        "used": used if not fresh else 0, "remaining": remaining})
    return out


def bump_usage(conn, cur, tid, found):
    """credits_used += found (email found = 1 credit). Reset when month rolls."""
    cur.execute("""
        UPDATE apollo_tokens
        SET credits_used = CASE WHEN used_reset_at IS NULL
                                  OR used_reset_at < now() - interval '28 days'
                            THEN %s ELSE credits_used + %s END,
            used_reset_at = CASE WHEN used_reset_at IS NULL
                                  OR used_reset_at < now() - interval '28 days'
                            THEN now() ELSE used_reset_at END
        WHERE id = %s""", (1 if found else 0, 1 if found else 0, tid))


def worklist(cur, limit, gold_first=True):
    order = ("(fb_checked_at IS NOT NULL AND fb_page_url IS NULL) DESC," if gold_first else "")
    cur.execute(f"""
        SELECT a.id, a.npi, a.org_name, a.city, a.state,
               a.owner_first_name, a.owner_last_name, a.owner_title
        FROM agencies a
        WHERE a.dnc IS NOT TRUE AND a.email IS NULL
          AND a.owner_last_name IS NOT NULL AND a.owner_first_name IS NOT NULL
          AND coalesce(a.phone, a.owner_phone) IS NOT NULL
        ORDER BY {order} a.id
        LIMIT %s""", (limit,))
    return cur.fetchall()


def enrich_one(token, row):
    """Returns (email, title, linkedin) or (None, None, None). Apollo charges
    1 credit only when an email comes back."""
    aid, npi, org, city, state, first, last, title = row
    payload = {
        "organization_name": org,
        "first_name": (first or "").strip(),
        "last_name": (last or "").strip(),
    }
    try:
        data = http_post(API_URL, payload, token)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}") from None
    person = (data or {}).get("person") or {}
    email = person.get("email") or None
    title2 = person.get("title") or None
    li = person.get("linkedin_url") or None
    return email, title2, li


def store(cur, aid, email, title, li):
    cur.execute("""
        UPDATE agencies SET email = %s, email_source = 'apollo',
            owner_title = COALESCE(NULLIF(%s, ''), owner_title),
            updated_at = now()
        WHERE id = %s AND email IS NULL""", (email, title, aid))
    return cur.rowcount


def store_usage(cur, tid, aid, email, title, li):
    cur.execute("""INSERT INTO apollo_usage (token_id, agency_id, email_found, title, linkedin_url)
                   VALUES (%s,%s,%s,%s,%s)""",
                (tid, aid, bool(email), title, li))


def main():
    ap = argparse.ArgumentParser(description="Apollo multi-token email sync")
    ap.add_argument("--limit", type=int, default=50, help="max agencies to try this run")
    ap.add_argument("--no-gold-first", action="store_true",
                    help="plain id order instead of no-FB gold list first")
    ap.add_argument("--dry-run", action="store_true", help="show worklist, call nothing")
    ap.add_argument("--delay", type=float, default=CALL_DELAY)
    args = ap.parse_args()

    if os.path.exists(FLAG):
        log("another apollo_sync appears to be running (flag file exists); exiting.")
        return 1
    open(FLAG, "w").write(str(time.time()))

    conn = get_conn()
    try:
        cur = conn.cursor()
        rows = worklist(cur, args.limit, gold_first=not args.no_gold_first)
        log(f"worklist: {len(rows)} agencies to try (gold-first={not args.no_gold_first})")
        if args.dry_run:
            for r in rows[:15]:
                log(f"  would try: {r[2][:40]} | {r[5]} {r[6]} | {r[3] or ''}, {r[4] or ''}")
            os.remove(FLAG)
            return 0

        tokens = fetch_tokens(cur)
        conn.commit()
        if not tokens:
            # env fallback
            env_tokens = [t.strip() for t in
                          os.environ.get("TARGETO_APOLLO_TOKENS", "").split(",") if t.strip()]
            tokens = [{"id": None, "token": t, "cap": DEFAULT_CAP, "used": 0,
                       "remaining": DEFAULT_CAP} for t in env_tokens]
        if not tokens:
            log("NO TOKENS: add one on the dashboard (Apollo → Tokens) or set TARGETO_APOLLO_TOKENS.")
            os.remove(FLAG)
            return 1
        log(f"tokens available: {len(tokens)} · total remaining credits ≈ "
            f"{sum(t['remaining'] for t in tokens)}")

        ti, errors, found_emails, misses = 0, 0, 0, 0
        for i, row in enumerate(rows, 1):
            if ti >= len(tokens):
                log("all tokens exhausted — stopping.")
                break
            t = tokens[ti]
            aid, npi, org = row[0], row[1], row[2]
            try:
                email, title, li = enrich_one(t["token"], row)
                errors = 0
            except RuntimeError as e:
                errors += 1
                log(f"\n  !! {org[:30]}: {e}")
                time.sleep(ERROR_SLEEP)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    log("too many consecutive API errors — stopping (token/account problem?)")
                    break
                continue
            except urllib.error.URLError as e:
                errors += 1
                log(f"\n  !! network: {e.reason}")
                time.sleep(ERROR_SLEEP)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    log("network down — stopping.")
                    break
                continue

            found = bool(email)
            if found:
                store(cur, aid, email.lower(), title, li)
                found_emails += 1
            else:
                misses += 1
            if t["id"] is not None:
                store_usage(cur, t["id"], aid, email, title, li)
                bump_usage(conn, cur, t["id"], found)
            conn.commit()
            t["remaining"] -= 1 if found else 0
            t["used"] += 1 if found else 0
            if t["remaining"] <= 0:
                log(f"\n  token #{t['id']} hit its cap ({t['cap']} credits this month) → rotating.")
                ti += 1
            log(f"\r  {i}/{len(rows)} emails={found_emails} misses={misses}", end="", flush=True)
            time.sleep(args.delay)

        log(f"\nDone. emails found: {found_emails} · not found: {misses}")
        if found_emails:
            cur.execute("INSERT INTO activity (user_id, agency_id, action, detail) "
                        "SELECT id, NULL, 'apollo_sync', %s FROM users WHERE is_admin LIMIT 1",
                        (f"sync found {found_emails} emails",))
            conn.commit()
    finally:
        conn.close()
        if os.path.exists(FLAG):
            os.remove(FLAG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
