#!/usr/bin/env python
"""CLI: build a quantized, two-staff music21 Score from basic-pitch note events."""

import argparse
import json
from pathlib import Path

from music21 import clef, layout, meter, metadata, note, stream, tempo

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


def build_score(quantized: stream.Stream, detected_key, tempo_bpm: float, title: str) -> stream.Score:
    # PartStaff (not Part) + StaffGroup gives a proper piano grand staff: one
    # "Piano" label and a connecting brace, instead of two separate parts
    # each showing their own label -- which, if left unset, music21's
    # MusicXML writer fills with an auto-generated id (e.g. "Instr.
    # P732eba74387b09b6e4e545ae889b743c") since it requires *some* label.
    treble = stream.PartStaff(id="treble")
    treble.partName = "Piano"
    treble.insert(0, clef.TrebleClef())
    treble.insert(0, detected_key)
    treble.insert(0, meter.TimeSignature("4/4"))
    treble.insert(0, tempo.MetronomeMark(number=tempo_bpm))

    bass = stream.PartStaff(id="bass")
    bass.partName = "Piano"
    bass.insert(0, clef.BassClef())
    bass.insert(0, detected_key)
    bass.insert(0, meter.TimeSignature("4/4"))

    for n in quantized.notes:
        target = treble if n.pitch.midi >= MIDDLE_C else bass
        target.insert(n.offset, note.Note(n.pitch, quarterLength=n.duration.quarterLength))

    staff_group = layout.StaffGroup([treble, bass], name="Piano", symbol="brace")

    score = stream.Score()
    score.metadata = metadata.Metadata()
    score.metadata.title = title
    # Both default to auto-filled values otherwise: movementName defaults to
    # repeating the title as a subtitle, composer defaults to "Music21".
    score.metadata.movementName = ""
    score.metadata.composer = ""
    score.insert(0, treble)
    score.insert(0, bass)
    score.insert(0, staff_group)
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

    data = json.loads(args.input.read_text())
    notes = data["notes"]
    title = data.get("title") or "Untitled"
    if not notes:
        raise SystemExit(f"No notes found in {args.input}")

    quantized = quantize_to_stream(notes, args.tempo, args.grid)

    detected_key = quantized.analyze("key")
    print(f"Detected key: {detected_key}")

    score = build_score(quantized, detected_key, args.tempo, title)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    score.write("musicxml", fp=str(args.output))
    print(f"Saved MusicXML to {args.output}")


if __name__ == "__main__":
    main()
