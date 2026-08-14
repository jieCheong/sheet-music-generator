# Server Transcription Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `server/` to the existing `ml/` pipeline via BullMQ (Redis) + Postgres, per `docs/superpowers/specs/2026-08-13-server-transcription-pipeline-design.md`, implementing `POST /transcribe`, a worker that shells out to `ml/transcribe.py` then `ml/notation.py`, and `GET /transcribe/:jobId/status`.

**Architecture:** Express API inserts a `queued` Postgres row and enqueues a BullMQ job with the same id. A separate `worker.ts` process (also startable in-process from `index.ts` via `RUN_WORKER_INLINE=true`, for single-container deployment) picks up jobs, shells out to the two Python scripts via `child_process.spawn`, and writes `processing`/`completed`/`failed` back to Postgres. Postgres is the only thing the API reads for status — BullMQ is purely internal transport.

**Tech Stack:** Express 5, BullMQ + ioredis, `pg`, TypeScript (NodeNext ESM, matching the existing `server/tsconfig.json`), Node's built-in `node:test` for the one unit test (zero new test-framework dependency).

## Global Constraints

- Contract is fixed by `client/src/api.ts`'s `JobStatusResponse` type — do not deviate from its field names/shapes.
- Relative imports must use explicit `.js` extensions (NodeNext + `"type": "module"` requires it, exactly as the spec's file layout implies).
- `attempts: 1` on the BullMQ job — no automatic retries (spec: deterministic failures, retrying just re-runs a multi-minute pipeline).
- Each Python child process gets a 10-minute hard timeout (spec, verbatim).
- Local dev Postgres/Redis are the docker-compose services on ports **5433**/**6380** (`server/.env.example` already has the matching `DATABASE_URL`/`REDIS_URL`).
- Worker architecture resolution (this session, supersedes any ambiguity): `worker.ts` exports `startWorker()` and is also runnable standalone (`npm run worker`, matching the spec and docker-compose's two-process local dev model). `index.ts` calls `startWorker()` in-process only when `RUN_WORKER_INLINE=true`, keeping the existing Render Dockerfile/DEPLOY.md story true without contradicting the spec's "separate process" description for local dev.
- Per-column null-checks (`job.musicxml !== null`), not status-string re-checks, decide whether `musicxml`/`error` appear in the GET response — simpler and equivalent, since those columns are only ever populated in the matching status.

---

### Task 1: YouTube URL validator

**Files:**
- Create: `server/src/validation.ts`
- Create: `server/src/validation.test.ts`
- Modify: `server/package.json` (add `"test": "tsx --test src/**/*.test.ts"` script)

**Interfaces:**
- Produces: `validateYoutubeUrl(input: string): boolean` — used by Task 6 (`routes/transcribe.ts`).

- [ ] **Step 1: Write the failing tests**

```typescript
// server/src/validation.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { validateYoutubeUrl } from "./validation.js";

test("accepts a standard watch URL", () => {
  assert.equal(validateYoutubeUrl("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), true);
});

test("accepts bare youtube.com and m.youtube.com hosts", () => {
  assert.equal(validateYoutubeUrl("https://youtube.com/watch?v=abc123"), true);
  assert.equal(validateYoutubeUrl("https://m.youtube.com/watch?v=abc123"), true);
});

test("accepts a youtu.be short URL", () => {
  assert.equal(validateYoutubeUrl("https://youtu.be/dQw4w9WgXcQ"), true);
});

test("rejects a non-YouTube host", () => {
  assert.equal(validateYoutubeUrl("https://vimeo.com/12345"), false);
});

test("rejects a watch URL with no video id", () => {
  assert.equal(validateYoutubeUrl("https://www.youtube.com/watch"), false);
  assert.equal(validateYoutubeUrl("https://www.youtube.com/watch?v="), false);
});

test("rejects a youtu.be URL with no path", () => {
  assert.equal(validateYoutubeUrl("https://youtu.be/"), false);
});

test("rejects a malformed URL", () => {
  assert.equal(validateYoutubeUrl("not a url"), false);
});

test("rejects a non-http(s) protocol", () => {
  assert.equal(validateYoutubeUrl("ftp://youtube.com/watch?v=abc123"), false);
});
```

- [ ] **Step 2: Add the test script and run it to verify it fails**

Add to `server/package.json` `scripts`: `"test": "tsx --test src/**/*.test.ts"`.

Run: `npm test` (from `server/`)
Expected: FAIL — `Cannot find module './validation.js'`.

- [ ] **Step 3: Implement the validator**

```typescript
// server/src/validation.ts
const ALLOWED_HOSTS = new Set(["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"]);

export function validateYoutubeUrl(input: string): boolean {
  let url: URL;
  try {
    url = new URL(input);
  } catch {
    return false;
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") return false;

  const host = url.hostname.toLowerCase();
  if (!ALLOWED_HOSTS.has(host)) return false;

  if (host === "youtu.be") {
    return url.pathname.length > 1;
  }

  const videoId = url.searchParams.get("v");
  return videoId !== null && videoId.trim().length > 0;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test` (from `server/`)
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add server/src/validation.ts server/src/validation.test.ts server/package.json
git commit -m "feat(server): add YouTube URL validator"
```

---

### Task 2: Postgres jobs table + query helpers

**Files:**
- Create: `server/src/db/schema.sql`
- Create: `server/src/db.ts`
- Modify: `server/package.json` (add `pg`/`@types/pg` deps; extend `build` script to copy `db/schema.sql` into `dist/`)

**Interfaces:**
- Consumes: `process.env.DATABASE_URL`.
- Produces (used by Task 6 routes and Task 7 worker):
  - `pool: pg.Pool`
  - `type JobStatus = "queued" | "processing" | "completed" | "failed"`
  - `interface JobRow { id: string; youtube_url: string; instrument: string; status: JobStatus; musicxml: string | null; error: string | null; created_at: Date; updated_at: Date }`
  - `createTableIfNotExists(): Promise<void>`
  - `insertJob(id: string, youtubeUrl: string, instrument: string): Promise<void>`
  - `getJob(id: string): Promise<JobRow | null>`
  - `markProcessing(id: string): Promise<void>`
  - `markCompleted(id: string, musicxml: string): Promise<void>`
  - `markFailed(id: string, error: string): Promise<void>`

- [ ] **Step 1: Install dependencies**

```bash
cd server
npm install pg
npm install -D @types/pg
```

- [ ] **Step 2: Write the schema**

```sql
-- server/src/db/schema.sql
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
```

- [ ] **Step 3: Implement db.ts**

```typescript
// server/src/db.ts
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const { Pool } = pg;

const SCHEMA_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), "db", "schema.sql");

