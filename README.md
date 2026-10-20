<p align="center">
  <img src="assets/splash.jpg" alt="Beat Compile" width="420">
</p>

<h1 align="center">Beat Compile</h1>

<p align="center">
  Cut your own clips and stills to the beat of a song.<br>
  A <a href="https://github.com/comfyanonymous/ComfyUI">ComfyUI</a> node, built for <a href="https://graydient.ai">Graydient</a> workflows.
</p>

---

## What it does

Give it a song and a pile of clips. It finds the beat, works out the song's structure
(intro, build, drop, release, outro), and cuts the clips together so that:

- cuts land **on the beat**, getting faster as the song gets more intense
- **calm clips** sit in calm sections and **action clips** hit on the drops
- every clip can be given a **role** (*action*, *build*, *hold*, *hero*, ...) and rules like
  *"play this right after that one"* or *"only in the intro"*
- still images get a slow push-in, pull-out or pan
- the result is one finished mp4 with the song on it

It does **not** use a vision model. It reads the **prompt each clip was made from** ("slow dolly in on a
lighthouse", "whip pan down a street") to guess how much energy and what camera move each clip has. That is
free, instant, and usually right, because you wrote those prompts.

---

## Quick start

Reply to an audio file in Telegram with:

```
/workflow /run:beat-compile /video1:RaB3cD1 /video2:Rx9mWV1 /video3:RN6LGnk
1 action: whip pan down a street  2 hold: empty hallway  3 build: slow push in
```

That's all you need. The song is the audio file you replied to, the clips are the render hashes, and the line
under them describes the clips.

> **Render hashes** are written `R` + the 6-character hash. A render called `2pMZKl` is `R2pMZKl`.
> The leading `R` is required.

---

## Inputs

### The song

| How | When to use it |
|---|---|
| **Reply to an audio file** in Telegram | Always works. The best choice for songs from music workflows. |
| `/audio1:R<hash>` | Audio renders that have a **single output**. |
| A song link through the API (`placeholders`) | Scripts. See [Using the API](#using-the-api). |

> **Song renders with several outputs** (for example a spectrogram image *plus* the mp3) can't be used with
> `/audio1:R<hash>`. The platform looks at the first output, finds no audio, and answers *"unknown"*.
> Reply to the audio file instead.

### The clips

| Field | What it takes | Limit |
|---|---|---|
| `/video1` ... `/video8` | `R<hash>` of a video render | 8 |
| `/image1` ... `/image8` | `R<hash>` of an image render (becomes a still) | 8 |
| `https://... : description` in the entry line | Extra clips or stills by link | 24 |

Videos and stills can be mixed freely. A clip shorter than about 3 seconds automatically plays forward and then
backward (ping-pong) so it can repeat without a visible jump.

---

## Writing the entry line

The entry line is plain text under the command. Each clip gets a **number**, optional **terms**, a **colon**,
and a **description**:

```
1 action: whip pan down a street  2 hold flash: empty hallway  3 build with 4: slow push in
```

Taking one entry apart:

```
  1   action   :   whip pan down a street
  |     |      |            |
  |     |      |            '-- what the clip shows (used to read its camera move and energy)
  |     |      '--------------- the colon ends the terms
  |     '---------------------- terms: a role, flags, rules (all optional, any order)
  '---------------------------- clip number
```

**Numbering**

| Write | Means |
|---|---|
| `1`, `2`, `3` ... | the matching `/video` slot |
| `i1`, `i2` ... | the matching `/image` slot (a still) |
| `https://...mp4` or `.jpg` | an extra clip or still, by link |
| `all` | defaults for every clip, e.g. `all: punch dropflash` |

Numbers must run **in order** (`1`, `2`, `3`), so a description can contain something like "at 5: 30" without
being read as a new clip. To skip a number, write `3: -`.

If you write no terms at all, the clip's role is guessed from its description.

### Roles

A role says **where in the song a clip belongs**. Pick one per clip.

| Role | Also accepted | Where it lands |
|---|---|---|
| `action` | `fast`, `hype` | Drops and fast sections. Short, hard cuts. |
| `build` | `builder`, `rise`, `tension` | The run-up to a drop. Cuts get shorter as it climbs. |
| `hold` | `holder`, `calm`, `aftermath` | Right after a drop, and in breakdowns. Slow and settled. |
| `atmos` | `mood`, `ambient` | Intros, bridges and outros. Long, scene-setting cuts. |
| `hero` | `payoff`, `peak` | **Once**, at the biggest drop. |
| `accent` | `stinger`, `hit` | Never a normal cut. Quick 3-frame stingers punctuating each drop. |
| `trans` | `transition` | Just before a drop and at section changes. A whip or smear. |
| `open` | `opener` | **Always the first clip.** |
| `close` | `closer` | **Always the last clip.** |
| `filler` | `bg` | The fallback, used when nothing else fits. |

### Flags

Flags change **how** a clip is treated.

| Flag | Effect |
|---|---|
| `flash` | The clip cuts in for about 5 frames over the **action** parts, then the action carries on. A calm `hold` clip with `flash` makes a nice tension flicker. |
| `punch` | A quick zoom pulse on every cut of this clip, like a kick drum. |
| `pingpong` | Play forward then backward when the clip repeats. (Automatic for clips under 3 seconds.) |
| `jump` | Turn the automatic ping-pong **off** for this clip (plain jump-loop). |
| `once` | Use this clip a single time. |
| `dropflash` | A white flash on the first frames of each drop. Usually written as `all: dropflash`. |

### Rules between clips

Rules refer to other clips by number (`with 4`, or `with i2` for a still).

| Rule | Meaning | Example |
|---|---|---|
| `with N` | Keep these two near each other. | `3 build with 4:` |
| `then N` | Play clip N **right after** this one. | `2 action then 3:` |
| `not N` | Never put these two next to each other. | `5 hold not 1:` |
| `after S` | Prefer the part of the song after section `S`. | `4 hold after drop:` |
| `before S` | Prefer the part of the song before section `S`. | `2 trans before drop:` |
| `in S` | Prefer section `S`. | `i1 atmos in intro:` |

`S` is one of the section names below. Plural forms (`drops`, `builds`) work too.

### What the song's sections are called

The song is split by its energy curve. These are the names you can use in `after`, `before` and `in`.

| Section | What it is |
|---|---|
| `intro` | The calm lead-in before anything builds. |
| `build` | Rising energy, or the run-up to a drop. |
| `drop` | The high-energy sections. |
| `release` | A few beats of comedown right after a real drop. |
| `break` | A calm middle passage. |
| `outro` | The tail of the song. |

### Words that shape the camera and energy

Each clip's description is scanned for camera and energy words. They set how intense the clip is judged to be,
and (for stills) which move it gets.

| Kind | Example words | Effect |
|---|---|---|
| Whip / crash | `whip pan`, `crash zoom`, `shaky`, `fpv` | Highest energy. Stills get a fast push. |
| Zoom / push | `zoom in`, `dolly in`, `push in`, `pull back` | High energy. Stills push in or pull out. |
| Orbit / follow | `orbit`, `tracking shot`, `steadicam` | Medium-high. Stills slide sideways. |
| Fly | `drone`, `aerial`, `flyover` | Medium. |
| Pan / tilt | `pan`, `tilt`, `slide`, `dolly` | Medium. Stills slide sideways. |
| Static | `static`, `locked off`, `slow`, `gentle`, `calm` | Lowest. Stills drift slowly. |
| High energy | `dancing`, `running`, `explosion`, `chaos`, `storm` | Raises the energy. |
| Low energy | `quiet`, `empty`, `fog`, `sunset`, `sleeping` | Lowers the energy. |
| Tension | `ominous`, `suspense`, `looming`, `creeping` | Guessed as `build` when no role is given. |

> **Avoid** `[` `]` `<` `>` `/` and `|` in descriptions. Graydient treats them as its own syntax.

---

## Examples

The hashes below are placeholders. Use your own render hashes.

### 1. The simplest thing that works

Reply to a song with:

```
/workflow /run:beat-compile /video1:RaB3cD1 /video2:Rx9mWV1 /video3:RN6LGnk
```

No entry line, so the clips are cut on the beat with no role logic. Add an entry line when you want control.

### 2. Roles for a trailer-style edit

```
/workflow /run:beat-compile /video1:RaB3cD1 /video2:Rx9mWV1 /video3:RN6LGnk /video4:RlwObzq
1 open: slow aerial over a quiet harbour
2 build: slow push in on a figure, ominous
3 action: whip pan, explosion, chaos
4 hold flash: empty street at dawn
```

Expect: the harbour first, the push-in building into the drop, the explosion clip hitting on the drops, and the
empty street breathing in right after, flickering in over the action.

### 3. Mixing in stills

```
/workflow /run:beat-compile /video1:RaB3cD1 /video2:Rx9mWV1 /image1:RjjAG2A /image2:Rx9mgR1
1 action: fast chase  2 action: crash zoom  i1 atmos in intro: foggy harbour  i2 hero: the villain's face
```

Expect: the foggy still drifting through the intro, the villain's face landing once on the biggest drop, and the
two action clips carrying the rest.

### 4. A frenetic "everything in" edit

```
/workflow /run:beat-compile /slot7:0.8 /slot8:1 /video1:RaB3cD1 /video2:Rx9mWV1 /video3:RN6LGnk
1 action: shaky handheld  2 action punch: whip pan  3 hold flash: empty hallway  all: dropflash
```

`/slot7:0.8` turns on rhythmic over-editing and `/slot8:1` guarantees every clip appears. See
[Controls](#controls). Options go **before** the media fields.

### 5. Telling clips to stick together

```
1 build with 2: slow push in  2 build: creeping closer  3 action then 4: crash zoom  4 action: spinning camera
5 hold not 3: calm lake
```

Clips 1 and 2 stay close, 3 is always followed by 4, and the lake never sits next to clip 3.

---

## Controls

Options go in the command **before** the media fields (`/video1...`). A `/slotN:` value reads up to the next
`/`, so any text typed after it without a `/` in between gets swallowed.

| Option | What it does | Default | Range |
|---|---|---|---|
| `/slot4:` | Output height in pixels | `720` | 360 to 1080 |
| `/slot5:` | Cut density. `2` = twice as many cuts, `0.5` = half | `1` | 0.25 to 4 |
| `/slot6:` | Aspect: `0` 16:9, `1` 9:16, `2` square, `3` 4:3 | `0` | 0 to 3 |
| `/slot7:` | Over-edit. Splits cuts into rhythms. `0` off, `1` frenetic | `0` | 0 to 1 |
| `/slot8:` | `1` = make sure **every clip appears** | `0` | 0 or 1 |
| `/fps:` | Output frame rate | `24` | 12 to 60 |
| `/length:` | Maximum frames. The song is trimmed to this | `2880` (2 min at 24 fps) | 48 to 7200 |
| `/seed:` | Shuffle seed. Change it for a different arrangement | `0` | any |

**Over-edit** adds a measurable rhythm to the cuts: eighth notes, a 3+3+2 tresillo, sixteenth-note stutter, and
an accelerating ramp into drops. No over-edited cut is shorter than about 4 frames. With `/slot8:1` the level
rises automatically until every clip has room to appear.

---

## How it works

1. **Analyse the song:** beats, tempo, and per-beat energy, then label the sections (intro, build, drop, ...).
2. **Read the clips:** keywords in each description give a camera move and an energy score. A role is taken
   from your terms, or guessed.
3. **Plan the cuts:** every cut is placed on a beat. Cuts get shorter as energy rises, and drops cut on every
   beat.
4. **Place the clips:** each segment picks the clip whose role and energy fit best, honouring `once`, `then`,
   `with`, `not`, the hero, the opener and the closer.
5. **Add the extras:** flash-overs, accent stingers, punch-ins, drop flashes, ping-pong loops, and the still-image
   camera moves.
6. **Render:** the segments are encoded in parallel with ffmpeg, joined, and the song is added.

It also returns a **plan** (every cut, the sections found, and each clip's role and energy) as a small data image,
so you can see exactly what it decided.

---

## Tips

- **Write real descriptions.** The prompt a clip was made from is the best input. Edit-style prompts like
  "replace the subject with..." say nothing about motion, so add your own words.
- **Give the song contrast.** A track that stays at one energy level gives long, even cuts. Tracks with a clear
  build and drop show the roles off best.
- **A few clips repeated is fine.** With `/slot8:1` and over-edit, a handful of clips can fill a whole song.
- **Short clips loop smoothly.** Under 3 seconds, they ping-pong automatically.
- **Use `hero` sparingly.** It lands once, so make it your best shot.
- **Try another `/seed:`** if the arrangement is nearly right.

---

## Troubleshooting

| What you see | Why | Fix |
|---|---|---|
| `unknown /audio1:R...` | The song render has several outputs and the first isn't audio | **Reply to the audio file** instead |
| `unknown /video1:...` | Missing the leading `R`, or not a video render | Write `R` + the 6-character hash |
| Everything is cut the same way | No entry line, so every clip is "filler" | Add `1 action: ...` style lines |
| Entry line ignored or text vanishes | A `/slotN:` value swallowed it | Put options before the media fields |
| A clip never appears | The song is too short or calm for that many cuts | Add `/slot8:1`, or raise `/slot7:` |
| Black stills | The source image itself is black | Check the original render |
| Submit returns HTTP 500 | Usually another render of yours is still queued or running | Wait for it to finish, then resend |

---

## Using the API

`tools/beat_compile_submit.py` (in the Graydient workflow repo) turns render hashes into a finished request. It
picks the audio and video out of each render, so even multi-output song renders work:

```bash
python tools/beat_compile_submit.py --song w9MjqZ \
  --clips "Y826pz action" "LPZkOx hold flash" "dOky3n build with 4" \
  --stills "Wb819y atmos in intro" --all "punch dropflash" \
  --overedit 0.6 --use-all --seed 7 --wait --out out/
```

---

## Install

**On Graydient:** the `beat-compile` workflow pulls this repo for you.

**In ComfyUI:** clone into `ComfyUI/custom_nodes/` and install the requirements.

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/UnlimitedEditing/ComfyUI-BeatCompile
pip install -r ComfyUI-BeatCompile/requirements.txt
```

You also need `ffmpeg` and `ffprobe` on your `PATH`.

### Repo layout

| File | What's in it |
|---|---|
| `nodes.py` | The ComfyUI node, `BeatCompile` |
| `core.py` | Song analysis, cut planning, rendering |
| `roles.py` | The entry-line grammar, section typing and role placement |

`core.py` and `roles.py` do not import ComfyUI, so they can be used and tested on their own.
