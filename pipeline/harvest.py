#!/usr/bin/env python3
"""Targeto Lead Pipeline — NPI Registry API harvester.

Pulls organizational NPI records by state + taxonomy from the official CMS
NPI Registry API (free, no key) and upserts them into Postgres.

Usage:
  python -m pipeline.harvest --states FL,TX --taxonomy "Home Health"
  python -m pipeline.harvest --states FL --taxonomy "Home Health Aide" --max-pages 3

The API returns up to 200 records per page. When a full 200-record page comes
back, more pages may exist — but the live API caps results at 5 pages
(1,000 records) per (state, taxonomy) query. When we hit the cap, the harvest
is logged as truncated and the NPPES bulk importer should be used for full
coverage of that state.
"""
import argparse
import time
import sys
from urllib.parse import urlencode

from . import DB, NPI_API, get_conn, normalize_name, normalize_city, fmt_phone, http_get_json

PAGE_SIZE = 200
API_PAGE_CAP = 1000  # live registry returns at most 1000 results per query


def upsert_agency(cur, rec) -> bool:
    """Insert or update one agency row. Returns True if written."""
    basic = rec.get("basic", {}) or {}
    org_name = basic.get("organization_name") or ""
    if not org_name:
        return False

    npi = str(rec.get("number", "")).strip()
    loc = next((a for a in rec.get("addresses", []) if a.get("address_purpose") == "LOCATION"),
               rec.get("addresses", [{}])[0] if rec.get("addresses") else {})
    mail = next((a for a in rec.get("addresses", []) if a.get("address_purpose") == "MAILING"), loc)

    phone = loc.get("telephone_number") or mail.get("telephone_number") or ""
    postal = (loc.get("postal_code") or "")[:5]
    city = loc.get("city") or ""
    state = loc.get("state") or ""

    ao_first = basic.get("authorized_official_first_name") or ""
    ao_last = basic.get("authorized_official_last_name") or ""
    ao_title = basic.get("authorized_official_title_or_position") or ""
    ao_phone = basic.get("authorized_official_telephone_number") or ""

    cur.execute(
        """
        INSERT INTO agencies (
            npi, org_name, org_name_norm, sole_owner, status, enumeration_date,
            last_updated, address_1, address_2, city, city_norm, state, postal_code,
            phone, owner_first_name, owner_last_name, owner_title, owner_phone,
            source, updated_at
        ) VALUES (
            %(npi)s, %(org_name)s, %(org_name_norm)s, %(sole_owner)s, %(status)s, %(enumeration_date)s,
            %(last_updated)s, %(address_1)s, %(address_2)s, %(city)s, %(city_norm)s, %(state)s, %(postal_code)s,
            %(phone)s, %(owner_first_name)s, %(owner_last_name)s, %(owner_title)s, %(owner_phone)s,
            'npi_api', now()
        )
        ON CONFLICT (npi) DO UPDATE SET
            org_name          = EXCLUDED.org_name,
            org_name_norm     = EXCLUDED.org_name_norm,
            status            = EXCLUDED.status,
            enumeration_date  = EXCLUDED.enumeration_date,
            address_1         = EXCLUDED.address_1,
            city              = EXCLUDED.city,
            city_norm         = EXCLUDED.city_norm,
            state             = EXCLUDED.state,
            postal_code       = EXCLUDED.postal_code,
            phone             = COALESCE(NULLIF(EXCLUDED.phone, ''), agencies.phone),
            owner_first_name  = COALESCE(NULLIF(EXCLUDED.owner_first_name, ''), agencies.owner_first_name),
            owner_last_name   = COALESCE(NULLIF(EXCLUDED.owner_last_name, ''), agencies.owner_last_name),
            owner_title       = COALESCE(NULLIF(EXCLUDED.owner_title, ''), agencies.owner_title),
            owner_phone       = COALESCE(NULLIF(EXCLUDED.owner_phone, ''), agencies.owner_phone),
            updated_at        = now()
        RETURNING id, (xmax = 0) AS inserted
        """,
        dict(
            npi=npi,
            org_name=org_name,
            org_name_norm=normalize_name(org_name),
            sole_owner=False,
            status=basic.get("status") or "A",
            enumeration_date=basic.get("enumeration_date") or None,
            last_updated=basic.get("last_updated") or None,
            address_1=loc.get("address_1") or "",
            address_2=loc.get("address_2") or "",
            city=city,
            city_norm=normalize_city(city),
            state=state,
            postal_code=postal,
            phone=fmt_phone(phone),
            owner_first_name=ao_first,
            owner_last_name=ao_last,
            owner_title=ao_title,
            owner_phone=fmt_phone(ao_phone),
        ),
    )
    row = cur.fetchone()
    agency_id, inserted = row[0], row[1]

    # taxonomies
    for tax in rec.get("taxonomies", []) or []:
        cur.execute(
            """
            INSERT INTO agency_taxonomies (agency_id, code, description, is_primary)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (agency_id, code) DO UPDATE SET
                description = EXCLUDED.description,
                is_primary  = EXCLUDED.is_primary
            """,
            (agency_id, tax.get("code"), tax.get("desc"), bool(tax.get("primary"))),
        )
    return bool(inserted)