export const pool = new pg.Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.DATABASE_URL?.includes("sslmode=require") ? { rejectUnauthorized: false } : undefined,
});

export type JobStatus = "queued" | "processing" | "completed" | "failed";

export interface JobRow {
  id: string;
  youtube_url: string;
  instrument: string;
  status: JobStatus;
  musicxml: string | null;
  error: string | null;
  created_at: Date;
  updated_at: Date;
}

export async function createTableIfNotExists(): Promise<void> {
  const schema = await readFile(SCHEMA_PATH, "utf-8");
  await pool.query(schema);
}

export async function insertJob(id: string, youtubeUrl: string, instrument: string): Promise<void> {
  await pool.query(
    `INSERT INTO jobs (id, youtube_url, instrument, status) VALUES ($1, $2, $3, 'queued')`,
    [id, youtubeUrl, instrument],
  );
}

export async function getJob(id: string): Promise<JobRow | null> {
  const result = await pool.query<JobRow>(`SELECT * FROM jobs WHERE id = $1`, [id]);
  return result.rows[0] ?? null;
}

export async function markProcessing(id: string): Promise<void> {
  await pool.query(`UPDATE jobs SET status = 'processing', updated_at = now() WHERE id = $1`, [id]);
}

export async function markCompleted(id: string, musicxml: string): Promise<void> {
  await pool.query(
    `UPDATE jobs SET status = 'completed', musicxml = $2, updated_at = now() WHERE id = $1`,
    [id, musicxml],
  );
}

