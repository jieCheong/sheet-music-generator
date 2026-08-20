import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const SCHEMA_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), "db", "schema.sql");

export const pool = new pg.Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.DATABASE_URL?.includes("sslmode=require") ? { rejectUnauthorized: false } : undefined,
});

export type JobStatus = "queued" | "processing" | "completed" | "failed";

export type JobMode = "transcription" | "church_sheet";

export interface JobRow {
  id: string;
  youtube_url: string;
  instrument: string;
  mode: JobMode;
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

export async function insertJob(id: string, youtubeUrl: string, instrument: string, mode: JobMode): Promise<void> {
  await pool.query(
    `INSERT INTO jobs (id, youtube_url, instrument, mode, status) VALUES ($1, $2, $3, $4, 'queued')`,
    [id, youtubeUrl, instrument, mode],
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
