"""Beat-synced compilation core (pure python + ffmpeg + librosa; no ComfyUI imports).

Pipeline:  manifest text -> per-clip motion tags (keyword rules, no VL model)
           audio -> beats + per-beat energy rank + drop markers
           plan  -> cut points on the beat grid, clips matched by energy/motion
           render-> parallel ffmpeg segment encode, concat, mux the song
"""
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (compatible; BeatCompile/1.0)"}

# ── motion tagging ───────────────────────────────────────────────────────────
# First matching rule sets (motion, base intensity 0-1). Order matters: most
# energetic / most specific first. Intensity is "how much visual energy does
# this clip carry", which is what gets matched against the song's energy.
CAMERA_RULES = [
    ("whip",   0.95, r"whip[- ]?pan|crash[- ]?zoom|snap[- ]?zoom|rapid(ly)? (pan|zoom|cut)|"
                     r"shaky|handheld chase|fpv|speed ramp|dutch roll|spinning camera|rotating camera"),
    ("zoom",   0.70, r"zoom(ing)? in|push[- ]?in|dolly[- ]?in|dolly zoom|punch[- ]?in|move(s|d)? closer|"
                     r"zoom(ing)? out|pull[- ]?out|pull(s|ed)? back|dolly[- ]?out|crane (up|down)"),
    ("orbit",  0.65, r"orbit|arc shot|arcing|360|circl(e|ing) (around|the)|revolv|tracking shot|"
                     r"follow(s|ing)? (shot|the)|steadicam|truck(ing)? (left|right)"),
    ("fly",    0.60, r"drone|aerial|fly(ing)? (over|through|above)|flyover|sweeping"),
    ("pan",    0.50, r"\bpan(s|ning)?\b|\btilt(s|ing)?\b|slide[sr]?|dolly|travel(l)?ing shot|parallax"),
    ("handheld", 0.55, r"handheld|hand-held|documentary|found footage"),
    ("static", 0.15, r"static|locked[- ]?off|tripod|still shot|fixed camera|slow(ly)?|gentle|"
                     r"subtle|calm|serene|peaceful|time[- ]?lapse of clouds|breathing"),
]
HIGH_WORDS = r"danc|run(ning)?|sprint|chase|explo|fight|battle|crash|speed|racing|storm|fire|jump|spin|" \
             r"fast|energetic|intense|dynamic|chaos|burst|flash|strobe|party|crowd|dramatic"
LOW_WORDS = r"sleep|rest(ing)?|quiet|still|empty|calm|dim|fog|mist|slow|stare|stand(ing)?|sit(ting)?|" \
            r"portrait|close[- ]?up of (a )?(face|eyes)|sunset|night time scene|serene"


def tag_prompt(prompt):
    """-> (motion, intensity). Pure keyword rules; unknown prompt = neutral."""
    p = (prompt or "").lower()
    motion, base = "unknown", 0.40
    for name, inten, rx in CAMERA_RULES:
        if re.search(rx, p):
            motion, base = name, inten
            break
    else:
        if re.search(r"[a-z]", p):
            motion, base = "static", 0.30      # a described scene with no camera words
    base += 0.12 * min(2, len(re.findall(HIGH_WORDS, p)))
    base -= 0.10 * min(2, len(re.findall(LOW_WORDS, p)))
    return motion, round(max(0.05, min(1.0, base)), 3)


