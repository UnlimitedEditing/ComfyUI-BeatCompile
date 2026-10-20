# ComfyUI-BeatCompile

One node, `BeatCompile`: cuts video clips and still images to the beat and energy of a song.
Camera motion and energy of each clip are inferred from the clip's own generation prompt
(keyword rules), so no vision-language model is needed.

Needs `ffmpeg`/`ffprobe` on PATH and `librosa`. Logic lives in `core.py` (analysis, planning,
render) and `roles.py` (entry grammar, section typing, role placement); neither imports ComfyUI.

## Entry lines (the `manifest` text)

    1 action: whip pan down a street  2 hold flash: empty hallway  3 build with 4: slow push in
    4 build: creeping camera  i1 atmos in intro: foggy harbour  all: punch dropflash

- `N` = video N, `iN` = still N, a URL = extra clip, `all` = defaults for every clip.
- Roles: action, build, hold, atmos, hero, accent, trans, open, close, filler (+ synonyms).
- Flags: flash, punch, pingpong, jump, once, dropflash.
- Relations: with N, then N, not N, after S, before S, in S (S = intro build drop release break outro).
- Text after the colon describes the clip; with no role given it is auto-classified from it.
