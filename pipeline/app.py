#!/usr/bin/env python3
"""Targeto Lead Pipeline — Phase 2 dashboard.

Local web UI over the Postgres lead database:
  /                 city browser with counts
  /leads            searchable/filterable lead list (tabs per ad status)
  /agency/<id>      agency detail: attempts history, notes, taxonomies
  /export.csv       export exactly the current filter to CSV
  /api/...          status / notes / attempts / saved-views endpoints

Run:  python -m pipeline.app   → http://localhost:8010
"""
import csv
import io
import json
import math
import os
import re
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from . import get_conn
from .auth import (SESSION_COOKIE, current_user, ensure_admin, hash_password,
                   require_admin, require_login, verify_password)
from .email_harvest import GENERIC_LOCAL, is_personal, name_match_strength

app = Flask(__name__)
app.secret_key = os.environ.get("TARGETO_SECRET_KEY", "dev-only-change-me-9f8e7d6c")


@app.context_processor
def _inject_user():
    """base.html nav renders the logged-in user (Team tab, name, logout)."""
    n_due = 0
    try:
        n_due = q("SELECT count(*) FROM agencies WHERE dnc IS NOT TRUE AND follow_up_at IS NOT NULL AND follow_up_at <= now()", one=True)[0]
    except Exception:
        pass
    u = current_user()
    return {"current_user": current_user, "followups_due": n_due,
            "email_kinds": EMAIL_KINDS,
            "email_default_id": _default_template_id(),
            "email_templates": _email_templates_list(),
            "me": u["username"] if u else ""}


def _email_templates_list():
    try:
        return q("SELECT id, name, kind, is_default FROM email_templates ORDER BY is_default DESC, name")
    except Exception:
        return []


def _default_template_id():
    try:
        r = q("SELECT id FROM email_templates WHERE is_default ORDER BY id LIMIT 1", one=True)
        return r[0] if r else None
    except Exception:
        return None


def log_action(action, agency_id=None, contact_id=None, detail=None):
    """Attribute every work action to the logged-in user (team analytics)."""
    u = current_user()
    if not u:
        return
    ex("INSERT INTO activity (user_id, agency_id, contact_id, action, detail) VALUES (%s,%s,%s,%s,%s)",
       (u["id"], agency_id, contact_id, action, detail))


def apply_followup_status(aid, status, keep=False):
    """Schedule/clear the follow-up resurfacing date per FOLLOWUP_RULES.
    keep=True keeps any explicitly-set date (Call Back Later / manual)."""
    if keep:
        return
    days = FOLLOWUP_RULES.get(status, 0)
    if days is None:
        ex("UPDATE agencies SET follow_up_at = NULL WHERE id = %s", (aid,))
    elif days:
        ex("UPDATE agencies SET follow_up_at = now() + (%s || ' days')::interval WHERE id = %s", (days, aid))


def set_dnc(aid, flag):
    ex("UPDATE agencies SET dnc = %s, updated_at = now() WHERE id = %s", (bool(flag), aid))
    if flag:
        ex("UPDATE agencies SET follow_up_at = NULL WHERE id = %s", (aid,))
        log_action("dnc", agency_id=aid, detail="do-not-call")
    else:
        log_action("undnc", agency_id=aid, detail="dnc cleared")

PAGE_SIZES = [25, 50, 100, 250]
STATUSES = ["New", "Email Sent", "Call Tried", "No Pickup", "Voicemail",
            "Picked - Interested", "Picked - Not Interested", "Call Back Later", "Won", "Lost"]

# Rule-driven follow-up engine: each status change auto-schedules when the
# lead should resurface in /followups. NULL = never resurface.
FOLLOWUP_RULES = {
    "New":                   None,
    "Email Sent":            3,    # chase unanswered emails
    "Call Tried":            2,    # no real conversation yet — retry soon
    "No Pickup":             2,
    "Voicemail":             3,
    "Picked - Interested":   1,    # hottest leads resurface fastest
    "Picked - Not Interested": None,
    "Call Back Later":       None, # caller sets an exact date via the API
    "Won":                   None,
    "Lost":                  None,
}
AD_TABS = {
    "all":       (None, "All"),
    "no-ads":    ("NO_ADS", "🔴 Not Running Ads"),
    "running":   ("RUNNING", "🟢 Running Ads"),
    "ran-before":("RAN_BEFORE", "🟡 Ran Before"),
    "no-page":   ("NO_FB_PAGE", "⚫ No FB Page"),
    "unresolved":("UNRESOLVED", "❓ Unresolved"),
}
# Outreach email template categories — pick by what happened on the call
EMAIL_KINDS = {"no_pickup": "Didn't pick up", "picked": "Picked the call", "interested": "Interested"}
SORTS = {
    "org": "org_name", "city": "city", "state": "state, city",
    "status": "outreach_status", "updated": "updated_at DESC", "enum": "enumeration_date DESC NULLS LAST",
}

# ---------------------------------------------------------------- helpers

def q(sql, args=None, one=False):
    import psycopg2.extras
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.NamedTupleCursor)
        cur.execute(sql, args or ())
        return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()

def ex(sql, args=None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, args or ())
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

def miles_to_deg(miles):
    return (miles / 69.0, miles / 55.0)


def is_generic_email(email):
    """Role inbox (info@/sales@/hello@…) — Saroj's no-go list."""
    if not email:
        return False
    local = email.split("@")[0].lower().replace("_", "-")
    if local in GENERIC_LOCAL:
        return True
    return any(local.startswith(g + ".") or local.startswith(g + "-")
               for g in GENERIC_LOCAL)


def personal_ids(rows):
    """Row ids whose stored email carries the owner's own name (👤 badge).
    Everything else — info@/sales@ AND guessed-but-unverified addresses —
    renders as 🏢 so Saroj knows it is not the person yet."""
    return {r.id for r in rows if r.email and is_personal(
        r.email, (r.owner_first_name or "").lower(), (r.owner_last_name or "").lower())}


def owner_search_urls(row):
    """One-click 'find this person's personal email' links: LinkedIn + Google.
    Query = owner name + agency name (+ city) — exactly what Apollo/LinkedIn
    need to surface the human behind the business."""
    owner = " ".join(x for x in ((row.owner_first_name or "").strip(),
                                 (row.owner_last_name or "").strip()) if x)
    if not owner:
        return None
    q = f"{owner} {row.org_name}" + (f" {row.city}" if row.city else "")
    q = q.strip()
    return {
        "linkedin": "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote_plus(q),
        "google": "https://www.google.com/search?q=" + urllib.parse.quote_plus(q + " email"),
    }