# ── manifest ─────────────────────────────────────────────────────────────────
def parse_manifest(text):
    """-> (by_slot {1..8: prompt}, extras [(url, prompt)]).

    Accepts JSON ({"1": "...", "https://..": "..."} or a list) or lines:
        2: slow dolly in on a lighthouse
        https://host/clip.mp4 | whip pan across a street      (extra clip, no slot)
        a line with no prefix                                  (next free slot)
    Chat frontends escape < and > with a backslash; strip those.
    """
    text = (text or "").replace("\\<", "<").replace("\\>", ">").strip()
    by_slot, extras = {}, []
    if not text:
        return by_slot, extras
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for k, v in data.items():
                if str(k).startswith("http"):
                    extras.append((str(k), str(v)))
                elif str(k).isdigit():
                    by_slot[int(k)] = str(v)
            return by_slot, extras
        if isinstance(data, list):
            for i, v in enumerate(data, 1):
                by_slot[i] = str(v)
            return by_slot, extras
    except ValueError:
        pass
    # The Graydient API collapses newlines to spaces, so a manifest usually arrives as ONE line.
    # Re-split before each "N:" marker (N must run 1,2,3.. in order, so "at 5: 30" inside a prompt
    # does not split; to skip a slot write "3: -") and before bare URLs.
    marks, last = [], 0
    for m in re.finditer(r"(?:(?<=\s)|^)(\d{1,2})\s*:\s", text):
        n = int(m.group(1))
        if (last == 0 and n <= 8) or n == last + 1:      # numbers must run consecutively
            marks.append(m.start())
            last = n
    for m in re.finditer(r"(?:(?<=\s)|^)https?://\S+\s*\|", text):
        marks.append(m.start())
    if marks:
        cuts = sorted(set(marks))
        pieces = ([text[:cuts[0]]] if cuts[0] > 0 else []) + \
                 [text[a:b] for a, b in zip(cuts, cuts[1:] + [len(text)])]
        text = "\n".join(p.strip() for p in pieces if p.strip())
    nxt = 1
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        m = re.match(r"^(\d{1,2})\s*[:|)\-]\s*(.*)$", line)
        if m:
            by_slot[int(m.group(1))] = m.group(2).strip()
            nxt = int(m.group(1)) + 1
        elif line.startswith("http"):
            url, _, pr = line.partition("|")
            extras.append((url.strip(), pr.strip()))
        else:
            while nxt in by_slot:
                nxt += 1
            by_slot[nxt] = line
            nxt += 1
    return by_slot, extras


# ── media helpers ────────────────────────────────────────────────────────────
def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe_duration(path):
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nk=1:nw=1", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def download(url, dest_dir):
    ext = os.path.splitext(url.split("?")[0])[-1] or ".mp4"
    path = os.path.join(dest_dir, f"dl_{abs(hash(url)) % 10**8}{ext}")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)
    return path


def resolve_file(name, input_dir, tmp_dir):
    """Graydient numbered fields deliver a filename in input/; also accept a path or URL."""
    name = (name or "").strip()
    if not name:
        return None
    if name.startswith(("http://", "https://")):
        return download(name, tmp_dir)
    for cand in (os.path.join(input_dir, name) if input_dir else None, name):
        if cand and os.path.isfile(cand):
            return cand
    return None


