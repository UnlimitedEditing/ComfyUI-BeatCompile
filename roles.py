"""Clip roles, the natural-language entry grammar, section typing and role-aware placement.

Entry line (phone-friendly; survives the API collapsing newlines, avoids [ ] < > / | which are
Graydient syntax):

    1 action chase: whip pan down a street  2 hold flash: empty hallway  3 build with 4: slow push in
    4 build: creeping camera  i1 atmos: foggy harbour  https://x.com/a.mp4 accent: explosion  all: punch

  N / iN / URL / all     which clip (N = video N, iN = still N, all = defaults for every clip)
  role words             action build hold atmos hero accent trans open close filler (+ synonyms)
  flags                  flash punch pingpong jump once dropflash
  relations              with N | then N | not N        (stay near / play right before / keep apart)
                         after S | before S | in S      (S = intro build drop release break outro)
  after the colon        the clip's own description (used to auto-classify when no role is given)
"""
import random
import re

ROLE_ALIASES = {
    "action": "action", "fast": "action", "hype": "action",
    "build": "build", "builder": "build", "rise": "build", "tension": "build",
    "hold": "hold", "holder": "hold", "calm": "hold", "aftermath": "hold",
    "atmos": "atmos", "mood": "atmos", "ambient": "atmos",
    "hero": "hero", "payoff": "hero", "peak": "hero",
    "accent": "accent", "stinger": "accent", "hit": "accent",
    "trans": "transition", "transition": "transition",
    "open": "opener", "opener": "opener", "close": "closer", "closer": "closer",
    "filler": "filler", "bg": "filler",
}
FLAG_WORDS = {"flash", "punch", "pingpong", "jump", "once", "dropflash"}
SECTIONS = ("intro", "build", "drop", "release", "break", "outro")
SEC_WORDS = {s: s for s in SECTIONS}
SEC_WORDS.update({"drops": "drop", "builds": "build", "intros": "intro", "outros": "outro",
                  "breaks": "break", "releases": "release"})

_simple = sorted(list(ROLE_ALIASES) + list(FLAG_WORDS), key=len, reverse=True)
_secs = "|".join(sorted(SEC_WORDS, key=len, reverse=True))
TAGRX = (r"(?:" + "|".join(_simple) + r"|(?:with|then|not)\s+[vi]?\d{1,2}|(?:after|before|in)\s+(?:"
         + _secs + r"))")
# Between the clip number and the colon: known tag words, or any other short word (ignored, so a
# free-form label like "chase" cannot break the line). The consecutive-number rule in parse_manifest
# keeps prompt text such as "at 5: 30" from being mistaken for a new entry.
MARK = re.compile(r"(?:(?<=\s)|^)(all|[vi]?\d{1,2}|https?://\S+?)((?:\s+(?:" + TAGRX + r"|[a-z][a-z'-]{0,15}))*)\s*:\s", re.I)
TAGTOK = re.compile(TAGRX, re.I)


def _ref(tok):
    tok = tok.lower()
    return ("image" if tok.startswith("i") else "video") + re.sub(r"\D", "", tok)


def parse_tags(tagstr):
    e = {"role": None, "flags": set(), "with": [], "then": [], "not": [],
         "after": [], "before": [], "in": []}
    for t in TAGTOK.findall(tagstr or ""):
        w = t.lower().split()
        if len(w) == 1:
            if w[0] in ROLE_ALIASES:
                e["role"] = ROLE_ALIASES[w[0]]
            else:
                e["flags"].add(w[0])
        elif w[0] in ("with", "then", "not"):
            e[w[0]].append(_ref(w[1]))
        else:
            e[w[0]].append(SEC_WORDS[w[1]])
    return e


