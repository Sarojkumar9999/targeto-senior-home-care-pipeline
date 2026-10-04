# Muse Verification Campaign — Coordinator Protocol

This is the durable brain of the campaign. Any coordinator (this session or a
future one) follows it exactly. The goal: verify all 4,144 agencies' FB page +
ad status with Muse's independent live-browser verdicts, as fast as Meta's
defenses allow, without getting blocked.

## Work lists

Tier CSVs live in `docs/verification/tiers/` (derived from
`backups/targeto_freeze_2026-09-30.sql`; NPI is the key):

| Tier | File | Rows | Check |
|---|---|---|---|
| 1 | `tier1_exact_id.csv` | 452 | FB page ID known — Ad Library by page ID |
| 2 | `tier2_url_no_id.csv` | 2,181 | FB URL known — open page, confirm identity, then ads |
| 3 | `tier3_name_only.csv` | 192 | Page name only — search, disambiguate lookalikes, then ads |
| 4 | `tier4_no_fb.csv` | 1,319 | No FB trace — confirmatory search, usually NO_FB_PAGE |

Order: tier 1 → 2 → 3 → 4. Within a tier, any order.

## Completion record (single source of truth)

`data/muse_verifications.csv` — one row per verified agency (append-only).
Done NPIs = NPIs present in that file. To resume: read the CSV, build the
done-set, filter each tier list. Never re-verify a done NPI.

## Verification rules (per agency)

1. Confirm the FB page is really that agency (name + city/state match; reject
   cross-state lookalikes and generic-name traps).
2. Check Meta Ad Library for that exact page (page ID preferred).
3. Classify: `RUNNING` / `RAN_BEFORE` / `NO_ADS` / `NO_FB_PAGE` / `UNRESOLVED`.
4. **Honesty rule:** if the advertiser/page cannot be matched confidently,
   verdict is `UNRESOLVED` — never `NO_ADS`. Uncertainty is data, not failure.
5. Record: `muse_fb_page`, `muse_fb_page_id`, `muse_fb_page_url`,
   `muse_fb_confidence` (high/medium/low), `muse_ad_status`, `muse_ad_notes`
   (e.g. "3 active caregiver-recruitment ads").

## Worker protocol

- Workers are browser subagents. Each takes a micro-batch (10–15 agencies),
  verifies them, and RETURNS the rows to the coordinator. Workers never write
  to the repo directly (single writer = no conflicts).
- Pace: ~10–20s between Ad Library checks per worker. Human-like, no hammering.
- A failed check (block, captcha, timeout, inconclusive load) counts as an
  error for the auto-scaler. Retry once later; if it still fails, record the
  agency as UNRESOLVED with note "check failed, retry later" — do not burn
  retries on one agency.

## Persistence protocol (non-negotiable)

After every micro-batch:
1. Append rows to `data/muse_verifications.csv`.
2. Recompute `docs/verification/progress.json` (counts, rate, tiers, batches).
3. Commit + push BOTH files immediately.
A crash must never lose more than one micro-batch (~10–15 agencies).

## Auto-scale protocol (AIMD — the manager)

The coordinator adjusts the worker pool on a ~10 minute evaluation window.
All decisions go into `progress.json → scaling_log` with numbers + reason.

Definitions:
- `per_minute` = verified agencies / elapsed minutes in the window (EMA-smoothed).
- `error_rate` = failed checks / total checks in the window.

Rules (in order):
1. **Manual override wins.** If `worker_config.json → paused` is true: stop all
   workers, set status `paused`. If `mode` is `manual`: hold exactly
   `desired_workers`, skip rules 2–6.
2. **Error spike:** `error_rate > 10%` → scale down by 2 (min 1), 20-min
   cooldown before any scale-up. Reason: "throttling detected".
3. **Elevated errors:** `error_rate` 5–10% → hold worker count, keep observing.
4. **Healthy (`error_rate` < 5%):**
   - Throughput up >5% since last increase → +1 worker (max `max_workers`).
   - Throughput flat (±5%) → hold; one more +1 probe after 20 min, then hold.
   - Throughput down >5% → −1 worker (diminishing returns / early warning).
5. **Block event** (IP-level block, mass captchas): drop to 2 workers immediately,
   30-min cooldown, then resume probing from there.
6. Bounds: never above `max_workers` (default 8), never below 1 while running.

Also maintain `throughput_by_workers` in progress.json:
`{ "<workers>": <best observed per_minute> }` — the dashboard charts this so
the manager sees exactly what each worker count yields.

## Manual override (emergency only)

Edit `docs/verification/worker_config.json` on GitHub:
- `paused: true` → everything stops within minutes.
- `mode: "manual", desired_workers: N` → fixed pool, auto-scaler stands down.
- `mode: "auto"` → resume auto-scaling.
The coordinator re-reads this file every few minutes.
