#!/usr/bin/env python3
"""Targeto Lead Pipeline — Facebook page discovery (Saroj's method).

The problem: NPI legal business name != FB page name, so searching the Ad
Library with the NPI name returns wrong/no results. The fix:

  1. site:facebook.com search on the business name (several query variants)
  2. Validate each candidate FB URL: page slug must match distinctive
     business tokens AND the result title (FB page display name) must too
     (rejects wrong-state lookalikes).
  3. Store the EXACT fb_page_url AND fb_page_id on the agency row. When we
     have the numeric page ID (from /pages/Name-123456 URLs), the dashboard
     "check ads" link uses &view_all_page_id=<id> — the EXACT page, no name
     guessing at all.
  4. Website crawl first when website is known (best-quality source).

Usage:
  .venv/bin/python -m pipeline.fb_find --city phoenix --limit 30
  .venv/bin/python -m pipeline.fb_find --state TX --limit 200
"""
import argparse
import re
import sys
import time
import urllib.parse

from . import get_conn, normalize_name
from .resolve import http_get, extract_fb_links

# URL shapes we accept as "a business page"
PAGE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|web\.)?facebook\.com/"
    r"(?:"
    r"(?P<slug>[A-Za-z0-9_.\-]{3,60})/?$"              # /SomePageName
    r"|pages/category/[^/]+/[^/]+-(?P<pid>\d{6,})"
    r"|pages/(?P<cat>[^/]+)/(?P<name>[^/]+)-(?P<pid2>\d{6,})"
    r")", re.I)

BAD_SLUGS = {"pages", "pg", "groups", "events", "marketplace", "watch", "profile.php",
             "sharer", "share.php", "dialog", "plugins", "hashtag", "login",
             "policies", "privacy", "help", "reel", "reels", "photo", "story.php"}


def url_to_page_parts(url: str):
    """Extract (slug_or_name, full_url, page_id_or_None) from a facebook URL."""
    m = PAGE_URL_RE.match(url.rstrip("/"))
    if not m:
        return None
    slug = m.group("slug")
    if slug:
        if slug.lower() in BAD_SLUGS or slug.lower().startswith(("people/", "pg/")):
            return None
        # search results often collapse /pages/Name-123456 into /Name-123456 —
        # a trailing 9+ digit number IS the page ID
        m2 = re.match(r"^(.+?)-(\d{9,})$", slug)
        if m2:
            return m2.group(1), url.rstrip("/"), m2.group(2)
        return slug, url.rstrip("/"), None
    if m.group("name"):                          # /pages/.../Name-123456
        return m.group("name"), url.rstrip("/"), m.group("pid2") or m.group("pid")
    if m.group("pid"):                           # pages/category/... shape, no name
        return None, url.rstrip("/"), m.group("pid")
    return None


COMMON: set[str] = set()   # DB-derived overused name tokens, set at run start

GEO_TOKENS = {"arizona", "florida", "texas", "phoenix", "tucson", "mesa", "chandler",
              "scottsdale", "gilbert", "glendale", "tempe", "peoria", "surprise",
              "houston", "dallas", "austin", "sanantonio", "elpaso", "fortworth",
              "miami", "orlando", "tampa", "jacksonville", "naples", "tallahassee",
              "america", "american"}

# US state names + biggest cities — a candidate page naming one of these that
# is NOT the agency's own geo is a lookalike from somewhere else.
STATE_NAMES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado",
               "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
               "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
               "maine", "maryland", "massachusetts", "michigan", "minnesota",
               "mississippi", "missouri", "montana", "nebraska", "nevada",
               "hampshire", "jersey", "mexico", "york", "carolina", "dakota",
               "ohio", "oklahoma", "oregon", "pennsylvania", "island", "tennessee",
               "utah", "vermont", "virginia", "washington", "wisconsin", "wyoming"}
