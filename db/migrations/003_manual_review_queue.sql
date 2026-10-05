-- 003: Manual review queue from the Muse verification campaign.
--
-- Meaning of the two UNRESOLVEDs (do not confuse):
--   * the pipeline's own ad_status = 'UNRESOLVED' means "Saroj hasn't manually
--     checked this agency in the Ad Library yet" (his workflow state);
--   * muse_ad_status = 'UNRESOLVED' means "Muse's independent verification
--     genuinely could not determine the ad status" (a verdict, not a todo).
--
-- The campaign already did the Ad Library work for ~3,800 of 4,144 agencies,
-- so Saroj's real manual queue is ONLY the rows where manual_review_needed
-- is TRUE (319 rows at campaign close). Imported from
-- data/fb_page_corrections.csv by pipeline/muse_import.py.
--
-- NOTE: agencies.review_needed (pre-existing) is Saroj's own manual triage
-- flag, toggled in the UI. manual_review_needed is campaign-derived and
-- must not be confused with it.
-- Idempotent: safe to re-run (IF NOT EXISTS everywhere).
ALTER TABLE agencies
  ADD COLUMN IF NOT EXISTS manual_review_needed BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS review_task TEXT
    CHECK (review_task IS NULL OR review_task IN ('check_ad_library','recheck_page_search'));

CREATE INDEX IF NOT EXISTS idx_agencies_manual_review ON agencies(manual_review_needed)
  WHERE manual_review_needed IS TRUE;
