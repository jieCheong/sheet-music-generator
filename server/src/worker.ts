import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Worker } from "bullmq";
import { createTableIfNotExists, markCompleted, markFailed, markProcessing } from "./db.js";
import { connection, QUEUE_NAME, type TranscribeJobData } from "./queue.js";
import { runPipeline } from "./pipeline.js";

const MARK_COMPLETED_RETRIES = 3;
const MARK_COMPLETED_RETRY_DELAY_MS = 500;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// The pipeline already succeeded and the MusicXML is safely on disk at this
// point — worth a few retries on a transient write failure rather than
// mislabeling a real success as "failed" because Postgres hiccuped.
async function markCompletedWithRetry(id: string, musicxml: string): Promise<void> {
  for (let attempt = 1; ; attempt++) {
    try {
      await markCompleted(id, musicxml);
      return;
    } catch (err) {
      if (attempt >= MARK_COMPLETED_RETRIES) throw err;
      await sleep(MARK_COMPLETED_RETRY_DELAY_MS);
    }
  }
}

export function startWorker(): Worker<TranscribeJobData> {
  const worker = new Worker<TranscribeJobData>(
    QUEUE_NAME,
    async (job) => {
      const { id } = job;
      if (!id) throw new Error("job has no id");

      await markProcessing(id);
      const { musicxmlPath } = await runPipeline(id, job.data.youtubeUrl, job.data.mode);
      const musicxml = await readFile(musicxmlPath, "utf-8");
      await markCompletedWithRetry(id, musicxml);
    },
    { connection, concurrency: 1 },
  );

  worker.on("failed", (job, err) => {
    if (!job?.id) return;
    const message = err instanceof Error ? err.message : String(err);
    // Deliberately not awaited: EventEmitter listeners aren't awaited by
    // BullMQ, so an unhandled rejection here would crash the whole process
    // (the API too, under RUN_WORKER_INLINE) on a transient Postgres error.
    markFailed(job.id, message).catch((markFailedErr) => {
      console.error(`Failed to record failure for job ${job.id}:`, markFailedErr);
    });
  });

  worker.on("error", (err) => {
    console.error("Worker error:", err);
  });

  console.log("Worker started, waiting for jobs...");
  return worker;
}

async function main(): Promise<void> {
  try {
    await createTableIfNotExists();
  } catch (err) {
    console.error("Worker failed to initialize the database, exiting:", err);
    process.exit(1);
  }
  startWorker();
}

// Only auto-run when this file is the process entrypoint (`npm run worker`
// / `node dist/worker.js`) — not when index.ts imports startWorker for
// RUN_WORKER_INLINE mode.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await main();
}
