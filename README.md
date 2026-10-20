# ComfyUI-BeatCompile

One node, `BeatCompile`: cuts up to 8 video clips (plus URL extras listed in the manifest) to the
beat and energy of a song. Camera motion and energy of each clip are inferred from the clip's
original generation prompt (keyword rules), so no vision-language model is needed.

Needs `ffmpeg`/`ffprobe` on PATH and `librosa`. Core logic lives in `core.py` (no ComfyUI imports).
