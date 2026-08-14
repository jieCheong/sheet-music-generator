# Server transcription pipeline — design

## Overview

Wire `server/` up to the existing `ml/` pipeline (`transcribe.py` then
`notation.py`) via a BullMQ job queue backed by Redis, with job records and
results (the produced MusicXML) persisted in Postgres. Two Node processes —
an Express API and a separate worker — share the Postgres jobs table and the
Redis queue. `client/`'s transcription page (already built) is the consumer
of this API and fixes the response contract this design must match.

## Architecture

- **API** (`server/src/index.ts`) — Express app.
  - `POST /transcribe` validates the YouTube URL and instrument, inserts a
    `queued` row into Postgres, enqueues a BullMQ job with that same id as
    the job id, returns `{ jobId }`.
  - `GET /transcribe/:jobId/status` reads the job row from Postgres and
    returns it. **Postgres is the single source of truth for
    client-facing status** — the API never queries BullMQ/Redis directly.
    BullMQ is purely the internal transport between API and worker.
    (Alternative considered: querying `job.getState()` from BullMQ live and
    only hitting Postgres for the final result. Rejected — it creates two
    sources of truth to reconcile for no real benefit at this scale.)
- **Worker** (`server/src/worker.ts`, run via `npm run worker`, a separate
  process from the API) — a BullMQ `Worker` with concurrency 1 (the
  pipeline is CPU-heavy TensorFlow inference; running two jobs at once just
  contends for the same cores). On each job:
  1. Mark the Postgres row `processing`.
  2. Spawn `python transcribe.py <url> --output ml/output/<jobId>/notes.json`.
  3. Spawn `python notation.py --input .../notes.json --output ml/output/<jobId>/score.musicxml`.
  4. Read the resulting file, mark the row `completed` with the MusicXML
     text stored in the row.
  5. On any failure (bad URL rejected by yt-dlp, non-zero exit from either
     script, timeout), mark the row `failed` with an error message.

  Per-job output directories (`ml/output/<jobId>/`) are kept on disk, not
  deleted — useful for `diagnostics.py` or opening directly in a notation
  editor.
- **Local infra**: root-level `docker-compose.yml` with `redis` and
  `postgres` services. `server/.env` holds `REDIS_URL` / `DATABASE_URL` /
  `PORT`. No separate migration tooling — `src/db.ts` runs
  `CREATE TABLE IF NOT EXISTS jobs (...)` once at startup in both the API
  and worker process, since this is a single table.

## File layout (new, under `server/src/`)

```
index.ts             — Express app, mounts routes (existing file, extended)
worker.ts             — BullMQ Worker entrypoint (new npm script "worker")
queue.ts              — shared BullMQ Queue + ioredis connection
db.ts                 — pg Pool + createTableIfNotExists + query helpers
db/schema.sql          — jobs table DDL (source of truth, read by db.ts)
routes/transcribe.ts   — POST /transcribe, GET /transcribe/:jobId/status
pipeline.ts            — spawns transcribe.py then notation.py; resolves the
                          venv python path cross-platform (Scripts/python.exe
                          on Windows, bin/python elsewhere)
validation.ts          — YouTube URL validator (host allowlist: youtube.com,
                          www.youtube.com, m.youtube.com, youtu.be; must
                          contain a video id)
```

## Data model

```sql
CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,        -- shared with BullMQ's jobId
  youtube_url TEXT NOT NULL,
  instrument  TEXT NOT NULL,           -- free text, stored only, not yet
                                        -- consumed by the pipeline
  status      TEXT NOT NULL CHECK (status IN ('queued','processing','completed','failed')),
  musicxml    TEXT,                    -- populated when status = completed
  error       TEXT,                    -- populated when status = failed
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## API contracts

Both endpoints must match `client/src/api.ts`'s existing `JobStatusResponse`
type exactly — the client was already built against this contract.

- `POST /transcribe`
  - Body: `{ "youtubeUrl": string, "instrument": string }`.
  - 400 if the URL fails validation or `instrument` is empty/missing.
  - 202 `{ "jobId": "<uuid>" }` on success.
- `GET /transcribe/:jobId/status`
  - 404 if `jobId` is unknown.
  - 200 `{ jobId, status, youtubeUrl, instrument, createdAt, updatedAt, musicxml?, error? }`.
    `musicxml` present only when `status === "completed"`; `error` present
    only when `status === "failed"`.

## Error handling

- Bad URL / missing instrument → 400 before anything touches the queue.
- yt-dlp failure (private video, no audio track, invalid id) or a
  non-zero exit from either Python script → caught in the worker, job row
  → `failed`, with a truncated stderr excerpt as `error`.
- No automatic BullMQ retries (`attempts: 1`) — these failures are
  deterministic; retrying just re-runs a multi-minute ML pipeline for the
  same result.
- Each child process is wrapped in a generous hard timeout (10 minutes) as
  a safety net against a hung download or inference call.

## Testing

No test runner exists yet in `server/`. Plan:
- Unit test for the URL validator (pure function, cheap to isolate).
- Manual end-to-end verification: `docker compose up` → POST a real
  YouTube URL → poll status → confirm MusicXML comes back and renders in
  the already-built client page. Mocking BullMQ/Postgres/child_process for
  this loop would test very little of what actually matters.