US_CITIES = {"brooklyn", "queens", "bronx", "chicago", "philadelphia", "denver",
             "seattle", "boston", "detroit", "nashville", "memphis", "portland",
             "louisville", "baltimore", "milwaukee", "albuquerque", "fresno",
             "sacramento", "atlanta", "omaha", "raleigh", "miami", "oakland",
             "minneapolis", "tulsa", "wichita", "arlington", "tampa", "orleans",
             "cleveland", "honolulu", "anaheim", "lexington", "stockton",
             "cincinnati", "riverside", "newark", "buffalo", "scottsdale", "glendale",
             "chandler", "gilbert", "tempe", "peoria", "mesa", "tucson", "vegas",
             "columbus", "indianapolis", "oklahoma", "florissant", "munster",
             "wayne", "yeadon", "bacoor"}
STATE_CODE_RE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|"
    r"MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|"
    r"WY|DC)\b")


STATE_FULL = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
              "CA": "california", "CO": "colorado", "CT": "connecticut",
              "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii",
              "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
              "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
              "MD": "maryland", "MA": "massachusetts", "MI": "michigan",
              "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
              "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "hampshire",
              "NJ": "jersey", "NM": "mexico", "NY": "york", "NC": "carolina",
              "ND": "dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
              "PA": "pennsylvania", "RI": "island", "SC": "carolina",
              "SD": "dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
              "VT": "vermont", "VA": "virginia", "WA": "washington",
              "WV": "virginia", "WI": "wisconsin", "WY": "wyoming", "DC": "columbia"}


def home_state_of(state: str | None) -> str | None:
    return (state or "").strip().upper() or None


def common_words(conn) -> set[str]:
    """Tokens appearing in >25 agencies' names can't identify one business
    ('choice', 'first', 'devoted'...). Those names are generic — geo proof
    becomes mandatory."""
    with conn.cursor() as cur:
        cur.execute("""SELECT word FROM (
                          SELECT regexp_split_to_table(
                                     regexp_replace(lower(org_name), '[^a-z0-9]+', ' ', 'g'),
                                     ' ') AS word,
                                 count(*)
                          FROM agencies GROUP BY 1 HAVING count(*) > 25) t
                      WHERE length(word) >= 4""")
        return {w for (w,) in cur.fetchall()}
        return {w for (w,) in cur.fetchall()}


def title_matches_business(title: str, tokens: list[str]) -> bool:
    """The search-result title (FB page display name) must contain most of
    the business's distinctive tokens. Rejects lookalikes from other states."""
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    if not tokens:
        return False
    hits = sum(1 for tok in tokens if tok in t)
    return hits >= max(1, int(len(tokens) * 0.5))


def mentions_other_geo(*texts: str, home_geo: set[str] | None = None) -> bool:
    """True if any text names a US state/city that is NOT the agency's own
    geo. Kills cross-state lookalikes ('Acts of Kindness | Yeadon PA' for a
    Houston business). home_geo = geo tokens of THIS agency (may be empty)."""
    home_geo = home_geo or set()
    blob = " ".join(re.sub(r"[^a-z0-9 ]", " ", (x or "").lower()) for x in texts)
    words = set(blob.split())
    return bool(words & (GEO_TOKENS - home_geo))


def slug_matches_business(slug: str, tokens: list[str]) -> bool:
    """The FB slug must share at least one distinctive token (len>=4) with
    the business name."""
    s = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    if not s:
        return False
    for t in tokens:
        if len(t) >= 4 and t in s:
            return True
    return False


def mentions_geo(*texts: str) -> bool:
    """True if any text mentions a known geo token — used to CONFIRM loose
    matches (unquoted query) have a real local presence."""
    blob = " ".join(re.sub(r"[^a-z0-9 ]", " ", (x or "").lower()) for x in texts)
    return bool(set(blob.split()) & GEO_TOKENS)


