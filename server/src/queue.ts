import { Queue } from "bullmq";
import { Redis } from "ioredis";
import type { JobMode } from "./db.js";

export const QUEUE_NAME = "transcribe";

export interface TranscribeJobData {
  youtubeUrl: string;
  instrument: string;
  mode: JobMode;
}

// BullMQ requires maxRetriesPerRequest: null on any connection it drives
// (both Queue and Worker) — otherwise ioredis warns/misbehaves on its
// blocking commands.
export const connection = new Redis(process.env.REDIS_URL ?? "redis://localhost:6380", {
  maxRetriesPerRequest: null,
});

export const transcribeQueue = new Queue<TranscribeJobData>(QUEUE_NAME, { connection });

const ENQUEUE_TIMEOUT_MS = 5000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out after ${ms}ms`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      },
    );
  });
}

// maxRetriesPerRequest: null (required above for BullMQ's blocking commands)
// also means non-blocking commands like .add() retry a down connection
// forever instead of rejecting. Wrapping in a timeout keeps a Redis outage
// from hanging the HTTP request indefinitely.
export async function enqueueTranscribeJob(jobId: string, data: TranscribeJobData): Promise<void> {
  await withTimeout(transcribeQueue.add("transcribe", data, { jobId, attempts: 1 }), ENQUEUE_TIMEOUT_MS);
}
