import subprocess

from PIL import Image

from app.images.video import ffmpeg_exe, make_reel


async def test_reel_is_instagram_ready(tmp_path):
    slides = []
    for i, color in enumerate([(79, 70, 229), (14, 165, 233)]):
        path = tmp_path / f"s{i}.jpg"
        Image.new("RGB", (1080, 1350), color).save(path, "JPEG")
        slides.append(path)
    out = await make_reel(slides, tmp_path / "reel.mp4")

    info = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    video = next(line for line in info.splitlines() if "Video:" in line)
    audio = next(line for line in info.splitlines() if "Audio:" in line)
    # Limited-range yuv420p, not full-range yuvj420p (ffmpeg builds word the details differently).
    assert "h264" in video and "yuv420p" in video and "yuvj420p" not in video
    assert "1080x1920" in video and "30 fps" in video
    assert "aac" in audio  # Instagram wants an audio track, even a silent one
    duration = next(line for line in info.splitlines() if "Duration:" in line)
    assert "00:00:06.4" in duration or "00:00:06.5" in duration  # 2 x 3.5 s minus one 0.5 s crossfade
