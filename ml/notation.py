#!/usr/bin/env python
"""CLI: build a quantized, two-staff music21 Score from basic-pitch note events."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from music21 import chord, clef, layout, meter, metadata, note, stream, tempo

SCRIPT_DIR = Path(__file__).resolve().parent
MIDDLE_C = 60

# Hand-assignment hysteresis: pitches strictly outside this zone are
# unambiguous (treble above, bass below). Pitches inside it only decide the
# staff on their own if nothing unambiguous shares their exact onset+duration
# -- otherwise they follow whichever staff the rest of that chord belongs to,
# instead of a bare per-note cutoff splitting one chord across both staves.
AMBIGUOUS_LOW = 56  # G#3
AMBIGUOUS_HIGH = 64  # E4

OUTLIER_ISOLATION_WINDOW_S = 2.0
OUTLIER_SHORT_DURATION_S = 0.08
OUTLIER_PITCH_DEVIATION = 18  # semitones from local neighborhood average
OUTLIER_SCORE_THRESHOLD = 2  # signals required before reporting


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


def assign_staff(pitches: list[int]) -> str:
    """Decide which staff a group of simultaneous pitches (one chord/moment)
    belongs to, keeping the group together when possible instead of a bare
    per-note cutoff splitting one chord across both staves. Returns "treble",
    "bass", or "split" (a genuine two-hand chord spanning both)."""
    clearly_treble = [p for p in pitches if p > AMBIGUOUS_HIGH]
    clearly_bass = [p for p in pitches if p < AMBIGUOUS_LOW]

    if clearly_treble and not clearly_bass:
        return "treble"
    if clearly_bass and not clearly_treble:
        return "bass"
    if clearly_treble and clearly_bass:
        return "split"
    # Every pitch in this group is in the ambiguous zone: fall back to the
    # group's average against middle C.
    return "treble" if (sum(pitches) / len(pitches)) >= MIDDLE_C else "bass"


def group_by_staff(quantized: stream.Stream) -> tuple[dict, dict]:
    """Groups quantized notes into (offset, duration) chords, then assigns
    each chord to a staff as a whole (see assign_staff) rather than
    splitting by individual pitch."""
    moments: dict[tuple[float, float], list[int]] = defaultdict(list)
    for n in quantized.notes:
        moments[(n.offset, n.duration.quarterLength)].append(n.pitch.midi)

    treble_groups: dict[tuple[float, float], list[int]] = defaultdict(list)
    bass_groups: dict[tuple[float, float], list[int]] = defaultdict(list)
    for key, pitches in moments.items():
        decision = assign_staff(pitches)
        if decision == "treble":
            treble_groups[key] = pitches
        elif decision == "bass":
            bass_groups[key] = pitches
        else:
            for p in pitches:
                (treble_groups if p >= MIDDLE_C else bass_groups)[key].append(p)

    return treble_groups, bass_groups


def find_suspected_outliers(notes: list[dict]) -> list[dict]:
    """Report-only, conservative, multi-signal flagging of notes that are
    plausibly transcription noise -- never removes anything. A note is
    flagged only when *multiple* independent signals agree (isolated in
    pitch, far from its local pitch neighborhood, and short), not from a
    single fixed pitch cutoff. Returns the flagged notes with their reasons;
    callers decide what, if anything, to do with them."""
    flagged = []
    for n in notes:
        reasons = []

        neighborhood = [
            other["pitch"]
            for other in notes
            if other is not n and abs(other["start_time"] - n["start_time"]) <= OUTLIER_ISOLATION_WINDOW_S
        ]
        if neighborhood:
            local_avg = sum(neighborhood) / len(neighborhood)
            if abs(n["pitch"] - local_avg) >= OUTLIER_PITCH_DEVIATION:
                reasons.append(f"pitch {n['pitch']} is {abs(n['pitch'] - local_avg):.0f} semitones from the local average")

        is_isolated = not any(
            other is not n and abs(other["pitch"] - n["pitch"]) <= 1 and abs(other["start_time"] - n["start_time"]) <= OUTLIER_ISOLATION_WINDOW_S
            for other in notes
        )
        if is_isolated:
            reasons.append("no nearby note at a similar pitch (isolated occurrence)")

        duration = n["end_time"] - n["start_time"]
        if duration <= OUTLIER_SHORT_DURATION_S:
            reasons.append(f"very short duration ({duration:.3f}s)")

        if len(reasons) >= OUTLIER_SCORE_THRESHOLD:
            flagged.append({**n, "reasons": reasons})

    return flagged


def build_staff_pair(detected_key, tempo_bpm: float, title: str) -> tuple[stream.PartStaff, stream.PartStaff, layout.StaffGroup, stream.Score]:
    """Piano grand-staff scaffolding (clefs, key/time signature, PartStaff+
    StaffGroup brace, title/composer metadata) shared by both notation modes
    -- this part has nothing to do with arrangement philosophy. Callers still
    insert their own notes/chords and call makeMeasures/makeVoices themselves,
    since that IS mode-specific."""
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

    return treble, bass, staff_group, score


def build_score(quantized: stream.Stream, detected_key, tempo_bpm: float, title: str) -> stream.Score:
    treble, bass, staff_group, score = build_staff_pair(detected_key, tempo_bpm, title)

    # Group simultaneous notes into a single Chord instead of inserting each
    # as an independent Note at the same offset. Without this, basic-pitch's
    # raw polyphonic output -- which has no concept of "these pitches are
    # one chord" -- left every simultaneous pitch as its own overlapping
    # Note, and music21's MusicXML writer resolved the ambiguity by
    # auto-splitting them into far more simultaneous voices than real piano
    # notation ever uses for a single chord.
    treble_groups, bass_groups = group_by_staff(quantized)

    for target, groups in ((treble, treble_groups), (bass, bass_groups)):
        for (offset, quarter_length), pitches in groups.items():
            element = (
                chord.Chord(pitches, quarterLength=quarter_length)
                if len(pitches) > 1
                else note.Note(pitches[0], quarterLength=quarter_length)
            )
            target.insert(offset, element)

    # Split into measures and let music21 assign voices per measure
    # independently, instead of writing the whole flat, overlapping stream
    # in one shot. The whole-stream write chooses ONE voice count for the
    # entire piece (the maximum simultaneity found anywhere) and applies it
    # to every measure uniformly -- a piece needing 5 simultaneous voices
    # for two beats produced 5 voices, mostly empty whole-measure rests, in
    # every other measure too. Per-measure assignment gives each measure
    # only as many voices as it actually needs.
    for target in (treble, bass):
        target.makeMeasures(inPlace=True)
        for m in target.getElementsByClass(stream.Measure):
            m.makeVoices(inPlace=True, fillGaps=True)

    return score


def print_diagnostics(score: stream.Score, tempo_bpm: float, outliers: list[dict]) -> None:
    print("\n--- Diagnostics ---")
    print(f"Detected BPM: {tempo_bpm}")

    for part in score.parts:
        measures = list(part.getElementsByClass(stream.Measure))
        voice_counts = [len(m.voices) if m.voices else 1 for m in measures]
        by_count: dict[int, int] = defaultdict(int)
        for c in voice_counts:
            by_count[min(c, 4)] += 1  # bucket 4+ together, matching the ask

        max_simultaneous = 0
        rare_voice_measures = 0
        empty_rest_measures = 0
        for m in measures:
            voices = list(m.voices) if m.voices else [m]
            max_simultaneous = max(max_simultaneous, len(voices))
            for v in voices:
                real = [el for el in v.notesAndRests if not el.isRest]
                if not real:
                    empty_rest_measures += 1
            if len(voices) > 2:
                rare_voice_measures += 1

        print(f"\nPart: {part.partName} (id={part.id})")
        print(f"  Total measures: {len(measures)}")
        print(f"  Max voices in any measure: {max_simultaneous}")
        print(
            "  Measures needing 1 / 2 / 3 / 4+ voices: "
            f"{by_count[1]} / {by_count[2]} / {by_count[3]} / {by_count[4]}"
        )
        print(f"  Measures with >2 voices (worth reviewing): {rare_voice_measures}")
        print(f"  Empty (whole-rest-only) voice-slots generated: {empty_rest_measures}")

        if rare_voice_measures:
            print(f"  Detail for measures requiring >2 voices in {part.partName}:")
            for m in measures:
                voices = list(m.voices) if m.voices else []
                if len(voices) <= 2:
                    continue
                print(f"    Measure {m.number}:")
                for i, v in enumerate(voices):
                    real = [el for el in v.notesAndRests if not el.isRest]
                    if not real:
                        print(f"      voice {i + 1}: empty (whole-measure rest)")
                        continue
                    summary = ", ".join(
                        (f"chord{[p.midi for p in el.pitches]}" if el.isChord else f"note{el.pitch.midi}")
                        for el in real
                    )
                    print(f"      voice {i + 1}: {summary}")

    print(f"\nSuspected transcription outliers (report-only, none removed): {len(outliers)}")
    for o in outliers[:20]:
        print(f"  pitch={o['pitch']:>3} at {o['start_time']:.2f}s -- {'; '.join(o['reasons'])}")
    if len(outliers) > 20:
        print(f"  ... and {len(outliers) - 20} more")
    print("--- End diagnostics ---\n")


def voice_stats(score: stream.Score) -> dict:
    """Small subset of print_diagnostics' numbers, used to give church_sheet
    mode a real (not fabricated) before/after comparison against transcription
    mode on the same input."""
    total_rests = 0
    total_measures = 0
    max_voices = 1
    over_2_voices = 0
    for part in score.parts:
        for m in part.getElementsByClass(stream.Measure):
            total_measures += 1
            voices = list(m.voices) if m.voices else [m]
            max_voices = max(max_voices, len(voices))
            if len(voices) > 2:
                over_2_voices += 1
            total_rests += sum(1 for v in voices for el in v.notesAndRests if el.isRest)
    return {
        "max_voices": max_voices,
        "avg_rests": total_rests / total_measures if total_measures else 0.0,
        "over_2_voices": over_2_voices,
    }


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
        default=None,
        help="Tempo in BPM, used to convert note event seconds into beats. Defaults to "
        "the tempo transcribe.py detected from the audio (stored in --input); falls "
        "back to 120 only if that's missing (e.g. an older notes.json).",
    )
    parser.add_argument(
        "--grid",
        type=int,
        default=16,
        help="Quantization grid as a note-value denominator, e.g. 16 for sixteenth notes (default: 16)",
    )
    parser.add_argument(
        "--mode",
        choices=["transcription", "church_sheet"],
        default="transcription",
        help="transcription (default): preserve as much detected musical information as "
        "reasonably possible. church_sheet: prioritize a clean, sight-readable arrangement "
        "-- extracted melody, estimated harmony, and generated accompaniment, expected to "
        "drop many detected notes. These are two separate algorithms, not a shared path.",
    )
    args = parser.parse_args()
    if args.grid <= 0:
        parser.error("--grid must be a positive integer")

    data = json.loads(args.input.read_text())
    notes = data["notes"]
    title = data.get("title") or "Untitled"
    tempo_bpm = args.tempo if args.tempo is not None else data.get("tempo", 120.0)
    if not notes:
        raise SystemExit(f"No notes found in {args.input}")

    outliers = find_suspected_outliers(notes)

    quantized = quantize_to_stream(notes, tempo_bpm, args.grid)

    detected_key = quantized.analyze("key")
    print(f"Detected key: {detected_key}")

    transcription_score = build_score(quantized, detected_key, tempo_bpm, title)

    if args.mode == "church_sheet":
        import church_sheet

        score = church_sheet.run(
            notes,
            tempo_bpm,
            title,
            build_staff_pair,
            transcription_comparison=voice_stats(transcription_score),
            raw_outlier_count=len(outliers),
        )
    else:
        score = transcription_score
        print_diagnostics(score, tempo_bpm, outliers)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    score.write("musicxml", fp=str(args.output))
    print(f"Saved MusicXML to {args.output}")


if __name__ == "__main__":
    main()
