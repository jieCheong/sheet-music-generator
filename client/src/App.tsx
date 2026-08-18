import { jsPDF } from "jspdf";
import "svg2pdf.js";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { useEffect, useRef, useState, type FormEvent } from "react";
import "./App.css";
import { getJobStatus, submitTranscription, type JobStatus } from "./api";

const INSTRUMENT = "piano";
const POLL_INTERVAL_MS = 2000;
const A4_WIDTH_MM = 210;
const A4_HEIGHT_MM = 297;

type AppState =
  | { phase: "idle" }
  | { phase: "submitting" }
  | { phase: "polling"; jobId: string; status: JobStatus }
  | { phase: "completed"; jobId: string; musicxml: string }
  | { phase: "failed"; error: string };

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function App() {
  const [url, setUrl] = useState("");
  const [state, setState] = useState<AppState>({ phase: "idle" });
  const [downloadingPdf, setDownloadingPdf] = useState(false);
  const osmdContainerRef = useRef<HTMLDivElement>(null);

  const pollingJobId = state.phase === "polling" ? state.jobId : null;

  useEffect(() => {
    if (pollingJobId === null) return;

    let cancelled = false;

    const poll = async () => {
      try {
        const job = await getJobStatus(pollingJobId);
        if (cancelled) return;

        if (job.status === "completed") {
          setState({ phase: "completed", jobId: pollingJobId, musicxml: job.musicxml ?? "" });
        } else if (job.status === "failed") {
          setState({ phase: "failed", error: job.error ?? "Transcription failed." });
        } else {
          setState({ phase: "polling", jobId: pollingJobId, status: job.status });
        }
      } catch (err) {
        if (!cancelled) {
          setState({ phase: "failed", error: errorMessage(err) });
        }
      }
    };

    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [pollingJobId]);

  const completedMusicXml = state.phase === "completed" ? state.musicxml : null;

  useEffect(() => {
    if (completedMusicXml === null || !osmdContainerRef.current) return;

    let cancelled = false;
    const container = osmdContainerRef.current;
    container.innerHTML = "";

    const osmd = new OpenSheetMusicDisplay(container, { autoResize: true, pageFormat: "A4_P" });

    osmd
      .load(completedMusicXml)
      .then(() => {
        if (!cancelled) osmd.render();
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({ phase: "failed", error: `Failed to render sheet music: ${errorMessage(err)}` });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [completedMusicXml]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;

    setState({ phase: "submitting" });
    try {
      const { jobId } = await submitTranscription(trimmed, INSTRUMENT);
      setState({ phase: "polling", jobId, status: "queued" });
    } catch (err) {
      setState({ phase: "failed", error: errorMessage(err) });
    }
  };

  const handleDownloadPdf = async () => {
    const container = osmdContainerRef.current;
    if (!container) return;

    // OSMD (pageFormat: "A4_P") renders one <svg> per page.
    const pages = Array.from(container.querySelectorAll("svg"));
    if (pages.length === 0) return;

    setDownloadingPdf(true);
    try {
      const doc = new jsPDF({ orientation: "portrait", unit: "mm", format: "a4" });

      for (let i = 0; i < pages.length; i++) {
        if (i > 0) doc.addPage();
        await doc.svg(pages[i], { x: 0, y: 0, width: A4_WIDTH_MM, height: A4_HEIGHT_MM });
      }

      doc.save("sheet-music.pdf");
    } catch (err) {
      setState({ phase: "failed", error: `Failed to generate PDF: ${errorMessage(err)}` });
    } finally {
      setDownloadingPdf(false);
    }
  };

  const busy = state.phase === "submitting" || state.phase === "polling";

  return (
    <main className="page">
      <h1>YouTube to Sheet Music</h1>

      <form className="url-form" onSubmit={handleSubmit}>
        <input
          type="url"
          placeholder="https://www.youtube.com/watch?v=..."
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          disabled={busy}
          required
        />
        <button type="submit" disabled={busy}>
          {state.phase === "submitting" ? "Submitting..." : "Transcribe"}
        </button>
      </form>

      {state.phase === "polling" && (
        <div className="status-panel loading" role="status">
          <span className="spinner" aria-hidden="true" />
          <p>{state.status === "queued" ? "Waiting in queue..." : "Transcribing audio..."}</p>
        </div>
      )}

      {state.phase === "failed" && (
        <div className="status-panel error" role="alert">
          <p>{state.error}</p>
          <button type="button" onClick={() => setState({ phase: "idle" })}>
            Try again
          </button>
        </div>
      )}

      {state.phase === "completed" && (
        <button type="button" className="download-pdf" onClick={handleDownloadPdf} disabled={downloadingPdf}>
          {downloadingPdf ? "Preparing PDF..." : "Download PDF"}
        </button>
      )}

      <div ref={osmdContainerRef} className="score-container" hidden={state.phase !== "completed"} />
    </main>
  );
}

export default App;
