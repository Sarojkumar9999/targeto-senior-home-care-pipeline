#!/usr/bin/env python3
"""Targeto Lead Pipeline — Phase 3 resolver.

For each agency without a website:
  1. Probe candidate domains guessed from the org name (concurrently, DNS pre-check).
  2. Fallback: DuckDuckGo search for "name" + city + home care.
  3. VERIFY a candidate by matching the agency's NPI phone number in the page
     text (gold standard), or fall back to a strict name/title heuristic.
  4. From the verified site, extract facebook.com page slugs.

Precision over recall: a wrong website is worse than no website. Unresolved
agencies stay NULL and can be retried or handled via manual review.

Usage:
  python -m pipeline.resolve --limit 50 --state FL
  python -m pipeline.resolve --city miami --limit 100
"""
import argparse
import concurrent.futures as cf
import html as htmllib
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

from . import get_conn, normalize_name

# Optional but recommended: Google Custom Search JSON API (100 free queries/day).
# Create a Programmable Search Engine at programmablesearchengine.google.com
# (enable "Search the entire web") and an API key in Google Cloud. Then:
#   export TARGETO_GOOGLE_CSE_KEY=...  TARGETO_GOOGLE_CSE_ID=...
# Without keys: domain guessing only (DDG is commonly IP-blocked for scripts).

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TLDs = ["com", "net", "org"]
TIMEOUT = 5

FB_RE = re.compile(r"https?://(?:www\.|m\.)?facebook\.com/([A-Za-z0-9_.\-]{2,60})/?", re.I)
FB_BAD_PREFIX = ("pg/", "people/", "profile.php", "pages/", "events/",
                 "marketplace/", "groups/", "hashtag/", "story.php", "permalink.php")
BLOCKED_HOSTS = ("facebook.com", "linkedin.com", "instagram.com", "caring.com",
                 "yelp.com", "google.", "youtube.", "linktr.ee", "indeed.",
                 "npino.", "healthgrades", "birdeye", "bbb.org", "agingcare",
                 "npidataservices", "aplaceformom", "seniorcare", "seniorly",
                 "yellowpages", "mapquest", "nicelocal", "chamberofcommerce",
                 "manta.com", "alignable", "nextdoor", "wikipedia",
                 "seniorcenter.us", "findrightcare", "homecareatlas", "families.care",
                 "wecarely", "careinhomes", "seniorcareauthority", "allbiz",
                 "npidb.org", "npino.com", "carepath", "eldertreep", "sunshinesearch",
                 "directory", "listing")
# URL path markers of directory/listing pages (never the agency's own site)
DIR_PATH_MARKS = ("/facility/", "/providers", "/provider/", "/agency/",
                  "/business-directory/", "/local/", "/hhca/", "/npi",
                  "/home-health/", "/profile/", "/company/", "/listing", "/l--")


def http_get(url: str, timeout: int = TIMEOUT) -> str | None:
    try:
        host = urllib.parse.urlparse(url).netloc
        socket.getaddrinfo(host, 443)  # fast DNS fail before HTTP attempt
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            return resp.read(400_000).decode("utf-8", errors="replace")
    except Exception:  # incl. http.client.InvalidURL (space/control chars in URL)
        return None


def search_google(query: str) -> list[str]:
    """Google Custom Search JSON API (if keys configured). Clean and stable."""
    key = os.environ.get("TARGETO_GOOGLE_CSE_KEY")
    cx = os.environ.get("TARGETO_GOOGLE_CSE_ID")
    if not key or not cx:
        return []
    try:
        qs = urllib.parse.urlencode({"key": key, "cx": cx, "q": query, "num": 5})
        req = urllib.request.Request(f"https://www.googleapis.com/customsearch/v1?{qs}", headers=UA)
        import json
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [item["link"].rstrip("/") for item in data.get("items", [])][:5]
    except Exception:
        return []


