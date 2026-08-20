#!/usr/bin/env python
"""Church/easy-piano arrangement mode: builds a simplified, readable piano
arrangement from raw note events, instead of transcribing them note-for-note.

Deliberately a separate algorithm from notation.py's transcription mode --
it does not call assign_staff/group_by_staff/build_score, and transcription
mode does not call anything here. The two modes have different goals
(accuracy vs. readability) and are kept as two real, independent code paths
per an explicit project requirement, not one shared path behind a flag.

Pipeline (see docs/superpowers/specs/2026-08-18-church-sheet-mode-design.md):
  raw notes -> melody extraction (DP) -> chord region detection ->
  LH accompaniment generation -> RH chord-tone stacking ->
  rhythm simplification -> engrave (chord symbols, system breaks) ->
  readability diagnostics
"""

import math
from collections import defaultdict

from music21 import chord as m21chord, harmony, key as m21key, layout, note as m21note, stream

# ---- Melody extraction (DP / Viterbi-style path selection) ----

MELODY_GRID = 16  # sixteenth-note slices, matches transcription mode's default --grid
MAX_CANDIDATES_PER_SLICE = 6  # bounds DP width; only the loudest candidates matter
REGISTER_WINDOW_BEATS = 3.0  # window for the local "expected register" average

JUMP_WEIGHT = 0.15
LARGE_JUMP_SEMITONES = 12
LARGE_JUMP_MULTIPLIER = 1.5
HOLD_BONUS = -0.15  # negative cost: rewards staying on the same pitch (fewer note changes).
# Was -1.0, then -0.3: still large enough that a longer-duration note
# accumulates this bonus over more consecutive slices than a shorter one at
# the same velocity, making raw detected duration alone able to outweigh
# register-appropriateness -- confirmed on real data (a 2-beat B3 beat a
# 0.75-beat B4 of identical velocity purely on accumulated hold bonus, even
# though B4 was the plausible melody note). -0.15 shrinks that duration bias
# further; GLOBAL_REGISTER_WEIGHT below is the real fix for this class of
# error, this is a supporting change.
VELOCITY_WEIGHT = 0.03
REGISTER_WEIGHT = 0.08
TOP_BIAS = 0.5  # small reward for picking the highest active candidate (classic melody heuristic)
REST_COST = 0.2  # cost of choosing silence; lets DP drop a note that's worse than resting

# The local register term (REGISTER_WEIGHT, below) uses a narrow window and
# re-centers on whatever was just chosen -- once the melody path wavers into
# a different register for a few consecutive slices, the "locally expected
# register" drifts with it instead of resisting, so nothing pulls a
# temporarily-wrong choice back toward the song's actual melodic range. This
# adds a second, whole-song anchor that doesn't drift: the average of the
# single highest pitch active at each populated slice across the entire
# song (a skyline estimate) -- computed once upfront from the raw audio
# data, not from whichever pitches the DP ends up choosing, so it can't
# compound the same drift it's meant to correct.
GLOBAL_REGISTER_WEIGHT = 0.2

# Local register-consistency alone isn't enough: a passage where every detected
# note is genuinely bass-register (e.g. an instrumental bridge with no clear
# vocal/lead line) drags the local average down with it, and the DP would
# happily pick a bass note as "melody" since it's locally consistent. This is
# an absolute floor/ceiling on top of that -- plausible melodic/vocal range,
# roughly C3-E6 -- so the DP strongly prefers resting over notating something
# that isn't a melody just because it happened to be loud and locally coherent.
MELODY_PLAUSIBLE_LOW = 48  # C3
MELODY_PLAUSIBLE_HIGH = 88  # E6
OUT_OF_RANGE_PENALTY = 1.5  # cost per semitone outside the plausible register -- 0.5
# was too weak: a note only 2 semitones below the floor with decent velocity
# (e.g. a sustained Bb2 in an instrumental passage with nothing else
# detected above it) still scored cheaper than resting, so a real
# instrumental/no-melody stretch got notated as an awkward low RH "melody"
# instead of resting. 1.5 makes even a small violation cost more than
# REST_COST unless the candidate is very loud, which is what should happen:
# prefer silence over a note that isn't really a melody.

FLOURISH_BEATS = 0.5  # runs shorter than this, jump-isolated on both sides, are dropped as noise


def _to_beats(notes: list[dict], tempo_bpm: float) -> list[dict]:
    events = [
        {
            "pitch": n["pitch"],
            "start": n["start_time"] * tempo_bpm / 60.0,
            "end": n["end_time"] * tempo_bpm / 60.0,
            "velocity": n["velocity"],
        }
        for n in notes
    ]
    events.sort(key=lambda e: e["start"])
    return events


def _slice_song(events: list[dict], grid: int) -> tuple[list[list[dict]], float, int]:
    slice_len = 4.0 / grid
    song_end = max(e["end"] for e in events)
    num_slices = max(1, math.ceil(song_end / slice_len))
    slices = []
    for i in range(num_slices):
        s0, s1 = i * slice_len, (i + 1) * slice_len
        slices.append([e for e in events if e["start"] < s1 and e["end"] > s0])
    return slices, slice_len, num_slices


