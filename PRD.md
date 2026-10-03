# Targeto Lead Pipeline — Product Requirements Document (PRD)

**Product:** Targeto Lead Pipeline
**Owner:** Saroj Kumar (Targeto — solo Meta-ads agency)
**Version:** 1.0 · **Date:** 2026-10-04 · **Status:** Built & live
**Live URL:** http://80.225.252.48:8010 (Oracle Cloud, Always Free tier)

---

## 1. Product overview

**The one-liner:** a private lead-generation and cold-calling dashboard that finds US senior home care / home health agencies that are *not* running Meta ads, gets their owner's contact details, and turns them into $500–700/mo Meta-ads management clients.

**The business logic:**

- Agencies **not running ads** → ideal customers (they need what Targeto sells).
- Agencies **already running ads** → competitor intelligence ("they are ahead of you — let's fix that").
- Every agency row carries the **official owner name, title and phone** from the US NPI Registry — no scraping guesswork for the primary contact.

**Who uses it:** Saroj (admin) plus his 4-person calling team. Each caller logs in under their own account; every call, email, status change and added contact is attributed to them.

**Design principles:**

1. **Static database** — everything is pre-computed into Postgres. Clicking around the dashboard never calls an external API. Zero per-click dependencies, zero per-click cost.
2. **$0/month to run** — Oracle Cloud Always Free tier + Docker.
3. **Honest data** — company inboxes are flagged, not passed off as personal emails; unverified guesses are labelled as guesses; DNC-flagged leads never leak into exports.

---

## 2. Current data state (frozen snapshot, 2026-09-30)

| Metric | Value | Notes |
|---|---|---|
| Agencies in DB | **4,144** | FL, TX, AZ + spillover from 38 other states |
| With owner + phone | 100% | Official NPI Registry data |
| Websites resolved | **1,433** | Verified by phone-number match (100% precision on samples) |
| Facebook pages found | **2,621 (63.3%)** | Full-DB sweep complete; 444 with exact page IDs |
| Verified no-FB agencies | **1,523** | The cold-call gold list |
| Personal (owner) emails | **24** | Name-matched, still unverified (see Blockers) |
| Email guess candidates | **9,129** | Across 1,015 agencies — unconfirmed patterns (see Blockers) |
| Users | **5** | saroj (admin) + 4 team accounts |
| ZIP centroids loaded | 33,144 | Powers radius search |

> The database is intentionally **frozen** as of Sep 30, 2026 (owner directive): no indexing workers run anywhere. Re-indexing happens on the server, only when asked (see Blockers).

---

## 3. Feature list — everything that is built

### 3.1 Lead data pipeline

| # | Feature | What it does |
|---|---|---|
| F1 | **NPI agency import** | US senior home care / home health agencies pulled from the official NPPES/NPI Registry with owner name, title and phone. Deduped by NPI. |
| F2 | **Website resolver** | Legal name → domain guessing → search-engine fallback (engine rotation to survive rate limits) → verification by phone-number match on the page. Directory/lookalike hosts blocked. Runs as a 6-shard parallel fleet. |
| F3 | **Facebook page discovery** | Full-DB sweep (4,144/4,144 checked). Anti-lookalike validators: geo-conflict rejection ("Page \| Brooklyn NY" for a Texas agency → rejected), generic-name traps handled, foreign-country pages rejected, "\| Location" suffixes stripped. Exact page IDs captured where possible. |
| F4 | **Ad-status classification** | Per-agency status: Running / Ran Before / No Ads / No FB Page / Unresolved — set manually from one-click **Meta Ad Library links** (exact page when ID known, pre-filled search otherwise). Every check is audited. |
| F5 | **Personal-only email harvester** | Crawls each agency's homepage + 11 contact/about/team paths; decodes obfuscated emails (`name [at] site [dot] com`); **only stores an email if it contains the owner's own name** — ~70 generic inboxes (info@, sales@, contact@…) are hard-rejected. One email per agency, never overwrites manual entries. |
| F6 | **Email guess engine** | Generates 9 ranked patterns per owner (first.last > firstlast > first_last > …), MX-gated via DNS-over-HTTPS (works on port 443), stored in a separate `email_guesses` table with status candidate/confirmed/rejected. **Never auto-confirms** — the human decides. |
| F7 | **Apollo sync machine (built, awaiting tokens)** | Multi-token credit pool (`apollo_tokens` with monthly caps + usage audit), gold-list-first worklist, per-owner people-match lookups, credit rotation across accounts. Dry-run verified; blocked on real API keys (see Blockers). |