def title_geo_conflict(title: str | None, slug: str | None,
                       state: str | None, city: str | None) -> bool:
    """Reject candidates whose page title/slug names a DIFFERENT US state or
    a different major city. FB titles look like 'Page Name | Brooklyn NY'.
    City check only fires when the agency has its own city to compare."""
    t = " ".join(re.sub(r"[^a-z0-9 ]", " ", (x or "").lower()) for x in (title, slug))
    if not t:
        return False
    words = set(t.split())
    st = home_state_of(state)
    home = {st.lower()} | {STATE_FULL.get(st, "")} if st else set()
    if st and (words & home):
        return False                       # right state — can't conflict
    if st and (words & (STATE_NAMES - home)):
        return True
    if st and STATE_CODE_RE.search((title or "")):
        codes = {m.group(1) for m in STATE_CODE_RE.finditer(title or "")}
        if st not in codes:
            return True                    # '... | Brooklyn NY' vs TX agency
    if city:
        for c in US_CITIES:
            if c in words and c != city.lower().replace(" ", ""):
                return True
    return False


# Engine rotation: each engine has its own rate budget. Track cooldowns so
# burned engines rest while healthy ones carry the load.
ENGINE_POOL = ["bing", "duckduckgo", "brave", "mojeek", "yahoo", "google"]
_engine_cooldown: dict[str, float] = {}      # engine -> ready-after timestamp
_ctrl_trusted: dict[str, float] = {}         # engine -> zeros trusted until
CONTROL_QUERY = '"home health" site:facebook.com'   # must return hits if honest
_engine_lock = __import__("threading").Lock()


def _engine_order() -> list[str]:
    """Engines not in cooldown, healthiest first."""
    now = time.time()
    with _engine_lock:
        alive = [e for e in ENGINE_POOL if _engine_cooldown.get(e, 0) <= now]
        resting = [e for e in ENGINE_POOL if e not in alive]
    return alive + resting


def _engine_search(eng: str, query: str) -> list[dict]:
    """One query on one engine; raises on failures (incl. empty-result
    exception used by ddgs when an engine returns nothing)."""
    from ddgs import DDGS
    with DDGS() as d:
        return [r for r in d.text(query, backend=eng, max_results=6)
                if r.get("href")]


def _cooldown(eng: str, secs: float = 300):
    with _engine_lock:
        _engine_cooldown[eng] = time.time() + secs


def _ddg_results(query: str) -> tuple[list[dict], bool]:
    """Search via the first engine that answers honestly.
    Returns (results, answered). answered=True ⇒ a live engine genuinely
    processed the query and its 'no results' is REAL (safe to record as
    'agency has no FB page'). Lie detection: an engine that returns zero
    must first pass a CONTROL query with known hits — silent soft-blocks
    (throttling that looks like zero results) fail the control, get
    cooldown, and we rotate. ConnectErrors → cooldown + rotate."""
    now = time.time()
    for eng in _engine_order():
        try:
            results = _engine_search(eng, query)
            with _engine_lock:
                _ctrl_trusted[eng] = now + 600
            return results, True
        except Exception as e:
            if "No results" not in str(e):
                _cooldown(eng)                       # network / rate-limit
                continue
        # engine returned ZERO results — is it honest?
        if _ctrl_trusted.get(eng, 0) > now:
            return [], True                          # recently verified honest
        try:
            ctrl = _engine_search(eng, CONTROL_QUERY)
            with _engine_lock:
                _ctrl_trusted[eng] = now + 600
            if ctrl:
                return [], True                      # honest zero
        except Exception:
            pass
        _cooldown(eng, 600)                          # liar / soft-blocked
    return [], False


# Non-US signals for the FB title's location segment ('Name | Kuala Lumpur'):
# a US agency's page located abroad is a WRONG match, reject it.
FOREIGN_COUNTRIES = {"malaysia", "pakistan", "india", "australia", "canada",
                     "united kingdom", "uk", "england", "scotland", "wales",
                     "new zealand", "singapore", "philippines", "nigeria",
                     "kenya", "south africa", "uae", "dubai", "bangladesh",
                     "sri lanka", "nepal", "indonesia", "thailand", "vietnam",
                     "china", "japan", "germany", "france", "spain", "italy",
                     "brazil", "mexico", "jamaica", "ghana"}
