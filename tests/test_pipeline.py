from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.generate.schemas import ReviewResult
from app.images.providers import Photo
from app.models import PostStatus
from app.pipeline import Pipeline
from app.publish.meta import MetaError
from app.samples import placeholder_photo
from tests.conftest import FakeClaude, FakeNotifier, good_draft

MONDAY = date(2026, 11, 9)
THURSDAY = date(2026, 11, 12)


class Clock(Pipeline):
    """Pipeline with a controllable clock."""

    at = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)  # 08:00 Bucharest

    def now(self):
        return self.at


class FakePhotos:
    def __init__(self, tmp: Path):
        self.path = placeholder_photo(tmp / "ph.jpg")
        self.excluded: list[set] = []

    async def find(self, query, out_dir, exclude_ids):
        self.excluded.append(set(exclude_ids))
        n = len(self.excluded)
        return Photo(path=self.path, source="pexels", source_id=f"p{n}", credit="Ana", page_url="u")


class FakeStore:
    async def put(self, local, key):
        return f"https://media.abonix.net/{key}"


class FakeMeta:
    def __init__(self, fail_fb_times=0):
        self.fail_fb_times = fail_fb_times
        self.ig_published = 0
        self.fb_published = 0
        self.calls: list[tuple] = []  # (method, args) of every create/publish
        self.insights = {"reach": 200, "likes": 10, "comments": 2, "saved": 6, "shares": 3}

    async def publishing_limit(self):
        return {"quota_usage": 0, "quota_total": 100}

    async def create_ig_container(self, url, caption, alt_text=None):
        self.calls.append(("ig_image", url, alt_text))
        return "C1"

    async def create_ig_carousel(self, urls, caption, alt_text=None):
        self.calls.append(("ig_carousel", list(urls), alt_text))
        return "C1"

    async def create_ig_reel(self, url, caption):
        self.calls.append(("ig_reel", url))
        return "C1"

    async def wait_ig_container(self, cid, **kw):
        self.calls.append(("wait", kw.get("timeout_s")))
        return None

    async def publish_ig_container(self, cid):
        self.ig_published += 1
        return "IG1"

    def _fb(self, kind, *args):
        if self.fail_fb_times:
            self.fail_fb_times -= 1
            raise MetaError("temporary", retryable=True)
        self.calls.append((kind, *args))
        self.fb_published += 1
        return "FB1"

    async def publish_facebook_photo(self, url, message):
        return self._fb("fb_photo", url)

    async def publish_facebook_carousel(self, urls, message):
        return self._fb("fb_carousel", list(urls))

    async def publish_facebook_video(self, url, description):
        return self._fb("fb_video", url)

    async def ig_insights(self, media_id):
        return dict(self.insights)

    async def fb_post_stats(self, post_id):
        return {"reactions": 4, "comments": 1, "shares": 1}


def make(settings, tmp_path, **kw):
    n = FakeNotifier()
    p = Clock(settings, claude=kw.pop("claude", FakeClaude()), images=FakePhotos(tmp_path), notifier=n, **kw)
    return p, n


async def test_generate_approve_publish_dry_run(settings, tmp_path):
    p, n = make(settings, tmp_path)
    post = await p.generate_for(MONDAY)
    assert post.status == PostStatus.draft and Path(post.image_path).is_file()
    assert post.subject == "reminders"  # first in the rotation
    assert n.drafts == [post.id] and post.telegram_message_ids

    post = await p.approve(post.id)
    assert post.status == PostStatus.approved
    assert await p.publish_due() == []  # 18:30 not reached

    Clock.at = datetime(2026, 11, 9, 16, 31, tzinfo=timezone.utc)  # 18:31 Bucharest
    [done] = await p.publish_due()
    assert done.status == PostStatus.published and done.fb_post_id == "dry-run"
    Clock.at = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)


async def test_no_second_post_for_an_approved_day(settings, tmp_path):
    p, n = make(settings, tmp_path)
    post = await p.generate_for(MONDAY)
    await p.approve(post.id)
    again = await p.generate_for(MONDAY, force=True)
    assert again.id == post.id and len(n.drafts) == 1


