# Deployment

- `client/` → **Vercel** (static Vite build).
- `server/` → **Render** (single free Web Service — see [Worker note](#worker-note) below).
- Postgres → an external free-tier provider (**Neon** or **Supabase**), not Render's.
- Redis → **Upstash** free tier, not Render's.

Render's own free Postgres expires 30 days after creation and its free Redis
("Key Value") loses all data on every restart, so both data stores are kept
off Render. See [Known limitations](#known-limitations) for why.

## client/ (Vercel)

`client/vercel.json` pins the build: framework `vite`, build command
`npm run build`, output directory `dist`. No rewrites are needed — the app
is a single page with no client-side routing.

Steps:
1. Import the repo in Vercel, set the project root to `client/`.
2. Set the environment variable below in the Vercel project settings
   (Production, and Preview if you want previews to hit a real backend).
3. Deploy.

| Variable | Value | Notes |
|---|---|---|
| `VITE_API_BASE_URL` | e.g. `https://sheet-music-server.onrender.com` | The deployed Render server's public URL. Baked in at build time (Vite), so redeploy after changing it. |

## server/ (Render)

`server/Dockerfile.sheet-music-generator` builds a single image containing
both the Node/Express server and the `ml/` Python pipeline (Python 3.11,
ffmpeg, and a venv with yt-dlp/basic-pitch/music21 baked in).

Render service settings:
- **Environment**: Docker
- **Dockerfile Path**: `server/Dockerfile.sheet-music-generator`
- **Docker Build Context Directory**: repo root (`.`) — the Dockerfile
  needs both `server/` and `ml/`, so it can't build from `server/` alone.

<a id="worker-note"></a>
**Worker note:** the BullMQ worker is meant to run combined into the same
process as the API (per the [design](docs/superpowers/specs/2026-08-13-server-transcription-pipeline-design.md)),
so a single Render Web Service can run everything on the free tier — Render
doesn't reliably offer a free separate background-worker service. This
Dockerfile's `CMD` (`node dist/index.js`) is already correct for that; it
just needs `src/index.ts` to actually start the worker in-process once the
pipeline (`queue.ts`, `worker.ts`, `db.ts`, routes) is implemented. Until
then, the deployed service has no `/transcribe` routes.

| Variable | Value | Notes |
|---|---|---|
| `PORT` | — | Set automatically by Render; already read via `process.env.PORT` in `src/index.ts`. |
| `CLIENT_ORIGIN` | e.g. `https://sheet-music-app.vercel.app` | CORS allow-origin, already wired in `src/index.ts`. Omit only for local dev (defaults to `*`). |
| `DATABASE_URL` | Postgres connection string from Neon/Supabase | Consumed by the pending `src/db.ts`. |
| `REDIS_URL` | Redis connection string from Upstash (`rediss://...`) | Consumed by the pending `src/queue.ts`. Upstash requires TLS — use the `rediss://` URL it gives you; `ioredis` (which BullMQ uses) enables TLS automatically for that scheme. |

## Data stores

**Postgres — Neon or Supabase (either works, pick one):**
- Create a free project, copy its connection string into `DATABASE_URL`.
- Neither expires like Render's free Postgres does, but both free tiers
  auto-suspend/pause after a period of inactivity — the first request after
  a pause will be slow (cold start), same trade-off as Render's own spin-down.

**Redis — Upstash:**
- Create a free database, copy the `rediss://` connection string into
  `REDIS_URL`.
- Free tier is request-capped (check Upstash's current limits) but persists
  data and survives restarts, unlike Render's free Key Value.

## Known limitations

- **Render free web service = 512 MB RAM / 0.1 CPU.** TensorFlow (basic-pitch's
  model runtime) alone can approach that just importing, before processing
  any audio. This is a real risk of OOM kills or very slow inference on
  anything but short clips. If you hit this, the fix is upgrading the Render
  instance type — there isn't a free-tier workaround beyond keeping test
  clips short.
- **Free services spin down after 15 minutes idle** (cold start ~30-60s on
  the next request). A transcription job that arrives right after a cold
  start will queue behind the spin-up.
- **Image size is ~4.3 GB** (verified locally), almost entirely TensorFlow
  and its transitive deps. Expect slower builds/deploys than a typical
  Node-only service; stay mindful of Render's free tier's monthly build-minute
  cap.
- **Combined process**: because the worker runs in the same container as the
  API (see [Worker note](#worker-note)), a crash in the Python pipeline
  subprocess must not be allowed to take down the Express process — the
  worker implementation needs to catch and record failures per-job, not let
  them propagate.
