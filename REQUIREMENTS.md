# REQUIREMENTS.md — Targeto Lead Pipeline (v2)

**Owner:** Saroj Kumar (Targeto) · **Date:** 2026-09-25
**Goal:** Find US senior home care / home health agencies that are NOT running Meta ads, reach their owners, and convert them to $500–700/mo Meta ads clients. Agencies already running ads are tracked as competitor intelligence for pitching ("they are already ahead of you").

**Design principle: scalable to ANY city/ZIP in the USA.** Search-first, nationwide data, local stack, $0/month.

---

## 1. Feature list

### Core pipeline

| # | Feature | Description | Status |
|---|---------|-------------|--------|
| F1 | **Nationwide import** | NPPES national bulk file (all 50 states, ~9 GB CSV, monthly refresh) + NPI Registry API for targeted/incremental pulls. Taxonomies: Home Health, Home Health Aide (Assisted Living optional). Dedupe by NPI and name+phone. | Phase 1 |
| F2 | **Website & FB resolver** | Legal name → guessed/verified domains → search-engine fallback → FB footer-link extraction → page name/ID. Solves legal-name ≠ operating-name ≠ FB-page-name. Unresolved → manual review queue in UI. | Phase 3 |
| F3 | **Meta ads classifier** | RUNNING / RAN_BEFORE / NO_ADS / NO_FB_PAGE / UNRESOLVED via Ad Library API (batched page-ID checks, keyword fallback). Re-checkable anytime. | Phase 3 |
| F4 | **Competitor intel** | Active ad creatives stored per agency/city (body, CTA, media, delivery dates) for pitch one-pagers. | Phase 3 |

### Search & filtering (scalable to any US city/ZIP)

| # | Feature | Description | Status |
|---|---------|-------------|--------|
| F10 | **Search** | Global fuzzy search (agency name, owner name, phone — typo-tolerant trigram). **ZIP search**: exact 5-digit + prefix (`331` → all 331xx). **Radius search**: within N miles of a ZIP (Census ZCTA centroids). Multi-select state, city autocomplete, county, taxonomy, ad status, outreach status, has-phone/has-website/has-FB, owner title, agency age (enumeration range), active/deactivated. Combine any filters, sort any column, page-size control. **Saved views** (named filter bookmarks). **Export exactly what you filtered** to CSV/Excel. | Phase 2 |
| F11 | **City browse** | City list with counts (total / running ads / not running / untouched) → per-city two tabs: 🔴 Not Running Ads (call list) · 🟢 Running Ads (competitors) + Unresolved queue. | Phase 2 |
| F12 | **Outreach tracking** | Status dropdown per agency: New → Email Sent → Call Tried → No Pickup → Voicemail → Picked–Interested / Picked–Not Interested → Call Back Later → Won / Lost. Every change appends to an attempts history (timestamp, channel, outcome, note). | Phase 2 |

### Platform

| # | Feature | Description | Status |
|---|---------|-------------|--------|
| F5 | **Postgres 17 + pgAdmin (Docker)** | Pre-wired compose, persistent volumes, pgAdmin auto-registered. Local ports: PG 5434, pgAdmin 5050 (5432/5433 were occupied). | Phase 1 ✅ |
| F6 | **Flask dashboard** | Server-rendered, no build tooling, light JS. | Phase 2 |
| F7 | **Bulk-scale design** | Trigram indexes on names, btree on city/state/ZIP/status, streaming CSV import, city rollups. Comfortable at 100k+ agencies on 13 GB RAM / no GPU. | Phase 1 ✅ |
| F8 | **Exports** | Filtered CSV/Excel call sheets + per-city competitor one-pagers. | Phase 2/4 |
| F9 | **Memory** | `AGENT.md` persists business context/decisions across chats. | Live ✅ |

---

## 2. Architecture

```
[NPPES bulk CSV]──► bulk importer ──►┌────────────┐   ┌─────────────────────┐
[NPI API]─────────► harvester ──────►│ PostgreSQL │◄──│ Flask dashboard     │
[name→site→FB]───► resolver ────────►│ (Docker)   │   │ search · ZIP/radius │
[FB page IDs]────► ad checker ──────►│  port 5434 │   │ city tabs · status  │
                                     └────────────┘   │ saved views · CSV   │
                                          ▲           └─────────────────────┘
                                     pgAdmin (Docker, port 5050)
```

**Tables:** `agencies` (master, incl. owner name/title/phone + enrichment + outreach status) · `agency_taxonomies` · `ad_checks` · `ad_creatives` · `attempts` · `review_queue` · `saved_views` · `zip_centroids` · `harvest_log`.

---

## 3. Data sources

| Source | Use | Cost | Reliability |
|--------|-----|------|-------------|
| NPI Registry API (npiregistry.cms.hhs.gov) | Targeted pulls, refresh; owner name/title/phone included | $0, no key | ~95% (verified live) |
| NPPES monthly bulk CSV (~9 GB) | Full-US import, any city scalable | $0 | ~95% |
| Agency websites | Domain discovery → FB links | $0 | 50–70% site discovery |
| Meta Ad Library API | Ad status + creatives | $0 (one-time Meta app setup) | ~95% of pages found |
| Caring.com | Optional enrichment only | $0 | OK, slow |
| A Place For Mom | — | — | Dropped (bot-protected) |

---

## 4. Honest feasibility verdict

- **NPI list + owner contact: ~95%** — official API, verified live.
- **Website discovery: 50–70%** — probabilistic; contained by review queue, never wrong data.
- **FB page discovery: ~80% of sites found.** **Ad status: ~95% of pages found** (needs token).
- **Net:** tens of thousands of callable "no ads" leads with owner phone nationwide.
- **Limits (accepted):** NPI universe = home-health-oriented agencies (pure companion-care without NPI invisible until optional directory enrichment); ad statuses drift weekly (re-check loop); emails stay manual (Apollo) — call-first strategy needs phone, which NPI supplies.
- **Verdict: doable end-to-end, $0/month, on Saroj's laptop.**

---

## 5. Build phases

1. **Phase 1 — Foundation (now):** compose + schema + indexes, NPI API harvester, NPPES bulk importer, seed FL/TX/AZ, verify search speed. ✅ building
2. **Phase 2 — Dashboard v1:** global search, ZIP/radius, all filters, city tabs, status dropdown + attempts log, saved views, filtered CSV export.
3. **Phase 3 — Resolution + ads:** website/FB resolver, review queue UI, Ad Library API integration (needs Meta token), re-check button, competitor creatives view.
4. **Phase 4 — Polish:** Excel export, weekly re-check scheduler, pitch one-pager generator.

Phases 1–2 need nothing from Saroj. Phase 3 unblocks with the Meta Ad Library API token (30-min walkthrough available).

---

## 6. Runbook (local)

```bash
docker compose up -d                # Postgres :5434 + pgAdmin :5050
source .venv/bin/activate
python -m pipeline.harvest --states FL --taxonomy "Home Health"   # targeted API pull
python -m pipeline.nppes_import --file ~/Downloads/npidata_pfile_*.zip  # full US
python -m pipeline.app              # dashboard on http://localhost:8000 (Phase 2)
```
pgAdmin: http://localhost:5050 · saroj@teamtargeto.com / targeto_local
