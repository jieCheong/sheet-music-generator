CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  youtube_url TEXT NOT NULL,
  instrument  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('queued','processing','completed','failed')),
  musicxml    TEXT,
  error       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
