const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:3010";

export type JobStatus = "queued" | "processing" | "completed" | "failed";
export type JobMode = "transcription" | "church_sheet";

export interface JobStatusResponse {
  jobId: string;
  status: JobStatus;
  youtubeUrl: string;
  instrument: string;
  mode: JobMode;
  createdAt: string;
  updatedAt: string;
  musicxml?: string;
  error?: string;
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: string };
    if (body.error) return body.error;
  } catch {
    // response wasn't JSON; fall through to the generic message
  }
  return `Request failed with status ${response.status}`;
}

export async function submitTranscription(
  youtubeUrl: string,
  instrument: string,
  mode: JobMode,
): Promise<{ jobId: string }> {
  const response = await fetch(`${API_BASE}/transcribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ youtubeUrl, instrument, mode }),
  });

  if (!response.ok) {
    throw new Error(await parseErrorMessage(response));
  }

  return response.json() as Promise<{ jobId: string }>;
}

export async function getJobStatus(jobId: string): Promise<JobStatusResponse> {
  const response = await fetch(`${API_BASE}/transcribe/${jobId}/status`);

  if (!response.ok) {
    throw new Error(await parseErrorMessage(response));
  }

  return response.json() as Promise<JobStatusResponse>;
}
