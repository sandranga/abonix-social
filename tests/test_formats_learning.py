"""Carousels, reels, Insights and learning from the editor."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import pipeline as pipeline_mod
from app.models import PostStatus
from tests.conftest import FakeClaude, good_draft
from tests.test_pipeline import MONDAY, THURSDAY, Clock, FakeMeta, FakeStore, make

TUESDAY = date(2026, 11, 10)


def prompt(claude, i=-1) -> str:
    return claude.beta.messages.calls[i]["messages"][0]["content"]


@pytest.fixture(autouse=True)
def reset_clock():
    Clock.at = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
    yield
    Clock.at = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake_reel(monkeypatch):
    """ffmpeg is tested in test_video.py; here a reel is just a file."""
    made = []

    async def make_reel(images, out):
        out.write_bytes(b"mp4")
        made.append([Path(i).name for i in images])
        return out

    monkeypatch.setattr(pipeline_mod, "make_reel", make_reel)
    return made


async def publish_now(p, post_id):
    await p.approve(post_id)
    Clock.at = datetime(2026, 11, 9, 16, 31, tzinfo=timezone.utc) + timedelta(days=(p.get(post_id).for_date - MONDAY).days)
    return await p.publish(post_id)


# ---------------------------------------------------------------- formats
async def test_monday_is_a_carousel_published_as_one(settings, tmp_path):
    settings.dry_run = False
    meta = FakeMeta()
    p, _ = make(settings, tmp_path, store=FakeStore(), meta=meta)
    post = await p.generate_for(MONDAY)
    assert post.format == "carousel"
    # cover + 2 slides + "link în bio"
    assert len(post.media_paths) == 4 and all(Path(m).is_file() for m in post.media_paths)
    assert post.media_paths[0] == post.image_path

    done = await publish_now(p, post.id)
    assert done.status == PostStatus.published
    kinds = {c[0]: c for c in meta.calls}
    assert len(kinds["ig_carousel"][1]) == 4 and kinds["ig_carousel"][2]  # all slides, with alt text
    assert len(kinds["fb_carousel"][1]) == 4
    assert done.media_urls[0] == done.image_url


async def test_tuesday_is_a_reel(settings, tmp_path, fake_reel):
    settings.dry_run = False
    meta = FakeMeta()
    p, _ = make(settings, tmp_path, store=FakeStore(), meta=meta)
    post = await p.generate_for(TUESDAY)
    assert post.format == "reel" and post.media_paths[0].endswith(".mp4")
    assert len(fake_reel[0]) == 4  # the reel is made of cover + slides + final slide

    done = await publish_now(p, post.id)
    assert done.status == PostStatus.published
    assert ("ig_reel", done.media_urls[0]) in meta.calls
    assert ("wait", 600) in meta.calls  # reels get longer to process
    assert ("fb_video", done.media_urls[0]) in meta.calls


async def test_single_image_drops_slides(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    post = await p.generate_for(THURSDAY)
    assert post.format == "single" and post.content["slides"] == [] and len(post.media_paths) == 1


async def test_prompt_states_the_format(settings, tmp_path):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    await p.generate_for(MONDAY)
    assert "Format: carusel" in prompt(claude)
    await p.generate_for(THURSDAY)
    assert "Format: o singură imagine" in prompt(claude)


async def test_slide_rules(settings, tmp_path):
    from app.generate.review import rule_issues

    long = good_draft(slides=[{"title": "Un titlu de slide mult prea lung azi", "body": "scurt"}])
    issues = " ".join(rule_issues(long, "feature", "carousel"))
    assert "1 slide-uri (2-4)" in issues and "Slide 2: titlul" in issues
    assert not any("Slide" in i or "slide" in i for i in rule_issues(long, "photo", "single"))


# --------------------------------------------------------------- learning
async def test_feedback_is_remembered_for_later_posts(settings, tmp_path):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_for(MONDAY)
    await p.regenerate(post.id, feedback="fără emoji în caption")
    assert p.get(post.id).feedback == ["fără emoji în caption"]
    await p.generate_for(THURSDAY)
    assert "fără emoji în caption" in prompt(claude) and "Instrucțiuni date recent" in prompt(claude)


async def test_standing_rules(settings, tmp_path):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    p.add_rule("titlul fără semn de exclamare")
    p.add_rule("de șters")
    assert p.remove_rule(2) == "de șters" and p.rules() == ["titlul fără semn de exclamare"]
    with pytest.raises(ValueError):
        p.remove_rule(5)
    await p.generate_for(MONDAY)
    assert "Regulile permanente" in prompt(claude) and "titlul fără semn de exclamare" in prompt(claude)


async def test_skipped_subject_goes_back(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    first = await p.generate_for(MONDAY)
    assert first.subject == "reminders"
    await p.skip(first.id)
    second = await p.generate_for(MONDAY + timedelta(days=7))
    assert second.subject == "checkin"  # a skip no longer brings the subject straight back
    assert "memento-urile automate" in " ".join(p.rejected_hooks("feature"))


async def test_subject_skipped_twice_goes_last(settings, tmp_path):
    from app import db
    from app.models import Post

    p, _ = make(settings, tmp_path)
    plan = pipeline_mod.content.weekday_plan(0)
    with db.session() as s:
        # reminders: oldest use, so it would normally come next...
        for d in (date(2026, 10, 1), date(2026, 10, 8)):
            s.add(Post(for_date=d, theme="feature", subject="reminders", template="feature",
                       status=PostStatus.skipped))
        # ...every other subject was published more recently.
        for i, subj in enumerate(x for x in plan.subjects if x != "reminders"):
            s.add(Post(for_date=date(2026, 10, 20) + timedelta(days=i), theme="feature", subject=subj,
                       template="feature", status=PostStatus.published))
    assert p.pick_subject(plan) == plan.subjects[1]  # the oldest of the others, not reminders

    with db.session() as s:  # once approved again, reminders is back in the normal rotation
        s.add(Post(for_date=date(2026, 10, 9), theme="feature", subject="reminders", template="feature",
                   status=PostStatus.published))
    assert p.pick_subject(plan) == "reminders"


async def test_season_and_stories_reach_the_prompt(settings, tmp_path, monkeypatch):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    monkeypatch.setattr(pipeline_mod.content, "stories", lambda: [{"key": "x", "consent": True, "quote": "„Merge.”"}])
    await p.generate_for(MONDAY)  # November
    assert "Toamna, sezon plin" in prompt(claude)
    assert "Povești reale cu acord" in prompt(claude) and "„Merge.”" in prompt(claude)


def test_only_consented_stories_are_used(tmp_path, monkeypatch):
    from app import content

    (tmp_path / "stories.yaml").write_text(
        "stories:\n  - {key: a, consent: true, quote: da}\n  - {key: b, consent: false, quote: nu}\n  - {key: c, quote: nu}\n",
        encoding="utf-8")
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    content.stories.cache_clear()
    try:
        assert [s["key"] for s in content.stories()] == ["a"]
    finally:
        content.stories.cache_clear()


# --------------------------------------------------------------- insights
async def test_metrics_report_and_examples(settings, tmp_path):
    settings.dry_run = False
    meta = FakeMeta()
    claude = FakeClaude()
    p, n = make(settings, tmp_path, claude=claude, store=FakeStore(), meta=meta)
    post = await p.generate_for(MONDAY)
    await publish_now(p, post.id)

    assert await p.collect_metrics() == 1
    got = p.get(post.id)
    assert got.metrics["ig"]["reach"] == 200 and got.metrics["fb"]["shares"] == 1
    # weighted: likes 10 + 2*2 comments + 3*6 saves + 3*3 shares + fb 4 + 2*1 + 3*1
    assert pipeline_mod.interactions(got) == 10 + 4 + 18 + 9 + 4 + 2 + 3
    assert pipeline_mod.score(got) == pytest.approx(50 / 200)

    report = p.report_text()
    assert "Pe teme:" in report and "feature" in report and got.content["headline"] in report
    assert p.best_examples()[0]["headline"] == got.content["headline"]

    await p.generate_for(THURSDAY)
    assert "Postări aprobate care au mers bine" in prompt(claude)


async def test_no_metrics_in_dry_run(settings, tmp_path):
    p, _ = make(settings, tmp_path, meta=FakeMeta())
    post = await p.generate_for(MONDAY)
    await publish_now(p, post.id)
    assert await p.collect_metrics() == 0
    assert "Nicio postare cu statistici" in p.report_text()


def test_migration_adds_every_new_column(tmp_path):
    import sqlite3

    from sqlalchemy import inspect

    from app import db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, for_date DATE)")
    con.execute("INSERT INTO posts (for_date) VALUES ('2026-11-09')")
    con.commit(); con.close()
    db.init_db(f"sqlite:///{path.as_posix()}")
    try:
        cols = {c["name"] for c in inspect(db._engine).get_columns("posts")}
        assert {name for name, _ in db.NEW_POST_COLUMNS} <= cols
        row = sqlite3.connect(path).execute("SELECT format, media_paths, feedback, metrics FROM posts").fetchone()
        assert row == ("single", "[]", "[]", "{}")
    finally:
        db._engine.dispose()


# ------------------------------------------------------------ /post requests
async def test_custom_post_is_extra_and_keeps_its_request(settings, tmp_path):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    daily = await p.generate_for(MONDAY)
    await p.approve(daily.id)

    post = await p.generate_custom(MONDAY, "anunță programul redus de Crăciun")
    assert post.extra and post.theme == "custom" and post.format == "single" and len(post.media_paths) == 1
    assert "«anunță programul redus de Crăciun»" in prompt(claude)
    assert p.get(daily.id).status == PostStatus.approved  # the daily post is untouched

    again = await p.regenerate(post.id, feedback="mai scurt")
    assert again.revision == 2 and "«anunță programul redus de Crăciun»" in prompt(claude)


async def test_custom_photo_post_uses_a_stock_photo(settings, tmp_path):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_custom(MONDAY, "pentru cluburile de înot", template="photo")
    assert post.template == "photo" and post.photo and "photo_query" in prompt(claude)


# ------------------------------------------------- /post follows the request
def test_keyword_fallback_reads_the_whole_request():
    from app.generate.request import guess

    assert guess("vreau un reel despre memento-uri").format == "reel"
    assert guess("fă-mi un carusel cu 3 sfaturi").format == "carousel"
    assert guess("o poză cu un club de înot").template == "photo"
    kiosk = guess("reel cu tableta de check-in la intrare")
    assert (kiosk.format, kiosk.template, kiosk.subject) == ("reel", "feature", "kiosk")
    assert guess("anunță programul de Crăciun") .template == "quote"


async def test_post_uses_claudes_reading_of_the_request(settings, tmp_path, fake_reel):
    from app.generate.request import RequestPlan

    claude = FakeClaude(request_plan=RequestPlan(format="carousel", template="feature", subject="reminders"))
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_custom(MONDAY, "carusel despre memento-uri, cu capturi")
    assert (post.format, post.template, post.subject) == ("carousel", "feature", "reminders")
    assert post.request == "carusel despre memento-uri, cu capturi" and len(post.media_paths) == 4
    sent = prompt(claude)
    assert "«carusel despre memento-uri, cu capturi»" in sent
    assert "memento-setari.webp" in sent and "panou.webp" not in sent.split("Capturi reale")[1]  # only its screens

    again = await p.regenerate(post.id, feedback="mai scurt")  # keeps both the request and the feature
    assert again.template == "feature" and "«carusel despre memento-uri, cu capturi»" in prompt(claude)


async def test_unknown_key_from_claude_falls_back_to_text(settings, tmp_path):
    from app.generate.request import RequestPlan

    claude = FakeClaude(request_plan=RequestPlan(format="single", template="feature", subject="teleportare"))
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_custom(MONDAY, "ceva despre teleportare")
    assert post.template == "quote" and post.subject == "ceva despre teleportare"


async def test_slides_can_show_screens_and_unknown_ones_are_dropped(settings, tmp_path):
    from app.generate.review import rule_issues

    slides = [{"title": "Memento automat", "body": "Pleacă singur.", "screen": "memento-setari.webp"},
              {"title": "Inventat", "body": "Nu există ecranul.", "screen": "nu-exista.webp"}]
    issues = " ".join(rule_issues(good_draft(slides=slides), "feature", "carousel"))
    assert "„nu-exista.webp” nu există" in issues

    long = [{"title": "Cu captură", "body": " ".join(["cuvânt"] * 19), "screen": "panou.webp"},
            {"title": "Fără", "body": " ".join(["cuvânt"] * 19)}]
    issues = rule_issues(good_draft(slides=long), "feature", "carousel")
    assert any("Slide 2: textul are 19 cuvinte (max 18)" in i for i in issues)
    assert not any("Slide 3: textul" in i for i in issues)  # without a screen 25 is fine

    p, _ = make(settings, tmp_path, claude=FakeClaude(drafts=[good_draft(slides=slides)]))
    post = await p.generate_for(MONDAY)
    assert [s["screen"] for s in post.content["slides"]] == ["memento-setari.webp", None]


def test_brand_palette_only():
    import re

    from app.config import TEMPLATES_DIR
    from app.generate.schemas import PostDraft

    assert good_draft(accent="gradient").accent == "gradient"
    with pytest.raises(ValueError):
        PostDraft(**{**good_draft().model_dump(), "accent": "green"})
    # Backgrounds and tints use the palette variables, not stray colours.
    css = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    accents = "\n".join(line for line in css.splitlines() if line.startswith(".accent-") and "background" in line)
    assert "#6366f1" not in accents and "#2563eb" not in accents
    assert re.findall(r"#[0-9a-fA-F]{6}", accents) == []