export async function markFailed(id: string, error: string): Promise<void> {
  await pool.query(
    `UPDATE jobs SET status = 'failed', error = $2, updated_at = now() WHERE id = $1`,
    [id, error],
  );
}
```

Note: `Pool`'s `ssl` option is `undefined` for local dev (docker-compose Postgres has no SSL) and auto-enables for any `DATABASE_URL` containing `sslmode=require` (matches Neon/Supabase-style connection strings from `DEPLOY.md`) — not spec-mandated, but a one-line default that avoids a footgun when this eventually points at production.

- [ ] **Step 4: Extend the build script to copy schema.sql**

Update `server/package.json`'s `build` script:

```json
"build": "tsc && node -e \"require('fs').cpSync('src/db','dist/db',{recursive:true})\""
```

(`tsc` only compiles `.ts` files, so `db/schema.sql` needs an explicit copy into `dist/` or the compiled `dist/db.js` can't find it at runtime.)

- [ ] **Step 5: Verify it compiles and the schema copies correctly**

Run (from `server/`): `npm run build`
Expected: `dist/db.js` and `dist/db/schema.sql` both exist (`ls dist/db`).

- [ ] **Step 6: Commit**

```bash
git add server/src/db.ts server/src/db/schema.sql server/package.json server/package-lock.json
git commit -m "feat(server): add Postgres jobs table and query helpers"
```

---

### Task 3: BullMQ queue

**Files:**
- Create: `server/src/queue.ts`
- Modify: `server/package.json` (add `bullmq`, `ioredis` deps)

**Interfaces:**
- Consumes: `process.env.REDIS_URL`.
- Produces (used by Task 6 routes and Task 7 worker):
  - `QUEUE_NAME: string`
  - `interface TranscribeJobData { youtubeUrl: string; instrument: string }`
  - `connection: IORedis`
  - `transcribeQueue: Queue<TranscribeJobData>`

- [ ] **Step 1: Install dependencies**

```bash
cd server
npm install bullmq ioredis
```

- [ ] **Step 2: Implement queue.ts**

```typescript
// server/src/queue.ts
import { Queue } from "bullmq";
import IORedis from "ioredis";

export const QUEUE_NAME = "transcribe";

export interface TranscribeJobData {
  youtubeUrl: string;
  instrument: string;
}

// BullMQ requires maxRetriesPerRequest: null on any connection it drives
// (both Queue and Worker) — otherwise ioredis warns/misbehaves on its
// blocking commands.
export const connection = new IORedis(process.env.REDIS_URL ?? "redis://localhost:6380", {
  maxRetriesPerRequest: null,
});

export const transcribeQueue = new Queue<TranscribeJobData>(QUEUE_NAME, { connection });
```

- [ ] **Step 3: Verify it compiles**

Run (from `server/`): `npm run build`
Expected: no TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add server/src/queue.ts server/package.json server/package-lock.json
git commit -m "feat(server): add BullMQ transcribe queue"
```

---

### Task 4: Python pipeline runner

**Files:**
- Create: `server/src/pipeline.ts`

**Interfaces:**
- Consumes: nothing new (Node built-ins only).
- Produces (used by Task 7 worker): `runPipeline(jobId: string, youtubeUrl: string): Promise<{ musicxmlPath: string }>`.

- [ ] **Step 1: Implement pipeline.ts**

