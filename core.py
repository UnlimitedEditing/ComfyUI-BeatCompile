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

try:
    from .roles import (parse_manifest, classify_role, label_beats, section_list, assign_clips,
                        apply_inserts)
except ImportError:                      # standalone use / tests
    from roles import (parse_manifest, classify_role, label_beats, section_list, assign_clips,
                       apply_inserts)

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
    return {"duration": duration, "bpm": bpm, "beats": beats, "energy": rank.tolist(), "drop": drop,
            "labels": label_beats(rank.tolist(), duration, beats)}


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


# ── still images: Ken Burns move picked from the prompt's camera words ───────
def still_filter(c, use_n, nf, w, h, fps):
    """zoompan chain for one still. Direction flips on every reuse so repeats do not look identical."""
    a = round(0.10 + 0.25 * c["intensity"], 3)          # move amount grows with clip intensity
    p = use_n % 2
    base = (f"scale={2 * w}:{2 * h}:force_original_aspect_ratio=increase,"
            f"crop={2 * w}:{2 * h},setsar=1")
    center = "x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2'"
    m = c["motion"]
    if m in ("zoom", "fly", "whip"):
        a = a * (1.6 if m == "whip" else 1.0)
        z = f"z='1+{a}*on/{nf}'" if p == 0 else f"z='{1 + a}-{a}*on/{nf}'"
        zp = f"{z}:{center}"
    elif m in ("pan", "handheld", "orbit"):
        direction = "on" if p == 0 else f"({nf}-on)"
        zp = f"z='1.18':x='{direction}/{nf}*(iw-iw/zoom)':y='(ih-ih/zoom)/2'"
    else:                                               # static / unknown: slow drift
        zp = f"z='1+0.05*on/{nf}':{center}" if p == 0 else f"z='1.05-0.05*on/{nf}':{center}"
    return f"{base},zoompan={zp}:d={nf}:s={w}x{h}:fps={fps}"


def seg_fx(c, s, nf, w, h, fps):
    """Extra filters appended after the base scale/crop: punch-in pulse on the kick, white flash on drops."""
    out = ""
    if "punch" in c["flags"] and nf >= 3:
        p = max(2, round(0.18 * fps))
        z = f"(1+0.12*max(0\\,1-n/{p}))"
        out += f",scale=w='trunc({w}*{z}/2)*2':h='trunc({h}*{z}/2)*2':eval=frame,crop={w}:{h}"
    if s.get("wflash"):
        out += f",fade=t=in:st=0:d={2.0 / fps:.4f}:color=white"
    return out


