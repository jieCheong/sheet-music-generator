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