def fetch_page(state: str, taxonomy: str, skip: int):
    params = {
        "version": "2.1",
        "enumeration_type": "NPI-2",
        "state": state,
        "taxonomy_description": taxonomy,
        "limit": PAGE_SIZE,
        "skip": skip,
    }
    return http_get_json(f"{NPI_API}?{urlencode(params)}")


def harvest_state_taxonomy(state: str, taxonomy: str, max_pages: int | None, delay: float):
    conn = get_conn()
    cur = conn.cursor()
    inserted = updated = skipped = 0
    total_seen = 0
    truncated = False
    error = None
    pages_fetched = 0

    cur.execute(
        """INSERT INTO harvest_log (source, state, taxonomy, started_at)
           VALUES ('npi_api', %s, %s, now()) RETURNING id""",
        (state, taxonomy),
    )
    log_id = cur.fetchone()[0]
    conn.commit()

    try:
        skip = 0
        while True:
            if max_pages and pages_fetched >= max_pages:
                break
            data = fetch_page(state, taxonomy, skip)
            results = data.get("results", []) or []
            pages_fetched += 1
            if not results:
                break
            for rec in results:
                total_seen += 1
                try:
                    if upsert_agency(cur, rec):
                        inserted += 1
                    else:
                        updated += 1
                except Exception:
                    skipped += 1
            conn.commit()
            if len(results) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
            if skip >= API_PAGE_CAP:
                truncated = True
                break
            time.sleep(delay)
    except Exception as e:
        error = str(e)
    finally:
        cur.execute(
            """UPDATE harvest_log
               SET pages_fetched=%s, records_upserted=%s, skipped=%s,
                   truncated=%s, error=%s, finished_at=now()
               WHERE id=%s""",
            (pages_fetched, inserted + updated, skipped, truncated, error, log_id),
        )
        conn.commit()
        cur.close()
        conn.close()

    return dict(state=state, taxonomy=taxonomy, pages=pages_fetched, seen=total_seen,
                inserted=inserted, updated=updated, skipped=skipped, truncated=truncated,
                error=error)


def main():
    ap = argparse.ArgumentParser(description="Harvest NPI Registry by state + taxonomy")
    ap.add_argument("--states", required=True, help="Comma-separated 2-letter states, e.g. FL,TX,AZ")
    ap.add_argument("--taxonomy", default="Home Health",
                    help="Taxonomy description, e.g. 'Home Health', 'Home Health Aide'")
    ap.add_argument("--max-pages", type=int, default=None, help="Safety cap on pages per state")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between API pages")
    args = ap.parse_args()

    states = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    print(f"Harvesting states={states} taxonomy='{args.taxonomy}'")
    grand = dict(inserted=0, updated=0, skipped=0)
    for st in states:
        r = harvest_state_taxonomy(st, args.taxonomy, args.max_pages, args.delay)
        print(f"  {st}: seen={r['seen']} inserted={r['inserted']} updated={r['updated']} "
              f"skipped={r['skipped']} truncated={r['truncated']} error={r['error']}")
        grand["inserted"] += r["inserted"]
        grand["updated"] += r["updated"]
        grand["skipped"] += r["skipped"]
        if r["truncated"]:
            print(f"  ⚠️  {st}: API page cap reached — use NPPES bulk import for full coverage")
    print(f"Done. inserted={grand['inserted']} updated={grand['updated']} skipped={grand['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