def build_filters(args):
    """Translate query params → (where_sql, params, human label bits)."""
    where, params = ["1=1"], []
    if args.get("q"):
        like = f"%{args['q'].strip()}%"
        where.append(
            "(org_name ILIKE %s OR org_name_norm ILIKE %s"
            " OR (owner_first_name || ' ' || owner_last_name) ILIKE %s"
            " OR regexp_replace(coalesce(phone,'') || coalesce(owner_phone,''), '\\D', '', 'g') ILIKE %s)"
        )
        digits = "".join(c for c in args["q"] if c.isdigit())
        params += [like, like, like, f"%{digits}%" if digits else "\x00none\x00"]
    if args.get("state"):
        where.append("state = %s"); params.append(args["state"].upper())
    if args.get("city"):
        where.append("city_norm = %s"); params.append(args["city"].strip().lower())
    zipv = (args.get("zip") or "").strip()
    if zipv:
        if len(zipv) == 5 and zipv.isdigit():
            where.append("postal_code = %s"); params.append(zipv)
        elif zipv.isdigit():
            where.append("postal_code LIKE %s"); params.append(zipv + "%")
    if args.get("radius_zip") and args.get("radius_mi"):
        try:
            mi = float(args["radius_mi"])
            row = q("SELECT lat, lon FROM zip_centroids WHERE zip = %s", (args["radius_zip"].strip(),), one=True)
            if row and mi > 0:
                dlat, dlon = miles_to_deg(mi)
                where.append("postal_code IN (SELECT zip FROM zip_centroids WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s)")
                params += [float(row[0]) - dlat, float(row[0]) + dlat, float(row[1]) - dlon, float(row[1]) + dlon]
        except ValueError:
            pass
    if args.get("ad_status"):
        where.append("ad_status = %s"); params.append(args["ad_status"])
    if args.get("outreach"):
        where.append("outreach_status = %s"); params.append(args["outreach"])
    if args.get("dnc") == "only":
        where.append("dnc IS TRUE")
    else:
        where.append("dnc IS NOT TRUE")  # DNC leads are hidden from every normal view
    # Muse live-verification filter (independent of the pipeline's own detection)
    if args.get("muse") == "verified":
        where.append("muse_ad_checked_at IS NOT NULL")
    elif args.get("muse") == "disagree":
        where.append("muse_ad_checked_at IS NOT NULL AND muse_ad_status IS DISTINCT FROM ad_status")
    elif args.get("muse") == "unchecked":
        where.append("muse_ad_checked_at IS NULL")
    if args.get("has") == "phone":
        where.append("coalesce(phone, owner_phone) IS NOT NULL")
    elif args.get("has") == "website":
        where.append("website IS NOT NULL")
    elif args.get("has") == "fb":
        where.append("fb_page_url IS NOT NULL")
    elif args.get("has") == "nofb":
        where.append("fb_page_url IS NULL")
    elif args.get("has") == "owner":
        where.append("owner_last_name IS NOT NULL")
    elif args.get("has") == "email":
        where.append("email IS NOT NULL")
    elif args.get("has") == "noemail":
        where.append("email IS NULL")
    elif args.get("has") == "personal":
        where.append("""email IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM unnest(%s::text[]) g(x)
            WHERE split_part(email, '@', 1) = lower(g.x)
               OR split_part(email, '@', 1) LIKE lower(g.x) || '.%%'
               OR split_part(email, '@', 1) LIKE lower(g.x) || '-%%'
               OR split_part(email, '@', 1) LIKE lower(g.x) || '_%%')""")
        params.append(list(GENERIC_LOCAL))
    elif args.get("has") == "company":
        where.append("""email IS NOT NULL AND EXISTS (
            SELECT 1 FROM unnest(%s::text[]) g(x)
            WHERE split_part(email, '@', 1) = lower(g.x)
               OR split_part(email, '@', 1) LIKE lower(g.x) || '.%%'
               OR split_part(email, '@', 1) LIKE lower(g.x) || '-%%'
               OR split_part(email, '@', 1) LIKE lower(g.x) || '_%%')""")
        params.append(list(GENERIC_LOCAL))
    if args.get("title"):
        where.append("owner_title ILIKE %s"); params.append(f"%{args['title']}%")
    if args.get("tax"):
        where.append("id IN (SELECT agency_id FROM agency_taxonomies WHERE description ILIKE %s)")
        params.append(f"%{args['tax']}%")
    if args.get("enum_from"):
        where.append("enumeration_date >= %s"); params.append(args["enum_from"])
    if args.get("enum_to"):
        where.append("enumeration_date <= %s"); params.append(args["enum_to"])
    return " AND ".join(where), params

# ---------------------------------------------------------------- routes

@app.route("/")
@require_login
def index():
    state = request.args.get("state")
    states = q("SELECT state, count(*) FROM agencies WHERE state IS NOT NULL GROUP BY state ORDER BY count(*) DESC")
    cities = q(
        """SELECT state, city, count(*) AS total,
                  count(*) FILTER (WHERE ad_status='RUNNING') AS running,
                  count(*) FILTER (WHERE ad_status IN ('NO_ADS','NO_FB_PAGE','RAN_BEFORE')) AS not_running,
                  count(*) FILTER (WHERE ad_status='UNRESOLVED') AS unresolved,
                  count(*) FILTER (WHERE outreach_status='New') AS untouched
           FROM agencies WHERE city IS NOT NULL AND (%s IS NULL OR state = %s)
           GROUP BY state, city ORDER BY total DESC LIMIT 400""",
        (state, state),
    )
    totals = q("""SELECT count(*) total,
                         count(*) FILTER (WHERE ad_status='RUNNING') running,
                         count(*) FILTER (WHERE ad_status IN ('NO_ADS','NO_FB_PAGE','RAN_BEFORE')) not_running,
                         count(*) FILTER (WHERE ad_status='UNRESOLVED') unresolved
                  FROM agencies WHERE (%s IS NULL OR state = %s)""", (state, state), one=True)
    return render_template("index.html", states=states, cities=cities, totals=totals,
                           sel_state=state, PAGE_SIZES=PAGE_SIZES)

def adlib_query(fb_page: str | None, org_name: str) -> str:
    """Best search term for the Ad Library: the FB page slug from the exact
    page URL (minus trailing page-ID), falling back to the NPI legal name."""
    if fb_page:
        slug = re.sub(r"-\d{6,}$", "", fb_page)
        return slug.replace("-", " ")
    return org_name


def e164(num: str | None) -> str | None:
    """US numbers for paste-into-CloudTalk: +1 followed by 10 digits.
    Falls back to the raw value when the number isn't recognizable."""
    if not num:
        return None
    d = re.sub(r"\D", "", num)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None


TZ_MAP = {"FL": "America/New_York", "TX": "America/Chicago", "AZ": "America/Phoenix"}
TZ_NAMES = {"America/New_York": "Eastern", "America/Chicago": "Central",
            "America/Phoenix": "Mountain (no DST)", "America/Denver": "Mountain"}


def agency_tz(state: str | None):
    from zoneinfo import ZoneInfo
    tzname = TZ_MAP.get((state or "").upper())
    return ZoneInfo(tzname) if tzname else None


def us_time_parts(state: str | None):
    """('(9:41 AM', 'Eastern', pct) — live US local time at the agency, pct =
    that timezone's offset from IST in hours (India is 9.5-12.5 ahead of US).
    None when the state isn't in our 3-state coverage."""
    tz = agency_tz(state)
    if not tz:
        return None
    now = datetime.now(tz)
    label = TZ_NAMES.get(tz.key, tz.key)
    return now.strftime("%I:%M %p").lstrip("0"), label, now.utcoffset().total_seconds() / 3600


def adlib_url(fb_page: str | None, fb_page_id: str | None, org_name: str) -> str:
    """Build the Ad Library link. When we know the numeric FB page ID, use
    view_all_page_id=<id> — that targets the EXACT page (no name guessing).
    Otherwise fall back to name search with the FB page name, then NPI name."""
    base = "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&media_type=all"
    if fb_page_id:
        return f"{base}&view_all_page_id={urllib.parse.quote(fb_page_id)}"
    return f"{base}&q={urllib.parse.quote(adlib_query(fb_page, org_name))}"


# ------------------------------------------------------- email one-click