def parse_manifest(text):
    """-> (entries {"video1": entry, "image2": entry}, extras [entry with "url"], all_flags set)."""
    text = (text or "").replace("\\<", "<").replace("\\>", ">").strip()
    text = re.sub(r"(https?://\S+)\s*\|\s*", r"\1: ", text)            # legacy "url | prompt"
    entries, extras, all_flags = {}, [], set()
    accepted, last = [], {"v": 0, "i": 0}
    seen_all = False
    for m in MARK.finditer(text):
        tok = m.group(1)
        low = tok.lower()
        if low == "all":
            if seen_all:
                continue
            seen_all = True
        elif low.startswith("http"):
            pass
        else:
            kind = "i" if low.startswith("i") else "v"
            n = int(re.sub(r"\D", "", low))
            if not ((last[kind] == 0 and n <= 8) or n == last[kind] + 1):
                continue
            last[kind] = n
        accepted.append(m)
    for k, m in enumerate(accepted):
        end = accepted[k + 1].start() if k + 1 < len(accepted) else len(text)
        e = parse_tags(m.group(2))
        e["prompt"] = text[m.end():end].strip()
        low = m.group(1).lower()
        if low == "all":              # "all punch:" or the natural "all: punch dropflash"
            all_flags |= parse_tags(m.group(2) + " " + e["prompt"])["flags"]
        elif low.startswith("http"):
            e["url"] = m.group(1)
            extras.append(e)
        else:
            entries[_ref(low)] = e
    if not accepted and text:                      # no markers at all: plain lines = video prompts
        for n, line in enumerate([l.strip() for l in text.splitlines() if l.strip()], 1):
            entries[f"video{n}"] = {**parse_tags(""), "prompt": line}
    return entries, extras, all_flags


# ── role classification (when no role word was given) ───────────────────────
_BUILD = r"ominous|tension|suspense|eerie|creep|rising|approach|stalk|slow push|slow dolly in|looming|anticipat"
_HOLD = r"aftermath|empty|silence|quiet|linger|still life|lonely|abandoned|motionless|resting|stare"


def classify_role(prompt, motion, intensity):
    p = (prompt or "").lower()
    if re.search(_BUILD, p):
        return "build"
    if re.search(_HOLD, p):
        return "hold"
    if intensity >= 0.70:
        return "action"
    if motion == "zoom" and intensity < 0.70:
        return "build"
    if intensity <= 0.25:
        return "hold"
    if motion in ("fly", "pan", "orbit"):
        return "atmos"
    return "filler"