FOREIGN_CITIES = {"kuala", "lampur", "islamabad", "lahore", "karachi", "delhi",
                  "mumbai", "bangalore", "chennai", "hyderabad", "toronto",
                  "vancouver", "montreal", "london", "manchester", "birmingham",
                  "leeds", "glasgow", "sydney", "melbourne", "brisbane",
                  "perth", "adelaide", "auckland", "wellington", "manila",
                  "cebu", "lagos", "nairobi", "johannesburg", "doha", "riyadh",
                  "dublin", "amsterdam", "singapore"}
NON_US_CODES = {"vic", "nsw", "qld", "wa", "sa", "tas", "nt", "on", "bc",
                "ab", "mb", "sk", "ns", "nb", "nl", "pe"}


def pipe_location_reject(title: str | None) -> bool:
    """FB search-result titles are 'Page Name | Location'. If the location
    segment names a foreign country/city (or a non-US state code), the page
    is NOT a US business — reject. (US locations pass: they're validated
    elsewhere against the agency's own state.)"""
    if not title or "|" not in title:
        return False
    seg = title.rsplit("|", 1)[1].lower()
    words = set(re.sub(r"[^a-z0-9 ]", " ", seg).split())
    if words & FOREIGN_COUNTRIES:
        return True
    if words & FOREIGN_CITIES:
        return True
    if words & NON_US_CODES:
        return True
    return False


def clean_page_title(title: str) -> str | None:
    """ddgs result title for an FB page is the page's DISPLAY name, e.g.
    '1st Choice Home Health Care - Home | Facebook' → strip the cruft.
    CRITICAL: also drop the '| Location' suffix — Ad Library keyword search
    must get the bare page name ('Adore Home Care Centre', NOT 'Adore Home
    Care Centre | Kuala Lumpur' which can never match)."""
    t = (title or "").strip()
    t = re.sub(r"\s*[-|–—]\s*Home\s*\|\s*Facebook.*$", "", t, flags=re.I)
    t = re.sub(r"\s*\|\s*Facebook.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[-–—]\s*Facebook.*$", "", t, flags=re.I)
    if "|" in t:
        t = t.rsplit("|", 1)[0]          # 'Name | City ST' → 'Name'
    t = t.strip(" -|–—")
    if not t or t.lower() in {"facebook", "log in or sign up", "facebook log in or sign up"}:
        return None
    return t[:80]


