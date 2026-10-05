#!/usr/bin/env python3
"""Import Muse's FB/Ad Library verifications into the pipeline DB.

Reads data/fb_page_corrections.csv (preferred) or data/muse_verifications.csv
(legacy). Rows are matched by NPI; unknown NPIs are reported and skipped.

Corrections CSV header (exact):
  npi,verified_fb_page,verified_fb_page_id,verified_fb_page_url,
  page_match_confidence,verified_ad_status,manual_review_needed,
  review_task,pipeline_stored_url_was_wrong,verifier_notes

Mapping (this is the import Saroj asked for):
- verified_ad_status  -> muse_ad_status  (audit) AND -> ad_status (PRIMARY
  answer). Saroj 2026-10-06: "in has fb page, update with your muse filter
  and not old one, same goes to ads status and other filters as well - u
  have more accurate data than the previous stale db". PENDING (not a
  pipeline tab value) stays muse-only; it does not occur in the final export.
- verified_fb_page*   -> muse_fb_page / muse_fb_page_id / muse_fb_page_url /
  muse_fb_confidence (audit) AND, when verifier-confirmed, -> fb_page /
  fb_page_id / fb_page_url (PRIMARY). Rules:
    * NO_FB_PAGE          -> clear primary fb_page/fb_page_id/fb_page_url.
      verified_fb_page may hold a low-confidence REJECTED candidate here
      (personal profile etc.) - never promoted.
    * verified page name  -> write fb_page (+ fb_page_url / fb_page_id when
      known; the id is NULLed only when the URL is replaced, so a stale id
      is never left attached to a new URL)
    * was_wrong=yes and no verified replacement -> clear primary fb_*
      (the stored URL is the wrong business)
    * UNRESOLVED with a carried-over URL but no verified page name -> clear
      as well (verifier_notes document it as the wrong business)
    * otherwise -> don't touch the primary fb columns
- manual_review_needed (yes/no) -> manual_review_needed boolean
- review_task         -> review_task ('check_ad_library' | 'recheck_page_search')
- verifier_notes      -> muse_ad_notes

Primary promotion applies to the corrections CSV only; the legacy
muse_verifications.csv fallback keeps the old muse_*-only behavior.

Re-running the import re-applies the verified values to the primary columns,
so manual ad_status/page edits made in the UI after an import would be reset
by a later re-import. The muse_* columns stay the untouched audit trail.

Only manual_review_needed=TRUE rows (319 at campaign close) need Saroj's
manual Ad Library check — everything else is settled by the import.

Rules:
- Empty cells mean "don't touch". A row may verify only the FB side,
  only the ads side, or both.
- verified_ad_status must be one of RUNNING, RAN_BEFORE, NO_ADS,
  NO_FB_PAGE, UNRESOLVED, PENDING. Bad values are reported and the whole
  row is skipped.
- muse_*_checked_at is set to now() for each side that received new data.
- Idempotent: re-running the same file changes nothing.

Usage (on the VM):
  docker compose -f deploy/docker-compose.yml run --rm dashboard \
      python -m pipeline.muse_import [path/to/csv]
"""
import csv
import os
import sys

from . import get_conn

AD_STATUSES = {"RUNNING", "RAN_BEFORE", "NO_ADS", "NO_FB_PAGE", "UNRESOLVED", "PENDING"}
# pipeline ad_status tab values (AD_TABS in app.py) - the verified values map 1:1
PIPELINE_AD_STATUSES = {"RUNNING", "RAN_BEFORE", "NO_ADS", "NO_FB_PAGE", "UNRESOLVED"}
REVIEW_TASKS = {"check_ad_library", "recheck_page_search"}

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = [
    os.path.join(HERE, "..", "data", "fb_page_corrections.csv"),
    os.path.join(HERE, "..", "data", "muse_verifications.csv"),
]

# new header -> (db column, kind) ; kind decides the checked_at stamp
NEW_MAP = {
    "verified_fb_page": ("muse_fb_page", "fb"),
    "verified_fb_page_id": ("muse_fb_page_id", "fb"),
    "verified_fb_page_url": ("muse_fb_page_url", "fb"),
    "page_match_confidence": ("muse_fb_confidence", "fb"),
    "verified_ad_status": ("muse_ad_status", "ad"),
    "verifier_notes": ("muse_ad_notes", "ad"),
}
# legacy header passthrough
OLD_MAP = {
    "muse_fb_page": ("muse_fb_page", "fb"),
    "muse_fb_page_id": ("muse_fb_page_id", "fb"),
    "muse_fb_page_url": ("muse_fb_page_url", "fb"),
    "muse_fb_confidence": ("muse_fb_confidence", "fb"),
    "muse_ad_status": ("muse_ad_status", "ad"),
    "muse_ad_notes": ("muse_ad_notes", "ad"),
}


