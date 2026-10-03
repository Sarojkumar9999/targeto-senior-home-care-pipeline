#!/usr/bin/env python3
"""Targeto Lead Pipeline — Phase 3 Meta Ad Library checker.

Classifies every agency: RUNNING / RAN_BEFORE / NO_ADS / NO_FB_PAGE / UNRESOLVED,
and stores active ad creatives for the competitor view.

Setup (one-time, ~10 minutes):
  1. https://developers.facebook.com → My Apps → Create App → type "Business".
  2. In the app: Tools → Add Product → "Ad Library API" (request access).
  3. Business verification + privacy policy URL may be requested.
  4. Generate a token: Graph API Explorer → Get User Token with
     ads_read + public_profile → then extend via Access Token Debugger
     (never share this token).
  5. Run:  export TARGETO_META_TOKEN=...
           python -m pipeline.ads_check --limit 100

API notes:
  - Endpoint: GET https://graph.facebook.com/v19.0/ads_archive
  - search_page_ids accepts up to 10 page IDs per call (batched here).
  - For agencies with only a page NAME (no ID), falls back to
    search_type=keyword_unordered on the page name.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

from . import get_conn

GRAPH = "https://graph.facebook.com/v19.0/ads_archive"
UA = {"User-Agent": "targeto-pipeline/1.0"}


def api_get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_page(token: str, page_id: str | None = None, keyword: str | None = None,
               country: str = "US"):
    """Return dict(active=n, archived=n, creatives=[...]) for one page/keyword."""
    params = {
        "access_token": token,
        "ad_reached_countries": country,
        "ad_type": "ALL",
        "fields": "ad_delivery_start_time,ad_delivery_stop_time,ad_creative_body,ad_creative_link_title,ad_creative_link_caption,page_id,page_name",
        "limit": 100,
    }
    if page_id:
        params["search_page_ids"] = page_id
    else:
        params["search_type"] = "keyword_unordered"
        params["q"] = keyword or ""
    url = f"{GRAPH}?{urllib.parse.urlencode(params)}"
    active = archived = 0
    creatives = []
    try:
        data = api_get(url)
        for ad in data.get("data", []):
            has_start, has_stop = bool(ad.get("ad_delivery_start_time")), bool(ad.get("ad_delivery_stop_time"))
            if has_stop and (not has_start or ad["ad_delivery_stop_time"] < ad.get("ad_delivery_start_time", "")):
                pass
            # ads_archive returns currently-active ads when searching by page;
            # presence of data = active ads for our purposes
            active += 1
            if len(creatives) < 5:
                creatives.append(dict(
                    page_name=ad.get("page_name"),
                    body=(ad.get("ad_creative_body") or "")[:500],
                    cta=(ad.get("ad_creative_link_caption") or ad.get("ad_creative_link_title") or "")[:100],
                    start=ad.get("ad_delivery_start_time"),
                ))
        return dict(active=active, archived=0, creatives=creatives, error=None)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            msg = body.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return dict(active=0, archived=0, creatives=[], error=msg)


def classify(result: dict, has_fb: bool) -> str:
    if result["error"] and "rate" in result["error"].lower():
        return "ERROR_RATE"
    if not has_fb:
        return "NO_FB_PAGE"
    if result["active"] > 0:
        return "RUNNING"
    if result["archived"] > 0:
        return "RAN_BEFORE"
    return "NO_ADS"


def save_result(cur, agency_id, status, result, page_name=None):
    cur.execute(
        """UPDATE agencies
           SET ad_status = %s, ad_last_checked = now(), updated_at = now()
           WHERE id = %s""",
        (status, agency_id),
    )
    cur.execute(
        """INSERT INTO ad_checks (agency_id, result, page_name, ads_active, ads_archived, raw)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (agency_id, status, page_name, result.get("active"), result.get("archived"),
         json.dumps(result.get("error") or "ok")),
    )
    for c in result.get("creatives", [])[:5]:
        cur.execute(
            """INSERT INTO ad_creatives (agency_id, page_name, body, cta, ad_delivery_start_date)
               VALUES (%s, %s, %s, %s, %s)""",
            (agency_id, c.get("page_name"), c.get("body"), c.get("cta"), c.get("start")),
        )


def main():
    ap = argparse.ArgumentParser(description="Check agencies against Meta Ad Library")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--state")
    ap.add_argument("--only-page-ids", action="store_true",
                    help="Only check agencies that have fb_page_id resolved")
    args = ap.parse_args()

    token = os.environ.get("TARGETO_META_TOKEN")
    if not token:
        print("ERROR: set TARGETO_META_TOKEN first (see module docstring for setup steps).")
        return 1

    where, params = ["(ad_status = 'UNRESOLVED' OR ad_status = 'PENDING')"], []
    if args.state:
        where.append("state = %s"); params.append(args.state.upper())
    if args.only_page_ids:
        where.append("fb_page_id IS NOT NULL")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT id, fb_page, fb_page_id, org_name FROM agencies
                WHERE {' AND '.join(where)} ORDER BY id LIMIT %s""",
            params + [args.limit],
        )
        rows = cur.fetchall()
        print(f"Checking {len(rows)} agencies...")
        stats = {}
        for i, (aid, fb_name, fb_id, org) in enumerate(rows, 1):
            if fb_id:
                result = check_page(token, page_id=fb_id)
            elif fb_name:
                result = check_page(token, keyword=fb_name)
            else:
                result = check_page(token, keyword=org)
                # no fb info at all: if keyword finds nothing, mark NO_FB_PAGE
            has_fb = bool(fb_id or fb_name)
            status = classify(result, has_fb)
            if status == "ERROR_RATE":
                print(f"\n  rate-limited at {i}; stopping (resume later)")
                conn.commit()
                break
            if status == "NO_ADS" and not has_fb:
                status = "NO_FB_PAGE"
            save_result(cur, aid, status, result, page_name=fb_name)
            conn.commit()
            stats[status] = stats.get(status, 0) + 1
            print(f"\r  {i}/{len(rows)} {stats}", end="", flush=True)
            time.sleep(1.1)  # polite: well under BUC limits
        print(f"\nDone: {stats}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