def render_email(tpl_subject: str, tpl_body: str, r) -> tuple[str, str]:
    """Fill a template's {{placeholders}} for agency row r.
    Supported: {{owner_name}} {{company}} {{city}} {{state}} {{targeto_user}}."""
    owner = " ".join(x for x in ((getattr(r, "owner_first_name", None) or "").strip(),
                                 (getattr(r, "owner_last_name", None) or "").strip()) if x)
    vals = {
        "owner_name": owner or "there",
        "company": r.org_name or "",
        "city": getattr(r, "city", None) or "",
        "state": getattr(r, "state", None) or "",
        "targeto_user": (current_user() or {}).get("username", ""),
    }
    def fill(s):
        out = s or ""
        for k, v in vals.items():
            out = out.replace("{{" + k + "}}", v)
        return out
    return fill(tpl_subject), fill(tpl_body)


def mailto_url(email: str, subject: str, body: str) -> str:
    """mailto: with RFC-6068 encoding — opens Gmail compose when the browser
    is logged into a single Google account (chrome://settings/handlers must
    allow mailto → Gmail)."""
    return ("mailto:" + urllib.parse.quote(email or "") +
            "?subject=" + urllib.parse.quote(subject) +
            "&body=" + urllib.parse.quote(body))


# ------------------------------------------------------------- auth routes

@app.before_request
def _boot_admin_once():
    if not app.config.get("_admin_ok"):
        try:
            ensure_admin()
            app.config["_admin_ok"] = True
        except Exception:
            pass  # DB not up yet; will retry on next request


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    nxt = request.values.get("next") or "/leads"
    if not nxt.startswith("/"):
        nxt = "/leads"
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, password_hash, is_admin FROM users WHERE username = %s AND active", (username,))
                row = cur.fetchone()
        finally:
            conn.close()
        if row and verify_password(password, row[1]):
            session["uid"] = row[0]
            session.permanent = True
            return redirect(nxt)
        error = "Wrong username or password"
    return render_template("login.html", error=error, next=nxt)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ------------------------------------------------------------- contacts