def pick_csv(explicit):
    if explicit:
        return explicit
    for p in CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def main() -> int:
    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    path = pick_csv(explicit)
    if not path or not os.path.exists(path):
        print("no corrections csv found, nothing to import")
        return 0
    print(f"importing {path}")
    conn = get_conn()
    cur = conn.cursor()
    updated_fb = updated_ads = updated_q = skipped = prim_promoted = prim_cleared = prim_ads = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        mapping = NEW_MAP if "verified_ad_status" in cols else OLD_MAP
        is_new = mapping is NEW_MAP
        for i, row in enumerate(reader, start=2):
            npi = (row.get("npi") or "").strip()
            if not npi:
                print(f"line {i}: missing npi, skipped")
                skipped += 1
                continue
            vals = {}
            kinds = set()
            for src, (dst, kind) in mapping.items():
                v = (row.get(src) or "").strip()
                if src in ("verified_ad_status", "muse_ad_status"):
                    v = v.upper()
                    if v and v not in AD_STATUSES:
                        print(f"line {i}: npi {npi}: bad ad status '{v}', row skipped")
                        vals = None
                        break
                if v:
                    vals[dst] = v
                    kinds.add(kind)
            if vals is None:
                skipped += 1
                continue
            # manual review queue (corrections CSV only)
            queue_vals = {}
            if is_new:
                yn = (row.get("manual_review_needed") or "").strip().lower()
                if yn in ("yes", "true", "1", "y"):
                    queue_vals["manual_review_needed"] = True
                elif yn in ("no", "false", "0", "n", ""):
                    queue_vals["manual_review_needed"] = False
                else:
                    print(f"line {i}: npi {npi}: bad manual_review_needed '{yn}', row skipped")
                    skipped += 1
                    continue
                rt = (row.get("review_task") or "").strip()
                if rt:
                    if rt not in REVIEW_TASKS:
                        print(f"line {i}: npi {npi}: bad review_task '{rt}', row skipped")
                        skipped += 1
                        continue
                    queue_vals["review_task"] = rt
                elif yn in ("yes", "true", "1", "y"):
                    queue_vals["review_task"] = None  # unknown task, still queued
            # ---- promote verified data to the PRIMARY answer columns ----
            prim_sets, prim_params = [], []
            if is_new and vals is not None:
                ad = (row.get("verified_ad_status") or "").strip().upper()
                vpage = (row.get("verified_fb_page") or "").strip()
                vid = (row.get("verified_fb_page_id") or "").strip()
                vurl = (row.get("verified_fb_page_url") or "").strip()
                was_wrong = (row.get("pipeline_stored_url_was_wrong") or "").strip().lower() in (
                    "yes", "true", "1", "y")
                if ad in PIPELINE_AD_STATUSES:
                    prim_sets.append("ad_status = %s")
                    prim_params.append(ad)
                    prim_ads += 1
                if ad == "NO_FB_PAGE":
                    # verified: no FB page. verified_fb_page may hold a rejected
                    # low-confidence candidate - never promote it.
                    prim_sets += ["fb_page = NULL", "fb_page_id = NULL", "fb_page_url = NULL"]
                    prim_cleared += 1
                elif vpage:
                    # verifier-confirmed page
                    prim_sets.append("fb_page = %s")
                    prim_params.append(vpage)
                    if vurl:
                        prim_sets.append("fb_page_url = %s")
                        prim_params.append(vurl)
                        # id NULLed only with a URL replacement: never leave a
                        # stale id attached to a new URL
                        prim_sets.append("fb_page_id = %s")
                        prim_params.append(vid or None)
                    prim_promoted += 1
                elif was_wrong or (ad == "UNRESOLVED" and vurl):
                    # no verified replacement, but the stored URL is the wrong
                    # business (flagged, or documented in verifier_notes for
                    # UNRESOLVED rows whose carried-over URL resolves elsewhere)
                    prim_sets += ["fb_page = NULL", "fb_page_id = NULL", "fb_page_url = NULL"]
                    prim_cleared += 1
                # else: nothing verified about the page - don't touch primary fb columns
            cur.execute("SELECT id FROM agencies WHERE npi = %s", (npi,))
            hit = cur.fetchone()
            if not hit:
                print(f"line {i}: npi {npi} not in agencies, skipped")
                skipped += 1
                continue
            sets, params = [], []
            for k, v in vals.items():
                sets.append(f"{k} = %s")
                params.append(v)
            if "fb" in kinds:
                sets.append("muse_fb_checked_at = now()")
                updated_fb += 1
            if "ad" in kinds:
                sets.append("muse_ad_checked_at = now()")
                updated_ads += 1
            if queue_vals:
                for k, v in queue_vals.items():
                    sets.append(f"{k} = %s")
                    params.append(v)
                if queue_vals.get("manual_review_needed"):
                    updated_q += 1
            if prim_sets:
                sets.extend(prim_sets)
                params.extend(prim_params)
            if sets:
                sets.append("updated_at = now()")
                cur.execute(
                    f"UPDATE agencies SET {', '.join(sets)} WHERE id = %s",
                    params + [hit[0]],
                )
    conn.commit()
    print(f"done: {updated_fb} fb verifications, {updated_ads} ad verifications, "
          f"{updated_q} queued for manual review, {skipped} skipped")
    print(f"primary: {prim_ads} ad_status promoted, {prim_promoted} fb pages promoted, "
          f"{prim_cleared} fb pages cleared")
    try:
        cur.execute("SELECT ad_status, count(*) FROM agencies GROUP BY 1 ORDER BY 1")
        print("primary ad_status now:", dict(cur.fetchall()))
        cur.execute("SELECT count(*) FROM agencies WHERE fb_page_url IS NOT NULL")
        print("primary has-fb-page count:", cur.fetchone()[0])
    except Exception as e:
        print("post-import count check failed:", e)
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