# ── audio analysis ───────────────────────────────────────────────────────────
def analyze_audio(audio_path, max_seconds, tmp_dir):
    import numpy as np
    import librosa

    wav = os.path.join(tmp_dir, "audio_mono.wav")
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", audio_path, "-t", str(max_seconds),
              "-ac", "1", "-ar", "22050", wav])
    if r.returncode != 0 or not os.path.isfile(wav):
        raise RuntimeError(f"ffmpeg could not decode audio: {r.stderr[-300:]}")
    y, sr = librosa.load(wav, sr=22050, mono=True)
    duration = float(len(y)) / sr
    hop = 512
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop)
    bpm = float(np.asarray(tempo).item())
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop).tolist()
    if len(beats) < 4 or bpm <= 0:          # no usable pulse: fixed half-second grid
        bpm = 120.0
        beats = [i * 0.5 for i in range(int(duration / 0.5))]
    # anchor the grid at 0 so the first cut is the start of the song
    if beats[0] > 0.05:
        step = 60.0 / bpm
        pre = []
        t = beats[0] - step
        while t > 0.05:
            pre.append(t)
            t -= step
        beats = [0.0] + sorted(pre) + beats
    else:
        beats[0] = 0.0

    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    n = min(len(rms), len(onset))
    rms, onset = rms[:n], onset[:n]
    ft = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=hop)
    edges = beats + [duration]
    raw = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (ft >= a) & (ft < max(b, a + 1e-3))
        if not m.any():
            raw.append(raw[-1] if raw else 0.0)
            continue
        raw.append(float(0.7 * rms[m].mean() / (rms.max() or 1) + 0.3 * onset[m].mean() / (onset.max() or 1)))
    raw = np.array(raw)
    # light smoothing, then percentile rank so the whole 0-1 range gets used
    sm = np.convolve(np.pad(raw, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
    rank = sm.argsort().argsort() / max(1, len(sm) - 1)
    # drops: energy jumps well above the previous 4 beats
    drop = [False] * len(sm)
    for i in range(4, len(sm)):
        if sm[i] - sm[i - 4:i].mean() > 0.35 * (sm.max() - sm.min() + 1e-9) and rank[i] > 0.7:
            drop[i] = True
    return {"duration": duration, "bpm": bpm, "beats": beats, "energy": rank.tolist(), "drop": drop}


# ── planning ─────────────────────────────────────────────────────────────────
def _pow2_divisor(i):
    return 8 if i == 0 else min(8, i & -i)


def plan_cuts(analysis, density, min_seg=0.30):
    beats, energy, drop = analysis["beats"], analysis["energy"], analysis["drop"]
    duration = analysis["duration"]
    segs, i, nb = [], 0, len(beats)
    while i < nb:
        e = energy[i]
        base = 1 if e >= 0.8 else 2 if e >= 0.6 else 4 if e >= 0.35 else 8
        if drop[i]:
            base = 1
        L = max(1, round(base / max(0.25, density)))
        L = 1 << int(math.floor(math.log2(L)))             # power of two
        L = min(L, _pow2_divisor(i))                       # land on bar lines
        j = min(nb, i + L)
        t0 = beats[i]
        t1 = beats[j] if j < nb else duration
        while t1 - t0 < min_seg and j < nb:                # too short: absorb next beat
            j += 1
            t1 = beats[j] if j < nb else duration
        if t1 - t0 > 0.05:
            segs.append({"t0": t0, "t1": t1, "energy": float(sum(energy[i:j]) / max(1, j - i)),
                         "drop": any(drop[i:j]), "i": i, "j": j})
        i = j
    return segs



# ── over-editing: measurable rhythmic subdivision ───────────────────────────
# Units are sixteenth notes (4 per beat). A level picks a repeating pattern of unit lengths:
#   1: eighths [2]   2: tresillo [3,3,2]   3: eighth + two sixteenths [2,1,1]   4: sixteenth stutter [1]
# Drops at level >= 2 use an accelerating ramp [8,4,2,1,1] instead (halving = audible build).
LEVEL_PATTERNS = {1: [2], 2: [3, 3, 2], 3: [2, 1, 1], 4: [1]}
RAMP = [8, 4, 2, 1, 1]


def _unit_times(seg, beats, duration):
    """Evenly spaced sixteenth boundaries between the beats a segment covers."""
    out = []
    nb = len(beats)
    for b in range(seg["i"], seg["j"]):
        a = beats[b]
        z = beats[b + 1] if b + 1 < nb else duration
        out += [a + (z - a) * u / 4.0 for u in range(4)]
    out.append(seg["t1"])
    return out


def _seg_level(energy, over):
    return max(0, min(4, int(round(over * 4 * (0.5 + energy)))))


def apply_rhythm(segs, analysis, over, fps, force_level=None):
    """Split segments into rhythmic pieces; pieces shorter than ~4 frames are merged."""
    if over <= 0 and not force_level:
        return segs
    beats, duration = analysis["beats"], analysis["duration"]
    min_d = max(4.0 / fps, 0.15)
    out = []
    for s in segs:
        lvl = force_level or _seg_level(s["energy"], over)
        if lvl <= 0:
            out.append(s)
            continue
        pat = RAMP if (s["drop"] and lvl >= 2) else LEVEL_PATTERNS[lvl]
        ut = _unit_times(s, beats, duration)
        n_units = len(ut) - 1
        cuts, pos, k = [0], 0, 0
        while pos < n_units:
            pos += pat[k % len(pat)]
            k += 1
            cuts.append(min(pos, n_units))
        # merge pieces shorter than min_d into the previous one
        keep = [cuts[0]]
        for c in cuts[1:]:
            if ut[c] - ut[keep[-1]] >= min_d or c == n_units and len(keep) == 1:
                keep.append(c)
            elif c == n_units:
                keep[-1] = c if len(keep) > 1 else keep[-1]
        if keep[-1] != n_units:
            keep.append(n_units)
        for a, b in zip(keep[:-1], keep[1:]):
            if ut[b] - ut[a] > 0.05:
                out.append({**s, "t0": ut[a], "t1": ut[b]})
    return out


def plan_with_coverage(analysis, density, over, fps, n_clips, use_all):
    segs = apply_rhythm(plan_cuts(analysis, density), analysis, over, fps)
    if not use_all:
        return segs, over
    # raise the over-edit level until there are enough cuts for every clip to appear
    o = over
    while len(segs) < n_clips and o < 1.0:
        o = min(1.0, o + 0.1)
        segs = apply_rhythm(plan_cuts(analysis, density), analysis, o, fps)
    if len(segs) < n_clips:
        segs = apply_rhythm(plan_cuts(analysis, density), analysis, 1.0, fps, force_level=4)
        o = 1.0
    return segs, o


def assign_clips(segs, clips, seed, use_all=False):
    rng = random.Random(seed)
    uses = {c["id"]: 0 for c in clips}
    cursor = {c["id"]: 0.0 for c in clips}
    prev_id = prev_motion = None
    forced = {}
    if use_all:   # every clip gets the segment whose energy best matches it (rarest fit first)
        free = set(range(len(segs)))
        order = sorted(clips, key=lambda c: -abs(c["intensity"] - 0.5))
        for c in order:
            if not free:
                break
            k = min(free, key=lambda i: abs(segs[i]["energy"] - c["intensity"]) +
                    (0.5 if (i - 1 in forced and forced[i - 1] == c["id"]) or
                     (i + 1 in forced and forced[i + 1] == c["id"]) else 0.0))
            forced[k] = c["id"]
            free.discard(k)
    by_id = {c["id"]: c for c in clips}
    for idx, s in enumerate(segs):
        d = s["t1"] - s["t0"]
        target = min(1.0, s["energy"] + (0.12 if s["drop"] else 0.0))
        best, best_score = None, -1e9
        for c in ([by_id[forced[idx]]] if idx in forced else clips):
            sc = -abs(c["intensity"] - target)
            if len(clips) > 1 and c["id"] == prev_id:
                sc -= 1.0
            if c["motion"] == prev_motion and c["motion"] != "unknown":
                sc -= 0.12
            sc -= 0.15 * uses[c["id"]]
            if c["duration"] - cursor[c["id"]] < d:
                sc -= 0.25
            sc += rng.uniform(-0.05, 0.05)
            if sc > best_score:
                best, best_score = c, sc
        c = best
        if cursor[c["id"]] + d > c["duration"] - 0.05:
            cursor[c["id"]] = 0.0
        s["clip"], s["src_start"] = c["id"], round(cursor[c["id"]], 3)
        cursor[c["id"]] += d
        uses[c["id"]] += 1
        prev_id, prev_motion = c["id"], c["motion"]
    return segs


# ── render ───────────────────────────────────────────────────────────────────
def render(segs, clips_by_id, audio_path, out_path, width, height, fps, tmp_dir, total_dur):
    # frame-exact boundaries: round cumulative times, never per-segment lengths
    bounds = [round(s["t0"] * fps) for s in segs] + [round(total_dur * fps)]
    jobs = []
    for k, s in enumerate(segs):
        nf = bounds[k + 1] - bounds[k]
        if nf <= 0:
            continue
        c = clips_by_id[s["clip"]]
        seg_path = os.path.join(tmp_dir, f"seg_{k:04d}.mp4")
        need = nf / fps
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},fps={fps},setsar=1,"
              f"tpad=stop_mode=clone:stop_duration={need:.3f}")
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{s['src_start']:.3f}", "-i", c["path"],
               "-an", "-vf", vf, "-frames:v", str(nf), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps), seg_path]
        jobs.append((k, cmd, seg_path))

    def go(job):
        k, cmd, p = job
        r = _run(cmd)
        if r.returncode != 0 or not os.path.isfile(p):
            raise RuntimeError(f"segment {k} failed: {r.stderr[-300:]}")
        return p

    workers = max(1, min(6, os.cpu_count() or 2))
    with ThreadPoolExecutor(workers) as ex:
        seg_files = list(ex.map(go, jobs))
    lst = os.path.join(tmp_dir, "concat.txt")
    with open(lst, "w") as f:
        for p in seg_files:
            f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
    silent = os.path.join(tmp_dir, "video_only.mp4")
    r = _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", silent])
    if r.returncode != 0:
        raise RuntimeError(f"concat failed: {r.stderr[-300:]}")
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", silent, "-i", audio_path, "-map", "0:v:0",
              "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-t", f"{total_dur:.3f}", "-movflags", "+faststart", out_path])
    if r.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError(f"mux failed: {r.stderr[-300:]}")
    return out_path


