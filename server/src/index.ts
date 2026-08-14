import cors from "cors";
import express from "express";
import { createTableIfNotExists } from "./db.js";
import { transcribeRouter } from "./routes/transcribe.js";
import { startWorker } from "./worker.js";

const app = express();
const PORT = process.env.PORT ?? 3010;

app.use(cors({ origin: process.env.CLIENT_ORIGIN ?? "*" }));
app.use(express.json());
app.use(transcribeRouter);

await createTableIfNotExists();

if (process.env.RUN_WORKER_INLINE === "true") {
  startWorker();
}

app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
