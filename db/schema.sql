-- Targeto Lead Pipeline schema (Postgres 17)
-- Loaded automatically on first container start via docker-entrypoint-initdb.d

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS agencies (
  id                 BIGSERIAL PRIMARY KEY,
  npi                VARCHAR(20) UNIQUE,
  org_name           TEXT NOT NULL,
  org_name_norm      TEXT,
  sole_owner         BOOLEAN DEFAULT FALSE,
  status             CHAR(1) DEFAULT 'A',              -- NPPES status: A=active, D=deactivated
  enumeration_date   DATE,
  last_updated       DATE,
  address_1          TEXT,
  address_2          TEXT,
  city               TEXT,
  city_norm          TEXT,
  state              VARCHAR(2),
  postal_code        VARCHAR(10),
  county_name        TEXT,
  phone              TEXT,
  -- enrichment: website
  website            TEXT,
  website_source     TEXT,                             -- guess | search | directory | manual
  website_confidence TEXT,                             -- high | medium | low
  -- enrichment: facebook page
  fb_page            TEXT,                             -- page name / slug
  fb_page_id         TEXT,
  fb_source          TEXT,                             -- site_link | keyword | manual
  fb_confidence      TEXT,
  fb_checked_at      TIMESTAMPTZ,                      -- discovery attempted (any outcome)
  -- meta ads classification
  ad_status          TEXT DEFAULT 'UNRESOLVED' CHECK (ad_status IN
                       ('RUNNING','RAN_BEFORE','NO_ADS','NO_FB_PAGE','UNRESOLVED','PENDING')),
  ad_last_checked    TIMESTAMPTZ,
  -- outreach tracking
  outreach_status    TEXT DEFAULT 'New' CHECK (outreach_status IN
                       ('New','Email Sent','Call Tried','No Pickup','Voicemail',
                        'Picked - Interested','Picked - Not Interested',
                        'Call Back Later','Won','Lost')),
  review_needed      BOOLEAN DEFAULT FALSE,
  dnc                BOOLEAN DEFAULT FALSE,             -- do-not-call: hard-blocked from call lists & exports
  follow_up_at       TIMESTAMPTZ,                       -- when this lead should resurface in /followups
  notes              TEXT,
  -- decision maker (authorized official from NPPES)
  owner_first_name   TEXT,
  owner_last_name    TEXT,
  owner_title        TEXT,
  owner_phone        TEXT,
  email              TEXT,                              -- decision-maker email (manual / harvested / Apollo later)
  email_source       TEXT,                              -- website | manual | apollo
  -- bookkeeping
  source             TEXT DEFAULT 'npi_api',
  created_at         TIMESTAMPTZ DEFAULT now(),
  updated_at         TIMESTAMPTZ DEFAULT now()
);

