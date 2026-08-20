import { jsPDF } from "jspdf";
import "svg2pdf.js";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { useEffect, useRef, useState, type FormEvent } from "react";
import "./App.css";
import { getJobStatus, submitTranscription, type JobMode, type JobStatus } from "./api";

const INSTRUMENT = "piano";
const POLL_INTERVAL_MS = 2000;
const A4_WIDTH_MM = 210;
const A4_HEIGHT_MM = 297;
// OSMD lays out pagination (systems per page) based on the actual pixel
// width of the element it renders into -- not just visual CSS scaling. A
// narrow viewport fed directly to OSMD as its render width makes it think
// an A4 page is only that many pixels wide, so far fewer systems fit per
// page (confirmed: 10 pages at 900px vs. 71 at 350px for the same piece,
// mostly near-empty). Always rendering at this fixed width, then scaling
// the result down with a CSS transform for narrow screens, keeps
// pagination identical regardless of viewport.
const OSMD_RENDER_WIDTH = 850;

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
  const [mode, setMode] = useState<JobMode>("church_sheet");
  const [state, setState] = useState<AppState>({ phase: "idle" });
  const [downloadingPdf, setDownloadingPdf] = useState(false);
  // scaleWrapperRef: the visible, responsive box (width scales with the page).
  // renderTargetRef: fixed-width child OSMD actually renders into; scaled
  // down via CSS transform to fit inside scaleWrapperRef.
  const scaleWrapperRef = useRef<HTMLDivElement>(null);
  const renderTargetRef = useRef<HTMLDivElement>(null);

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

  // Scales renderTargetRef (fixed OSMD_RENDER_WIDTH) down to fit
  // scaleWrapperRef's actual available width, and sets the wrapper's height
  // to match -- a CSS transform doesn't affect layout flow, so without this
  // the wrapper would either clip the scaled-down content or leave the
  // original (unscaled) empty space beneath it.
  const rescale = () => {
    const wrapper = scaleWrapperRef.current;
    const target = renderTargetRef.current;
    if (!wrapper || !target) return;

    const style = getComputedStyle(wrapper);
    const paddingX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const paddingY = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);

    const scale = Math.min(1, (wrapper.clientWidth - paddingX) / OSMD_RENDER_WIDTH);
    target.style.transform = `scale(${scale})`;
    wrapper.style.height = `${target.scrollHeight * scale + paddingY}px`;
  };

  useEffect(() => {
    if (completedMusicXml === null || !renderTargetRef.current) return;

    let cancelled = false;
    const target = renderTargetRef.current;
    target.innerHTML = "";

    const osmd = new OpenSheetMusicDisplay(target, { autoResize: false, pageFormat: "A4_P" });
    // Default (4) renders titles at a fixed size that doesn't shrink to fit
    // the page -- an overlong title just overflows off the edge. notation.py
    // also caps title length at the source, but this is a second line of
    // defense for any title that still doesn't fit.
    osmd.EngravingRules.SheetTitleHeight = 2.5;

    osmd
      .load(completedMusicXml)
      .then(() => {
        if (cancelled) return;
        osmd.render();
        rescale();
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

  useEffect(() => {
    if (completedMusicXml === null) return;
    window.addEventListener("resize", rescale);
    return () => window.removeEventListener("resize", rescale);
  }, [completedMusicXml]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;

    setState({ phase: "submitting" });
    try {
      const { jobId } = await submitTranscription(trimmed, INSTRUMENT, mode);
      setState({ phase: "polling", jobId, status: "queued" });
    } catch (err) {
      setState({ phase: "failed", error: errorMessage(err) });
    }
  };

  const handleDownloadPdf = async () => {
    const target = renderTargetRef.current;
    if (!target) return;

    // OSMD (pageFormat: "A4_P") renders one <svg> per page.
    const pages = Array.from(target.querySelectorAll("svg"));
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
      <header className="app-header">
        <div className="brand">
          <span className="brand-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
              <circle cx="6" cy="18" r="2.6" fill="currentColor" />
              <circle cx="16" cy="16" r="2.6" fill="currentColor" />
              <path
                d="M8.6 18V6.5L18.6 4.5V14.5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <h1>Sheet Music</h1>
        </div>
        <p className="tagline">YouTube &rarr; Piano Score, Entirely Offline</p>
      </header>

      <section className="mode-section">
        <h2 className="section-label">Output Mode</h2>
        <div className="mode-toggle" role="radiogroup" aria-label="Arrangement style">
          <label className={mode === "church_sheet" ? "selected" : undefined}>
            <input
              type="radio"
              name="mode"
              value="church_sheet"
              checked={mode === "church_sheet"}
              onChange={() => setMode("church_sheet")}
              disabled={busy}
            />
            <span className="mode-icon" aria-hidden="true">
              &#9834;
            </span>
            <span className="mode-title">Easy Piano</span>
            <span className="mode-desc">Melody + generated left-hand accompaniment, beginner-readable</span>
          </label>
          <label className={mode === "transcription" ? "selected" : undefined}>
            <input
              type="radio"
              name="mode"
              value="transcription"
              checked={mode === "transcription"}
              onChange={() => setMode("transcription")}
              disabled={busy}
            />
            <span className="mode-icon" aria-hidden="true">
              &#9835;
            </span>
            <span className="mode-title">Full Transcription</span>
            <span className="mode-desc">Every detected note, closest to the original recording</span>
          </label>
        </div>
      </section>

      <form className="url-form" onSubmit={handleSubmit}>
        <label className="field-label" htmlFor="youtube-url">
          YouTube URL
        </label>
        <input
          id="youtube-url"
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

      <div ref={scaleWrapperRef} className="score-container" hidden={state.phase !== "completed"}>
        <div ref={renderTargetRef} className="score-render-target" style={{ width: OSMD_RENDER_WIDTH }} />
      </div>

      <footer className="app-footer">yt-dlp &middot; basic-pitch &middot; music21 &middot; OpenSheetMusicDisplay</footer>
    </main>
  );
}

export default App;