def _local_register(events: list[dict], num_slices: int, slice_len: float) -> list[float | None]:
    """Weighted-average pitch of notes sounding near each slice, used to penalize
    melody candidates that are implausibly far from the surrounding texture."""
    avgs: list[float | None] = []
    for i in range(num_slices):
        center = (i + 0.5) * slice_len
        nearby = [e for e in events if abs((e["start"] + e["end"]) / 2 - center) <= REGISTER_WINDOW_BEATS]
        if nearby:
            total_w = sum(e["velocity"] for e in nearby) or len(nearby)
            avgs.append(sum(e["pitch"] * e["velocity"] for e in nearby) / total_w if total_w else None)
        else:
            avgs.append(None)

    # Fill gaps (slices with nothing nearby) via forward-fill then backward-fill,
    # falling back to the song's overall median pitch if every slice is empty.
    overall_median = sorted(e["pitch"] for e in events)[len(events) // 2] if events else 60
    last = None
    for i in range(num_slices):
        if avgs[i] is None:
            avgs[i] = last
        else:
            last = avgs[i]
    last = None
    for i in range(num_slices - 1, -1, -1):
        if avgs[i] is None:
            avgs[i] = last if last is not None else overall_median
        else:
            last = avgs[i]
    return avgs


def _global_register_estimate(slices: list[list[dict]]) -> float:
    skyline = [max(e["pitch"] for e in active) for active in slices if active]
    return sum(skyline) / len(skyline) if skyline else 60.0


def _transition_cost(prev: int | None, cur: int | None) -> float:
    if prev is None or cur is None:
        return 0.0  # free to enter/leave silence
    if prev == cur:
        return HOLD_BONUS
    jump = abs(cur - prev)
    multiplier = LARGE_JUMP_MULTIPLIER if jump > LARGE_JUMP_SEMITONES else 1.0
    return JUMP_WEIGHT * jump * multiplier


def _emission_cost(
    state: int | None,
    velocity_map: dict,
    candidates: list,
    register_avg: float | None,
    global_register: float,
) -> float:
    if state is None:
        return REST_COST
    cost = -VELOCITY_WEIGHT * velocity_map.get(state, 0)
    if register_avg is not None:
        cost += REGISTER_WEIGHT * abs(state - register_avg)
    cost += GLOBAL_REGISTER_WEIGHT * abs(state - global_register)
    cost += OUT_OF_RANGE_PENALTY * max(0, MELODY_PLAUSIBLE_LOW - state)
    cost += OUT_OF_RANGE_PENALTY * max(0, state - MELODY_PLAUSIBLE_HIGH)
    real_candidates = [p for p in candidates if p is not None]
    if real_candidates and state == max(real_candidates):
        cost -= TOP_BIAS
    return cost


def _extract_melody_path(slices: list[list[dict]], register_avg: list[float | None]) -> list[int | None]:
    global_register = _global_register_estimate(slices)
    states_per_slice = []
    for i, active in enumerate(slices):
        by_pitch: dict[int, int] = {}
        for e in active:
            by_pitch[e["pitch"]] = max(by_pitch.get(e["pitch"], 0), e["velocity"])
        top = sorted(by_pitch.items(), key=lambda kv: -kv[1])[:MAX_CANDIDATES_PER_SLICE]
        states_per_slice.append({"pitches": [p for p, _ in top] + [None], "velocity": by_pitch})

    n = len(states_per_slice)
    dp: list[dict[int | None, tuple[float, int | None]]] = [dict() for _ in range(n)]
    first = states_per_slice[0]
    for s in first["pitches"]:
        dp[0][s] = (_emission_cost(s, first["velocity"], first["pitches"], register_avg[0], global_register), None)

    for i in range(1, n):
        cur = states_per_slice[i]
        prev_layer = dp[i - 1]
        for s in cur["pitches"]:
            best_cost, best_prev = math.inf, None
            for ps, (pcost, _) in prev_layer.items():
                total = pcost + _transition_cost(ps, s)
                if total < best_cost:
                    best_cost, best_prev = total, ps
            emission = _emission_cost(s, cur["velocity"], cur["pitches"], register_avg[i], global_register)
            dp[i][s] = (best_cost + emission, best_prev)

    end_state = min(dp[n - 1], key=lambda s: dp[n - 1][s][0])
    path: list[int | None] = [None] * n
    path[n - 1] = end_state
    for i in range(n - 1, 0, -1):
        path[i - 1] = dp[i][path[i]][1]
    return path


def _segment_path(path: list[int | None], slice_len: float) -> list[dict]:
    """Collapse consecutive identical states into runs, then drop runs that are
    both short and jump-isolated (a note the DP still let through but that
    doesn't belong to a coherent line -- a safety net, not the primary defense)."""
    runs = []
    i = 0
    while i < len(path):
        j = i
        while j + 1 < len(path) and path[j + 1] == path[i]:
            j += 1
        if path[i] is not None:
            runs.append({"pitch": path[i], "start": i * slice_len, "end": (j + 1) * slice_len})
        i = j + 1

    cleaned = []
    for idx, run in enumerate(runs):
        duration = run["end"] - run["start"]
        prev_pitch = runs[idx - 1]["pitch"] if idx > 0 else None
        next_pitch = runs[idx + 1]["pitch"] if idx + 1 < len(runs) else None
        isolated = (
            duration <= FLOURISH_BEATS
            and (prev_pitch is None or abs(run["pitch"] - prev_pitch) > LARGE_JUMP_SEMITONES)
            and (next_pitch is None or abs(run["pitch"] - next_pitch) > LARGE_JUMP_SEMITONES)
        )
        if not isolated:
            cleaned.append(run)
    return cleaned


def extract_melody(notes: list[dict], tempo_bpm: float, grid: int = MELODY_GRID) -> list[dict]:
    events = _to_beats(notes, tempo_bpm)
    slices, slice_len, num_slices = _slice_song(events, grid)
    register_avg = _local_register(events, num_slices, slice_len)
    path = _extract_melody_path(slices, register_avg)
    return _segment_path(path, slice_len)


# ---- Chord region detection (template matching) ----

CHORD_REGION_BEATS = 2.0  # harmonic "resolution" -- half a 4/4 measure
CHORD_QUALITIES = {
    "major": (0, 4, 7),
    "minor": (0, 3, 7),
    "dim": (0, 3, 6),
    "dom7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10),
}
CHORD_QUALITY_COMPLEXITY = {"major": 0, "minor": 0, "dim": 1, "dom7": 2, "maj7": 2, "min7": 2}
COMPLEXITY_PENALTY = 0.04  # don't chase 7th-chord extensions unless clearly a better fit
WEAK_ROOT_PENALTY = 0.15
BASS_MATCH_BONUS = 0.06
LOW_PITCH_CUTOFF = 60  # notes below middle C count toward the bass pitch-class cross-check


def _score_chord(weight: list[float], total: float, low_root_guess: int | None) -> tuple[int, str]:
    best, best_score = None, -math.inf
    for root in range(12):
        for quality, intervals in CHORD_QUALITIES.items():
            pcs = [(root + iv) % 12 for iv in intervals]
            score = sum(weight[pc] for pc in pcs) / total
            score -= COMPLEXITY_PENALTY * CHORD_QUALITY_COMPLEXITY[quality]
            if weight[root] / total < 0.05:
                score -= WEAK_ROOT_PENALTY
            if low_root_guess is not None and root == low_root_guess:
                score += BASS_MATCH_BONUS
            if score > best_score:
                best_score, best = score, (root, quality)
    return best


def _detect_raw_regions(events: list[dict], song_end: float) -> list[tuple[int, str] | None]:
    regions = []
    t = 0.0
    while t < song_end:
        end = min(t + CHORD_REGION_BEATS, song_end)
        weight = [0.0] * 12
        low_weight = [0.0] * 12
        for e in events:
            overlap = min(e["end"], end) - max(e["start"], t)
            if overlap <= 0:
                continue
            pc = e["pitch"] % 12
            w = overlap * (e["velocity"] / 127.0)
            weight[pc] += w
            if e["pitch"] < LOW_PITCH_CUTOFF:
                low_weight[pc] += w
        total = sum(weight)
        if total <= 1e-6:
            regions.append(None)
        else:
            low_root_guess = low_weight.index(max(low_weight)) if sum(low_weight) > 0 else None
            regions.append(_score_chord(weight, total, low_root_guess))
        t = end
    return regions


def _choose_bass_octave(root_pc: int, target_midi: int = 45) -> int:
    candidates = [root_pc + 12 * o for o in range(8) if 0 <= root_pc + 12 * o <= 96]
    return min(candidates, key=lambda m: abs(m - target_midi))


def detect_chord_regions(notes: list[dict], tempo_bpm: float) -> list[dict]:
    """Returns merged harmonic regions: [{start, end, root_pc, quality, bass_midi, symbol}, ...]
    Adjacent fixed-size slots with the same chord are merged so chord symbols only
    appear at real harmonic changes, and brief silences carry the previous chord
    forward (a melodic rest doesn't usually pause the accompaniment)."""
    events = _to_beats(notes, tempo_bpm)
    song_end = max(e["end"] for e in events)
    raw = _detect_raw_regions(events, song_end)

    merged: list[dict] = []
    carry: tuple[int, str] | None = None
    for i, r in enumerate(raw):
        chord_val = r if r is not None else carry
        if chord_val is None:
            continue
        start, end = i * CHORD_REGION_BEATS, (i + 1) * CHORD_REGION_BEATS
        if merged and merged[-1]["_chord"] == chord_val:
            merged[-1]["end"] = end
        else:
            merged.append({"start": start, "end": end, "_chord": chord_val})
        carry = chord_val

    for region in merged:
        root_pc, quality = region["_chord"]
        region["root_pc"] = root_pc
        region["quality"] = quality
        region["bass_midi"] = _choose_bass_octave(root_pc)
        del region["_chord"]
    return merged


def _find_region(regions: list[dict], t: float) -> dict | None:
    for r in regions:
        if r["start"] <= t < r["end"]:
            return r
    return None


# Major-scale pitch classes for each of the 12 possible tonics, and each
# tonic's sharps count (a relative major/minor pair shares the same key
# signature, e.g. D major and B minor are both 2 sharps -- so scoring only
# needs to find the best-fitting 7-note scale, not decide major vs minor).
MAJOR_SCALE_INTERVALS = (0, 2, 4, 5, 7, 9, 11)
MAJOR_TONIC_SHARPS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: -5, 8: -4, 3: -3, 10: -2, 5: -1}


