CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  youtube_url TEXT NOT NULL,
  instrument  TEXT NOT NULL,
  mode        TEXT NOT NULL DEFAULT 'church_sheet' CHECK (mode IN ('transcription','church_sheet')),
  status      TEXT NOT NULL CHECK (status IN ('queued','processing','completed','failed')),
  musicxml    TEXT,
  error       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- CREATE TABLE IF NOT EXISTS is a no-op on a database that already has a
-- jobs table (e.g. this machine's), so a new column there must be added
-- explicitly. ADD COLUMN IF NOT EXISTS is safe to rerun on every worker
-- startup, matching how this whole file is already executed each boot.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'church_sheet'
  CHECK (mode IN ('transcription','church_sheet'));