-- Manually added contacts (e.g. from Apollo/LinkedIn/website research).
-- Separate from NPI 'agencies'; may be linked to one.
CREATE TABLE IF NOT EXISTS contacts (
  id              SERIAL PRIMARY KEY,
  agency_id       INTEGER REFERENCES agencies(id) ON DELETE SET NULL,
  company_name    TEXT,
  first_name      TEXT NOT NULL,
  last_name       TEXT,
  role            TEXT,                                 -- free text: CEO/CMO/handler/...
  phone           TEXT,
  email           TEXT,
  linkedin_url    TEXT,
  notes           TEXT,
  outreach_status TEXT DEFAULT 'New' CHECK (outreach_status IN
                    ('New','Email Sent','Call Tried','No Pickup','Voicemail',
                     'Picked - Interested','Picked - Not Interested',
                     'Call Back Later','Won','Lost')),
  source          TEXT DEFAULT 'manual',
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contacts_agency ON contacts(agency_id);

-- Reusable outreach email templates (Saroj writes subject+body; placeholders
-- {{owner_name}}, {{company}}, {{city}}, {{targeto_user}} auto-fill at send time).
CREATE TABLE IF NOT EXISTS email_templates (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'no_pickup' CHECK (kind IN ('no_pickup','picked','interested')),
  subject     TEXT NOT NULL,
  body        TEXT NOT NULL,
  is_default  BOOLEAN DEFAULT FALSE,
  created_by  INTEGER REFERENCES users(id),
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Apollo API token pool (multiple free accounts, rotated by apollo_sync)
CREATE TABLE IF NOT EXISTS apollo_tokens (
  id            SERIAL PRIMARY KEY,
  label         TEXT,
  token         TEXT NOT NULL UNIQUE,
  monthly_cap   INTEGER DEFAULT 60,                     -- free plan ≈ 50-75 email credits/mo
  credits_used  INTEGER DEFAULT 0,
  used_reset_at TIMESTAMPTZ,
  active        BOOLEAN DEFAULT TRUE,
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS apollo_usage (
  id            BIGSERIAL PRIMARY KEY,
  token_id      INTEGER REFERENCES apollo_tokens(id),
  agency_id     BIGINT,
  email_found   BOOLEAN,
  title         TEXT,
  linkedin_url  TEXT,
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- Personal-email GUESS sheet (pattern permutator, MX-gated via DoH).
-- Candidates are NEVER auto-confirmed: a human ✅ moves them into
-- agencies.email (source='manual'); ❌ rejects. status: candidate|confirmed|rejected
CREATE TABLE IF NOT EXISTS email_guesses (
  id            SERIAL PRIMARY KEY,
  agency_id     INTEGER NOT NULL REFERENCES agencies(id) ON DELETE CASCADE,
  email         TEXT NOT NULL,
  pattern       TEXT,                                   -- first.last | initlast | ...
  rank          INTEGER,                                -- 1 = most likely
  mx_ok         BOOLEAN DEFAULT TRUE,                   -- domain MX verified via DoH
  status        TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','confirmed','rejected')),
  created_at    TIMESTAMPTZ DEFAULT now(),
  confirmed_at  TIMESTAMPTZ,
  UNIQUE (agency_id, email)
);
CREATE INDEX IF NOT EXISTS idx_email_guesses_agency ON email_guesses(agency_id);
CREATE INDEX IF NOT EXISTS idx_email_guesses_status ON email_guesses(status);

-- Multi-user auth + per-user work attribution
CREATE TABLE IF NOT EXISTS users (
  id            SERIAL PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,                        -- pbkdf2$hexsalt$hash
  is_admin      BOOLEAN DEFAULT FALSE,
  active        BOOLEAN DEFAULT TRUE,
  created_at    TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE agencies ADD COLUMN IF NOT EXISTS claimed_by INTEGER REFERENCES users(id);
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id);
ALTER TABLE attempts ADD COLUMN IF NOT EXISTS user_id   INTEGER REFERENCES users(id);
CREATE TABLE IF NOT EXISTS activity (
  id         SERIAL PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  agency_id  INTEGER,
  contact_id INTEGER,
  action     TEXT NOT NULL,                           -- contacted|call|email|classified|contact_added
  detail     TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activity_user_time ON activity(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_activity_time ON activity(created_at);

-- search & filter indexes (nationwide scale)
CREATE INDEX IF NOT EXISTS idx_agencies_state          ON agencies(state);
CREATE INDEX IF NOT EXISTS idx_agencies_city_norm      ON agencies(city_norm);
CREATE INDEX IF NOT EXISTS idx_agencies_postal         ON agencies(postal_code);
CREATE INDEX IF NOT EXISTS idx_agencies_postal_prefix  ON agencies(postal_code text_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_agencies_ad_status      ON agencies(ad_status);
CREATE INDEX IF NOT EXISTS idx_agencies_outreach       ON agencies(outreach_status);
CREATE INDEX IF NOT EXISTS idx_agencies_followup       ON agencies(follow_up_at) WHERE follow_up_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agencies_name_trgm      ON agencies USING gin (org_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_agencies_owner_trgm     ON agencies USING gin ((coalesce(owner_first_name,'') || ' ' || coalesce(owner_last_name,'')) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS agency_taxonomies (
  agency_id   BIGINT REFERENCES agencies(id) ON DELETE CASCADE,
  code        TEXT,
  description TEXT,
  is_primary  BOOLEAN,
  PRIMARY KEY (agency_id, code)
);
CREATE INDEX IF NOT EXISTS idx_taxonomies_desc ON agency_taxonomies(description);

CREATE TABLE IF NOT EXISTS ad_checks (
  id           BIGSERIAL PRIMARY KEY,
  agency_id    BIGINT REFERENCES agencies(id) ON DELETE CASCADE,
  checked_at   TIMESTAMPTZ DEFAULT now(),
  result       TEXT,                                  -- RUNNING | RAN_BEFORE | NO_ADS | NO_FB_PAGE | ERROR
  page_name    TEXT,
  page_id      TEXT,
  ads_active   INT,
  ads_archived INT,
  raw          JSONB
);

CREATE TABLE IF NOT EXISTS ad_creatives (
  id                      BIGSERIAL PRIMARY KEY,
  agency_id               BIGINT REFERENCES agencies(id) ON DELETE CASCADE,
  page_name               TEXT,
  page_id                 TEXT,
  ad_delivery_start_date  DATE,
  ad_delivery_stop_date   DATE,
  body                    TEXT,
  cta                     TEXT,
  media_url               TEXT,
  raw                     JSONB,
  fetched_at              TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_creatives_agency ON ad_creatives(agency_id);

CREATE TABLE IF NOT EXISTS attempts (
  id           BIGSERIAL PRIMARY KEY,
  agency_id    BIGINT REFERENCES agencies(id) ON DELETE CASCADE,
  attempted_at TIMESTAMPTZ DEFAULT now(),
  channel      TEXT CHECK (channel IN ('call','email','whatsapp','linkedin','other')),
  outcome      TEXT,
  note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_agency ON attempts(agency_id);

CREATE TABLE IF NOT EXISTS review_queue (
  id          BIGSERIAL PRIMARY KEY,
  agency_id   BIGINT UNIQUE REFERENCES agencies(id) ON DELETE CASCADE,
  reason      TEXT,
  added_at    TIMESTAMPTZ DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS saved_views (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT UNIQUE,
  filter_json JSONB,
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS zip_centroids (
  zip VARCHAR(5) PRIMARY KEY,
  lat NUMERIC(9,6),
  lon NUMERIC(9,6)
);

CREATE TABLE IF NOT EXISTS harvest_log (
  id              BIGSERIAL PRIMARY KEY,
  source          TEXT,
  state           VARCHAR(2),
  taxonomy        TEXT,
  pages_fetched   INT,
  records_upserted INT,
  skipped         INT DEFAULT 0,
  truncated       BOOLEAN DEFAULT FALSE,
  error           TEXT,
  started_at      TIMESTAMPTZ DEFAULT now(),
  finished_at     TIMESTAMPTZ
);