def infer_key_signature(regions: list[dict]) -> m21key.KeySignature:
    """Estimates the key from the detected chord regions' roots (weighted by
    duration), rather than trusting a generic pitch-histogram key-profile
    analysis of the raw notes (music21's `Stream.analyze('key')`, what
    transcription mode uses). That analysis can misfire on real
    audio-derived note data -- on one real song it returned "g minor" while
    the actual detected chord progression (D, A, Bm, Dmaj7, E7, Gmaj7,
    F#m, B7...) is almost entirely diatonic to D major. Spelling chord
    symbols from the wrong key produced needlessly wrong-looking
    accidentals (F#m spelled as Gbm) -- this is church_sheet mode's own key
    estimate, independent of transcription mode's."""
    if not regions:
        return m21key.KeySignature(0)
    best_tonic, best_score = 0, -1.0
    for tonic in range(12):
        scale = {(tonic + iv) % 12 for iv in MAJOR_SCALE_INTERVALS}
        score = sum(r["end"] - r["start"] for r in regions if r["root_pc"] in scale)
        if score > best_score:
            best_score, best_tonic = score, tonic
    return m21key.KeySignature(MAJOR_TONIC_SHARPS[best_tonic])


PITCH_CLASS_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PITCH_CLASS_FLAT = ["C", "D-", "D", "E-", "E", "F", "G-", "G", "A-", "A", "B-", "B"]
CHORD_SUFFIX = {"major": "", "minor": "m", "dim": "dim", "dom7": "7", "maj7": "maj7", "min7": "m7"}


