import cors from "cors";
import express, { type NextFunction, type Request, type Response } from "express";
import { createTableIfNotExists } from "./db.js";
import { transcribeRouter } from "./routes/transcribe.js";
import { startWorker } from "./worker.js";

const app = express();
const PORT = process.env.PORT ?? 3010;

app.use(cors({ origin: process.env.CLIENT_ORIGIN ?? "*" }));
app.use(express.json());
app.use(transcribeRouter);

// Keeps unexpected errors (e.g. a transient DB failure) on the same
// { error: string } contract the rest of the API uses, instead of
// Express's default HTML error page.
app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
  console.error("Unhandled request error:", err);
  res.status(500).json({ error: "Internal server error" });
});

try {
  await createTableIfNotExists();
} catch (err) {
  console.error("Failed to initialize the database, exiting:", err);
  process.exit(1);
}

if (process.env.RUN_WORKER_INLINE === "true") {
  startWorker();
}

app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
