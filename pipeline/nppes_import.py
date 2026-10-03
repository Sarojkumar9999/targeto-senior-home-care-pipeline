#!/usr/bin/env python3
"""Targeto Lead Pipeline — NPPES national bulk importer.

Imports the full-US NPPES Downloadable File (all 50 states) into Postgres,
filtered to home-care-relevant organizational taxonomies. This is what makes
the system scalable to ANY US city/ZIP — the live API caps at 1,000 records
per state+taxonomy query, the bulk file has everything.

Download (free, ~9 GB zip, monthly full replacement):
  https://download.cms.gov/nppes/NPI_Files.html
  → "Monthly NPPES Downloadable File" (ends in npidata_pfile_yyyymmdd-yyyymmdd.zip)
  The zip contains npidata_pfile_yyyymmdd-yyyymmdd.csv

Usage:
  python -m pipeline.nppes_import --file ~/Downloads/npidata_pfile_20050523-20250914.zip
  python -m pipeline.nppes_import --file ... --taxonomies "Home Health" "Home Health Aide"

Streaming design: reads the CSV row-by-row (never loads the file in memory),
filters by entity type + taxonomy keywords, upserts in batches of 2,000.
Roughly 15–40 min for the full file on Saroj's laptop.
"""
import argparse
import csv
import io
import sys
import zipfile
from datetime import datetime

from . import get_conn, normalize_name, normalize_city, fmt_phone

BATCH = 2000

# Organization taxonomies relevant to the senior-care niche (keyword match on
# taxonomy description columns). Home Health, Home Health Aide are core;
# hospice/companion/respite included by default — trim the list if too broad.
TAXONOMY_KEYWORDS_DEFAULT = [
    "home health", "home care", "homemaker", "companion", "hospice",
    "respite care", "assisted living",
]

UPSERT_SQL = """
INSERT INTO agencies (
    npi, org_name, org_name_norm, sole_owner, status, enumeration_date,
    last_updated, address_1, address_2, city, city_norm, state, postal_code,
    phone, owner_first_name, owner_last_name, owner_title, owner_phone, source
) VALUES (
    %(npi)s, %(org_name)s, %(org_name_norm)s, %(sole_owner)s, %(status)s, %(enumeration_date)s,
    %(last_updated)s, %(address_1)s, %(address_2)s, %(city)s, %(city_norm)s, %(state)s, %(postal_code)s,
    %(phone)s, %(owner_first_name)s, %(owner_last_name)s, %(owner_title)s, %(owner_phone)s,
    'nppes_bulk'
)
ON CONFLICT (npi) DO UPDATE SET
    org_name         = EXCLUDED.org_name,
    org_name_norm    = EXCLUDED.org_name_norm,
    status           = EXCLUDED.status,
    last_updated     = EXCLUDED.last_updated,
    address_1        = EXCLUDED.address_1,
    address_2        = EXCLUDED.address_2,
    city             = EXCLUDED.city,
    city_norm        = EXCLUDED.city_norm,
    state            = EXCLUDED.state,
    postal_code      = EXCLUDED.postal_code,
    phone            = COALESCE(NULLIF(EXCLUDED.phone, ''), agencies.phone),
    owner_first_name = COALESCE(NULLIF(EXCLUDED.owner_first_name, ''), agencies.owner_first_name),
    owner_last_name  = COALESCE(NULLIF(EXCLUDED.owner_last_name, ''), agencies.owner_last_name),
    owner_title      = COALESCE(NULLIF(EXCLUDED.owner_title, ''), agencies.owner_title),
    owner_phone      = COALESCE(NULLIF(EXCLUDED.owner_phone, ''), agencies.owner_phone),
    updated_at       = now()
"""

TAXONOMY_SQL = """
INSERT INTO agency_taxonomies (agency_id, code, description, is_primary)
VALUES (%s, %s, %s, %s)
ON CONFLICT (agency_id, code) DO UPDATE SET
    description = EXCLUDED.description, is_primary = EXCLUDED.is_primary
"""


def clean_date(v: str):
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def match_taxonomies(row) -> list[tuple[str, str, bool]]:
    """Return [(code, description, is_primary), ...] matching keywords."""
    out = []
    for i in range(1, 16):
        desc = (row.get(f"Healthcare Provider Taxonomy Description_{i}") or "").strip()
        code = (row.get(f"Healthcare Provider Taxonomy Code_{i}") or "").strip()
        if not desc:
            continue
        primary = (row.get(f"Healthcare Provider Primary Taxonomy Switch_{i}") or "").strip() == "Y"
        if primary:
            out.insert(0, (code, desc, True))
        else:
            out.append((code, desc, False))
    return out