```typescript
// server/src/pipeline.ts
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// server/src/pipeline.ts -> ../../ml (repo root/ml). Same relative depth
// from dist/pipeline.js, and from /app/server/dist/pipeline.js in the
// Docker image (-> /app/ml), so this resolves correctly everywhere.
const ML_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../ml");
const TRANSCRIBE_SCRIPT = path.join(ML_DIR, "transcribe.py");
const NOTATION_SCRIPT = path.join(ML_DIR, "notation.py");

const CHILD_TIMEOUT_MS = 10 * 60 * 1000; // spec: "generous hard timeout (10 minutes)"
const STDERR_EXCERPT_LIMIT = 2000;

function resolvePythonPath(): string {
  return process.platform === "win32"
    ? path.join(ML_DIR, ".venv", "Scripts", "python.exe")
    : path.join(ML_DIR, ".venv", "bin", "python");
}

function runPythonScript(scriptPath: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(resolvePythonPath(), [scriptPath, ...args], { timeout: CHILD_TIMEOUT_MS });

    let stderr = "";
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });

    child.on("error", (err) => reject(err));

    child.on("close", (code, signal) => {
      if (signal) {
        reject(
          new Error(
            `${path.basename(scriptPath)} was killed (${signal}), likely a timeout after ${CHILD_TIMEOUT_MS / 1000}s`,
          ),
        );
      } else if (code !== 0) {
        // Tail, not head: Python tracebacks put the actual exception
        // message last, which is the useful part to surface.
        const excerpt = stderr.trim().slice(-STDERR_EXCERPT_LIMIT);
        reject(new Error(`${path.basename(scriptPath)} exited with code ${code}: ${excerpt}`));
      } else {
        resolve();
      }
    });
  });
}

export async function runPipeline(jobId: string, youtubeUrl: string): Promise<{ musicxmlPath: string }> {
  const jobDir = path.join(ML_DIR, "output", jobId);
  const notesPath = path.join(jobDir, "notes.json");
  const musicxmlPath = path.join(jobDir, "score.musicxml");

  await runPythonScript(TRANSCRIBE_SCRIPT, [youtubeUrl, "--output", notesPath]);
  await runPythonScript(NOTATION_SCRIPT, ["--input", notesPath, "--output", musicxmlPath]);

  return { musicxmlPath };
}
```

- [ ] **Step 2: Verify it compiles**

Run (from `server/`): `npm run build`
Expected: no TypeScript errors.

- [ ] **Step 3: Commit**

```bash
git add server/src/pipeline.ts
git commit -m "feat(server): add child_process runner for the ml/ pipeline"
```

---

### Task 5: POST /transcribe and GET /transcribe/:jobId/status

**Files:**
- Create: `server/src/routes/transcribe.ts`
- Modify: `server/src/index.ts` (mount the router, call `createTableIfNotExists()`)

**Interfaces:**
- Consumes: `validateYoutubeUrl` (Task 1), `insertJob`/`getJob`/`createTableIfNotExists` (Task 2), `transcribeQueue` (Task 3).
- Produces: `transcribeRouter: express.Router`, mounted in `index.ts`.

- [ ] **Step 1: Implement the router**

```typescript
// server/src/routes/transcribe.ts
import { randomUUID } from "node:crypto";
import { Router } from "express";
import { getJob, insertJob } from "../db.js";
import { transcribeQueue } from "../queue.js";
import { validateYoutubeUrl } from "../validation.js";

export const transcribeRouter = Router();

transcribeRouter.post("/transcribe", async (req, res) => {
  const body = req.body as { youtubeUrl?: unknown; instrument?: unknown };

  if (typeof body.youtubeUrl !== "string" || !validateYoutubeUrl(body.youtubeUrl)) {
    res.status(400).json({ error: "youtubeUrl must be a valid YouTube video URL" });
    return;
  }
  if (typeof body.instrument !== "string" || body.instrument.trim() === "") {
    res.status(400).json({ error: "instrument is required" });
    return;
  }

  const jobId = randomUUID();
  await insertJob(jobId, body.youtubeUrl, body.instrument);
  await transcribeQueue.add(
    "transcribe",
    { youtubeUrl: body.youtubeUrl, instrument: body.instrument },
    { jobId, attempts: 1 },
  );

  res.status(202).json({ jobId });
});

transcribeRouter.get("/transcribe/:jobId/status", async (req, res) => {
  const job = await getJob(req.params.jobId);
  if (!job) {
    res.status(404).json({ error: "job not found" });
    return;
  }

  res.status(200).json({
    jobId: job.id,
    status: job.status,
    youtubeUrl: job.youtube_url,
    instrument: job.instrument,
    createdAt: job.created_at.toISOString(),
    updatedAt: job.updated_at.toISOString(),
    ...(job.musicxml !== null ? { musicxml: job.musicxml } : {}),
    ...(job.error !== null ? { error: job.error } : {}),
  });
});
```

