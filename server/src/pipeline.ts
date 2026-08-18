import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// server/src/pipeline.ts -> ../../ml (repo root/ml). Same relative depth
// from dist/pipeline.js, and from /app/server/dist/pipeline.js in the
// Docker image (-> /app/ml), so this resolves correctly everywhere.
const ML_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../ml");
const TRANSCRIBE_SCRIPT = path.join(ML_DIR, "transcribe.py");
const NOTATION_SCRIPT = path.join(ML_DIR, "notation.py");

// basic-pitch's inference is CPU-bound and scales with audio length; a real
// ~5 minute video took >10 minutes under normal machine load (downloads
// finish in seconds -- inference is where the time goes), so 10 minutes
// wasn't actually "generous" for anything but short clips. 20 minutes
// still bounds a genuine hang while giving longer videos real headroom.
const CHILD_TIMEOUT_MS = 20 * 60 * 1000;
const STDERR_EXCERPT_LIMIT = 2000;

function resolvePythonPath(): string {
  return process.platform === "win32"
    ? path.join(ML_DIR, ".venv", "Scripts", "python.exe")
    : path.join(ML_DIR, ".venv", "bin", "python");
}

// Line-buffers a child stream and logs each line with a prefix, so a job's
// progress (e.g. transcribe.py's "Downloading audio...", "Running
// basic-pitch prediction...") shows up live in the worker's own log instead
// of only being visible by inspecting the OS process directly.
function logWithPrefix(stream: NodeJS.ReadableStream, prefix: string): void {
  let buffer = "";
  stream.on("data", (chunk: Buffer) => {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (line.trim()) console.log(`[${prefix}] ${line}`);
    }
  });
  stream.on("end", () => {
    if (buffer.trim()) console.log(`[${prefix}] ${buffer}`);
  });
}

function runPythonScript(scriptPath: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(resolvePythonPath(), [scriptPath, ...args], { timeout: CHILD_TIMEOUT_MS });
    const label = path.basename(scriptPath);

    let stderr = "";
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    logWithPrefix(child.stdout, label);
    logWithPrefix(child.stderr, `${label} stderr`);

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

  const transcribeArgs = [youtubeUrl, "--output", notesPath];
  // Local-dev-only escape hatch for YouTube's bot detection: reads cookies
  // from a real logged-in browser on this machine. Never set in production
  // (Render has no browser/user session to read from).
  const cookiesFromBrowser = process.env.YT_DLP_COOKIES_BROWSER;
  if (cookiesFromBrowser) {
    transcribeArgs.push("--cookies-from-browser", cookiesFromBrowser);
  }

  await runPythonScript(TRANSCRIBE_SCRIPT, transcribeArgs);
  await runPythonScript(NOTATION_SCRIPT, ["--input", notesPath, "--output", musicxmlPath]);

  return { musicxmlPath };
}