def chord_symbol_text(root_pc: int, quality: str, use_flats: bool) -> str:
    name = PITCH_CLASS_FLAT[root_pc] if use_flats else PITCH_CLASS_SHARP[root_pc]
    return f"{name}{CHORD_SUFFIX[quality]}"


# ---- LH accompaniment generation (patterns A-D) ----

BASS_DENSITY_BROKEN_THRESHOLD = 1.2  # bass notes/beat above this -> Pattern C (broken chord)
BASS_DENSITY_SPARSE_THRESHOLD = 0.4  # bass notes/beat below this with slow harmony -> Pattern A (root only)
SLOW_HARMONY_BEATS = 4.0


def pick_lh_pattern(notes: list[dict], tempo_bpm: float, regions: list[dict]) -> str:
    """Picks ONE pattern for the whole song from a simple density heuristic.
    Predictability was an explicit requirement -- this deliberately does not
    switch patterns measure-to-measure."""
    if not regions:
        return "B"
    events = _to_beats(notes, tempo_bpm)
    bass_events = [e for e in events if e["pitch"] < 55]
    span = regions[-1]["end"] - regions[0]["start"]
    bass_density = len(bass_events) / max(span, 1e-6)
    avg_region_len = sum(r["end"] - r["start"] for r in regions) / len(regions)

    if bass_density >= BASS_DENSITY_BROKEN_THRESHOLD:
        return "C"
    if avg_region_len >= SLOW_HARMONY_BEATS and bass_density < BASS_DENSITY_SPARSE_THRESHOLD:
        return "A"
    return "B"


def generate_lh_events(regions: list[dict], pattern: str) -> list[dict]:
    events = []
    for region in regions:
        root = region["bass_midi"]
        fifth = root + (6 if region["quality"] == "dim" else 7)
        third = root + (3 if region["quality"] in ("minor", "dim", "min7") else 4)
        start, end = region["start"], region["end"]
        length = end - start

        if pattern == "A":
            events.append({"start": start, "end": end, "pitches": [root]})
        elif pattern == "B":
            events.append({"start": start, "end": end, "pitches": [root, fifth]})
        elif pattern == "C":
            t, toggle = start, True
            while t < end - 1e-6:
                seg_end = min(t + 1.0, end)
                events.append({"start": t, "end": seg_end, "pitches": [root if toggle else fifth]})
                toggle, t = not toggle, seg_end
        elif pattern == "D":
            first_len = min(1.0, length)
            events.append({"start": start, "end": start + first_len, "pitches": [root, third, fifth]})
            if length > 2.0:
                second_len = min(1.0, length - 2.0)
                events.append({"start": start + 2.0, "end": start + 2.0 + second_len, "pitches": [root, fifth]})
    return events


# ---- RH chord-tone stacking (melody stays on top) ----

