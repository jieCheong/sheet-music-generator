import { Queue } from "bullmq";
import { Redis } from "ioredis";

export const QUEUE_NAME = "transcribe";

export interface TranscribeJobData {
  youtubeUrl: string;
  instrument: string;
}

// BullMQ requires maxRetriesPerRequest: null on any connection it drives
// (both Queue and Worker) — otherwise ioredis warns/misbehaves on its
// blocking commands.
export const connection = new Redis(process.env.REDIS_URL ?? "redis://localhost:6380", {
  maxRetriesPerRequest: null,
});

export const transcribeQueue = new Queue<TranscribeJobData>(QUEUE_NAME, { connection });
