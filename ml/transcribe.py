#!/usr/bin/env python
"""CLI: transcribe a YouTube video's audio into raw note events with basic-pitch."""

import argparse
import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from basic_pitch.inference import predict

SCRIPT_DIR = Path(__file__).resolve().parent


def normalize_youtube_url(url: str) -> str:
    """Strip playlist/radio/timestamp params so yt-dlp always downloads
    exactly the linked video, not YouTube's algorithmic "radio" continuation
    (a URL with &list=RD...&start_radio=1 otherwise makes yt-dlp follow the
    mix instead of the specific video)."""
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("youtu.be"):
        video_id = parsed.path.lstrip("/")
    else:
        video_id = parse_qs(parsed.query).get("v", [None])[0]

    if not video_id:
        return url
    return f"https://www.youtube.com/watch?v={video_id}"


def download_audio(url: str, dest_dir: Path, cookies_from_browser: str | None = None) -> Path:
    url = normalize_youtube_url(url)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / "audio.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "quiet": True,
        "no_warnings": True,
        # YouTube's "web" client increasingly 403s yt-dlp's format URLs; the
        # android client uses a different endpoint that isn't affected.
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    audio_path = dest_dir / "audio.wav"
    if not audio_path.exists():
        raise RuntimeError("yt-dlp did not produce an audio file")
    return audio_path


def transcribe(audio_path: Path, onset_threshold: float, frame_threshold: float) -> list[dict]:
    _, _, raw_note_events = predict(
        str(audio_path),
        onset_threshold=onset_threshold,
        frame_threshold=frame_threshold,
    )

    notes = [
        {
            "pitch": int(pitch),
            "start_time": round(float(start_time), 4),
            "end_time": round(float(end_time), 4),
            "velocity": int(round(127 * amplitude)),
        }
        for start_time, end_time, pitch, amplitude, _pitch_bend in raw_note_events
    ]
    notes.sort(key=lambda note: note["start_time"])
    return notes


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe a YouTube video to note events.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--output",
        type=Path,
        default=SCRIPT_DIR / "output" / "notes.json",
        help="Path to write the note events JSON (default: output/notes.json)",
    )
    parser.add_argument(
        "--onset-threshold",
        type=float,
        default=0.5,
        help="Minimum energy required for a note onset to be considered present "
        "(default: 0.5, basic-pitch's default). Raise to suppress spurious notes.",
    )
    parser.add_argument(
        "--frame-threshold",
        type=float,
        default=0.3,
        help="Minimum energy required for a frame to be considered present "
        "(default: 0.3, basic-pitch's default). Raise to suppress spurious notes.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        help="Browser to read cookies from (e.g. chrome, firefox, edge) to get past "
        "YouTube's bot-check ('Sign in to confirm you're not a bot'). Requires that "
        "browser to be installed on this machine with a logged-in YouTube session, "
        "and usually that the browser be closed (it locks its cookie database while "
        "running). Local dev only -- there's no browser session in a deployment.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp_dir:
        print(f"Downloading audio from {args.url}...")
        audio_path = download_audio(args.url, Path(tmp_dir), args.cookies_from_browser)

        print("Running basic-pitch prediction...")
        notes = transcribe(audio_path, args.onset_threshold, args.frame_threshold)

    print(f"\nFound {len(notes)} note events:\n")
    for note in notes:
        print(
            f"pitch={note['pitch']:>3}  "
            f"start={note['start_time']:>8.3f}s  "
            f"end={note['end_time']:>8.3f}s  "
            f"velocity={note['velocity']:>3}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(notes, indent=2))
    print(f"\nSaved {len(notes)} note events to {args.output}")


if __name__ == "__main__":
    main()