HARMONIZE_MIN_DURATION_BEATS = 2.0  # only stack chord tones under held/strong melody notes -- a
# half note or longer. 1.0 (quarter note) caught nearly every melody note on
# real audio (most melody notes end up >=1 beat once rhythm-simplified),
# producing a chord under almost everything instead of "primarily melody plus
# optional chord tones." 2.0 keeps harmony at phrase/cadence points instead.
MAX_RH_CHORD_TONES = 2  # + the melody note itself = 3, matching "normally 3 notes" guidance
LOWEST_USABLE_PITCH = 21  # A0


def build_rh_events(melody: list[dict], regions: list[dict]) -> list[dict]:
    events = []
    for mn in melody:
        pitches = [mn["pitch"]]
        if mn["end"] - mn["start"] >= HARMONIZE_MIN_DURATION_BEATS:
            region = _find_region(regions, mn["start"])
            if region:
                chord_pcs = [(region["root_pc"] + iv) % 12 for iv in CHORD_QUALITIES[region["quality"]]]
                melody_pc = mn["pitch"] % 12
                candidates = []
                for pc in chord_pcs:
                    if pc == melody_pc:
                        continue
                    p = mn["pitch"] - ((mn["pitch"] - pc) % 12)
                    if p > mn["pitch"]:
                        p -= 12
                    if mn["pitch"] - p <= 12 and p >= LOWEST_USABLE_PITCH:
                        candidates.append(p)
                under_notes = sorted(set(candidates), reverse=True)[:MAX_RH_CHORD_TONES]
                pitches = sorted(under_notes + [mn["pitch"]])
        events.append({"start": mn["start"], "end": mn["end"], "pitches": pitches})
    return events


# ---- Rhythm simplification ----

# Simple -> complex; snapping prefers the earliest (simplest) entry within tolerance.
# Every value here MUST be a multiple of MELODY_GRID's slice length (0.25, a
# sixteenth note) -- melody/chord-region boundaries are all computed on that
# grid, so a vocabulary value that isn't a multiple of it (0.375, a dotted
# eighth, was here before) knocks every later note off-grid by the remainder
# once it's used, and that drift compounds across the piece. This also was
# never part of the requested vocabulary (whole/half/quarter/eighth/dotted
# half/dotted quarter/sixteenth-when-needed) -- it was an unrequested addition.
RHYTHM_VOCAB = [4.0, 3.0, 2.0, 1.5, 1.0, 0.5, 0.25]
RHYTHM_TOLERANCE = 0.0375  # ~15% of a sixteenth note's quarter-length


def _snap_duration(duration: float, max_allowed: float) -> float:
    """Snaps to the simplest vocabulary value close to `duration`, but never
    past `max_allowed` (the gap to the next event) -- picking a "simpler"
    value that doesn't fit and then clipping it back down after the fact was
    an earlier bug here: it silently crushed a chord decided at, say, a
    dotted-quarter duration down to a sixteenth note when the next melody
    note came in sooner than expected, producing exactly the cluttered
    "3-note chord jammed into a sixteenth" notation this mode exists to
    avoid.

    A duration longer than the vocabulary's max (a whole note) is a
    different case, not a "simplify this" case -- it's a chord held across
    more than one measure (a real LH accompaniment event can span a
    multi-measure harmonic region). Snapping that down to 4.0 was a second,
    worse bug: it discarded the excess instead of leaving it to notate as a
    tied whole+half (completely normal notation), which silently deleted
    real LH content and left measures looking empty despite a valid chord."""
    if duration > RHYTHM_VOCAB[0]:
        return min(duration, max_allowed) if max_allowed > 0 else duration
    candidates = [v for v in RHYTHM_VOCAB if v <= max_allowed + 1e-6]
    if not candidates:
        return max(max_allowed, 0.0)
    best_err = min(abs(v - duration) for v in candidates)
    for v in candidates:  # already ordered simple -> complex
        if abs(v - duration) <= best_err + RHYTHM_TOLERANCE:
            return v
    return candidates[-1]


def simplify_rhythm(events: list[dict]) -> list[dict]:
    events = sorted(events, key=lambda e: e["start"])
    for i, e in enumerate(events):
        raw_duration = e["end"] - e["start"]
        max_allowed = (events[i + 1]["start"] - e["start"]) if i + 1 < len(events) else raw_duration
        e["end"] = e["start"] + _snap_duration(raw_duration, max_allowed)
    return events


# ---- Score construction ----

MEASURE_BEATS = 4.0
DENSE_MEASURE_NOTE_COUNT = 8  # average notes/measure above this -> tighter system breaks
MEASURES_PER_SYSTEM_DENSE = 3
MEASURES_PER_SYSTEM_NORMAL = 4


def _insert_events(target: stream.PartStaff, events: list[dict]) -> None:
    for e in events:
        length = e["end"] - e["start"]
        if length <= 0:
            continue
        pitches = e["pitches"]
        element = (
            m21chord.Chord(pitches, quarterLength=length) if len(pitches) > 1 else m21note.Note(pitches[0], quarterLength=length)
        )
        target.insert(e["start"], element)