# ── top level ────────────────────────────────────────────────────────────────
def compile_montage(audio_file, manifest, video_files, input_dir, out_path, *, height=720, aspect=0,
                    fps=24, max_frames=2880, cut_density=1.0, seed=0, overedit=0.0, use_all=False,
                    log=print):
    tmp = tempfile.mkdtemp(prefix="beatcompile_")
    by_slot, extras = parse_manifest(manifest)
    clips = []
    for slot, name in enumerate(video_files, 1):
        p = resolve_file(name, input_dir, tmp)
        if p:
            clips.append({"id": f"video{slot}", "path": p, "prompt": by_slot.get(slot, "")})
    for n, (url, pr) in enumerate(extras[:24], 1):
        try:
            clips.append({"id": f"extra{n}", "path": download(url, tmp), "prompt": pr})
        except Exception as e:                                  # one bad URL should not kill the job
            log(f"[BeatCompile] extra clip {url} skipped: {e}")
    if not clips:
        raise RuntimeError("no input clips resolved (video1..video8 empty and no URLs in the manifest)")
    for c in clips:
        c["duration"] = probe_duration(c["path"])
        c["motion"], c["intensity"] = tag_prompt(c["prompt"])
    clips = [c for c in clips if c["duration"] > 0.2]
    audio = resolve_file(audio_file, input_dir, tmp)
    if not audio:
        raise RuntimeError(f"audio file not found: {audio_file!r}")
    max_seconds = max(5.0, max_frames / float(fps))
    an = analyze_audio(audio, max_seconds, tmp)
    segs, over_used = plan_with_coverage(an, cut_density, float(overedit), int(fps), len(clips), bool(use_all))
    segs = assign_clips(segs, clips, seed, bool(use_all))
    used = {s['clip'] for s in segs}
    if len(used) < len(clips):
        log(f"[BeatCompile] only {len(used)}/{len(clips)} clips fit this song's length")
    ar = {0: 16 / 9, 1: 9 / 16, 2: 1.0, 3: 4 / 3}.get(int(aspect), 16 / 9)
    h = int(height) // 2 * 2
    w = int(round(h * ar)) // 2 * 2
    log(f"[BeatCompile] {len(clips)} clips, {an['bpm']:.1f} bpm, {len(segs)} segments, {w}x{h}@{fps}")
    render(segs, {c["id"]: c for c in clips}, audio, out_path, w, h, int(fps), tmp, an["duration"])
    report = {"overedit_used": round(over_used, 2), "clips_used": len(used),
              "bpm": round(an["bpm"], 2), "duration": round(an["duration"], 2), "size": f"{w}x{h}",
              "clips": [{k: c[k] for k in ("id", "prompt", "motion", "intensity", "duration")} for c in clips],
              "segments": [{"t0": round(s["t0"], 2), "t1": round(s["t1"], 2), "energy": round(s["energy"], 2),
                            "drop": s["drop"], "clip": s["clip"], "src_start": s["src_start"]} for s in segs]}
    return out_path, report
