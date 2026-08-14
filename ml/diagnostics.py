#!/usr/bin/env python
"""CLI: print summary statistics on note events to help spot noisy transcription."""

import argparse
import json
from pathlib import Path

from music21 import pitch

SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Print summary statistics on a note events JSON file.")
    parser.add_argument(
        "--input",
        type=Path,
        default=SCRIPT_DIR / "output" / "notes.json",
        help="Path to note events JSON (default: output/notes.json)",
    )
    parser.add_argument(
        "--tempo",
        type=float,
        default=120.0,
        help="Tempo in BPM, used to define the 32nd-note noise threshold (default: 120)",
    )
    args = parser.parse_args()

    notes = json.loads(args.input.read_text())
    if not notes:
        raise SystemExit(f"No notes found in {args.input}")

    durations = [n["end_time"] - n["start_time"] for n in notes]
    pitches = [n["pitch"] for n in notes]

    thirty_second_note_seconds = 60.0 / args.tempo / 8
    short_notes = [d for d in durations if d < thirty_second_note_seconds]

    lowest, highest = min(pitches), max(pitches)

    print(f"Total notes:        {len(notes)}")
    print(f"Average duration:   {sum(durations) / len(durations):.4f}s")
    print(
        f"Notes < 32nd note:  {len(short_notes)} "
        f"(threshold {thirty_second_note_seconds:.4f}s at {args.tempo:.0f} BPM)"
    )
    print(
        f"Pitch range:        {lowest} ({pitch.Pitch(lowest).nameWithOctave}) - "
        f"{highest} ({pitch.Pitch(highest).nameWithOctave})"
    )


if __name__ == "__main__":
    main()