def build_church_score(
    rh_events: list[dict],
    lh_events: list[dict],
    regions: list[dict],
    key_signature: m21key.KeySignature,
    tempo_bpm: float,
    title: str,
    build_staff_pair,
) -> stream.Score:
    """build_staff_pair is notation.py's staff/metadata scaffolding, reused because
    it has nothing to do with arrangement philosophy (clefs, key/time signature,
    PartStaff+StaffGroup piano-brace setup, title/composer metadata)."""
    treble, bass, staff_group, score = build_staff_pair(key_signature, tempo_bpm, title)

    _insert_events(treble, rh_events)
    _insert_events(bass, lh_events)

    # RH and LH don't necessarily end at the same offset -- a final LH chord
    # can ring on after the last melody note (a real "outro chord" case, not
    # a bug). Left alone, the shorter part's makeMeasures stops short and the
    # longer part pads it out with a blank, rest-less trailing measure to
    # keep both parts the same length -- a visibly broken-looking measure
    # (redundant clef/time-signature reset, no rest, nothing). Padding both
    # parts to the shared end time with an explicit rest first makes that
    # last measure render as a normal held-note-plus-rest measure instead.
    shared_end = max(treble.highestTime, bass.highestTime)
    for target in (treble, bass):
        target.makeRests(refStreamOrTimeRange=(0.0, shared_end), fillGaps=True, inPlace=True)
        target.makeMeasures(inPlace=True)
        # makeMeasures alone does NOT split a note/chord that spans a barline
        # into per-measure tied fragments -- it was leaving the element
        # attached to its starting measure only, so a long held LH chord
        # (routine here: LH intentionally holds chords across multiple
        # measures) made the measure(s) it continued through look completely
        # empty despite a real, valid chord underneath.
        target.makeTies(inPlace=True)
        for m in target.getElementsByClass(stream.Measure):
            m.makeVoices(inPlace=True, fillGaps=True)

    # Chord symbols are inserted into their measure *after* makeRests/makeMeasures/
    # makeVoices, not before: harmony.ChordSymbol is internally a Chord subclass
    # with real pitches (e.g. "F7" carries actual F-A-C-Eb pitches), so inserting
    # it into the flat stream beforehand gets it swept up by every later
    # notes/chords scan (voice counting, density, readability diagnostics) as if
    # it were a notated chord in the music.
    use_flats = key_signature.sharps < 0
    treble_measures = list(treble.getElementsByClass(stream.Measure))
    for region in regions:
        cs = harmony.ChordSymbol(chord_symbol_text(region["root_pc"], region["quality"], use_flats))
        cs.writeAsChord = False
        target_measure = next((m for m in treble_measures if m.offset <= region["start"] < m.offset + m.barDuration.quarterLength), None)
        if target_measure is not None:
            target_measure.insert(region["start"] - target_measure.offset, cs)

    total_notes = sum(1 for el in treble.recurse().notes if not isinstance(el, harmony.Harmony)) + sum(
        1 for el in bass.recurse().notes if not isinstance(el, harmony.Harmony)
    )
    total_measures = len(list(treble.getElementsByClass(stream.Measure))) or 1
    avg_notes_per_measure = total_notes / total_measures
    measures_per_system = MEASURES_PER_SYSTEM_DENSE if avg_notes_per_measure > DENSE_MEASURE_NOTE_COUNT else MEASURES_PER_SYSTEM_NORMAL
    for target in (treble, bass):
        measures = list(target.getElementsByClass(stream.Measure))
        for i, m in enumerate(measures):
            if i > 0 and i % measures_per_system == 0:
                m.insert(0, layout.SystemLayout(isNew=True))

    return score


# ---- Diagnostics ----

READABILITY_THRESHOLD = 70
LOW_READING_RANGE = 36  # C2
HIGH_READING_RANGE = 84  # C6


def _measure_readability(m: stream.Measure) -> tuple[int, list[str]]:
    voices = list(m.voices) if m.voices else [m]
    problems = []
    penalty = 0

    if len(voices) > 1:
        penalty += (len(voices) - 1) * 15
        problems.append(f"{len(voices)} voices")

    all_elements = [el for v in voices for el in v.notesAndRests if not isinstance(el, harmony.Harmony)]
    rests = [el for el in all_elements if el.isRest]
    if rests:
        penalty += len(rests) * 2
        problems.append(f"{len(rests)} rests")

    real = [el for el in all_elements if not el.isRest]
    sixteenths = [el for el in real if el.duration.quarterLength <= 0.25 + 1e-6]
    if sixteenths:
        penalty += len(sixteenths) * 3
        problems.append(f"{len(sixteenths)} sixteenth-note fragment(s)")

    pitches_in_order = []
    for el in sorted(real, key=lambda e: e.offset):
        top_pitch = max(p.midi for p in el.pitches) if el.isChord else el.pitch.midi
        pitches_in_order.append(top_pitch)
    max_leap = max((abs(b - a) for a, b in zip(pitches_in_order, pitches_in_order[1:])), default=0)
    if max_leap > LARGE_JUMP_SEMITONES:
        penalty += (max_leap - LARGE_JUMP_SEMITONES) * 2
        problems.append(f"large leap ({max_leap} semitones)")

    max_chord_size = max((len(el.pitches) for el in real if el.isChord), default=0)
    if max_chord_size > 3:
        penalty += (max_chord_size - 3) * 10
        problems.append(f"{max_chord_size}-note chord")

    out_of_range = sum(
        1
        for el in real
        for p in (el.pitches if el.isChord else [el.pitch])
        if p.midi < LOW_READING_RANGE or p.midi > HIGH_READING_RANGE
    )
    if out_of_range:
        penalty += out_of_range * 5
        problems.append(f"{out_of_range} note(s) outside normal reading range")

    return max(0, 100 - penalty), problems