def make_pingpong(path, tmp, idx):
    """forward + reversed copy so a short clip loops without a jump cut."""
    out = os.path.join(tmp, f"pp_{idx}.mp4")
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", path, "-filter_complex",
              "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[o]", "-map", "[o]", "-an",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", out])
    return out if r.returncode == 0 and os.path.isfile(out) else None

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
        if c.get("kind") == "image":
            vf = still_filter(c, s.get("use_n", 0), nf, width, height, fps) + seg_fx(c, s, nf, width, height, fps)
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", c["path"], "-an", "-vf", vf,
                   "-frames:v", str(nf), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-pix_fmt", "yuv420p", "-r", str(fps), seg_path]
            jobs.append((k, cmd, seg_path))
            continue
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},fps={fps},setsar=1,"
              f"tpad=stop_mode=clone:stop_duration={need:.3f}" + seg_fx(c, s, nf, width, height, fps))
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
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def compile_montage(audio_file, manifest, video_files, input_dir, out_path, *, height=720, aspect=0,
                    fps=24, max_frames=2880, cut_density=1.0, seed=0, overedit=0.0, use_all=False,
                    image_files=(), log=print):
    tmp = tempfile.mkdtemp(prefix="beatcompile_")
    entries, extras, all_flags = parse_manifest(manifest)
    blank = {"role": None, "flags": set(), "with": [], "then": [], "not": [],
             "after": [], "before": [], "in": [], "prompt": ""}
    clips = []

    def add(cid, path, kind, e):
        clips.append({"id": cid, "path": path, "kind": kind, "entry": e, "prompt": e.get("prompt", "")})

    for slot, name in enumerate(video_files, 1):
        p = resolve_file(name, input_dir, tmp)
        if p:
            add(f"video{slot}", p, "video", entries.get(f"video{slot}", blank))
    for slot, name in enumerate(image_files, 1):
        p = resolve_file(name, input_dir, tmp)
        if p:
            add(f"image{slot}", p, "image", entries.get(f"image{slot}", blank))
    for n, e in enumerate(extras[:24], 1):
        try:
            path = download(e["url"], tmp)
            ext = os.path.splitext(e["url"].split("?")[0])[-1].lower()
            add(f"extra{n}", path, "image" if ext in IMAGE_EXT else "video", e)
        except Exception as ex:                                 # one bad URL should not kill the job
            log(f"[BeatCompile] extra clip {e['url']} skipped: {ex}")
    if not clips:
        raise RuntimeError("no input clips resolved (video1-8 / image1-8 empty and no URLs in the manifest)")
    for c in clips:
        c["duration"] = 1e6 if c["kind"] == "image" else probe_duration(c["path"])   # stills never run out
        c["motion"], c["intensity"] = tag_prompt(c["prompt"])
        e = c["entry"]
        c["role"] = e.get("role") or classify_role(c["prompt"], c["motion"], c["intensity"])
        c["flags"] = set(e.get("flags", ())) | set(all_flags)
        c["rel"] = {k: list(e.get(k, [])) for k in ("with", "then", "not", "after", "before", "in")}
    clips = [c for c in clips if c["duration"] > 0.2]
    for k, c in enumerate(clips):               # short clips loop forward+back instead of jump-cutting
        if c["kind"] == "video" and ("pingpong" in c["flags"] or
                                     (c["duration"] < 3.0 and "jump" not in c["flags"])):
            pp = make_pingpong(c["path"], tmp, k)
            if pp:
                c["path"], c["duration"], c["pingpong"] = pp, c["duration"] * 2, True
    audio = resolve_file(audio_file, input_dir, tmp)
    if not audio:
        raise RuntimeError(f"audio file not found: {audio_file!r}")
    max_seconds = max(5.0, max_frames / float(fps))
    an = analyze_audio(audio, max_seconds, tmp)
    segs, over_used = plan_with_coverage(an, cut_density, float(overedit), int(fps), len(clips), bool(use_all))
    labels = an["labels"]
    for s in segs:
        s["sec"] = labels[min(s["i"], len(labels) - 1)]
    if "dropflash" in all_flags or any("dropflash" in c["flags"] for c in clips):
        prev = None
        for s in segs:
            if s["sec"] == "drop" and prev != "drop":
                s["wflash"] = True
            prev = s["sec"]
    segs = assign_clips(segs, clips, seed, bool(use_all))
    segs = apply_inserts(segs, clips, int(fps))
    used = {s["clip"] for s in segs}
    if len(used) < len(clips):
        log(f"[BeatCompile] only {len(used)}/{len(clips)} clips fit this song's length")
    ar = {0: 16 / 9, 1: 9 / 16, 2: 1.0, 3: 4 / 3}.get(int(aspect), 16 / 9)
    h = int(height) // 2 * 2
    w = int(round(h * ar)) // 2 * 2
    log(f"[BeatCompile] {len(clips)} clips, {an['bpm']:.1f} bpm, {len(segs)} segments, {w}x{h}@{fps}")
    render(segs, {c["id"]: c for c in clips}, audio, out_path, w, h, int(fps), tmp, an["duration"])
    report = {"overedit_used": round(over_used, 2), "clips_used": len(used),
              "bpm": round(an["bpm"], 2), "duration": round(an["duration"], 2), "size": f"{w}x{h}",
              "sections": section_list(segs),
              "clips": [{"id": c["id"], "kind": c["kind"], "role": c["role"], "flags": sorted(c["flags"]),
                         "motion": c["motion"], "intensity": c["intensity"], "prompt": c["prompt"],
                         "pingpong": bool(c.get("pingpong"))} for c in clips],
              "segments": [{"t0": round(s["t0"], 2), "t1": round(s["t1"], 2), "sec": s["sec"],
                            "energy": round(s["energy"], 2), "drop": s["drop"], "clip": s["clip"],
                            "src_start": s["src_start"], "insert": bool(s.get("insert")),
                            "wflash": bool(s.get("wflash"))} for s in segs]}
    return out_path, report