### 3.2 Dashboard pages

| Page | What it gives you |
|---|---|
| **Cities** (home) | Every city with live counts → one-click "Call list" and "Competitors" views per city. |
| **Leads** | The main calling sheet. Two tab rows (outreach status + FB toggle with live counts), full filter stack (state, city, ZIP prefix, **radius search**, outreach status, has-owner/phone/email, 👤 personal vs 🏢 company email, has/has-no FB, DNC), sorting, page-size control, **saved views**, filtered **CSV export**. |
| **Agency detail** | Full record: owner, NPI link, website, FB page, ad status, email field (permanent save), 🕵️ guessed-email table with confirm/reject, attempt history, activity log, add-contact prefill. |
| **Contacts** | People tracker (CMO, second owner, handler…). Add from a leads row with company pre-filled/locked; inline status dropdown + notes autosave; copyable phone/email; delete. |
| **Follow-ups** | Auto-scheduled call-back queue. Every disposition schedules its own rule (No pickup → 2d, Voicemail → 3d, Email sent → 3d, Interested → 1d, Won/Lost → cleared). Overdue pills, 7-day buckets, 👍 done / +1d / exact-date buttons, tap-to-dial. |
| **✉ Templates** | Saroj writes his own email templates (kinds: didn't pick up / picked / interested), multiple versions, default star, placeholders `{{owner_name}} {{company}} {{city}} {{state}} {{targeto_user}}` in subject and body. |
| **🚀 Apollo hub** | Step 1: download the prioritized worklist CSV (no-email agencies with owner + phone, non-DNC, best first). Step 2: Apollo extension lookups. Step 3: paste results back — importer dedupes, attributes, never overwrites existing emails, 10-minute undo. Plus the API-token pool manager. |
| **Progress** | Indexing funnel: searched / found / no-FB counts, per-state coverage bars, live speed + ETA. |
| **✉ Email indexing** | Email funnel: websites % → emails %, source split (harvested vs manual), personal vs company counts, 20 latest finds with copy buttons, per-state coverage. |
| **Team** (admin) | Leaderboard per period (24h/7d/30d/90d): businesses contacted, calls, emails, classifications, contacts added, interested. Live activity feed (60 latest). Create / enable / disable users. |
| **Backup** (admin) | One-click real `pg_dump` download (~4 MB). Restore instructions included. |

### 3.3 Calling & email UX (the daily-driver details)

- **CloudTalk-ready numbers:** every phone is normalized to +1 E.164 with a 📋 copy button (vanity/short numbers honestly left un-copyable).
- **US local-time badges:** 🕒 live time under every lead's city — FL = Eastern, TX = Central, AZ = Mountain (no DST). Call at 6:30–10:30pm IST = their morning.
- **👤 / 🏢 email badges:** 👤 = owner's personal email (name-verified), 🏢 = company inbox. Filters `has=personal` / `has=company` + quick links with counts.
- **🔎 LinkedIn owner search:** one click opens LinkedIn people-search pre-filled with owner name + company + city → find the profile → Apollo extension → save the email on the agency page.
- **✉ one-click email:** mailto link with the chosen template rendered and encoded; logs the attempt, sets status "Email Sent", schedules the 3-day follow-up. Works with Gmail's mailto handler.
- **🚫 DNC flag:** per-row Do-Not-Call with confirm dialog, 55%-faded rows, show-DNC-only filter — and **exports never leak DNC leads** unless explicitly requested.
- **+👤 add-contact prefill:** from any leads row; company locked, phone field empty for the *new person's own mobile*.
- **NPI ↗ / Site ↗ links** on every row; review flag for suspicious records.
- **Notes autosave** + full attempt history per agency (timestamped, per-user).

### 3.4 Multi-user & team

- Login wall on every page and API; PBKDF2-salted password hashes (portable across servers).
- Per-user attribution: claimed leads, added contacts, calls, emails, classifications — all stamped with who and when.
- Admin-only Team page: leaderboard + activity feed + user management.
- Role separation: backups and user management are admin-only.

### 3.5 Deployment & operations

- **Stack:** Flask (Python) + PostgreSQL 17 + Docker Compose. No build tooling, no JS framework — fast to maintain, cheap to run.
- **Hosting:** Oracle Cloud **Always Free** VM (VM.Standard.E2.1.Micro, Mumbai region, Ubuntu 22.04), 2 GB swap + memory tuning, Docker + Compose, firewall open on the dashboard port. Uses ~300 MB RAM of 1 GB.
- **One-shot deploy kit:** `deploy/push-to-vm.sh` ships code + frozen DB snapshot to the VM, bootstraps it (idempotent), builds, waits for health, restores the snapshot, verifies counts — re-runnable end-to-end.
- **Pinned server secret** so logins survive rebuilds; DB lives in a named Docker volume.
- **Backups:** one-click download from the UI + frozen snapshot file kept in the repo (`backups/targeto_freeze_2026-09-30.sql`).

---

## 4. Tech stack summary

| Layer | Choice |
|---|---|
| Backend | Python / Flask (server-rendered, light vanilla JS) |
| Database | PostgreSQL 17 (Docker), 16 tables |
| Auth | PBKDF2 + signed-cookie sessions, multi-user |
| Indexing workers | Python modules run inside the container (resolver shards, harvester, guess loop, Apollo syncer) |
| Hosting | Oracle Cloud Always Free (E2.1.Micro, 1 OCPU / 1 GB RAM + 2 GB swap), Mumbai |
| Deploy | Docker Compose + bash deploy kit, frozen-snapshot restore |
| Local dev | Docker Desktop on laptop (PG :5434, dashboard :8010, pgAdmin :5050) |

---

## 5. Blockers & open items (honest list)

These are the things standing between "working product" and "fully loaded sales machine", in impact order.

1. **🔴 Apollo API tokens — missing (highest impact).** The Apollo sync machine is built and dry-run tested, but there are **zero real tokens**. It needs 2–3 free Apollo.io accounts registered with work emails (~50–75 email credits/month each, 1 credit charged only when an email is found). This is the **real personal-email machine** — the difference between 24 owner emails and hundreds.
2. **🔴 Contact genuineness not confirmed.** The 24 stored emails are name-matched but **unverified**, and all 9,129 guesses are **unconfirmed patterns** — nobody has proven they actually deliver. SMTP verification is impossible from the laptop (the ISP blocks outbound port 25). Options: run verification from the Oracle VM or a ~$5 VPS, or bounce-test with real sends and mark confirmed/rejected as results come in.
3. **🟠 Admin password still the default placeholder.** The `saroj` admin account is still on its bootstrap password — and this server is now on the public internet. **Change it today** (Team page → own password) and have each team member change theirs.
4. **🟠 VM is the smallest shape and cannot be resized.** Oracle's E2.1.Micro (1 GB RAM) is fine for the current frozen dashboard, but re-indexing thousands of sites will want more. The bigger Always Free shape (A1.Flex) is out of stock in Mumbai; migration = create new A1 instance + restore the backup. (Retries 8–11 AM IST sometimes catch stock.)
5. **🟡 Database is frozen — by directive.** No indexing workers run anywhere since Sep 30. All counts above are a snapshot; new agencies, websites and emails will not appear until re-indexing is switched on (on the VM, when asked).
6. **🟡 Meta Ad Library API is not usable for US commercial ads.** Meta's API gate requires government-ID verification and its scope only covers political ads worldwide + all ads for UK/EU. Ad status for US agencies is therefore **manual** classification via the built-in Ad Library links (~3 seconds per agency by eye).
7. **🟡 Google CSE key absent.** The website resolver currently runs on free search engines only. Adding the free Google Programmable Search key (100 queries/day) would raise website discovery above the current ~35%.
8. **⚪ Keep the VM warm.** Oracle reclaims Always Free instances that sit under 10% CPU for 7 days. Daily dashboard use (calling sessions) covers this — just don't leave it untouched for a week.

---

*Document generated 2026-10-04 from the live system. Feature list reflects what is actually built and deployed, not planned work.*
