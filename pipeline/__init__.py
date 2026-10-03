"""Shared helpers: Postgres connection, normalization, upsert logic."""
import os
import re
import unicodedata
import json
import urllib.request
import urllib.error

DB = dict(
    host=os.environ.get("TARGETO_PGHOST", "localhost"),
    port=int(os.environ.get("TARGETO_PGPORT", "5434")),
    dbname=os.environ.get("TARGETO_PGDATABASE", "targeto"),
    user=os.environ.get("TARGETO_PGUSER", "targeto"),
    password=os.environ.get("TARGETO_PGPASSWORD", "targeto_local"),
)

NPI_API = "https://npiregistry.cms.hhs.gov/api/"


def get_conn():
    import psycopg2
    return psycopg2.connect(**DB)


def normalize_name(name: str) -> str:
    """Normalize an org name for dedupe/matching: lowercase, strip accents,
    quotes, punctuation, and noisy corporate suffixes."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[\"']", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    for suffix in (
        " inc", " llc", " co", " corp", " corporation", " company",
        " ltd", " pc", " pllc", " pa", " lp", " llp", " holdings",
    ):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip()


def normalize_city(city: str) -> str:
    if not city:
        return ""
    s = unicodedata.normalize("NFKD", city)
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def normalize_phone(p: str) -> str:
    if not p:
        return ""
    return re.sub(r"\D", "", p)


def fmt_phone(digits: str) -> str:
    if not digits:
        return ""
    d = normalize_phone(digits)
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:10]}"
    if len(d) == 11 and d.startswith("1"):
        return f"({d[1:4]}) {d[4:7]}-{d[7:11]}"
    return digits


def slugify(name: str) -> str:
    s = normalize_name(name)
    return s.replace(" ", "-")


def http_get_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "targeto-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
