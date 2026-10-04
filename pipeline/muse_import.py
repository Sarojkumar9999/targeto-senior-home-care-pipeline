#!/usr/bin/env python3
"""Import Muse's manual FB/Ad Library verifications.

Reads a CSV with this header (exact):
  npi,muse_fb_page,muse_fb_page_id,muse_fb_page_url,muse_fb_confidence,muse_ad_status,muse_ad_notes

Rules:
- Rows are matched by NPI. Unknown NPIs are reported and skipped.
- Empty cells mean "don't touch". A row may verify only the FB side,
  only the ads side, or both.
- muse_ad_status must be one of RUNNING, RAN_BEFORE, NO_ADS, NO_FB_PAGE,
  UNRESOLVED, PENDING. Bad values are reported and the whole row is skipped.
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
FB_FIELDS = ["muse_fb_page", "muse_fb_page_id", "muse_fb_page_url", "muse_fb_confidence"]
DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "muse_verifications.csv"
)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    if not os.path.exists(path):
        print(f"no csv at {path}, nothing to import")
        return 0
    conn = get_conn()
    cur = conn.cursor()
    updated_fb = updated_ads = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            npi = (row.get("npi") or "").strip()
            if not npi:
                print(f"line {i}: missing npi, skipped")
                skipped += 1
                continue
            fb_vals = {k: (row.get(k) or "").strip() for k in FB_FIELDS}
            ad_status = (row.get("muse_ad_status") or "").strip().upper()
            ad_notes = (row.get("muse_ad_notes") or "").strip()
            if ad_status and ad_status not in AD_STATUSES:
                print(f"line {i}: npi {npi}: bad muse_ad_status '{ad_status}', row skipped")
                skipped += 1
                continue
            cur.execute("SELECT id FROM agencies WHERE npi = %s", (npi,))
            hit = cur.fetchone()
            if not hit:
                print(f"line {i}: npi {npi} not in agencies, skipped")
                skipped += 1
                continue
            sets, params = [], []
            if any(fb_vals.values()):
                for k, v in fb_vals.items():
                    if v:
                        sets.append(f"{k} = %s")
                        params.append(v)
                sets.append("muse_fb_checked_at = now()")
                updated_fb += 1
            if ad_status or ad_notes:
                if ad_status:
                    sets.append("muse_ad_status = %s")
                    params.append(ad_status)
                if ad_notes:
                    sets.append("muse_ad_notes = %s")
                    params.append(ad_notes)
                sets.append("muse_ad_checked_at = now()")
                updated_ads += 1
            if sets:
                sets.append("updated_at = now()")
                cur.execute(
                    f"UPDATE agencies SET {', '.join(sets)} WHERE id = %s",
                    params + [hit[0]],
                )
    conn.commit()
    print(f"done: {updated_fb} fb verifications, {updated_ads} ad verifications, {skipped} skipped")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