# ── section typing from the per-beat energy rank ────────────────────────────
def label_beats(energy, duration, beats):
    n = len(energy)
    e = list(energy)

    def mean(a):
        return sum(a) / len(a) if a else 0.0

    drop = [x >= 0.7 for x in e]
    for _ in range(2):                                       # close gaps of up to 3 beats inside a drop
        i = 0
        while i < n:
            if not drop[i]:
                j = i
                while j < n and not drop[j]:
                    j += 1
                if 0 < i and j < n and (j - i) <= 3:
                    for k in range(i, j):
                        drop[k] = True
                i = j
            else:
                i += 1
    for i in range(n):                                       # remove isolated 1-beat spikes
        if drop[i] and not ((i > 0 and drop[i - 1]) or (i + 1 < n and drop[i + 1])):
            drop[i] = False
    runs, i = [], 0                                          # (start, end_exclusive) of each drop run
    while i < n:
        if drop[i]:
            j = i
            while j < n and drop[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    lab = ["break"] * n
    for i in range(n):                                       # build: energy climbing toward something
        if 0.2 <= e[i] < 0.7 and mean(e[i + 1:i + 9]) - mean(e[max(0, i - 7):i + 1]) > 0.06:
            lab[i] = "build"
    for a, b in runs:                                        # ...or simply the run-up (<=32 beats) to a drop
        for i in range(max(0, a - 32), a):
            if lab[i] == "break" and e[i] >= 0.25:
                lab[i] = "build"
    for a, b in runs:                                        # release: up to 8 beats after a real (>=8 beat) drop
        if b - a >= 8:
            k = b
            while k < min(n, b + 8) and not drop[k] and e[k] < 0.6:
                lab[k] = "release"
                k += 1
    first = min([a for a, _ in runs] + [i for i in range(n) if lab[i] == "build"] + [n])
    if first >= 4:                                           # intro: the calm lead-in
        for i in range(first):
            lab[i] = "intro"
    for i in range(int(n * 0.85), n):                        # outro: calm tail
        if not drop[i]:
            lab[i] = "outro"
    for i in range(n):
        if drop[i]:
            lab[i] = "drop"
    return _smooth(lab)


def _smooth(lab):
    """Absorb short label runs (flicker) into the previous run; drops are never absorbed."""
    min_run = {"build": 4, "break": 4, "intro": 4, "outro": 4, "release": 2}
    for _ in range(len(lab) + 1):
        runs, i = [], 0
        while i < len(lab):
            j = i
            while j < len(lab) and lab[j] == lab[i]:
                j += 1
            runs.append((i, j, lab[i]))
            i = j
        changed = False
        for k, (a, b, l) in enumerate(runs):
            if l != "drop" and b - a < min_run.get(l, 4) and len(runs) > 1:
                tgt = runs[k - 1][2] if k > 0 else runs[k + 1][2]
                for x in range(a, b):
                    lab[x] = tgt
                changed = True
                break
        if not changed:
            break
    return lab


def section_list(segs):
    out = []
    for s in segs:
        if out and out[-1]["name"] == s["sec"]:
            out[-1]["t1"] = round(s["t1"], 2)
        else:
            out.append({"name": s["sec"], "t0": round(s["t0"], 2), "t1": round(s["t1"], 2)})
    return out


# ── role x section affinity (added to the energy-match score) ───────────────
AFF = {
    #            intro build drop  release break outro
    "action":     (-0.4, 0.1, 0.6, -0.3, -0.5, -0.2),
    "build":      (0.2, 0.7, -0.4, -0.1, 0.3, -0.2),
    "hold":       (0.3, -0.2, -0.5, 0.7, 0.6, 0.4),
    "atmos":      (0.6, 0.0, -0.4, 0.2, 0.5, 0.5),
    "hero":       (-0.5, -0.3, 0.3, -0.5, -0.5, -0.5),
    "accent":     (-2, -2, -2, -2, -2, -2),
    "transition": (-0.5, -0.2, -0.5, -0.2, -0.3, -0.5),
    "opener":     (0, 0, 0, 0, 0, 0),
    "closer":     (0, 0, 0, 0, 0, 0),
    "filler":     (-0.1, -0.1, -0.1, -0.1, -0.1, -0.1),
}


def _aff(role, sec):
    return 1.5 * AFF.get(role, AFF["filler"])[SECTIONS.index(sec)]


def assign_clips(segs, clips, seed, use_all=False):
    """Role-aware placement. Each clip dict needs: id, role, flags, rel (with/then/not/after/before/in),
    intensity, motion, duration. Fills seg["clip"], "src_start", "use_n"."""
    rng = random.Random(seed)
    by_id = {c["id"]: c for c in clips}
    uses = {c["id"]: 0 for c in clips}
    cursor = {c["id"]: 0.0 for c in clips}
    n = len(segs)
    # groups: union of "with" references (symmetric)
    group = {c["id"]: {c["id"]} for c in clips}
    for c in clips:
        for o in c["rel"]["with"]:
            if o in group:
                u = group[c["id"]] | group[o]
                for x in u:
                    group[x] = u
    avoid = {c["id"]: set(c["rel"]["not"]) for c in clips}
    for c in clips:
        for o in c["rel"]["not"]:
            if o in avoid:
                avoid[o].add(c["id"])
    heroes = [c for c in clips if c["role"] == "hero"]
    hero_idx = None
    drops = [i for i, s in enumerate(segs) if s["sec"] == "drop"]
    if drops:
        hero_idx = max(drops, key=lambda i: (segs[i]["energy"], -i))
    placeable = [c for c in clips if c["role"] != "accent"] or clips

    def score(c, idx, prev, recent, forced):
        s = segs[idx]
        sec = s["sec"]
        nxt = segs[idx + 1]["sec"] if idx + 1 < n else None
        prv = segs[idx - 1]["sec"] if idx > 0 else None
        target = min(1.0, s["energy"] + (0.12 if s["drop"] else 0.0))
        sc = -abs(c["intensity"] - target) + _aff(c["role"], sec)
        r = c["rel"]
        if sec in r["in"]:
            sc += 1.0
        if any(prv == a and sec != a for a in r["after"]) or ("drop" in r["after"] and sec == "release"):
            sc += 1.0
        if any(nxt == b and sec != b for b in r["before"]):
            sc += 1.0
        if c["role"] == "opener":
            sc += 3.0 if idx == 0 else -2.0
        if c["role"] == "closer":
            sc += 3.0 if idx == n - 1 else -2.0
        if c["role"] == "hero":
            sc += 3.0 if idx == hero_idx else -1.5
        if c["role"] == "transition":
            if nxt == "drop" and sec != "drop":
                sc += 1.5
            elif nxt is not None and nxt != sec:
                sc += 0.4
            else:
                sc -= 1.0
        if prev is not None:
            if c["id"] in group.get(prev, ()):
                sc += 0.5
            if c["id"] in avoid.get(prev, ()):
                sc -= 1.2
        if any(c["id"] in group.get(p, ()) for p in recent[-3:]):
            sc += 0.25
        if len(clips) > 1 and c["id"] == prev:
            sc -= 1.0
        share = uses[c["id"]] * len(placeable) / (idx + 1.0)       # 1.0 = exactly its fair share so far
        sc -= max(-0.2, min(1.0, 0.6 * (share - 1.0)))
        if ("once" in c["flags"] or c["role"] == "hero") and uses[c["id"]] >= 1 and not forced:
            sc -= 5.0
        if c["duration"] - cursor[c["id"]] < (s["t1"] - s["t0"]):
            sc -= 0.25
        sc += rng.uniform(-0.05, 0.05)
        return sc

    reserved = {}                       # opener = first segment, closer = last segment
    opener = next((c for c in clips if c["role"] == "opener"), None)
    closer = next((c for c in clips if c["role"] == "closer"), None)
    if opener and n:
        reserved[0] = opener["id"]
    if closer and n > 1:
        reserved[n - 1] = closer["id"]
    forced_cov = {}
    if use_all:   # every clip gets the segment where its role + energy fit best (hardest-to-place first)
        free = set(range(n)) - set(reserved)
        special = {"hero": 0, "opener": 1, "closer": 2}
        order = sorted((c for c in clips if c["role"] not in ("accent", "opener", "closer")),
                       key=lambda c: (special.get(c["role"], 3), -abs(c["intensity"] - 0.5)))
        for c in order:
            if not free:
                break
            best = max(free, key=lambda i: _aff(c["role"], segs[i]["sec"]) -
                       abs(segs[i]["energy"] - c["intensity"]) -
                       (0.5 if (i - 1 in forced_cov and forced_cov[i - 1] == c["id"]) or
                        (i + 1 in forced_cov and forced_cov[i + 1] == c["id"]) else 0.0) +
                       (3.0 if c["role"] == "hero" and i == hero_idx else 0.0) +
                       (3.0 if c["role"] == "opener" and i == 0 else 0.0) +
                       (3.0 if c["role"] == "closer" and i == n - 1 else 0.0))
            forced_cov[best] = c["id"]
            free.discard(best)

    prev, recent, forced_next = None, [], None
    for idx, s in enumerate(segs):
        d = s["t1"] - s["t0"]
        if idx in reserved:
            chosen = by_id[reserved[idx]]
        elif (forced_next and forced_next in by_id and forced_next != prev and
              not ((("once" in by_id[forced_next]["flags"]) or by_id[forced_next]["role"] == "hero")
                   and uses[forced_next] >= 1) and
              not (by_id[forced_next]["role"] == "hero" and idx != hero_idx)):
            chosen = by_id[forced_next]
        elif idx in forced_cov:
            chosen = by_id[forced_cov[idx]]
        else:
            chosen = max(placeable, key=lambda c: score(c, idx, prev, recent, False))
        forced_next = None
        for o in chosen["rel"]["then"]:                    # "then N": N plays right after this clip
            if o in by_id and o != chosen["id"]:
                forced_next = o
                break
        if cursor[chosen["id"]] + d > chosen["duration"] - 0.05:
            cursor[chosen["id"]] = 0.0
        s["clip"], s["src_start"], s["use_n"] = chosen["id"], round(cursor[chosen["id"]], 3), uses[chosen["id"]]
        cursor[chosen["id"]] += d
        uses[chosen["id"]] += 1
        prev = chosen["id"]
        recent.append(prev)
    return segs


# ── flash-over and accent inserts ───────────────────────────────────────────
def _split(seg, t_cut, head_clip, head_cursor):
    """Replace the head of seg (t0..t_cut) with head_clip; the tail keeps the original clip."""
    head = {**seg, "t1": t_cut, "clip": head_clip["id"], "src_start": round(head_cursor, 3), "insert": True}
    tail = {**seg, "t0": t_cut, "src_start": round(seg["src_start"] + (t_cut - seg["t0"]), 3)}
    tail.pop("wflash", None)
    return [head, tail]


def apply_inserts(segs, clips, fps):
    """flash clips cut in over drop segments (every 4th); accent clips punctuate the head of each drop
    and every 8th drop segment. Long segments are split (head/tail piece of ~3-5 frames); short ones
    (frenetic sections) are swapped whole."""
    flashers = [c for c in clips if "flash" in c["flags"] and c["role"] != "accent"]
    accents = [c for c in clips if c["role"] == "accent"]
    if not flashers and not accents:
        return segs
    fl, ac = max(4, round(0.2 * fps)) / fps, max(3, round(0.12 * fps)) / fps
    flash_ids = {x["id"] for x in flashers}
    offs = {c["id"]: 0.0 for c in clips}
    out, drop_n, prev_sec = [], 0, None
    for s in segs:
        d = s["t1"] - s["t0"]
        in_drop = s["sec"] == "drop"
        pieces = [s]
        if in_drop and accents and (prev_sec != "drop" or drop_n % 8 == 0):
            c = accents[drop_n % len(accents)]
            if d >= 2 * ac + 0.15:
                pieces = _split(s, s["t0"] + ac, c, offs[c["id"]])
            elif d <= 0.40:
                pieces = [{**s, "clip": c["id"], "src_start": round(offs[c["id"]], 3), "insert": True}]
            offs[c["id"]] = (offs[c["id"]] + ac) % max(0.1, c["duration"] - ac)
        if in_drop and flashers and drop_n % 4 == 3 and pieces[-1]["clip"] not in flash_ids \
                and not pieces[-1].get("insert"):
            c = flashers[(drop_n // 4) % len(flashers)]
            main = pieces[-1]
            md = main["t1"] - main["t0"]
            if md >= fl + 0.35:
                t_cut = main["t1"] - fl
                tail = {**main, "t0": t_cut, "clip": c["id"], "src_start": round(offs[c["id"]], 3), "insert": True}
                pieces = pieces[:-1] + [{**main, "t1": t_cut}, tail]
            elif md <= 0.40:
                pieces = pieces[:-1] + [{**main, "clip": c["id"], "src_start": round(offs[c["id"]], 3), "insert": True}]
            offs[c["id"]] = (offs[c["id"]] + fl) % max(0.1, c["duration"] - fl)
        out += pieces
        if in_drop:
            drop_n += 1
        prev_sec = s["sec"]
    return out