def search_ddg(query: str) -> list[str]:
    """DuckDuckGo HTML endpoint (often IP-blocked for scripted use)."""
    try:
        data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/", data=data,
            headers={**UA, "Referer": "https://duckduckgo.com/"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            page = resp.read(300_000).decode("utf-8", errors="replace")
        urls = re.findall(r'class="result__url"[^>]*>\s*(?:https?://)?([^<\s]+)', page)
        return [u.strip().rstrip("/") for u in urls][:8]
    except Exception:
        return []


def search_ddgs_lib(query: str) -> list[str]:
    """ddgs library (pip install ddgs) — works where raw endpoints are blocked."""
    try:
        from ddgs import DDGS
        with DDGS() as d:
            return [r["href"] for r in d.text(query, max_results=6)]
    except Exception:
        return []


def search(query: str) -> list[str]:
    """Search chain: ddgs lib → Google CSE (if configured) → raw DDG."""
    return search_ddgs_lib(query) or search_google(query) or search_ddg(query)


def name_tokens(org_name: str) -> list[str]:
    n = normalize_name(org_name)
    stop = {"home", "health", "healthcare", "care", "services", "agency", "inc", "llc",
            "the", "of", "and", "for", "senior", "seniors", "fl", "florida"}
    return [t for t in n.split() if t not in stop and len(t) >= 4]


def domain_candidates(org_name: str) -> list[str]:
    """Plausible domains: condensed name slugs x short TLD list."""
    n = normalize_name(org_name)
    words = n.split()
    stop = {"home", "health", "healthcare", "care", "services", "agency", "inc",
            "llc", "the", "of", "and"}
    core = [w for w in words if w not in stop] or words
    cands: list[str] = []
    if len(core) >= 2:
        cands.append("".join(core[:2]))                # aclass.com
        cands.append("".join(core))                    # aclasshome.com
        cands.append("".join(w[0] for w in core[:5]))  # achh.com
    if len(core) == 1 and len(core[0]) >= 5:
        cands.append(core[0])
    return [f"{stem}.{tld}" for stem in cands if stem for tld in TLDs][:9]


def page_text(html: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return htmllib.unescape(re.sub(r"\s+", " ", txt)).lower()


def verify_site(html: str, org: str, tokens: list[str], city: str | None,
                phones: list[str], postal: str | None) -> str | None:
    """Return 'high' | 'medium' | None for this candidate page."""
    h = html.lower()
    if any(x in h for x in ("domain is for sale", "buy this domain", "parked free",
                            "godaddy.com/domainsearch")):
        return None
    txt = page_text(html)
    # GOLD STANDARD: agency phone digits appear on the page
    for ph in phones:
        if ph and len(ph) == 10 and ph in re.sub(r"\D", "", txt):
            return "high"
    # STRONG NAME: full normalized org name (minus suffixes) in title/text
    title_m = re.search(r"<title[^>]*>(.*?)</title>", h, re.S)
    title = re.sub(r"<[^>]+>", " ", title_m.group(1)) if title_m else ""
    title_txt = htmllib.unescape(re.sub(r"\s+", " ", title)).lower()
    name_norm = normalize_name(org)
    core_norm = " ".join(w for w in name_norm.split()
                         if w not in ("home", "health", "care", "services", "agency"))
    looks_like_biz = any(x in txt for x in ("care", "health", "patient", "senior"))
    if core_norm and len(core_norm) >= 8 and (core_norm in title_txt or core_norm in txt) and looks_like_biz:
        return "high"
    # MEDIUM: 2+ distinctive tokens in title + looks like a care business
    tok_hits = sum(1 for t in tokens if t in title_txt)
    if tok_hits >= 2 and looks_like_biz:
        return "medium"
    if tok_hits >= 1 and looks_like_biz and postal and postal in txt:
        return "medium"
    return None


def extract_fb_links(html: str) -> list[str]:
    pages = []
    for m in FB_RE.finditer(html):
        slug = m.group(1)
        low = slug.lower()
        if low in FB_BAD_PREFIX or any(low.startswith(p) for p in FB_BAD_PREFIX):
            continue
        if low.isdigit():  # bare profile ids are useless for Ad Library search
            continue
        slug = re.sub(r"[?/&].*$", "", slug)
        if len(slug) >= 3:
            pages.append(slug)
    seen, out = set(), []
    for p in pages:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out[:3]


def try_candidates(domains: list[str], org: str, tokens: list[str],
                   city: str | None, phones: list[str], postal: str | None):
    """Probe domains concurrently; return (html, url, conf) of first verified match."""
    def probe(dom):
        html = http_get(f"https://{dom}", timeout=4)
        if not html:
            return None
        conf = verify_site(html, org, tokens, city, phones, postal)
        return (html, f"https://{dom}", conf) if conf else None

    order = {h: i for i, h in enumerate(("high", "medium"))}
    best = None
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(probe, domains):
            if res and (best is None or order[res[2]] < order[best[2]]):
                best = res
                if res[2] == "high":
                    break
    return best


def resolve_one(cur, row) -> str:
    """Returns 'guess' | 'search' | 'none'."""
    aid, org, city, state, phone, owner_phone, postal = row
    tokens = name_tokens(org)
    phones = [re.sub(r"\D", "", p or "") for p in (phone, owner_phone)]

    hit = try_candidates(domain_candidates(org), org, tokens, city, phones, postal)
    source = "guess" if hit else None

    if not hit:
        results = search(f'"{org}" {city or ""} home care')
        for url in results:
            if not url.startswith("http"):
                url = "https://" + url
            host = urllib.parse.urlparse(url).netloc.replace("www.", "")
            path = urllib.parse.urlparse(url).path.lower()
            if not host or any(b in host for b in BLOCKED_HOSTS):
                continue
            if any(m in path for m in DIR_PATH_MARKS):
                continue  # a directory listing page, not their website
            html = http_get(url, timeout=4)
            if not html:
                continue
            conf = verify_site(html, org, tokens, city, phones, postal)
            if conf == "high":  # search results: accept only phone/core-name verified
                hit, source = (html, url.rstrip("/"), conf), "search"
                break

    if not hit:
        return "none"

    site_html, site_url, conf = hit
    fb_links = extract_fb_links(site_html)
    cur.execute(
        """UPDATE agencies
           SET website = %s, website_source = %s, website_confidence = %s,
               fb_page = COALESCE(NULLIF(%s, ''), fb_page),
               fb_source = CASE WHEN %s <> '' THEN 'site_link' ELSE fb_source END,
               fb_confidence = CASE WHEN %s <> '' THEN %s ELSE fb_confidence END,
               updated_at = now()
           WHERE id = %s""",
        (site_url, source, conf,
         fb_links[0] if fb_links else "", fb_links[0] if fb_links else "",
         fb_links[0] if fb_links else "", conf, aid),
    )
    return source


def main():
    ap = argparse.ArgumentParser(description="Resolve agency websites + FB pages")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--state")
    ap.add_argument("--city")
    ap.add_argument("--shard", help="i/N — process only rows where id %% N = i-1 (parallel workers)")
    ap.add_argument("--delay", type=float, default=0.2)
    args = ap.parse_args()

    where, params = ["website IS NULL", "org_name IS NOT NULL"], []
    if args.shard and "/" in args.shard:
        i, n = args.shard.split("/", 1)
        if i.isdigit() and n.isdigit() and 1 <= int(i) <= int(n):
            where.append(f"id %% %s = %s")
            params += [int(n), int(i) - 1]
    if args.state:
        where.append("state = %s")
        params.append(args.state.upper())
    if args.city:
        where.append("city_norm = %s")
        params.append(args.city.strip().lower())

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT id, org_name, city, state, phone, owner_phone, postal_code
                FROM agencies WHERE {' AND '.join(where)} ORDER BY id LIMIT %s""",
            params + [args.limit],
        )
        rows = cur.fetchall()
        found = fb_found = fromsearch = 0
        for i, row in enumerate(rows, 1):
            src = resolve_one(cur, row)
            conn.commit()
            if src != "none":
                found += 1
                if src == "search":
                    fromsearch += 1
                cur.execute("SELECT fb_page FROM agencies WHERE id = %s", (row[0],))
                r = cur.fetchone()
                if r and r[0]:
                    fb_found += 1
            print(f"\r  {i}/{len(rows)} resolved={found} (via search {fromsearch}) fb={fb_found}", end="", flush=True)
            time.sleep(args.delay)
        print(f"\nDone. websites found: {found}/{len(rows)} · FB pages from sites: {fb_found}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