def row_matches(row, keywords) -> bool:
    """Organization (Type 2) whose primary taxonomy matches our keywords."""
    if (row.get("Entity Type Code") or "").strip() != "2":
        return False
    for i in range(1, 16):
        switch = (row.get(f"Healthcare Provider Primary Taxonomy Switch_{i}") or "").strip()
        if switch != "Y":
            continue
        desc = (row.get(f"Healthcare Provider Primary Taxonomy Description_{i}") or "").lower()
        return any(k in desc for k in keywords)
    return False


def build_record(row) -> dict:
    def loc(field):
        v = (row.get(f"Provider {field} Address {field} L1") or "").strip()
        return v

    city = (row.get("Provider City Address L1") or "").strip()
    state = (row.get("Provider State Address L1") or "").strip()
    postal = ((row.get("Provider Postal Code Address L1") or "").strip())[:5]
    phone = (row.get("Provider Telephone Number Address L1") or "").strip()

    ao_first = (row.get("Authorized Official First Name") or "").strip()
    ao_last = (row.get("Authorized Official Last Name") or "").strip()
    ao_title = (row.get("Authorized Official Title or Position") or "").strip()
    ao_phone = (row.get("Authorized Official Telephone Number") or "").strip()

    org = (row.get("Provider Organization Name (Legal Business Name)") or "").strip()
    return dict(
        npi=(row.get("NPI") or "").strip(),
        org_name=org,
        org_name_norm=normalize_name(org),
        sole_owner=(row.get("Provider Organization Name (Legal Business Name) Form") or "").strip() == "2",
        status="A",
        enumeration_date=clean_date(row.get("Provider Enumeration Date")),
        last_updated=clean_date(row.get("Last Update Date")),
        address_1=(row.get("Provider First Line Business Practice Location Address") or "").strip(),
        address_2=(row.get("Provider Second Line Business Practice Location Address") or "").strip(),
        city=city,
        city_norm=normalize_city(city),
        state=state,
        postal_code=postal,
        phone=fmt_phone(phone),
        owner_first_name=ao_first,
        owner_last_name=ao_last,
        owner_title=ao_title,
        owner_phone=fmt_phone(ao_phone),
    )


def iter_rows(path: str):
    """Yield dict rows from the NPPES zip (or raw csv), streaming."""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            with zf.open(name) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
                yield from reader
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            yield from reader


def main():
    ap = argparse.ArgumentParser(description="Import NPPES bulk file into Postgres")
    ap.add_argument("--file", required=True, help="Path to npidata_pfile_*.zip or .csv")
    ap.add_argument("--taxonomies", nargs="*", default=None,
                    help="Taxonomy keyword filters (default: senior-care set)")
    ap.add_argument("--states", default=None, help="Optional comma-separated state filter")
    args = ap.parse_args()

    keywords = [k.lower() for k in (args.taxonomies or TAXONOMY_KEYWORDS_DEFAULT)]
    states = {s.strip().upper() for s in args.states.split(",")} if args.states else None

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO harvest_log (source, state, taxonomy, started_at)
           VALUES ('nppes_bulk', 'ALL', %s, now()) RETURNING id""",
        (",".join(keywords),),
    )
    log_id = cur.fetchone()[0]
    conn.commit()

    seen = inserted = updated = skipped = 0
    tax_rows = []
    batch = []

    for row in iter_rows(args.file):
        seen += 1
        if states and (row.get("Provider Business Practice Location Address State Name") or "").strip().upper() not in states:
            continue
        if not row_matches(row, keywords):
            continue
        try:
            rec = build_record(row)
            if not rec["npi"] or not rec["org_name"]:
                skipped += 1
                continue
            batch.append(rec)
            for code, desc, primary in match_taxonomies(row):
                tax_rows.append((rec["npi"], code, desc, primary))
        except Exception:
            skipped += 1
            continue

        if len(batch) >= BATCH:
            ins = flush(cur, conn, batch, tax_rows)
            inserted += ins[0]; updated += ins[1]
            batch.clear(); tax_rows.clear()
            print(f"  ... scanned {seen:,} rows, matched {inserted + updated:,}", end="\r")

    if batch:
        ins = flush(cur, conn, batch, tax_rows)
        inserted += ins[0]; updated += ins[1]

    cur.execute(
        """UPDATE harvest_log
           SET pages_fetched=1, records_upserted=%s, skipped=%s, finished_at=now()
           WHERE id=%s""",
        (inserted + updated, skipped, log_id),
    )
    conn.commit()
    print(f"\nDone. scanned={seen:,} matched inserted={inserted:,} updated={updated:,} skipped={skipped:,}")
    return 0


def flush(cur, conn, batch, tax_rows):
    ids = {}
    for rec in batch:
        cur.execute(UPSERT_SQL, rec)
        row = cur.fetchone()
        ids[rec["npi"]] = row[0]
    for npi, code, desc, primary in tax_rows:
        if npi in ids:
            cur.execute(TAXONOMY_SQL, (ids[npi], code, desc, primary))
    conn.commit()
    return len(batch), 0  # updated counted implicitly by upsert; simple split


if __name__ == "__main__":
    sys.exit(main())