- [ ] **Step 2: Wire it into index.ts**

```typescript
// server/src/index.ts
import cors from "cors";
import express from "express";
import { createTableIfNotExists } from "./db.js";
import { transcribeRouter } from "./routes/transcribe.js";

const app = express();
const PORT = process.env.PORT ?? 3010;

app.use(cors({ origin: process.env.CLIENT_ORIGIN ?? "*" }));
app.use(express.json());
app.use(transcribeRouter);

await createTableIfNotExists();

app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
```

(The `RUN_WORKER_INLINE` addition to `index.ts` happens in Task 6, once `startWorker` exists — adding it here would be a forward reference to a file that doesn't exist yet.)

- [ ] **Step 3: Verify it compiles**

Run (from `server/`): `npm run build`
Expected: no TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add server/src/routes/transcribe.ts server/src/index.ts
git commit -m "feat(server): add POST /transcribe and GET /transcribe/:jobId/status"
```

---

### Task 6: BullMQ worker (standalone + inline-startable)

**Files:**
- Create: `server/src/worker.ts`
- Modify: `server/src/index.ts` (start the worker in-process when `RUN_WORKER_INLINE=true`)
- Modify: `server/package.json` (add `"worker"` and `"start:worker"` scripts)
- Modify: `server/.env.example` (document `RUN_WORKER_INLINE`)

**Interfaces:**
- Consumes: `connection`/`QUEUE_NAME`/`TranscribeJobData` (Task 3), `markProcessing`/`markCompleted`/`markFailed`/`createTableIfNotExists` (Task 2), `runPipeline` (Task 4).
- Produces: `startWorker(): Worker<TranscribeJobData>`, importable from `index.ts`.

- [ ] **Step 1: Implement worker.ts**

```typescript
// server/src/worker.ts
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Worker } from "bullmq";
import { createTableIfNotExists, markCompleted, markFailed, markProcessing } from "./db.js";
import { connection, QUEUE_NAME, type TranscribeJobData } from "./queue.js";
import { runPipeline } from "./pipeline.js";

export function startWorker(): Worker<TranscribeJobData> {
  const worker = new Worker<TranscribeJobData>(
    QUEUE_NAME,
    async (job) => {
      const { id } = job;
      if (!id) throw new Error("job has no id");

      await markProcessing(id);
      const { musicxmlPath } = await runPipeline(id, job.data.youtubeUrl);
      const musicxml = await readFile(musicxmlPath, "utf-8");
      await markCompleted(id, musicxml);
    },
    { connection, concurrency: 1 },
  );

  worker.on("failed", async (job, err) => {
    if (!job?.id) return;
    await markFailed(job.id, err instanceof Error ? err.message : String(err));
  });

  worker.on("error", (err) => {
    console.error("Worker error:", err);
  });

  console.log("Worker started, waiting for jobs...");
  return worker;
}

// Only auto-run when this file is the process entrypoint (`npm run worker`
// / `node dist/worker.js`) — not when index.ts imports startWorker for
// RUN_WORKER_INLINE mode.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await createTableIfNotExists();
  startWorker();
}
```

- [ ] **Step 2: Wire optional inline start into index.ts**

```typescript
// server/src/index.ts — add this import and block
import { startWorker } from "./worker.js";

