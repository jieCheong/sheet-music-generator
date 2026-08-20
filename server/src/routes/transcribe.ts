import { randomUUID } from "node:crypto";
import { Router } from "express";
import { getJob, insertJob, markFailed, type JobMode } from "../db.js";
import { enqueueTranscribeJob } from "../queue.js";
import { validateYoutubeUrl } from "../validation.js";

const VALID_MODES: readonly JobMode[] = ["transcription", "church_sheet"];

export const transcribeRouter = Router();

transcribeRouter.post("/transcribe", async (req, res) => {
  const body = req.body as { youtubeUrl?: unknown; instrument?: unknown; mode?: unknown };

  if (typeof body.youtubeUrl !== "string" || !validateYoutubeUrl(body.youtubeUrl)) {
    res.status(400).json({ error: "youtubeUrl must be a valid YouTube video URL" });
    return;
  }
  if (typeof body.instrument !== "string" || body.instrument.trim() === "") {
    res.status(400).json({ error: "instrument is required" });
    return;
  }
  // Defaults to church_sheet (the easy/beginner-friendly arrangement) rather
  // than transcription -- that's the mode the app is meant to hand new
  // users by default; transcription is the opt-in, detailed alternative.
  const mode: JobMode = body.mode === undefined ? "church_sheet" : (body.mode as JobMode);
  if (!VALID_MODES.includes(mode)) {
    res.status(400).json({ error: `mode must be one of: ${VALID_MODES.join(", ")}` });
    return;
  }

  const jobId = randomUUID();
  await insertJob(jobId, body.youtubeUrl, body.instrument, mode);

  try {
    await enqueueTranscribeJob(jobId, { youtubeUrl: body.youtubeUrl, instrument: body.instrument, mode });
  } catch (err) {
    // Redis was unreachable/slow: don't leave the row stuck at "queued"
    // forever with no worker ever going to see it.
    await markFailed(jobId, `Failed to queue job: ${err instanceof Error ? err.message : String(err)}`);
    res.status(503).json({ error: "Unable to queue transcription job right now, please try again" });
    return;
  }

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
    mode: job.mode,
    createdAt: job.created_at.toISOString(),
    updatedAt: job.updated_at.toISOString(),
    ...(job.musicxml !== null ? { musicxml: job.musicxml } : {}),
    ...(job.error !== null ? { error: job.error } : {}),
  });
});
