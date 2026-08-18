# Church sheet / easy piano mode (Stage A) — design

## Overview

Add a second arrangement mode to the `ml/` pipeline: `church_sheet`, alongside
the existing `transcription` mode (today's only behavior, kept unchanged and
default). Where `transcription` mode aims for note-for-note accuracy,
`church_sheet` mode aims for readability — the kind of simplified, single-line
melody + simplified bass arrangement found in published "easy piano" or
church accompaniment sheets.

This is **Stage A** of a two-stage plan. Stage A covers melody extraction,
bass reduction, and rhythm simplification, all working directly from
basic-pitch's transcribed note events. Stage B (deferred, not part of this
spec) covers chord/harmony estimation, generated LH accompaniment patterns
(root/fifth/triad/alternating-bass), and optional chord-symbol text above the
staff — all of which need a chord estimate that doesn't exist yet, so
building them on top of unvalidated Stage A output would compound risk.
Stage B is a separate future spec once Stage A's real output can be judged.

**Explicit requirement carried over from the request:** `transcription_mode`
and `church_sheet_mode` must remain two separate algorithms. `church_sheet`
logic does not call into `transcription` mode's hand-assignment/hysteresis
logic (`assign_staff`, `group_by_staff`), and vice versa.

## Architecture

- New file `ml/church_sheet.py` — the entire church_sheet arrangement
  algorithm (melody extraction, bass reduction, rhythm quantization).
- `ml/notation.py` gains a `--mode {transcription,church_sheet}` CLI flag,
  default `transcription`. When `church_sheet` is selected, `main()` calls
  into `church_sheet.py` instead of the existing
  `quantize_to_stream`/`group_by_staff`/`build_score` path. Nothing about
  `transcription` mode's code path changes.
- `church_sheet.py` reuses only arrangement-agnostic scaffolding already in
  `notation.py`: the grid-snapping math pattern from `quantize_to_stream`
  (reimplemented locally since church_sheet's quantization vocabulary is
  different — see below) and `build_score`'s PartStaff/StaffGroup/metadata
  construction (via a shared helper, since that part genuinely has nothing
  to do with arrangement philosophy).
- `find_suspected_outliers` (report-only outlier flagging) still runs in
  church_sheet mode too — it operates on raw note events, is orthogonal to
  arrangement philosophy, and its output is still useful diagnostic
  information regardless of mode.

## A discovered simplification

Both hands reduce to a **monophonic-selection problem**, not a
voice-management problem: at each moment, pick one "winning" pitch (or a
small onset-aligned chord around it) and discard the rest. A melody/bass
line built this way is 1 voice per measure by construction — church_sheet
mode does not need `transcription` mode's per-measure `makeVoices()`
machinery at all.

## Melody extraction (RH)

1. **Skyline pass.** Build a piano-roll from the raw note events at the
   quantization grid resolution (reuse the same grid-size concept as
   `transcription` mode's `--grid`, default sixteenth notes). For each grid
   slice, the melody candidate is the highest pitch sounding at that slice —
   determined by note interval containment (`start_time <= slice <
   end_time`), so a held note continues to count as sounding through a later
   note's onset, not just at its own onset.
2. **Segment.** Collapse consecutive slices that resolve to the same pitch
   into a single note-run. This raw run sequence is the unsmoothed melody
   line.
3. **Continuity smoothing.** A segment is a smoothing candidate when both:
   - its duration is below a "flourish" threshold (tunable constant,
     starting point: one eighth note's worth of grid slices), and
   - it is more than an octave (12 semitones) from *both* its immediate
     predecessor and successor segments.

   When both hold, check whether another note was sounding during that same
   span that is closer in pitch to the established register (the
   predecessor segment's pitch). If one exists, substitute it for the
   skyline pick. This directly implements the jump-penalty / continuity
   requirement as a concrete, boundable rule rather than a multi-factor
   score with no ground truth to validate it against.
4. **Chords.** At a melody note's onset, any other raw note event starting
   at that same onset (within quantization tolerance) is stacked into a
   Chord with it, capped at 4 pitches total. Notes starting at other times
   are not part of the melody. This is what keeps RH "mostly monophonic with
   occasional chords" — no separate voice-splitting logic is needed because
   only simultaneous-onset notes are ever combined.

## Bass reduction (LH)

Mirrors the melody algorithm exactly, with two differences:
- Each grid slice's candidate is the **lowest** sounding pitch, not the
  highest.
- Onset-aligned chord stacking is capped at **2** pitches (root + one
  interval), not 4 — a simplified bass line doesn't need 4-note bass
  chords.

Continuity smoothing uses the same rule (flourish-duration + octave-jump
threshold) mirrored for the low end. This produces real transcribed bass
content, simplified — it is explicitly **not** yet the generated
root/fifth/triad accompaniment pattern from the original request; that
requires a chord estimate and is Stage B.

## Rhythm quantization with complexity preference

Allowed duration vocabulary, in preference order (each step is "more
complex" and only used when a simpler value doesn't fit well):

1. Whole note
2. Dotted half
3. Half note
4. Dotted quarter
5. Quarter note
6. Eighth note
7. Dotted eighth
8. Sixteenth note

For each melody/bass segment's raw duration (in quarter-length units), find
the nearest allowed value by absolute error. If a simpler-ranked value's
error is within a tolerance band (tunable constant, starting point: 15% of a
sixteenth note's quarter-length) of the nearest value's error, prefer the
simpler one instead. Sixteenth notes are used only when no simpler value
comes within that tolerance band.

## Diagnostics

Extend `print_diagnostics` (or add a church_sheet-specific variant) to
report: total melody notes vs. total raw note events (showing the reduction
ratio), RH/LH voice-count distribution (expected to be ~100% single-voice,
confirming the "discovered simplification" holds on real audio), and a
breakdown of how many notes landed on each rhythm-vocabulary value (showing
whether sixteenth notes stayed rare as intended).

## Explicit non-goals for this spec (Stage B, future)

- Chord/harmony estimation (e.g. detecting "Eb major" from grouped
  simultaneous pitches).
- Generated LH accompaniment patterns (root, root+fifth, octave, simple
  triad, alternating bass) replacing transcribed bass content.
- Optional chord-symbol text (e.g. "Eb | Bb | Cm | Ab") above the staff.
- Any change to `server/`, `client/`, or the job pipeline — this mode is
  CLI-only (`notation.py --mode church_sheet`) for this round. Wiring it
  into the API/UI is future work, done only after real output from this
  spec has been judged.

## Testing

Run against the same "Pasilyo" audio already used as the regression case for
the `transcription` mode notation-engraving work. Compare, for the same
source audio: raw note count vs. church_sheet melody+bass note count, voice
counts per measure (expect ~1 for both hands), and the rhythm-vocabulary
distribution (expect sixteenth notes to be rare). Inspect the generated
MusicXML directly (and via OSMD, since the client already renders MusicXML)
to judge whether the melody line is recognizable and the bass is
sensible — this is a readability judgment call, not something a unit test
can fully verify, consistent with how the `transcription` mode notation
work was validated.
