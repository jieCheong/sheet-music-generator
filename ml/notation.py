#!/usr/bin/env python
"""CLI: build a quantized, two-staff music21 Score from basic-pitch note events."""

import argparse
import json
from pathlib import Path

from music21 import clef, meter, note, stream, tempo

SCRIPT_DIR = Path(__file__).resolve().parent
MIDDLE_C = 60


def quantize_to_stream(notes: list[dict], tempo_bpm: float, grid: int) -> stream.Stream:
    grid_quarter_length = 4.0 / grid

    quantized = stream.Stream()
    for n in notes:
        start_beats = n["start_time"] * tempo_bpm / 60.0
        end_beats = n["end_time"] * tempo_bpm / 60.0

        offset = round(start_beats / grid_quarter_length) * grid_quarter_length
        end = round(end_beats / grid_quarter_length) * grid_quarter_length
        duration = max(end - offset, grid_quarter_length)

        quantized.insert(offset, note.Note(n["pitch"], quarterLength=duration))

    return quantized


def build_score(quantized: stream.Stream, detected_key, tempo_bpm: float) -> stream.Score:
    treble = stream.Part(id="treble")
    treble.insert(0, clef.TrebleClef())
    treble.insert(0, detected_key)
    treble.insert(0, meter.TimeSignature("4/4"))
    treble.insert(0, tempo.MetronomeMark(number=tempo_bpm))

    bass = stream.Part(id="bass")
    bass.insert(0, clef.BassClef())
    bass.insert(0, detected_key)
    bass.insert(0, meter.TimeSignature("4/4"))

    for n in quantized.notes:
        target = treble if n.pitch.midi >= MIDDLE_C else bass
        target.insert(n.offset, note.Note(n.pitch, quarterLength=n.duration.quarterLength))

    score = stream.Score()
    score.insert(0, treble)
    score.insert(0, bass)
    return score


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn basic-pitch note events into a notated Score.")
    parser.add_argument(
        "--input",
        type=Path,
        default=SCRIPT_DIR / "output" / "notes.json",
        help="Path to note events JSON (default: output/notes.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=SCRIPT_DIR / "output" / "score.musicxml",
        help="Path to write the MusicXML score (default: output/score.musicxml)",
    )
    parser.add_argument(
        "--tempo",
        type=float,
        default=120.0,
        help="Tempo in BPM, used to convert note event seconds into beats (default: 120)",
    )
    parser.add_argument(
        "--grid",
        type=int,
        default=16,
        help="Quantization grid as a note-value denominator, e.g. 16 for sixteenth notes (default: 16)",
    )
    args = parser.parse_args()
    if args.grid <= 0:
        parser.error("--grid must be a positive integer")

    notes = json.loads(args.input.read_text())
    if not notes:
        raise SystemExit(f"No notes found in {args.input}")

    quantized = quantize_to_stream(notes, args.tempo, args.grid)

    detected_key = quantized.analyze("key")
    print(f"Detected key: {detected_key}")

    score = build_score(quantized, detected_key, args.tempo)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    score.write("musicxml", fp=str(args.output))
    print(f"Saved MusicXML to {args.output}")


if __name__ == "__main__":
    main()