@app.route("/contacts", methods=["GET", "POST"])
@require_login
def contacts():
    """Manually added contacts (Apollo / LinkedIn / website research).
    Free-text role; linked to an agency when one matches. Repeat-call
    history lives here so nothing vanishes."""
    if request.method == "POST":
        f = request.form
        agency_id = (f.get("agency_id") or "").strip()
        u = current_user()
        ex("""INSERT INTO contacts (agency_id, company_name, first_name, last_name,
                                  role, phone, email, linkedin_url, notes, source, created_by)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (int(agency_id) if agency_id.isdigit() else None,
             (f.get("company_name") or "").strip() or None,
             (f.get("first_name") or "").strip(),
             (f.get("last_name") or "").strip() or None,
             (f.get("role") or "").strip() or None,
             (f.get("phone") or "").strip() or None,
             (f.get("email") or "").strip() or None,
             (f.get("linkedin_url") or "").strip() or None,
             (f.get("notes") or "").strip() or None,
             (f.get("source") or "manual").strip(),
             u["id"] if u else None))
        log_action("contact_added", contact_id=None,
                   detail=f"{f.get('first_name','')} {f.get('role','')} @ {f.get('company_name','')}")
        return redirect("/contacts")
    qstr = (request.args.get("q") or "").strip()
    st = request.args.get("status") or ""
    where, params = ["1=1"], []
    if qstr:
        like = f"%{qstr}%"
        where.append("(c.first_name ILIKE %s OR c.last_name ILIKE %s OR c.company_name ILIKE %s"
                     " OR c.role ILIKE %s OR c.email ILIKE %s OR c.phone ILIKE %s)")
        params += [like, like, like, like, like, like]
    if st:
        where.append("c.outreach_status = %s"); params.append(st)
    rows = q(f"""SELECT c.id, c.agency_id, c.company_name, c.first_name, c.last_name,
                        c.role, c.phone, c.email, c.linkedin_url, c.notes,
                        c.outreach_status, c.source, c.created_at, c.updated_at,
                        a.org_name AS agency_name, a.dnc AS agency_dnc
                 FROM contacts c LEFT JOIN agencies a ON a.id = c.agency_id
                 WHERE {' AND '.join(where)}
                 ORDER BY c.updated_at DESC LIMIT 500""", params)
    cstats = q("""SELECT outreach_status, count(*) FROM contacts
                  GROUP BY outreach_status""")
    cstat_map = {s: n for s, n in cstats}
    return render_template("contacts.html", rows=rows, statuses=STATUSES,
                           sel=request.args, cstat_map=cstat_map,
                           total=len(rows))


@app.route("/api/contact/<int:cid>/status", methods=["POST"])
@require_login
def contact_status(cid):
    status = request.get_json(force=True).get("status")
    if status not in STATUSES:
        return jsonify(ok=False, error="bad status"), 400
    ex("UPDATE contacts SET outreach_status = %s, updated_at = now() WHERE id = %s",
       (status, cid))
    return jsonify(ok=True, status=status)


@app.route("/api/contact/<int:cid>/notes", methods=["POST"])
@require_login
def contact_notes(cid):
    ex("UPDATE contacts SET notes = %s, updated_at = now() WHERE id = %s",
       ((request.get_json(force=True).get("notes") or "").strip(), cid))
    return jsonify(ok=True)


@app.route("/api/contact/<int:cid>/delete", methods=["POST"])
@require_login
def contact_delete(cid):
    ex("DELETE FROM contacts WHERE id = %s", (cid,))
    return jsonify(ok=True)


@app.route("/progress")
@require_login
def progress():
    """Live view of the static-DB build: discovery coverage + ETA."""
    overall = q("""SELECT count(*) total,
                          count(*) FILTER (WHERE fb_checked_at IS NOT NULL) checked,
                          count(*) FILTER (WHERE fb_page_url IS NOT NULL) found
                   FROM agencies""", one=True)
    per_state = q("""SELECT state, count(*) total,
                            count(*) FILTER (WHERE fb_checked_at IS NOT NULL) checked,
                            count(*) FILTER (WHERE fb_page_url IS NOT NULL) found
                     FROM agencies WHERE state IS NOT NULL
                     GROUP BY state ORDER BY count(*) DESC""")
    checked_15m = q("""SELECT count(*) FROM agencies
                       WHERE fb_checked_at > now() - interval '15 minutes'""", one=True)[0]
    per_hour = int(checked_15m * 4)
    remaining = overall[0] - overall[1]
    eta_min = int(remaining * 60 / per_hour) if per_hour > 0 else None
    return render_template("progress.html", overall=overall, per_state=per_state,
                           per_hour=per_hour, eta_min=eta_min, remaining=remaining)


@app.route("/emails")
@require_login
def emails_progress():
    """Live tracking of the email-indexing chain: website resolution fleet +
    harvester — funnel, per-state coverage, live speed, ETA, latest finds."""
    overall = q("""SELECT count(*) AS total,
                          count(*) FILTER (WHERE website IS NOT NULL) AS sites,
                          count(*) FILTER (WHERE email IS NOT NULL) AS emailed,
                          count(*) FILTER (WHERE email_source = 'website') AS src_website,
                          count(*) FILTER (WHERE email_source = 'manual') AS src_manual
                   FROM agencies""", one=True)
    per_state = q("""SELECT state, count(*) AS total,
                             count(*) FILTER (WHERE website IS NOT NULL) AS sites,
                             count(*) FILTER (WHERE email IS NOT NULL) AS emails
                      FROM agencies WHERE state IS NOT NULL
                      GROUP BY state ORDER BY count(*) DESC""")
    sites_15m = q("""SELECT count(*) FROM agencies
                     WHERE website IS NOT NULL AND updated_at > now() - interval '15 minutes'""", one=True)[0]
    emails_15m = q("""SELECT count(*) FROM agencies
                      WHERE email IS NOT NULL AND updated_at > now() - interval '15 minutes'""", one=True)[0]
    recent = q("""SELECT id, org_name, city, state, email, email_source, updated_at,
                        owner_first_name, owner_last_name
                  FROM agencies WHERE email IS NOT NULL
                  ORDER BY updated_at DESC LIMIT 20""")
    personal_map = {r.id: is_personal(r.email, (r.owner_first_name or "").lower(),
                                      (r.owner_last_name or "").lower()) for r in recent}
    all_email_rows = q("""SELECT email, owner_first_name, owner_last_name FROM agencies
                          WHERE email IS NOT NULL""")
    n_personal = sum(1 for r in all_email_rows if is_personal(
        r.email, (r.owner_first_name or "").lower(), (r.owner_last_name or "").lower()))
    guess_stats = q("""SELECT count(*) AS cands, count(DISTINCT agency_id) AS agencies
                       FROM email_guesses WHERE status = 'candidate'""", one=True)
    remaining = overall.total - overall.sites
    sites_per_hour = sites_15m * 4
    eta_h = round(remaining / sites_per_hour) if sites_per_hour >= 5 else None
    fleet_alive = sites_15m > 0
    return render_template("emails.html", overall=overall, per_state=per_state,
                           sites_15m=sites_15m, emails_15m=emails_15m,
                           recent=recent, remaining=remaining,
                           sites_per_hour=sites_per_hour, eta_h=eta_h,
                           fleet_alive=fleet_alive, n_personal=n_personal,
                           personal_map=personal_map, guess_stats=guess_stats)


# ------------------------------------------------------------- apollo hub

def apollo_worklist():
    """Agencies worth an Apollo lookup, best first: no email yet, has owner,
    has phone, not DNC. Row numbers feed the match back by NPI."""
    return q("""SELECT row_number() OVER (ORDER BY id) AS rn, a.id, a.npi,
                        a.org_name, a.city, a.state,
                        trim(a.owner_first_name || ' ' || a.owner_last_name) AS owner_name,
                        a.owner_title, a.website
                 FROM agencies a
                 WHERE a.dnc IS NOT TRUE AND a.email IS NULL AND a.owner_last_name IS NOT NULL
                   AND coalesce(a.phone, a.owner_phone) IS NOT NULL
                 ORDER BY a.id""")


def _apollo_overall():
    return q("""SELECT count(*) FILTER (WHERE email IS NOT NULL) AS emailed,
                       count(*) FILTER (WHERE email IS NOT NULL AND email_source='apollo') AS apollo,
                       count(*) FILTER (WHERE email_source='apollo' AND owner_title IS NOT NULL) AS with_title
                FROM agencies""", one=True)


@app.route("/import")
@require_login
def import_page():
    return render_template("apollo.html", rows=apollo_worklist(),
                           overall=_apollo_overall(), tokens=_tokens_overall(),
                           recent=_recent_syncs(),
                           me=(current_user() or {}).get("username", ""))


def _tokens_overall():
    try:
        return q("SELECT count(*) AS n, coalesce(sum(monthly_cap),0) AS cap, "
                 "coalesce(sum(credits_used),0) AS used FROM apollo_tokens WHERE active", one=True)
    except Exception:
        return {"n": 0, "cap": 0, "used": 0}


def _recent_syncs():
    try:
        return q("""SELECT au.id, t.label, au.agency_id, a.org_name,
                           au.email_found, au.title, au.created_at
                    FROM apollo_usage au
                    LEFT JOIN apollo_tokens t ON t.id = au.token_id
                    LEFT JOIN agencies a ON a.id = au.agency_id
                    ORDER BY au.id DESC LIMIT 15""")
    except Exception:
        return []


@app.route("/import/tokens", methods=["POST"])
@require_admin
def import_tokens():
    """Add one or more Apollo API tokens (comma/newline separated), or
    toggle/delete an existing one. Tokens are secrets — stored, never displayed."""
    f = request.form
    act = f.get("act")
    if act == "toggle" and f.get("id", "").isdigit():
        ex("UPDATE apollo_tokens SET active = NOT active WHERE id = %s", (int(f["id"]),))
    elif act == "delete" and f.get("id", "").isdigit():
        ex("DELETE FROM apollo_tokens WHERE id = %s", (int(f["id"]),))
    else:
        raw = (f.get("token") or "").strip()
        parts = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
        try:
            cap = int(f.get("cap") or 60)
        except ValueError:
            cap = 60
        for p in parts:
            ex("INSERT INTO apollo_tokens (label, token, monthly_cap) VALUES (%s,%s,%s) "
               "ON CONFLICT (token) DO NOTHING",
               ((f.get("label") or "").strip() or None, p, cap))
    return redirect("/import")


@app.route("/import.csv")
@require_login
def import_csv():
    """Call-sheet for the Apollo extension session: #, NPI, company, city, state,
    owner, title, website. NPI is the match key when the CSV comes back."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "NPI", "Company", "City", "State", "Owner", "Title", "Website"])
    for r in apollo_worklist():
        w.writerow([r.rn, r.npi or "", r.org_name, r.city or "", r.state or "",
                    r.owner_name or "", r.owner_title or "", r.website or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=targeto_apollo_worklist.csv"})


@app.route("/import", methods=["POST"])
@require_login
def import_post():
    """Paste back the Apollo CSV (NPI + Email + Title columns, any order).
    Only NPI and Email are required; Title updates the owner_title when filled."""
    f = request.form
    if f.get("act") == "clear":
        ex("UPDATE agencies SET email = NULL, email_source = NULL "
           "WHERE email_source = 'apollo' AND updated_at > now() - interval '10 minutes'")
        return render_template("apollo.html", rows=apollo_worklist(),
                               overall=q("""SELECT count(*) FILTER (WHERE email IS NOT NULL) AS emailed,
                                                   count(*) FILTER (WHERE email IS NOT NULL AND email_source='apollo') AS apollo,
                                                   count(*) FILTER (WHERE email_source='apollo' AND owner_title IS NOT NULL) AS with_title
                                            FROM agencies""", one=True),
                               me=(current_user() or {}).get("username", ""),
                               msg="Last Apollo import cleared (10-min window).")
    text = (f.get("csv") or "").strip()
    if not text:
        return render_template("apollo.html", rows=apollo_worklist(),
                               overall=q("""SELECT count(*) FILTER (WHERE email IS NOT NULL) AS emailed,
                                                  count(*) FILTER (WHERE email IS NOT NULL AND email_source='apollo') AS apollo,
                                                  count(*) FILTER (WHERE email_source='apollo' AND owner_title IS NOT NULL) AS with_title
                                           FROM agencies""", one=True),
                               me=(current_user() or {}).get("username", ""),
                               msg="Nothing pasted.")
    rd = csv.reader(io.StringIO(text))
    header = [h.strip().lstrip("\ufeff").lower() for h in next(rd, [])]
    i_npi = next((i for i, h in enumerate(header) if h in ("npi", "npi number")), None)
    i_email = next((i for i, h in enumerate(header) if "email" in h), None)
    i_title = next((i for i, h in enumerate(header) if h in ("title", "job title", "position", "role")), None)
    i_name = next((i for i, h in enumerate(header) if h in ("name", "full name", "contact name")), None)
    if i_npi is None or i_email is None:
        return render_template("apollo.html", rows=apollo_worklist(),
                               overall=q("""SELECT count(*) FILTER (WHERE email IS NOT NULL) AS emailed,
                                                  count(*) FILTER (WHERE email IS NOT NULL AND email_source='apollo') AS apollo,
                                                  count(*) FILTER (WHERE email_source='apollo' AND owner_title IS NOT NULL) AS with_title
                                           FROM agencies""", one=True),
                               me=(current_user() or {}).get("username", ""),
                               msg="CSV needs 'NPI' and 'Email' columns (Title/Name optional).")
    u = current_user()
    updated = skipped_no_npi = skipped_bad_email = 0
    seen_npis = set()
    for row in rd:
        if not row or not any(c.strip() for c in row):
            continue
        get = lambda i: row[i].strip() if i is not None and i < len(row) and row[i].strip() else None
        npi, email = get(i_npi), get(i_email)
        if not npi or not email:
            skipped_no_npi += 1
            continue
        if "@" not in email or " " in email or len(email) > 120:
            skipped_bad_email += 1
            continue
        if npi in seen_npis:
            continue
        seen_npis.add(npi)
        title = get(i_title)
        contact_name = get(i_name)
        rc = ex("""UPDATE agencies SET email = %s, email_source = 'apollo',
                   owner_title = COALESCE(NULLIF(%s, ''), owner_title),
                   claimed_by = COALESCE(claimed_by, %s), updated_at = now()
                 WHERE npi = %s AND email IS NULL""",
                (email.lower(), title, u["id"], npi.strip()))
        if rc:
            updated += 1
        else:
            skipped_no_npi += 1  # NPI not found in DB, or agency already has an email
    overall = q("""SELECT count(*) FILTER (WHERE email IS NOT NULL) AS emailed,
                          count(*) FILTER (WHERE email IS NOT NULL AND email_source='apollo') AS apollo,
                          count(*) FILTER (WHERE email_source='apollo' AND owner_title IS NOT NULL) AS with_title
                   FROM agencies""", one=True)
    msg = f"Imported: {updated} matched · skipped {skipped_no_npi} rows without NPI/email, {skipped_bad_email} bad emails."
    return render_template("apollo.html", rows=apollo_worklist(), overall=overall, me=u["username"], msg=msg)


@app.route("/leads")
@require_login
def leads():
    tab = request.args.get("tab", "all")
    ad_status = AD_TABS.get(tab, AD_TABS["no-ads"])[0]
    args = request.args.to_dict()
    base_query = urlencode({k: v for k, v in args.items() if k not in ("tab", "page", "ad_status")})
    # FB toggle needs the current filters WITHOUT the previous has=fb/nofb,
    # otherwise switching buttons would stack contradictory has= params
    fb_query = urlencode({k: v for k, v in args.items()
                          if k not in ("tab", "page", "ad_status", "has")})
    if ad_status:
        args["ad_status"] = ad_status
    where, params = build_filters(args)
    sort = SORTS.get(request.args.get("sort", "org"), "org_name")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per = int(request.args.get("per", 50))
    except ValueError:
        per = 50
    per = per if per in PAGE_SIZES else 50
    total = q(f"SELECT count(*) FROM agencies WHERE {where}", params, one=True)[0]
    pages = max(1, math.ceil(total / per))
    page = min(page, pages)
    rows = q(
        f"""SELECT id, npi, org_name, city, state, postal_code, phone, owner_phone,
                   owner_first_name, owner_last_name, owner_title, website, fb_page,
                   fb_page_url, fb_page_id, ad_status, outreach_status, review_needed,
                   enumeration_date, updated_at, dnc, follow_up_at, email, email_source,
                   muse_fb_page, muse_fb_page_url, muse_ad_status, muse_ad_checked_at, muse_ad_notes
            FROM agencies WHERE {where}
            ORDER BY review_needed DESC, {sort}, org_name
            LIMIT %s OFFSET %s""",
        params + [per, (page - 1) * per],
    )
    adlib_urls = {r.id: adlib_url(r.fb_page, getattr(r, "fb_page_id", None), r.org_name) for r in rows}
    owner_urls = {r.id: owner_search_urls(r) for r in rows}
    generic_ids = {r.id for r in rows if is_generic_email(r.email)}
    personal_set = personal_ids(rows)
    phone_e164 = {r.id: e164(r.phone or r.owner_phone) for r in rows}
    counts = q("""SELECT ad_status, count(*) FROM agencies
                  WHERE (%s::text IS NULL OR state = %s) GROUP BY ad_status""",
               (request.args.get("state"), request.args.get("state")))
    cmap = {c: n for c, n in counts}
    saved_views = q("SELECT id, name FROM saved_views ORDER BY name")
    # FB toggle counts — same filters as the list, minus the FB toggle itself
    fb_args = {k: v for k, v in args.items() if k != "has"}
    where_fb, params_fb = build_filters(fb_args)
    fb_counts = q(f"""SELECT count(*) AS total,
                             count(*) FILTER (WHERE fb_page_url IS NOT NULL) AS has_fb,
                             count(*) FILTER (WHERE fb_page_url IS NULL) AS no_fb
                      FROM agencies WHERE {where_fb}""", params_fb, one=True)
    # Muse verification counts — same filters as the list
    muse_counts = q(f"""SELECT count(*) FILTER (WHERE muse_ad_checked_at IS NOT NULL) AS verified,
                               count(*) FILTER (WHERE muse_ad_checked_at IS NOT NULL
                                                 AND muse_ad_status IS DISTINCT FROM ad_status) AS disagree
                        FROM agencies WHERE {where_fb}""", params_fb, one=True)
    live_states = q("SELECT DISTINCT state FROM agencies WHERE state IN ('FL','TX','AZ')")
    local_time = {r[0]: us_time_parts(r[0]) for r in live_states if us_time_parts(r[0])}
    tpls = q("SELECT id, name, kind, is_default FROM email_templates ORDER BY is_default DESC, name")
    tpl_json = json.dumps([dict(t._mapping) if hasattr(t, "_mapping") else {"id": t[0], "name": t[1], "kind": t[2], "is_default": t[3]} for t in tpls])
    email_counts = q("""SELECT count(*) FILTER (WHERE email IS NOT NULL) AS with_email,
                               count(*) FILTER (WHERE email IS NOT NULL AND NOT EXISTS (
                                   SELECT 1 FROM unnest(%s::text[]) g(x)
                                   WHERE split_part(email, '@', 1) = lower(g.x)
                                      OR split_part(email, '@', 1) LIKE lower(g.x) || '.%%'
                                      OR split_part(email, '@', 1) LIKE lower(g.x) || '-%%'
                                      OR split_part(email, '@', 1) LIKE lower(g.x) || '_%%')) AS personal,
                               count(*) AS total
                        FROM agencies WHERE dnc IS NOT TRUE""",
                    (list(GENERIC_LOCAL),), one=True)
    return render_template(
        "leads.html", rows=rows, total=total, page=page, pages=pages, per=per,
        tab=tab, ad_tabs=AD_TABS, statuses=STATUSES, cmap=cmap,
        sel=args, PAGE_SIZES=PAGE_SIZES, page_sizes=PAGE_SIZES, saved_views=saved_views,
        base_query=base_query, adlib_urls=adlib_urls, fb_counts=fb_counts,
        muse_counts=muse_counts,
        fb_query=fb_query, phone_e164=phone_e164, owner_urls=owner_urls,
        generic_ids=generic_ids, personal_ids=personal_set,
        local_time=local_time, us_time=us_time_parts, now=datetime.now(timezone.utc),
        tpl_json=tpl_json, email_counts=email_counts, tpl_list=tpls,
    )

@app.route("/agency/<int:aid>", methods=["GET", "POST"])
@require_login
def agency(aid):
    if request.method == "POST":
        note = request.form.get("note", "").strip()
        if note:
            ex("UPDATE agencies SET notes = %s, updated_at = now() WHERE id = %s", (note, aid))
        return redirect(url_for("agency", aid=aid))
    row = q("SELECT * FROM agencies WHERE id = %s", (aid,), one=True)
    if not row:
        return "Not found", 404
    cols = [d[0] for d in q("SELECT * FROM agencies LIMIT 1").description] if False else None
    taxonomies = q("SELECT code, description, is_primary FROM agency_taxonomies WHERE agency_id = %s", (aid,))
    attempts = q("SELECT id, attempted_at, channel, outcome, note FROM attempts WHERE agency_id = %s ORDER BY attempted_at DESC", (aid,))
    checks = q("SELECT checked_at, result, page_name, ads_active, ads_archived FROM ad_checks WHERE agency_id = %s ORDER BY checked_at DESC LIMIT 10", (aid,))
    guesses = q("""SELECT email, pattern, rank, status FROM email_guesses
                   WHERE agency_id = %s AND status = 'candidate' ORDER BY rank""", (aid,))
    creatives = q("SELECT body, cta, media_url, ad_delivery_start_date FROM ad_creatives WHERE agency_id = %s ORDER BY fetched_at DESC LIMIT 10", (aid,))
    return render_template("agency.html", a=row, taxonomies=taxonomies, attempts=attempts,
                           checks=checks, creatives=creatives, statuses=STATUSES,
                           generic_email=is_generic_email(row.email), owner_urls=owner_search_urls(row),
                           personal_email=bool(row.email and is_personal(
                               row.email, (row.owner_first_name or "").lower(),
                               (row.owner_last_name or "").lower())),
                           guesses=guesses)

# ---------------------------------------------------------------- APIs

@app.route("/api/agency/<int:aid>/status", methods=["POST"])
@require_login
def set_status(aid):
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in STATUSES:
        return jsonify(ok=False, error="bad status"), 400
    ex("""UPDATE agencies SET outreach_status = %s,
           claimed_by = COALESCE(claimed_by, %s), updated_at = now() WHERE id = %s""",
       (status, current_user()["id"], aid))
    log_action("contacted", agency_id=aid, detail=status)
    apply_followup_status(aid, status, keep=bool(data.get("keep_followup")))
    return jsonify(ok=True, status=status)

@app.route("/api/agency/<int:aid>/notes", methods=["POST"])
@require_login
def set_notes(aid):
    data = request.get_json(force=True)
    ex("UPDATE agencies SET notes = %s, updated_at = now() WHERE id = %s", ((data.get("notes") or "").strip(), aid))
    return jsonify(ok=True)

@app.route("/api/agency/<int:aid>/review", methods=["POST"])
@require_login
def set_review(aid):
    data = request.get_json(force=True)
    ex("UPDATE agencies SET review_needed = %s, updated_at = now() WHERE id = %s", (bool(data.get("review")), aid))
    return jsonify(ok=True)


@app.route("/api/agency/<int:aid>/email", methods=["POST"])
@require_login
def set_email(aid):
    """Save/clear the decision-maker email on the agency (permanent storage —
    once found, never lost)."""
    val = ((request.get_json(force=True) or {}).get("email") or "").strip()
    if val and ("@" not in val or " " in val):
        return jsonify(ok=False, error="not a valid email"), 400
    ex("UPDATE agencies SET email = %s, updated_at = now() WHERE id = %s", (val or None, aid))
    if val:
        ex("""UPDATE email_guesses SET status = 'confirmed', confirmed_at = now()
               WHERE agency_id = %s AND lower(email) = lower(%s) AND status = 'candidate'""",
           (aid, val))
    return jsonify(ok=True)

@app.route("/api/agency/<int:aid>/guess", methods=["POST"])
@require_login
def guess_action(aid):
    """Confirm ✅ or reject ❌ a pattern guess. Confirm = write to agencies.email
    (source='manual', permanent) + mark the guess row; reject = park it."""
    data = request.get_json(force=True) or {}
    action, email = data.get("action"), (data.get("email") or "").strip().lower()
    if action not in ("confirm", "reject") or not email or "@" not in email:
        return jsonify(ok=False, error="bad request"), 400
    if action == "confirm":
        u = current_user()
        n = ex("""UPDATE agencies SET email = %s, email_source = 'manual',
                   claimed_by = COALESCE(claimed_by, %s), updated_at = now()
                   WHERE id = %s AND email IS NULL""", (email, u["id"], aid))
        if not n:  # already has an email — do not overwrite, refuse silently
            return jsonify(ok=False, error="agency already has an email"), 409
        ex("""UPDATE email_guesses SET status = 'confirmed', confirmed_at = now()
               WHERE agency_id = %s AND lower(email) = %s""", (aid, email))
        return jsonify(ok=True, email=email)
    ex("""UPDATE email_guesses SET status = 'rejected'
           WHERE agency_id = %s AND lower(email) = %s""", (aid, email))
    return jsonify(ok=True)

@app.route("/api/agency/<int:aid>/ad_status", methods=["POST"])
@require_login
def set_ad_status(aid):
    """Manual ad-status override after eyeballing the Ad Library link."""
    data = request.get_json(force=True)
    status = data.get("ad_status")
    allowed = {"RUNNING", "RAN_BEFORE", "NO_ADS", "NO_FB_PAGE", "UNRESOLVED"}
    if status not in allowed:
        return jsonify(ok=False, error="bad ad_status"), 400
    u = current_user()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE agencies SET ad_status = %s, ad_last_checked = now(),
                       claimed_by = COALESCE(claimed_by, %s), updated_at = now() WHERE id = %s""",
                   (status, u["id"], aid))
        cur.execute("""INSERT INTO ad_checks (agency_id, result, page_name, ads_active, ads_archived, raw)
                      VALUES (%s, %s, %s, NULL, NULL, %s)""",
                    (aid, status, data.get("page_name"), json.dumps("manual")))
        conn.commit()
    finally:
        conn.close()
    log_action("classified", agency_id=aid, detail=status)
    return jsonify(ok=True, ad_status=status)

@app.route("/api/agency/<int:aid>/attempt", methods=["POST"])
@require_login
def add_attempt(aid):
    data = request.get_json(force=True)
    channel = data.get("channel")
    if channel not in ("call", "email", "whatsapp", "linkedin", "other"):
        return jsonify(ok=False, error="bad channel"), 400
    u = current_user()
    ex("INSERT INTO attempts (agency_id, channel, outcome, note, user_id) VALUES (%s, %s, %s, %s, %s)",
       (aid, channel, (data.get("outcome") or "").strip(), (data.get("note") or "").strip(), u["id"]))
    ex("UPDATE agencies SET claimed_by = COALESCE(claimed_by, %s), updated_at = now() WHERE id = %s",
       (u["id"], aid))
    log_action(channel if channel in ("call", "email") else "contacted", agency_id=aid,
               detail=(data.get("outcome") or "").strip() or None)
    if data.get("status") in STATUSES:
        ex("UPDATE agencies SET outreach_status = %s, updated_at = now() WHERE id = %s", (data["status"], aid))
        apply_followup_status(aid, data["status"])
    return jsonify(ok=True)


@app.route("/api/agency/<int:aid>/dnc", methods=["POST"])
@require_login
def api_dnc(aid):
    """Do-Not-Call flag: hides the lead from every normal view and export,
    clears any pending follow-up. TCPAA safety net."""
    data = request.get_json(force=True)
    set_dnc(aid, bool(data.get("dnc")))
    return jsonify(ok=True, dnc=bool(data.get("dnc")))


@app.route("/api/agency/<int:aid>/followup", methods=["POST"])
@require_login
def api_followup(aid):
    """Explicit follow-up scheduling — 'call me back Thursday' support.
    Body: {"date": "YYYY-MM-DD"} or {"days": 3} or {"clear": true}."""
    data = request.get_json(force=True)
    if data.get("clear"):
        ex("UPDATE agencies SET follow_up_at = NULL, updated_at = now() WHERE id = %s", (aid,))
        return jsonify(ok=True, follow_up_at=None)
    when = None
    if data.get("date"):
        try:
            d = datetime.strptime(data["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            # 9am local US business hours ~ 13:30 UTC (ET); close enough for a queue
            when = d.replace(hour=13, minute=30)
        except ValueError:
            return jsonify(ok=False, error="bad date, use YYYY-MM-DD"), 400
    elif data.get("days") is not None:
        try:
            days = max(0, int(data["days"]))
        except ValueError:
            return jsonify(ok=False, error="bad days"), 400
        when = datetime.now(timezone.utc) + timedelta(days=days)
    else:
        return jsonify(ok=False, error="date/days/clear required"), 400
    ex("UPDATE agencies SET follow_up_at = %s, updated_at = now() WHERE id = %s", (when, aid))
    log_action("followup_set", agency_id=aid, detail=when.strftime("%d %b"))
    return jsonify(ok=True, follow_up_at=when.isoformat())


# ------------------------------------------------------------ follow-ups

@app.route("/followups")
@require_login
def followups():
    """Today's follow-ups: every non-DNC lead whose follow_up_at has come due,
    hottest first (overdue longest / most-recently-interested first)."""
    rows = q("""SELECT id, org_name, city, state, phone, owner_phone,
                       owner_first_name, owner_last_name, owner_title,
                       outreach_status, notes, follow_up_at, updated_at,
                       fb_page, fb_page_id, website, review_needed, email
                FROM agencies
                WHERE dnc IS NOT TRUE AND follow_up_at IS NOT NULL AND follow_up_at <= now()
                ORDER BY follow_up_at ASC
                LIMIT 200""")
    upcoming = q("""SELECT count(*) FROM agencies
                    WHERE dnc IS NOT TRUE AND follow_up_at > now()""", one=True)[0]
    # next 7 days preview
    soon = q("""SELECT follow_up_at::date AS day, count(*) AS n
                FROM agencies
                WHERE dnc IS NOT TRUE AND follow_up_at > now()
                  AND follow_up_at <= now() + interval '7 days'
                GROUP BY day ORDER BY day""")
    adlib_urls = {r.id: adlib_url(r.fb_page, getattr(r, "fb_page_id", None), r.org_name) for r in rows}
    owner_urls = {r.id: owner_search_urls(r) for r in rows}
    generic_ids = {r.id for r in rows if is_generic_email(r.email)}
    personal_set = personal_ids(rows)
    phone_e164 = {r.id: e164(r.phone or r.owner_phone) for r in rows}
    return render_template("followups.html", rows=rows, upcoming=upcoming, soon=soon,
                           statuses=STATUSES, adlib_urls=adlib_urls,
                           phone_e164=phone_e164, now_utc=datetime.now(timezone.utc),
                           owner_urls=owner_urls, generic_ids=generic_ids,
                           personal_ids=personal_set)


# ------------------------------------------------------- templates manager

@app.route("/templates", methods=["GET", "POST"])
@require_login
def templates_page():
    """Saroj writes email templates himself: subject + body, multiple versions
    per call outcome. Placeholders: {{owner_name}} {{company}} {{city}}
    {{state}} {{targeto_user}} — filled when a row's ✉ button opens Gmail."""
    msg = None
    u = current_user()
    if request.method == "POST":
        act = request.form.get("act")
        if act == "delete":
            tid = request.form.get("id", "")
            if tid.isdigit():
                ex("DELETE FROM email_templates WHERE id = %s", (int(tid),))
                msg = "Template deleted."
        elif act == "default":
            tid = request.form.get("id", "")
            if tid.isdigit():
                ex("UPDATE email_templates SET is_default = (id = %s)", (int(tid),))
                msg = "Default template updated — ✉ buttons use it unless you pick another."
        else:
            name = (request.form.get("name") or "").strip()
            kind = request.form.get("kind") or "no_pickup"
            subject = (request.form.get("subject") or "").strip()
            body = (request.form.get("body") or "").replace("\r\n", "\n").strip()
            tid = (request.form.get("id") or "").strip()
            if not (name and subject and body):
                msg = "Name, subject and body are all required."
            elif kind not in EMAIL_KINDS:
                msg = "Bad template kind."
            elif tid.isdigit():
                ex("""UPDATE email_templates SET name=%s, kind=%s, subject=%s, body=%s,
                       updated_at=now() WHERE id=%s""", (name, kind, subject, body, int(tid)))
                msg = f"Template '{name}' updated."
            else:
                ex("""INSERT INTO email_templates (name, kind, subject, body, created_by)
                     VALUES (%s,%s,%s,%s,%s)""", (name, kind, subject, body, u["id"]))
                if q("SELECT count(*) FROM email_templates WHERE is_default", one=True)[0] == 0:
                    ex("UPDATE email_templates SET is_default = TRUE WHERE id = (SELECT max(id) FROM email_templates)")
                    msg = f"Template '{name}' created and set as default."
                else:
                    msg = f"Template '{name}' created."
    rows = q("""SELECT id, name, kind, subject, body, is_default, created_at, updated_at
                FROM email_templates ORDER BY is_default DESC, kind, name""")
    return render_template("email_templates.html", rows=rows, msg=msg,
                           kinds=EMAIL_KINDS, me=u["username"])


@app.route("/api/email/<int:aid>")
@require_login
def email_for_agency(aid):
    """Recipient + rendered subject/body for the ✉ button. The browser builds
    the mailto: link and opens Gmail compose in a new tab."""
    r = q("SELECT id, org_name, city, state, owner_first_name, owner_last_name, email FROM agencies WHERE id = %s", (aid,), one=True)
    if not r:
        return jsonify(ok=False, error="not found"), 404
    if not r.email:
        return jsonify(ok=False, error="no email on file for this agency"), 400
    tpl_id = request.args.get("tpl")
    if tpl_id and tpl_id.isdigit():
        tpl = q("SELECT subject, body FROM email_templates WHERE id = %s", (int(tpl_id),), one=True)
    else:
        tpl = q("SELECT subject, body FROM email_templates ORDER BY is_default DESC, id LIMIT 1", one=True)
    if not tpl:
        return jsonify(ok=False, error="no template yet — create one on the Templates page"), 400
    subject, body = render_email(tpl[0], tpl[1], r)
    return jsonify(ok=True, to=r.email, subject=subject, body=body,
                   mailto=mailto_url(r.email, subject, body))


@app.route("/api/agency/<int:aid>/email_log", methods=["POST"])
@require_login
def email_log(aid):
    """Called after the ✉ compose tab opens: logs the attempt, attributes it,
    moves the lead to Email Sent and schedules its follow-up."""
    data = request.get_json(force=True) or {}
    u = current_user()
    ex("INSERT INTO attempts (agency_id, channel, outcome, note, user_id) VALUES (%s,'email',%s,%s,%s)",
       (aid, (data.get("outcome") or "sent").strip(), (data.get("note") or "").strip() or None, u["id"]))
    ex("UPDATE agencies SET outreach_status = 'Email Sent', claimed_by = COALESCE(claimed_by, %s), updated_at = now() WHERE id = %s",
       (u["id"], aid))
    log_action("email", agency_id=aid, detail="mailto one-click")
    apply_followup_status(aid, "Email Sent")
    return jsonify(ok=True)

@app.route("/api/views", methods=["GET", "POST"])
def views():
    if request.method == "GET":
        return jsonify([dict(zip(["id", "name"], r)) for r in q("SELECT id, name FROM saved_views ORDER BY name")])
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="name required"), 400
    ex("""INSERT INTO saved_views (name, filter_json) VALUES (%s, %s)
          ON CONFLICT (name) DO UPDATE SET filter_json = EXCLUDED.filter_json""",
       (name, json.dumps(request.args.to_dict())))
    return jsonify(ok=True)

@app.route("/api/view/<name>", methods=["GET"])
def load_view(name):
    row = q("SELECT filter_json FROM saved_views WHERE name = %s", (name,), one=True)
    if not row:
        return jsonify(ok=False), 404
    return jsonify(json.loads(row[0]))

@app.route("/api/view/<name>", methods=["DELETE"])
def del_view(name):
    ex("DELETE FROM saved_views WHERE name = %s", (name,))
    return jsonify(ok=True)

# ------------------------------------------------------------- team (admin)

PERIODS = {"today": "1 day", "week": "7 days", "month": "30 days", "quarter": "90 days"}


@app.route("/team", methods=["GET", "POST"])
@require_admin
def team():
    msg = None
    if request.method == "POST":
        u = current_user()
        if request.form.get("toggle_user"):
            tid = request.form.get("toggle_user")
            if tid.isdigit() and int(tid) != u["id"]:
                ex("UPDATE users SET active = NOT active WHERE id = %s", (int(tid),))
        else:
            username = (request.form.get("username") or "").strip().lower()
            password = request.form.get("password") or ""
            if username and len(password) >= 6:
                ex("""INSERT INTO users (username, password_hash, is_admin)
                     VALUES (%s, %s, %s) ON CONFLICT (username) DO NOTHING""",
                   (username, hash_password(password), bool(request.form.get("is_admin"))))
                msg = f"User '{username}' created (or already exists)."
            else:
                msg = "Password must be at least 6 characters."
    period = request.args.get("period", "week")
    interval = PERIODS.get(period, PERIODS["week"])
    users = q("SELECT id, username, is_admin, active, created_at FROM users ORDER BY id")
    leaderboard = q(f"""SELECT us.username, us.is_admin,
            count(DISTINCT a2.id) FILTER (WHERE act.action = 'contacted') AS contacted,
            count(*) FILTER (WHERE act.action = 'call') AS calls,
            count(*) FILTER (WHERE act.action = 'email') AS emails,
            count(*) FILTER (WHERE act.action = 'classified') AS classified,
            count(*) FILTER (WHERE act.action = 'contact_added') AS contacts_added,
            count(*) FILTER (WHERE act.detail = 'Picked - Interested') AS interested
        FROM users us
        LEFT JOIN activity act ON act.user_id = us.id
             AND act.created_at > now() - interval '{interval}'
        LEFT JOIN agencies a2 ON a2.claimed_by = us.id
             AND a2.updated_at > now() - interval '{interval}'
        GROUP BY us.id, us.username, us.is_admin
        ORDER BY contacted DESC, calls DESC""")
    feed = q(f"""SELECT us.username, act.action, act.detail, act.agency_id,
                        a.org_name, act.created_at
                 FROM activity act
                 JOIN users us ON us.id = act.user_id
                 LEFT JOIN agencies a ON a.id = act.agency_id
                 WHERE act.created_at > now() - interval '{interval}'
                 ORDER BY act.created_at DESC LIMIT 60""")
    return render_template("team.html", users=users, leaderboard=leaderboard,
                           feed=feed, period=period, msg=msg,
                           me=current_user()["username"])


# ---------------------------------------------------------------- export

@app.route("/export.csv")
@require_login
def export_csv():
    args = request.args.to_dict()
    if args.get("dnc") != "only":           # never leak do-not-call leads into exports
        args["dnc"] = ""
    where, params = build_filters(args)
    rows = q(
        f"""SELECT org_name, city, state, postal_code, phone, owner_phone,
                   owner_first_name, owner_last_name, owner_title,
                   website, fb_page, fb_page_url, ad_status, outreach_status, notes, npi,
                   muse_ad_status, muse_ad_notes
            FROM agencies WHERE {where} ORDER BY state, city, org_name""",
        params,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Agency", "City", "State", "ZIP", "Phone", "Owner Phone",
                "Owner First", "Owner Last", "Owner Title",
                "Website", "FB Page", "FB Page URL", "Ad Status", "Outreach Status", "Notes", "NPI",
                "Muse Ad Status", "Muse Notes"])
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=targeto_call_sheet.csv"})


# ---------------------------------------------------------------- backup

@app.route("/backup")
@require_admin
def backup():
    """One-click full-database backup via real pg_dump (installed in the
    dashboard image), pointed straight at the postgres host from TARGETO_PG*.
    Falls back to a pure-SQL dump via psycopg2 when pg_dump is unavailable."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    fname = f"targeto_backup_{ts}.sql"
    try:
        from . import DB
        cmd = ["pg_dump", "-F", "p", "--no-owner", "--no-privileges",
               "-h", DB["host"], "-p", str(DB["port"]), "-U", DB["user"], DB["dbname"]]
        env = dict(os.environ, PGPASSWORD=DB["password"])
        out = subprocess.run(cmd, capture_output=True, timeout=120, env=env)
        if out.returncode != 0 or not out.stdout:
            raise RuntimeError(out.stderr.decode(errors="replace")[:300])
        data = out.stdout
    except Exception:
        data = _sql_dump_fallback().encode()
    size = len(data)
    log_action("backup", detail=f"{fname} ({size // 1024} KB)")
    return Response(data, mimetype="application/sql",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


def _sql_dump_fallback():
    """Portable full-DB dump via SQL when docker exec isn't reachable
    (e.g. dashboard running directly on a VPS without docker)."""
    conn = get_conn()
    lines = ["-- Targeto full backup (pure-SQL fallback)",
             "-- Restore into an empty DB with: psql -f this_file.sql", "BEGIN;"]
    try:
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            if t in ("spatial_ref_sys", "geography_columns", "geometry_columns"):
                continue
            cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = %s AND table_schema='public' ORDER BY ordinal_position", (t,))
            cols = cur.fetchall()
            if not cols:
                continue
            collist = ", ".join(f'"{c}"' for c, _ in cols)
            lines.append(f"CREATE TABLE IF NOT EXISTS \"{t}\" (" +
                         ", ".join(f'"{c}" {_t}' for c, _t in cols) + ");")
            cur.execute(f'SELECT {collist} FROM "{t}"')
            collens = [d[0] for d in cur.description]
            for row in cur.fetchall():
                vals = []
                for cname, v in zip(collens, row):
                    if v is None:
                        vals.append("NULL")
                    elif isinstance(v, bool):
                        vals.append("TRUE" if v else "FALSE")
                    elif isinstance(v, (int, float)):
                        vals.append(str(v))
                    elif isinstance(v, dict):
                        vals.append("'" + json.dumps(v).replace("'", "''") + "'::jsonb")
                    elif isinstance(v, datetime):
                        vals.append("'" + v.isoformat() + "'")
                    else:
                        vals.append("'" + str(v).replace("'", "''") + "'")
                lines.append(f'INSERT INTO "{t}" ({collist}) VALUES (' + ", ".join(vals) + ");")
        lines.append("COMMIT;")
    finally:
        conn.close()
    return "\n".join(lines)

if __name__ == "__main__":
    import os
    app.run(host=os.environ.get("TARGETO_DASH_HOST", "127.0.0.1"),
            port=int(os.environ.get("TARGETO_DASH_PORT", "8010")), debug=False)
