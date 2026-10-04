-- 002: Muse verification — independent FB page + ad status verdicts,
-- kept side-by-side with the pipeline's automated detection.
-- Idempotent: safe to re-run (IF NOT EXISTS everywhere).
ALTER TABLE agencies
  ADD COLUMN IF NOT EXISTS muse_fb_page TEXT,
  ADD COLUMN IF NOT EXISTS muse_fb_page_id TEXT,
  ADD COLUMN IF NOT EXISTS muse_fb_page_url TEXT,
  ADD COLUMN IF NOT EXISTS muse_fb_confidence TEXT,
  ADD COLUMN IF NOT EXISTS muse_fb_checked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS muse_ad_status TEXT DEFAULT 'UNRESOLVED'
    CHECK (muse_ad_status IN ('RUNNING','RAN_BEFORE','NO_ADS','NO_FB_PAGE','UNRESOLVED','PENDING')),
  ADD COLUMN IF NOT EXISTS muse_ad_checked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS muse_ad_notes TEXT;

CREATE INDEX IF NOT EXISTS idx_agencies_muse_ad ON agencies(muse_ad_status);
CREATE INDEX IF NOT EXISTS idx_agencies_muse_checked ON agencies(muse_ad_checked_at);
