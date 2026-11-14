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


async def test_reel_with_music_is_trimmed_to_the_video(tmp_path):
    slides = []
    for i in range(3):
        path = tmp_path / f"s{i}.jpg"
        Image.new("RGB", (1080, 1350), (79, 70, 229)).save(path, "JPEG")
        slides.append(path)
    track = tmp_path / "track.m4a"  # a 30 s tone stands in for a song
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                    "-c:a", "aac", str(track)], check=True)
    out = await make_reel(slides, tmp_path / "reel.mp4", music=track)
    info = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    duration = next(line for line in info.splitlines() if "Duration:" in line)
    assert "00:00:10.0" in duration  # 3 slides x 3 s + 2 swipes x 0.5 s, not the song's 30 s
    assert "aac" in next(line for line in info.splitlines() if "Audio:" in line)


def test_music_rotates(tmp_path, monkeypatch):
    from app.images import video

    monkeypatch.setattr(video, "MUSIC_DIR", tmp_path)
    assert video.pick_music(5) is None  # no tracks: silent
    for name in ["b.mp3", "a.mp3", "notes.md"]:
        (tmp_path / name).write_bytes(b"x")
    assert [video.pick_music(i).name for i in range(3)] == ["a.mp3", "b.mp3", "a.mp3"]