def _part_measure_stats(part: stream.PartStaff) -> dict:
    """Per-part voice/rest/emptiness stats, used both for the report and for
    the assertions/warnings -- computed once, straight off the actual score
    object that gets written to disk (not off the intermediate event lists),
    so this is checking what the exporter really received."""
    max_voices = 1
    over_2_voices = 0
    total_rests = 0
    total_measures = 0
    high_rest_measures = []
    for m in part.getElementsByClass(stream.Measure):
        total_measures += 1
        voices = list(m.voices) if m.voices else [m]
        max_voices = max(max_voices, len(voices))
        if len(voices) > 2:
            over_2_voices += 1
        rests_here = sum(1 for v in voices for el in v.notesAndRests if el.isRest)
        total_rests += rests_here
        if rests_here > 4:
            high_rest_measures.append(m.number)
    return {
        "max_voices": max_voices,
        "over_2_voices": over_2_voices,
        "total_rests": total_rests,
        "total_measures": total_measures,
        "avg_rests": total_rests / total_measures if total_measures else 0.0,
        "high_rest_measures": high_rest_measures,
    }


def _empty_lh_despite_chord(bass: stream.PartStaff, regions: list[dict]) -> list[int]:
    """Measures that overlap a detected chord region but contain no actual
    LH note/chord -- i.e. LH going silent even though harmony exists to
    accompany. Structurally this shouldn't happen (LH is generated from every
    region, not from whether the source recording had an audible bass note),
    but it's checked directly against the real score object rather than
    assumed."""
    flagged = []
    for m in bass.getElementsByClass(stream.Measure):
        m_start, m_end = m.offset, m.offset + m.barDuration.quarterLength
        overlaps_region = any(r["start"] < m_end and r["end"] > m_start for r in regions)
        if not overlaps_region:
            continue
        has_note = any(not el.isRest for el in m.recurse().notesAndRests if not isinstance(el, harmony.Harmony))
        if not has_note:
            flagged.append(m.number)
    return flagged


def _count_out_of_range(part: stream.PartStaff) -> int:
    count = 0
    for el in part.recurse().notes:
        if isinstance(el, harmony.Harmony):
            continue
        for p in (el.pitches if el.isChord else [el.pitch]):
            if p.midi < LOW_READING_RANGE or p.midi > HIGH_READING_RANGE:
                count += 1
    return count