async def test_sunday_has_no_post(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    assert await p.generate_for(date(2026, 11, 15)) is None


async def test_rotation_moves_to_next_subject(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    first = await p.generate_for(MONDAY)
    await p.approve(first.id)
    second = await p.generate_for(MONDAY + timedelta(days=7))
    assert first.subject == "reminders" and second.subject == "checkin"


async def test_review_issues_trigger_one_fix_pass(settings, tmp_path):
    bad = good_draft(headline="Un titlu mult prea lung pentru imagine azi")
    claude = FakeClaude(drafts=[bad, good_draft()])
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_for(MONDAY)
    assert len(claude.beta.messages.calls) == 2
    assert post.review_issues == []
    assert "Corectează" in claude.beta.messages.calls[1]["messages"][0]["content"]


async def test_editor_issues_reach_telegram_when_unfixed(settings, tmp_path):
    claude = FakeClaude(review=ReviewResult(ok=False, issues=["Promite plăți online"]))
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_for(MONDAY)
    assert post.review_issues == ["Editor: Promite plăți online"]


async def test_regenerate_with_feedback_bumps_revision(settings, tmp_path):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_for(MONDAY)
    new = await p.regenerate(post.id, feedback="fără emoji")
    assert new.id == post.id and new.revision == 2 and new.status == PostStatus.draft
    assert "fără emoji" in claude.beta.messages.calls[-1]["messages"][0]["content"]


async def test_photo_image_only_regen_excludes_current_photo(settings, tmp_path):
    claude = FakeClaude()
    p, _ = make(settings, tmp_path, claude=claude)
    post = await p.generate_for(THURSDAY)
    assert post.template == "photo" and post.photo["source_id"] == "p1"
    calls_before = len(claude.beta.messages.calls)
    new = await p.regenerate(post.id, image_only=True)
    assert len(claude.beta.messages.calls) == calls_before  # text untouched
    assert "p1" in p.images.excluded[-1] and new.photo["source_id"] == "p2"


async def test_real_publish_is_idempotent_per_platform(settings, tmp_path):
    settings.dry_run = False
    meta = FakeMeta(fail_fb_times=1)
    p, n = make(settings, tmp_path, store=FakeStore(), meta=meta)
    post = await p.generate_for(MONDAY)
    await p.approve(post.id)
    Clock.at = datetime(2026, 11, 9, 16, 31, tzinfo=timezone.utc)
    [first] = await p.publish_due()
    assert first.status == PostStatus.approved and first.ig_media_id == "IG1" and first.fb_post_id is None
    assert any("reîncerc" in a for a in n.alerts)

    Clock.at += timedelta(minutes=11)
    [second] = await p.publish_due()
    assert second.status == PostStatus.published and second.fb_post_id == "FB1"
    assert meta.ig_published == 1 and meta.fb_published == 1  # IG not published twice
    Clock.at = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)


async def test_gives_up_after_max_attempts(settings, tmp_path):
    settings.dry_run = False
    meta = FakeMeta(fail_fb_times=99)
    p, n = make(settings, tmp_path, store=FakeStore(), meta=meta)
    post = await p.generate_for(MONDAY)
    await p.approve(post.id)
    Clock.at = datetime(2026, 11, 9, 16, 31, tzinfo=timezone.utc)
    for _ in range(3):
        await p.publish_due()
        Clock.at += timedelta(hours=3)
    assert p.get(post.id).status == PostStatus.failed
    assert any("definitiv" in a for a in n.alerts)
    Clock.at = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)


async def test_approving_after_slot_publishes_soon(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    post = await p.generate_for(MONDAY)
    Clock.at = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)  # 20:00, slot passed
    post = await p.approve(post.id)
    from app.pipeline import as_utc
    assert as_utc(post.scheduled_for) == Clock.at + timedelta(minutes=1)
    Clock.at = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad_status", [PostStatus.skipped, PostStatus.published])
