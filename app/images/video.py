"""Turns the rendered slides into a 9:16 reel.

Each 1080x1920 frame is composed once with Pillow (the slide over a blurred,
darkened full-screen copy of itself). ffmpeg then encodes the reel in small
pieces (each slide held still, each swipe between two slides) and joins them
without re-encoding. Feeding every slide into one ffmpeg graph queued the
frames of later slides in memory and got killed at the container's 1 GB.

Audio: a music track from assets/music/ if there is one (rotated by post),
otherwise a silent track (Instagram wants an audio stream). Uses the ffmpeg
bundled with imageio-ffmpeg, so the image needs no system packages."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from app.config import ASSETS_DIR

log = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
MUSIC_DIR = ASSETS_DIR / "music"
MUSIC_TYPES = (".mp3", ".m4a", ".aac", ".wav")


class VideoError(RuntimeError):
    pass


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def music_tracks() -> list[Path]:
    if not MUSIC_DIR.is_dir():
        return []
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in MUSIC_TYPES)


def pick_music(seed: int) -> Path | None:
    """One of your tracks, rotating so consecutive reels differ; None if there are none."""
    tracks = music_tracks()
    return tracks[seed % len(tracks)] if tracks else None


def compose_frame(slide: Path, out: Path) -> Path:
    """The slide centred on a blurred, slightly darker full-screen copy of itself."""
    with Image.open(slide) as im:
        im = im.convert("RGB")
        scale = H / im.height
        bg = im.resize((round(im.width * scale), H), Image.LANCZOS)
        left = (bg.width - W) // 2
        bg = bg.crop((left, 0, left + W, H)).filter(ImageFilter.GaussianBlur(40))
        bg = ImageEnhance.Brightness(bg).enhance(0.85)
        fg = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)
        bg.paste(fg, (0, (H - fg.height) // 2))
        bg.save(out, "JPEG", quality=92)
    return out


# Same encoder settings for every piece, so the pieces join without re-encoding.
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-color_range", "tv",
          "-r", str(FPS), "-threads", "2", "-video_track_timescale", "15360", "-an"]
PREP = "scale=out_range=tv,format=yuv420p,setsar=1"


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        why = "oprit de sistem, probabil memorie insuficientă" if proc.returncode in (-9, 137) else proc.stderr[-800:]
        raise VideoError(f"ffmpeg failed ({proc.returncode}): {why}")


def _hold(frame: Path, seconds: float, out: Path) -> Path:
    """One slide standing still."""
    _run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS),
          "-t", f"{seconds}", "-i", str(frame), "-vf", PREP, *ENCODE, str(out)])
    return out


def _transition(a: Path, b: Path, seconds: float, out: Path) -> Path:
    """The swipe from one slide to the next: only these two images in memory."""
    _run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
          "-loop", "1", "-framerate", str(FPS), "-t", f"{seconds}", "-i", str(a),
          "-loop", "1", "-framerate", str(FPS), "-t", f"{seconds}", "-i", str(b),
          "-filter_complex", f"[0:v]{PREP}[a];[1:v]{PREP}[b];"
                             f"[a][b]xfade=transition=smoothleft:duration={seconds}:offset=0,format=yuv420p",
          *ENCODE, str(out)])
    return out


def build_reel(images: list[Path], out: Path, music: Path | None = None,
               hold: float = 3.0, fade: float = 0.5) -> Path:
    """Slides held `hold` seconds each with `fade`-second swipes between them.
    Built piece by piece, so at most two images are ever in ffmpeg at once."""
    if not images:
        raise VideoError("a reel needs at least one image")
    total = len(images) * hold + (len(images) - 1) * fade
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        frames = [compose_frame(img, t / f"f{i}.jpg") for i, img in enumerate(images)]
        pieces = []
        for i, frame in enumerate(frames):
            pieces.append(_hold(frame, hold, t / f"h{i}.mp4"))
            if i + 1 < len(frames):
                pieces.append(_transition(frame, frames[i + 1], fade, t / f"t{i}.mp4"))
        listing = t / "pieces.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")

        cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(listing)]
        if music:  # trimmed to the video, faded in and out
            cmd += ["-stream_loop", "-1", "-i", str(music), "-filter_complex",
                    f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.4,"
                    f"afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5,aresample=48000[a]",
                    "-map", "0:v", "-map", "[a]"]
        else:  # Instagram wants an audio stream, even a silent one
            cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                    "-map", "0:v", "-map", "1:a"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-t", f"{total:.3f}",
                "-movflags", "+faststart", str(out)]
        _run(cmd)
    return out


async def make_reel(images: list[Path], out: Path, music: Path | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(build_reel, images, out, music)
    if not out.is_file():
        raise VideoError("ffmpeg produced no file")
    log.info("reel %s (%d slides, %s)", out, len(images), music.name if music else "silent")
    return out
