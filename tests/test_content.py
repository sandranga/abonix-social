from datetime import date

from app import content
from app.config import ASSETS_DIR


def test_every_weekday_resolves():
    plans = [content.day_plan(date(2026, 11, 9 + i)) for i in range(7)]  # Mon..Sun
    assert [p.theme if p else None for p in plans] == [
        "feature", "tip", "problem_solution", "club_type", "offer", "number", None]


def test_calendar_subjects_exist_in_product_sheet():
    days = content.calendar()["days"]
    for key in days[0]["subjects"]:
        assert content.feature(key), key
    for key in days[4]["subjects"]:
        assert content.offer(key), key
    for key in days[5]["subjects"]:
        assert key in content.facts(), key


def test_every_screen_file_exists():
    for s in content.all_screens():
        assert (ASSETS_DIR / "screens" / s).is_file(), s


def test_allowed_prices_match_pricing_page():
    assert content.allowed_prices() == {145, 215, 425, 69, 30, 99}


def test_screen_frames_follow_the_shape():
    from app.images.render import screen_kind

    assert screen_kind("panou.webp") == "desktop"
    assert screen_kind("app-acasa.webp") == "phone"
    assert screen_kind("extra/app-inghetare.webp") == "phone"
    assert screen_kind("kiosk-scan.webp") == "tablet"  # the entrance tablet gets a tablet frame
