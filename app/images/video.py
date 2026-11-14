"""Turns the rendered slides into a 9:16 reel: each slide over a blurred
full-screen copy of itself, a gentle zoom, crossfades between them and a
silent audio track (Instagram wants one). Uses the ffmpeg bundled with imageio-ffmpeg, so
the image needs no system packages."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

W, H = 1080, 1920
SLIDE_W, SLIDE_H = 1080, 1350
FPS = 30


class VideoError(RuntimeError):
    pass


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def reel_command(images: list[Path], out: Path, seconds: float = 3.5, fade: float = 0.5) -> list[str]:
    n = len(images)
    if n < 1:
        raise VideoError("a reel needs at least one image")
    total = n * (seconds - fade) + fade if n > 1 else seconds
    frames = int(seconds * FPS)
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    for img in images:
        cmd += ["-loop", "1", "-t", f"{seconds}", "-i", str(img)]
    cmd += ["-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=r=48000:cl=stereo"]
    chains = []
    for i in range(n):
        # The slide, slowly zooming 4%, over a blurred and darkened full-screen
        # copy of itself, so the 4:5 slide fills a 9:16 frame without seams.
        chains.append(
            f"[{i}:v]split[a{i}][b{i}];"
            f"[a{i}]scale=-2:{H},crop={W}:{H},boxblur=40:4,eq=brightness=-0.08,fps={FPS},setsar=1[bg{i}];"
            f"[b{i}]scale={SLIDE_W * 2}:{SLIDE_H * 2},"
            f"zoompan=z='min(1+0.04*on/{frames},1.04)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={SLIDE_W}x{SLIDE_H}:fps={FPS},setsar=1[fg{i}];"
            f"[bg{i}][fg{i}]overlay=0:{(H - SLIDE_H) // 2}:shortest=1,"
            f"scale=out_range=tv,format=yuv420p[v{i}]"
        )
    last = "v0"
    for i in range(1, n):
        offset = i * (seconds - fade)
        chains.append(f"[{last}][v{i}]xfade=transition=fade:duration={fade}:offset={offset:.3f}[x{i}]")
        last = f"x{i}"
    cmd += ["-filter_complex", ";".join(chains), "-map", f"[{last}]", "-map", f"{n}:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-color_range", "tv", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out)]
    return cmd


async def make_reel(images: list[Path], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = reel_command(images, out)
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        raise VideoError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-800:]}")
    log.info("reel %s (%d slides)", out, len(images))
    return out
