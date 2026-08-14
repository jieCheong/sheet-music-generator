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