def print_church_mode_report(
    score: stream.Score,
    treble: stream.PartStaff,
    bass: stream.PartStaff,
    regions: list[dict],
    pattern: str,
    use_flats: bool,
    trace: dict,
    transcription_comparison: dict | None,
    raw_outlier_count: int,
) -> None:
    raw_count = trace["raw_notes"]
    exported_count = trace["exported_notes"]
    reduction_pct = 100 * (1 - exported_count / raw_count) if raw_count else 0

    print("\n=== CHURCH MODE REPORT ===\n")
    print("Pipeline trace (proves the exporter received the arranged score, not the raw transcription):")
    print(f"  Raw transcription notes:        {trace['raw_notes']}")
    print(f"  Notes entering church arranger:  {trace['arranger_in']}")
    print(f"  Notes leaving church arranger:   {trace['arranger_out']} (RH events: {trace['rh_events']}, LH events: {trace['lh_events']})")
    print(f"  Notes/chords sent to exporter:   {trace['exported_notes']} (counted directly on the score object written to disk)")

    print(f"\nRaw detected notes:      {raw_count}")
    print(f"Final arranged notes:    {exported_count}")
    print(f"Reduction:               {reduction_pct:.1f}%")

    treble_stats = _part_measure_stats(treble)
    bass_stats = _part_measure_stats(bass)
    empty_lh = _empty_lh_despite_chord(bass, regions)
    out_of_range_after = _count_out_of_range(treble) + _count_out_of_range(bass)

    if transcription_comparison:
        print(f"\nRaw voices max (transcription mode, same input): {transcription_comparison['max_voices']}")
    print(f"Final RH voices max:     {treble_stats['max_voices']}")
    print(f"Final LH voices max:     {bass_stats['max_voices']}")

    print("\nAverage visible rests/measure:")
    if transcription_comparison:
        print(f"  before (transcription mode): {transcription_comparison['avg_rests']:.2f}")
    print(f"  after (church mode, RH):     {treble_stats['avg_rests']:.2f}")
    print(f"  after (church mode, LH):     {bass_stats['avg_rests']:.2f}")

    print(f"\nMeasures with >2 voices (RH): {treble_stats['over_2_voices']}")
    print(f"Measures with >2 voices (LH): {bass_stats['over_2_voices']}")
    print(f"Measures with empty LH despite valid chord: {len(empty_lh)}")

    print("\nIsolated extreme notes:")
    print(f"  before (transcription mode outlier flags): {raw_outlier_count}")
    print(f"  after (church mode, outside {LOW_READING_RANGE}-{HIGH_READING_RANGE} MIDI): {out_of_range_after}")

    print(f"\nDetected chord regions: {len(regions)}")
    print("  " + " | ".join(chord_symbol_text(r["root_pc"], r["quality"], use_flats) for r in regions[:24]) + (" ..." if len(regions) > 24 else ""))
    print(f"\nAccompaniment pattern: {pattern}")

    warnings = []
    if treble_stats["max_voices"] > 2:
        warnings.append(f"RH max voices is {treble_stats['max_voices']} (>2)")
    if bass_stats["max_voices"] > 2:
        warnings.append(f"LH max voices is {bass_stats['max_voices']} (>2)")
    if empty_lh:
        warnings.append(f"LH is empty despite a valid chord in {len(empty_lh)} measure(s): {empty_lh[:10]}{'...' if len(empty_lh) > 10 else ''}")
    if raw_count and exported_count / raw_count > 0.75:
        warnings.append(f"church mode may not be simplifying enough (final/raw = {exported_count / raw_count:.2f}, expected well below 0.75)")
    if treble_stats["high_rest_measures"] or bass_stats["high_rest_measures"]:
        combined = sorted(set(treble_stats["high_rest_measures"]) | set(bass_stats["high_rest_measures"]))
        warnings.append(f"unusually many rests (>4) in {len(combined)} measure(s): {combined[:10]}{'...' if len(combined) > 10 else ''}")

    print(f"\n=== WARNINGS ({len(warnings)}) ===")
    for w in warnings:
        print(f"  WARNING: {w}")
    if not warnings:
        print("  (none)")

    flagged = []
    for part in (treble, bass):
        for m in part.getElementsByClass(stream.Measure):
            score_val, problems = _measure_readability(m)
            if score_val < READABILITY_THRESHOLD:
                flagged.append((part.partName, m.number, score_val, problems))
    print(f"\nFlagged measures (readability < {READABILITY_THRESHOLD}): {len(flagged)}")
    for part_name, number, score_val, problems in flagged[:20]:
        print(f"  {part_name} measure {number}: {score_val}/100 -- {', '.join(problems)}")
    if len(flagged) > 20:
        print(f"  ... and {len(flagged) - 20} more")
    print("=== END CHURCH MODE REPORT ===\n")


def run(
    notes: list[dict],
    tempo_bpm: float,
    title: str,
    build_staff_pair,
    transcription_comparison: dict | None = None,
    raw_outlier_count: int = 0,
) -> stream.Score:
    trace = {"raw_notes": len(notes), "arranger_in": len(notes)}

    melody = extract_melody(notes, tempo_bpm)
    # Simplify the melody's rhythm first, so the harmonize/no-harmonize decision
    # in build_rh_events (and the chord's final notated duration) is made from
    # the duration that will actually be notated -- not a pre-quantization
    # estimate that a later clipping pass could shrink out from under it.
    melody = simplify_rhythm(melody)
    regions = detect_chord_regions(notes, tempo_bpm)
    # church_sheet mode's own key estimate, derived from the chord regions --
    # not transcription mode's Stream.analyze('key') on the raw notes (see
    # infer_key_signature's docstring for why that can disagree with the
    # actual detected harmony).
    key_signature = infer_key_signature(regions)
    pattern = pick_lh_pattern(notes, tempo_bpm, regions)

    rh_events = build_rh_events(melody, regions)
    lh_events = simplify_rhythm(generate_lh_events(regions, pattern))
    trace["rh_events"] = len(rh_events)
    trace["lh_events"] = len(lh_events)
    trace["arranger_out"] = len(rh_events) + len(lh_events)

    score = build_church_score(rh_events, lh_events, regions, key_signature, tempo_bpm, title, build_staff_pair)
    treble = next(p for p in score.parts if p.id == "treble")
    bass = next(p for p in score.parts if p.id == "bass")

    # Counted directly on the score object that gets handed to score.write() --
    # not on rh_events/lh_events -- so this number proves what the exporter
    # actually received, independent of anything upstream. Excludes tie
    # "continue"/"stop" fragments: makeTies splits one logical held chord
    # into several tied notation fragments, and counting those as separate
    # notes would make a single long LH chord look like 3-4 distinct events.
    def _logical_note_count(part: stream.PartStaff) -> int:
        return sum(
            1
            for el in part.recurse().notes
            if not isinstance(el, harmony.Harmony) and (el.tie is None or el.tie.type == "start")
        )

    trace["exported_notes"] = _logical_note_count(treble) + _logical_note_count(bass)

    use_flats = key_signature.sharps < 0
    print_church_mode_report(
        score,
        treble,
        bass,
        regions,
        pattern,
        use_flats,
        trace,
        transcription_comparison,
        raw_outlier_count,
    )
    return score