def label_from_slug(slug: str) -> str:
    """Human-ish search label from a URL slug: split camelCase + separators."""
    s = re.sub(r"[-_.]+", " ", slug)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_fb_for_agency(org: str, city: str | None, website: str | None,
                       tokens: list[str], geo: set[str] | None = None,
                       state: str | None = None, generic: bool = False
                       ) -> tuple[str | None, str | None, str | None, bool]:
    """Return (page_label, page_url, page_id_or_None, saw_results).
    page_label = FB page display name (best Ad Library search term).
    geo = this agency's own geo tokens; generic=True when the name is made of
    common words (no distinctive token) — then even quoted results must show
    the agency's own city/state, else they're lookalikes (Brooklyn '1st
    Choice' for a Houston '1st Choice').
    saw_results=True means at least one search query returned usable hits —
    False on a row with nothing found means the backend was likely
    rate-limiting us, so the row must NOT be recorded as 'no FB page'."""
    geo = {g.lower() for g in (geo or set())}
    # 1. website crawl (highest quality)
    if website:
        html = http_get(website, timeout=6)
        if html:
            for link in extract_fb_links(html):
                parts = url_to_page_parts(link)
                if parts and slug_matches_business(parts[0], tokens):
                    return label_from_slug(parts[0]), parts[1], parts[2], True
    # 2. site:facebook.com search — query variants, cheapest-first.
    #    quoted exact name (strict), then looser variants that catch pages
    #    whose FB name differs from the legal name — but those need a
    #    distinct token in the slug AND city/state confirmation.
    queries = [f'"{org}" site:facebook.com']
    if city:
        queries.append(f'"{org}" {city} site:facebook.com')
    queries.append(f"{org} site:facebook.com")            # unquoted
    if city:
        queries.append(f"{org} {city} facebook page")     # unquoted + city
    host = urllib.parse.urlparse(website or "").netloc.replace("www.", "")
    if host:
        queries.append(f"{host} site:facebook.com")       # find page via domain
    core = " ".join(tokens[:3]) if tokens else org
    if core and core.lower() != org.lower():
        queries.append(f'"{core}" home care site:facebook.com')
    seen_q = set()
    saw_results = False
    for q in queries:
        if q in seen_q:
            continue
        seen_q.add(q)
        # quoted-name queries pin the business by exact phrase; loose queries
        # need city/state proof. Generic names need geo proof even when quoted.
        strict = q.startswith('"') and not generic
        results, answered = _ddg_results(q)
        if answered:
            saw_results = True
        # pass 1: strict (slug + title); pass 2: page-ID URLs with title only
        best_pid_fallback = None
        for r in results:
            url = r.get("href", "")
            parts = url_to_page_parts(url)
            if not parts:
                continue
            slug, url, pid = parts
            if pid and slug is None:
                continue  # category-shape URL with no usable name
            title = clean_page_title(r.get("title", ""))
            title_ok = bool(title) and title_matches_business(title, tokens)
            # every candidate must not live in a different state/city
            if title and mentions_other_geo(title, slug or "", home_geo=geo):
                continue
            if title_geo_conflict(title, slug or "", state, city):
                continue
            if pipe_location_reject(r.get("title", "")):
                continue          # FB page located abroad → not our US agency
            if slug and slug_matches_business(slug, tokens) and title_ok \
                    and (strict or mentions_geo(title, slug or "", city)):
                return title or label_from_slug(slug), url, pid, True
            if pid and not best_pid_fallback and title_ok \
                    and (strict or mentions_geo(title, slug or "", city)):
                best_pid_fallback = (title or label_from_slug(slug or "page"), url, pid)
        if best_pid_fallback:
            return best_pid_fallback + (True,)
        time.sleep(1.0)
    return (None, None, None, saw_results)


def backfill_page_ids(conn) -> int:
    """Rows whose fb_page slug embeds a trailing page ID (search results often
    collapse /pages/Name-123456 into /Name-123456): split it out so the
    dashboard can use view_all_page_id links."""
    n = 0
    with conn.cursor() as cur:
        cur.execute("""SELECT id, fb_page_url FROM agencies
                      WHERE fb_page_id IS NULL AND fb_page_url ~ '-\\d{9,}$'""")
        for aid, url in cur.fetchall():
            parts = url_to_page_parts(url)
            if parts and parts[2]:
                cur.execute("UPDATE agencies SET fb_page = %s, fb_page_id = %s WHERE id = %s",
                            (parts[0], parts[2], aid))
                n += 1
    conn.commit()
    return n


