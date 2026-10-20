import asyncio
import json
import os
import tempfile

from . import core


def _encode_string_as_image(text):
    """Lossless data image: 4-byte BE length + UTF-8, 3 bytes/pixel (ForgeExpress convention)."""
    import struct
    import numpy as np
    import torch
    raw = text.encode("utf-8")
    payload = struct.pack(">I", len(raw)) + raw
    while len(payload) % 3:
        payload += bytes(1)
    side = max(1, int(np.ceil(np.sqrt(len(payload) // 3))))
    payload += bytes(1) * (side * side * 3 - len(payload))
    arr = np.frombuffer(payload, dtype=np.uint8).reshape(side, side, 3).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


class BeatCompile:
    """Cut up to 8 clips (+ optional URL extras in the manifest) to the beat of a song.

    video1..video8 / audio_file take the local filenames Graydient stages for its numbered
    media fields (or a path / http URL). `manifest` carries the clips' original prompts, one per
    line ("1: slow dolly in on ..."), used to tag camera motion + energy instead of a VL model.
    """

    @classmethod
    def INPUT_TYPES(cls):
        req = {
            "audio_file": ("STRING", {"default": ""}),
            "manifest": ("STRING", {"multiline": True, "default": ""}),
            "height": ("INT", {"default": 720, "min": 240, "max": 1080, "step": 2}),
            "aspect": ("INT", {"default": 0, "min": 0, "max": 3}),   # 0 16:9, 1 9:16, 2 1:1, 3 4:3
            "fps": ("INT", {"default": 24, "min": 12, "max": 60}),
            "max_frames": ("INT", {"default": 2880, "min": 48, "max": 7200}),
            "cut_density": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 4.0, "step": 0.05}),
            "shuffle_seed": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1}),
            "overedit": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05}),
            "use_all": ("INT", {"default": 0, "min": 0, "max": 1}),
        }
        for i in range(1, 9):
            req[f"video{i}"] = ("STRING", {"default": ""})
        for i in range(1, 9):
            req[f"image{i}"] = ("STRING", {"default": ""})
        return {"required": req}

    RETURN_TYPES = ("VIDEO", "STRING", "IMAGE")
    RETURN_NAMES = ("video", "report", "report_image")
    FUNCTION = "run"
    CATEGORY = "BeatCompile"

    async def run(self, audio_file, manifest, height, aspect, fps, max_frames, cut_density,
                  shuffle_seed, overedit, use_all, video1, video2, video3, video4, video5, video6, video7, video8,
                  image1, image2, image3, image4, image5, image6, image7, image8):
        # ffmpeg + librosa are blocking; keep the event loop alive (a blocked loop can make a
        # finished Graydient job report timed_out).
        return await asyncio.to_thread(
            self._run_sync, audio_file, manifest, height, aspect, fps, max_frames, cut_density,
            shuffle_seed, overedit, use_all, [video1, video2, video3, video4, video5, video6, video7, video8],
            [image1, image2, image3, image4, image5, image6, image7, image8])

    def _run_sync(self, audio_file, manifest, height, aspect, fps, max_frames, cut_density, seed,
                  overedit, use_all, vids, imgs):
        from comfy_api.latest import InputImpl
        try:
            import folder_paths
            input_dir = folder_paths.get_input_directory()
        except Exception:
            input_dir = ""
        out = os.path.join(tempfile.mkdtemp(prefix="beatcompile_out_"), "compilation.mp4")
        path, report = core.compile_montage(
            audio_file, manifest, vids, input_dir, out, height=height, aspect=aspect, fps=fps,
            max_frames=max_frames, cut_density=cut_density, seed=seed, overedit=overedit,
            use_all=bool(use_all), image_files=imgs)
        text = json.dumps(report, indent=1)
        return (InputImpl.VideoFromFile(path), text, _encode_string_as_image(text))


NODE_CLASS_MAPPINGS = {"BeatCompile": BeatCompile}
NODE_DISPLAY_NAME_MAPPINGS = {"BeatCompile": "Beat Compile (clips cut to a song)"}