// ... after `await createTableIfNotExists();`, before `app.listen(...)`:
if (process.env.RUN_WORKER_INLINE === "true") {
  startWorker();
}
```

- [ ] **Step 3: Add worker scripts to package.json**

```json
"worker": "tsx watch src/worker.ts",
"start:worker": "node dist/worker.js"
```

- [ ] **Step 4: Document RUN_WORKER_INLINE in .env.example**

```
# Set to "true" only for combined single-process deployment (Render). Leave
# unset for local dev — run `npm run worker` as a second process instead.
RUN_WORKER_INLINE=false
```

- [ ] **Step 5: Verify it compiles**

Run (from `server/`): `npm run build`
Expected: no TypeScript errors.

- [ ] **Step 6: Commit**

```bash
git add server/src/worker.ts server/src/index.ts server/package.json server/.env.example
git commit -m "feat(server): add BullMQ worker, standalone and inline-startable"
```

---

### Task 7: Update DEPLOY.md for the now-implemented worker wiring

**Files:**
- Modify: `DEPLOY.md`

- [ ] **Step 1: Replace the "Worker note" callout**

Replace the existing `<a id="worker-note"></a>` paragraph with:

```markdown
<a id="worker-note"></a>
**Worker note:** `src/worker.ts` exports `startWorker()` and runs standalone
via `npm run worker` for local dev (two processes, matching docker-compose).
For Render's single free Web Service, set `RUN_WORKER_INLINE=true` so
`index.ts` starts the worker in-process instead — no Dockerfile change
needed, `CMD ["node", "dist/index.js"]` already covers both cases.
```

- [ ] **Step 2: Add RUN_WORKER_INLINE to the server env var table**

Add a row to the `server/` (Render) env var table:

```markdown
| `RUN_WORKER_INLINE` | `true` | Starts the worker in-process alongside the API. Required on Render (see Worker note above). Leave unset for local dev. |
```

- [ ] **Step 3: Commit**

```bash
git add DEPLOY.md
git commit -m "docs: reflect implemented worker wiring in DEPLOY.md"
```

---

### Task 8: End-to-end verification

**Files:** none (verification only).

- [ ] **Step 1: Start local infra**

```bash
docker compose up -d
```

Expected: `smg-postgres` and `smg-redis` running (`docker ps`), no effect on any other project's containers.

- [ ] **Step 2: Run the full test suite and build**

```bash
cd server
npm test
npm run build
```

Expected: all tests pass, build succeeds.

- [ ] **Step 3: Start the API and worker as two processes**

```bash
# terminal 1
cd server && npm run dev
# terminal 2
cd server && npm run worker
```

Expected: API logs `Server listening on port 3010`; worker logs `Worker started, waiting for jobs...`; both connect to Postgres/Redis without errors (confirms `createTableIfNotExists` ran cleanly against the docker-compose Postgres).

- [ ] **Step 4: Exercise the failure path end-to-end**

```bash
curl -s -X POST http://localhost:3010/transcribe \
  -H "Content-Type: application/json" \
  -d '{"youtubeUrl":"https://www.youtube.com/watch?v=00000000000","instrument":"piano"}'
```

Poll the returned `jobId`:

```bash
curl -s http://localhost:3010/transcribe/<jobId>/status
```

Expected: status moves `queued` → `processing` → `failed`, with a non-empty `error` message (yt-dlp couldn't find the video) — this exercises validation, insert, enqueue, worker pickup, `markProcessing`, the real `transcribe.py` subprocess failing, and `markFailed`, i.e. everything except a successful transcription.

- [ ] **Step 5: Exercise validation errors**

```bash
curl -s -X POST http://localhost:3010/transcribe -H "Content-Type: application/json" -d '{"youtubeUrl":"not a url","instrument":"piano"}'
curl -s -X POST http://localhost:3010/transcribe -H "Content-Type: application/json" -d '{"youtubeUrl":"https://www.youtube.com/watch?v=abc","instrument":""}'
curl -s http://localhost:3010/transcribe/does-not-exist/status
```

Expected: first two return 400 with `{"error": "..."}`, third returns 404 with `{"error": "..."}`.

- [ ] **Step 6: Note the success-path gap**

The success path (a real video transcribing all the way to `completed` with MusicXML) needs a real YouTube URL, which should be a video you choose (per your "I'll test it against a solo piano YouTube video" from earlier) rather than one guessed for this plan. Everything upstream of basic-pitch's actual output — routing, queueing, Postgres writes, child_process orchestration, error propagation — is covered by Steps 4-5; only "does a real successful transcription reach `completed` with `musicxml` populated" remains to be checked with a real URL.

- [ ] **Step 7: Tear down**

```bash
# Ctrl-C both npm run dev / npm run worker processes
docker compose down
```

Expected: `docker ps` shows only your other projects' containers (e.g. studycast), nothing left running from this verification.