def process_rows(rows, cur, conn, retries: int = 1):
    """Run discovery for claimed rows, stamp fb_checked_at on every one.
    Rows where every search query came back empty (rate-limited backend) are
    un-claimed again so a later pass retries them — a 'no FB page' verdict is
    only recorded when the backend actually answered us."""
    found = 0
    for i, (aid, org, city, state, website) in enumerate(rows, 1):
        try:
            words = normalize_name(org).split()
            tokens = [t for t in words if len(t) >= 4]
            stop = {"home", "health", "healthcare", "care", "services", "agency",
                    "inc", "llc", "the", "and", "for", "senior", "seniors"}
            geo = {t for t in tokens if t in GEO_TOKENS}
            geo.add((city or "").strip().lower().replace(" ", ""))
            distinctive = [t for t in tokens if t not in stop and t not in GEO_TOKENS
                           and t not in COMMON]
            generic = not distinctive
            toks = distinctive or [t for t in tokens if t not in stop
                                   and t not in GEO_TOKENS] or tokens
            slug, url, pid, saw = find_fb_for_agency(org, city, website, toks, geo,
                                                     state=state, generic=generic)
        except Exception as e:
            print(f"\n  row {aid} ({org[:30]}) errored: {e}", flush=True)
            slug = url = pid = None
            saw = False
        if slug or pid:
            cur.execute("""UPDATE agencies SET fb_page = %s, fb_page_url = %s,
                           fb_page_id = %s, fb_source = 'site_search',
                           fb_confidence = 'high', fb_checked_at = now(),
                           updated_at = now() WHERE id = %s""",
                        (slug, url, pid, aid))
            found += 1
        elif saw or retries <= 0:
            # backend answered and found nothing → honest 'no FB page'
            cur.execute("UPDATE agencies SET fb_checked_at = now() WHERE id = %s", (aid,))
        else:
            # nothing answered → likely rate-limited; give the row back
            cur.execute("UPDATE agencies SET fb_checked_at = NULL WHERE id = %s", (aid,))
        conn.commit()
        print(f"\r  {i}/{len(rows)} fb_found={found}", end="", flush=True)
    print()
    return found


def claim_batch(cur, limit: int, shard: str | None, state: str | None,
                city: str | None) -> list:
    """Atomically claim the next batch of unchecked rows. SKIP LOCKED lets
    several fb_find workers run at once without ever touching the same row.
    shard='i/N' additionally splits rows between workers (not required for
    safety, but keeps each worker on a stable slice)."""
    where = ["a2.fb_page_url IS NULL", "a2.fb_checked_at IS NULL"]
    params: list = []
    if state:
        where.append("a2.state = %s"); params.append(state.upper())
    if city:
        where.append("a2.city_norm = %s"); params.append(city.strip().lower())
    if shard:
        i, n = shard.split("/")
        # %% — psycopg2 paramstyle needs a literal % escaped
        where.append(f"a2.id %% {int(n)} = {int(i) - 1}")
    cur.execute(f"""UPDATE agencies a SET fb_checked_at = now()
                    FROM (SELECT id FROM agencies a2
                          WHERE {' AND '.join(where)}
                          ORDER BY a2.id LIMIT %s FOR UPDATE SKIP LOCKED) c
                    WHERE a.id = c.id
                    RETURNING a.id, a.org_name, a.city, a.state, a.website""",
                params + [limit])
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description="Discover exact FB page URLs per agency")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--state"); ap.add_argument("--city")
    ap.add_argument("--shard", help="worker id/total, e.g. 2/4 — safe parallel runs")
    args = ap.parse_args()

    conn = get_conn()
    total_found = 0
    try:
        global COMMON
        COMMON = common_words(conn)
        n_ids = backfill_page_ids(conn)
        if n_ids:
            print(f"backfilled {n_ids} page IDs from embedded-URL slugs")
        # rows found by older runs never got a checked stamp — mark them so
        # the progress numbers are honest (they won't be re-searched)
        with conn.cursor() as c0:
            c0.execute("""UPDATE agencies SET fb_checked_at = to_timestamp(0)
                          WHERE fb_checked_at IS NULL AND fb_page_url IS NOT NULL""")
            conn.commit()
        cur = conn.cursor()
        done = 0
        empty_passes = 0
        while done < args.limit:
            batch = claim_batch(cur, min(25, args.limit - done), args.shard,
                                args.state, args.city)
            if not batch:
                # this worker's shard is drained — but rate-limit give-backs
                # may refill it; wait a bit and retry before exiting
                empty_passes += 1
                if empty_passes >= 6:
                    break
                time.sleep(45)
                continue
            empty_passes = 0
            total_found += process_rows(batch, cur, conn)
            done += len(batch)
        print(f"Done. FB pages discovered: {total_found}/{done}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