async def test_buttons_on_inactive_posts_do_nothing(settings, tmp_path, bad_status):
    from app import db
    from app.approve.telegram import TelegramBot
    from app.models import Post

    p, _ = make(settings, tmp_path)
    post = await p.generate_for(MONDAY)
    with db.session() as s:
        s.get(Post, post.id).status = bad_status

    class Api:
        def __init__(self):
            self.sent = []

        async def call(self, method, **kw):
            self.sent.append((method, kw))
            return {}

    api = Api()
    bot = TelegramBot(api, p, [111])
    await bot.handle({"callback_query": {"id": "q", "data": f"a:{post.id}:1", "message": {"chat": {"id": 111}}}})
    assert p.get(post.id).status == bad_status
    assert api.sent[0][1]["text"] == "Draftul ăsta nu mai e activ."


async def test_unknown_chat_cannot_approve(settings, tmp_path):
    from app.approve.telegram import TelegramBot

    p, _ = make(settings, tmp_path)
    post = await p.generate_for(MONDAY)

    class Api:
        async def call(self, method, **kw):
            return {}

    bot = TelegramBot(Api(), p, [111])
    await bot.handle({"callback_query": {"id": "q", "data": f"a:{post.id}:1", "message": {"chat": {"id": 999}}}})
    assert p.get(post.id).status == PostStatus.draft


class BrokenClaude(FakeClaude):
    def __init__(self):
        super().__init__()

        async def boom(**kw):
            raise RuntimeError("API down")
        self.beta.messages.parse = boom


async def test_failed_generation_leaves_nothing_stuck(settings, tmp_path):
    from sqlalchemy import select

    from app import db
    from app.models import Post

    p, n = make(settings, tmp_path, claude=BrokenClaude())
    with pytest.raises(RuntimeError):
        await p.generate_for(MONDAY)
    with db.session() as s:
        assert list(s.scalars(select(Post))) == []
    assert any("a eșuat" in a for a in n.alerts)


async def test_failed_regeneration_keeps_the_old_draft(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    post = await p.generate_for(MONDAY)
    p.claude = BrokenClaude()
    with pytest.raises(RuntimeError):
        await p.regenerate(post.id, feedback="altfel")
    after = p.get(post.id)
    assert after.status == PostStatus.draft and after.revision == 1 and after.content == post.content


async def test_extra_post_even_when_today_is_published(settings, tmp_path):
    p, n = make(settings, tmp_path)
    daily = await p.generate_for(MONDAY)
    await p.approve(daily.id)
    await p.publish(daily.id)
    assert p.get(daily.id).status == PostStatus.published

    extra = await p.generate_extra(MONDAY)
    assert extra.status == PostStatus.draft and extra.extra and extra.id != daily.id
    assert extra.subject != daily.subject  # rotation moves on within the theme
    # A second extra keeps the first one open; the daily guard is unchanged.
    extra2 = await p.generate_extra(MONDAY)
    assert p.get(extra.id).status == PostStatus.draft and extra2.id != extra.id
    assert (await p.generate_for(MONDAY, force=True)).id == daily.id


async def test_extra_does_not_block_or_expire_with_daily(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    extra = await p.generate_extra(MONDAY)
    await p.approve(extra.id)
    daily = await p.generate_for(MONDAY)  # 08:00 run still makes the daily post
    assert daily.id != extra.id and not daily.extra
    open_extra = await p.generate_extra(MONDAY)
    await p.generate_for(MONDAY, force=True)  # /azi replaces the daily draft only
    assert p.get(open_extra.id).status == PostStatus.draft


async def test_extra_borrows_another_days_theme(settings, tmp_path):
    p, n = make(settings, tmp_path)
    sunday = date(2026, 11, 15)
    assert await p.generate_extra(sunday) is None and n.infos
    post = await p.generate_extra(sunday, weekday=THURSDAY.weekday())
    assert post.template == "photo" and post.for_date == sunday
    again = await p.regenerate(post.id, image_only=True)  # uses the borrowed theme
    assert again.template == "photo" and again.revision == 2


def test_migration_adds_extra_column(tmp_path):
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
        assert "extra" in {c["name"] for c in inspect(db._engine).get_columns("posts")}
        assert sqlite3.connect(path).execute("SELECT extra FROM posts").fetchone() == (0,)
    finally:
        db._engine.dispose()


def test_parse_weekday():
    from app.approve.telegram import parse_weekday
    assert parse_weekday("joi") == 3 and parse_weekday("Sambata") == 5 and parse_weekday("marți") == 1
    assert parse_weekday("mâine") is None
