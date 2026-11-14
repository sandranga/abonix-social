"""The daily flow: theme -> Claude text -> review -> image -> render ->
Telegram approval -> scheduled, idempotent publishing to Facebook + Instagram."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import anthropic
from sqlalchemy import or_, select

from app import content, db
from app.config import Settings, get_settings
from app.generate.review import normalize, review
from app.generate.schemas import PostDraft
from app.generate.text import Brief, GenerationError, generate_draft
from app.images.providers import ImageError, ImageProvider, credit_line
from app.images.render import render_set
from app.images.video import make_reel
from app.models import KV, Post, PostStatus, PublishLog
from app.publish.meta import MetaClient, MetaError
from app.publish.storage import MediaStore

log = logging.getLogger(__name__)

RETRY_DELAYS = [timedelta(minutes=10), timedelta(minutes=30), timedelta(hours=2)]


class Notifier(Protocol):
    async def alert(self, text: str) -> None: ...
    async def info(self, text: str) -> None: ...
    async def send_draft(self, post: Post, when: datetime | None) -> tuple[int, list[int]] | None: ...
    async def mark(self, post: Post, note: str = "") -> None: ...


class NullNotifier:
    async def alert(self, text: str) -> None:
        log.warning("ALERT: %s", text)

    async def info(self, text: str) -> None:
        log.info("INFO: %s", text)

    async def send_draft(self, post: Post, when: datetime | None) -> None:
        log.info("draft #%s ready: %s", post.id, post.image_path)
        return None

    async def mark(self, post: Post, note: str = "") -> None:
        log.info("post #%s -> %s %s", post.id, post.status.value, note)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; everything is stored as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def reach(p: Post) -> int:
    return int(((p.metrics or {}).get("ig") or {}).get("reach") or 0)


def saves(p: Post) -> int:
    return int(((p.metrics or {}).get("ig") or {}).get("saved") or 0)


def shares(p: Post) -> int:
    m = p.metrics or {}
    return int((m.get("ig") or {}).get("shares") or 0) + int((m.get("fb") or {}).get("shares") or 0)


def interactions(p: Post) -> int:
    """Everything people did with the post, saves and shares weighted up:
    they say "this was worth keeping/sending", which a like doesn't."""
    ig = (p.metrics or {}).get("ig") or {}
    fb = (p.metrics or {}).get("fb") or {}
    return (int(ig.get("likes") or 0) + 2 * int(ig.get("comments") or 0)
            + 3 * int(ig.get("saved") or 0) + 3 * int(ig.get("shares") or 0)
            + int(fb.get("reactions") or 0) + 2 * int(fb.get("comments") or 0) + 3 * int(fb.get("shares") or 0))


def score(p: Post) -> float | None:
    """Weighted interactions per reached account; None until there is reach."""
    r = reach(p)
    return interactions(p) / r if r else None


def avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


class Pipeline:
    def __init__(self, settings: Settings | None = None, *, claude: anthropic.AsyncAnthropic | None = None,
                 images: ImageProvider | None = None, store: MediaStore | None = None,
                 meta: MetaClient | None = None, notifier: Notifier | None = None,
                 review_with_claude: bool = True):
        self.s = settings or get_settings()
        self.claude = claude
        self.images = images
        self.store = store
        self.meta = meta
        self.notifier: Notifier = notifier or NullNotifier()
        self.review_with_claude = review_with_claude
        self._lock = asyncio.Lock()          # one generation at a time
        self._publish_lock = asyncio.Lock()  # never publish the same post twice concurrently

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().astimezone(self.s.tz).date()

    def slot(self, d: date) -> datetime:
        return datetime.combine(d, self.s.publish_time, tzinfo=self.s.tz).astimezone(timezone.utc)

    # -------------------------------------------------------------------- kv
    def kv_get(self, key: str) -> str | None:
        with db.session() as s:
            row = s.get(KV, key)
            return row.value if row else None

    def kv_set(self, key: str, value: str) -> None:
        with db.session() as s:
            row = s.get(KV, key)
            if row:
                row.value = value
            else:
                s.add(KV(key=key, value=value))

    @property
    def paused(self) -> bool:
        return bool(self.kv_get("paused"))

    # --------------------------------------------------------------- queries
    def get(self, post_id: int) -> Post | None:
        with db.session() as s:
            return s.get(Post, post_id)

    def find_by_message(self, message_id: int) -> Post | None:
        with db.session() as s:
            posts = s.scalars(select(Post).where(Post.status == PostStatus.draft).order_by(Post.id.desc()).limit(20))
            return next((p for p in posts if message_id in (p.telegram_message_ids or [])), None)

    def recent(self, limit: int = 30) -> list[Post]:
        with db.session() as s:
            q = (select(Post).where(Post.status.in_([PostStatus.published, PostStatus.approved, PostStatus.draft,
                                                   PostStatus.publishing]))
                 .order_by(Post.for_date.desc(), Post.id.desc()).limit(limit))
            return list(s.scalars(q))

    def pick_subject(self, plan: content.DayPlan) -> str:
        """Rotation: the subject of this theme used longest ago (never used first).
        A skipped draft counts as used, so the subject goes to the back of the
        line; a subject you skipped twice in 60 days, with no approval since,
        comes only after all the others."""
        with db.session() as s:
            rows = s.execute(
                select(Post.subject, Post.for_date, Post.status)
                .where(Post.theme == plan.theme, Post.status != PostStatus.expired)
            ).all()
        last_used: dict[str, date] = {}
        skips: dict[str, int] = {}
        recent = self.today() - timedelta(days=60)
        for subj, d, status in sorted(rows, key=lambda r: r[1]):
            if subj not in last_used or d > last_used[subj]:
                last_used[subj] = d
            if status == PostStatus.skipped and d >= recent:
                skips[subj] = skips.get(subj, 0) + 1
            elif status in (PostStatus.approved, PostStatus.publishing, PostStatus.published):
                skips[subj] = 0
        return min(plan.subjects, key=lambda x: (skips.get(x, 0) >= 2, x in last_used,
                                                 last_used.get(x, date.min), plan.subjects.index(x)))

    # ------------------------------------------------------------- learning
    def rules(self) -> list[str]:
        """Your standing rules for every post (/regula)."""
        try:
            return list(json.loads(self.kv_get("rules") or "[]"))
        except ValueError:
            return []

    def add_rule(self, text: str) -> list[str]:
        rules = self.rules() + [text.strip()]
        self.kv_set("rules", json.dumps(rules, ensure_ascii=False))
        return rules

    def remove_rule(self, index: int) -> str:
        """1-based, as listed by /reguli."""
        rules = self.rules()
        if not 1 <= index <= len(rules):
            raise ValueError(f"nu există regula {index}")
        removed = rules.pop(index - 1)
        self.kv_set("rules", json.dumps(rules, ensure_ascii=False))
        return removed

    def past_feedback(self, limit: int = 12) -> list[str]:
        """The instructions you gave when regenerating drafts, newest first, no repeats."""
        since = self.today() - timedelta(days=90)
        with db.session() as s:
            posts = s.scalars(select(Post).where(Post.for_date >= since).order_by(Post.id.desc()).limit(60))
            out: list[str] = []
            for p in posts:
                for f in reversed(p.feedback or []):
                    if f.lower() not in [o.lower() for o in out]:
                        out.append(f)
        return out[:limit]

    def rejected_hooks(self, theme: str, limit: int = 5) -> list[str]:
        """The angles of drafts you skipped for this theme lately."""
        since = self.today() - timedelta(days=60)
        with db.session() as s:
            posts = s.scalars(select(Post).where(Post.theme == theme, Post.status == PostStatus.skipped,
                                                 Post.for_date >= since).order_by(Post.id.desc()).limit(limit))
            return [p.content.get("hook", "") for p in posts if p.content and p.content.get("hook")]

    def best_examples(self, limit: int = 3) -> list[dict[str, str]]:
        """Published posts that did best (engagement per reach), or the latest
        approved ones while there are no Insights yet."""
        since = self.today() - timedelta(days=120)
        with db.session() as s:
            posts = list(s.scalars(select(Post).where(
                Post.status.in_([PostStatus.published, PostStatus.approved]), Post.for_date >= since)
                .order_by(Post.id.desc()).limit(60)))
        scored = [p for p in posts if score(p) is not None]
        chosen = sorted(scored, key=lambda p: score(p), reverse=True)[:limit] if scored else posts[:limit]
        return [{"theme": p.theme, "headline": p.content.get("headline", ""),
                 "caption": " ".join(p.content.get("caption_ig", "").split())[:400]}
                for p in chosen if p.content]

    def subject_detail(self, plan: content.DayPlan, subject: str) -> dict[str, Any] | None:
        if plan.template == "feature":
            return content.feature(subject)
        if plan.template == "offer":
            return content.offer(subject)
        if plan.template == "number":
            return content.facts().get(subject)
        return None

    def used_photo_ids(self) -> set[str]:
        with db.session() as s:
            return {p["source_id"] for p in s.scalars(select(Post.photo).where(Post.photo.is_not(None))) if p}

    # ------------------------------------------------------------ generation
    async def generate_for(self, d: date, force: bool = False) -> Post | None:
        """Scheduled entry point. Skips days without a theme and days that
        already have an active post, unless `force`."""
        plan = content.day_plan(d)
        if plan is None:
            log.info("%s: no post on this day", d)
            if force:
                await self.notifier.info(f"{d:%d.%m} e zi fără postare în calendar.")
            return None
        with db.session() as s:
            locked = s.scalars(select(Post).where(
                Post.for_date == d, Post.extra.is_(False),
                Post.status.in_([PostStatus.approved, PostStatus.publishing, PostStatus.published]),
            )).first()
            draft = s.scalars(select(Post).where(Post.for_date == d, Post.extra.is_(False),
                                                 Post.status == PostStatus.draft)).first()
        # Never a second post for a day that is already approved/published.
        if locked:
            log.info("%s already has post #%s (%s)", d, locked.id, locked.status.value)
            if force:
                await self.notifier.info(f"{d:%d.%m} are deja postarea #{locked.id} ({locked.status.value}).")
            return locked
        if draft and not force:
            return draft
        with db.session() as s:
            # Older unapproved drafts are superseded (today's extras stay open).
            for old in s.scalars(select(Post).where(
                    Post.status == PostStatus.draft,
                    or_(Post.for_date < d, (Post.for_date == d) & Post.extra.is_(False)))):
                old.status = PostStatus.expired
        try:
            return await self._generate(d, plan)
        except Exception as e:
            log.exception("generation failed for %s", d)
            await self.notifier.alert(f"Generarea pentru {d:%d.%m} a eșuat: {e}")
            raise

    async def generate_extra(self, d: date, weekday: int | None = None) -> Post | None:
        """An additional post for `d`, on top of the daily one and whatever is
        already approved or published. `weekday` borrows that day's theme."""
        plan = content.weekday_plan(d.weekday() if weekday is None else weekday)
        if plan is None:
            await self.notifier.info("Ziua asta nu are temă. Alege una, de ex. /extra joi")
            return None
        try:
            return await self._generate(d, plan, extra=True)
        except Exception as e:
            log.exception("extra generation failed for %s", d)
            await self.notifier.alert(f"Generarea postării extra a eșuat: {e}")
            raise

    async def generate_custom(self, d: date, request: str, fmt: str | None = None,
                              template: str | None = None, subject: str | None = None) -> Post:
        """/post: a post written from your own request, on top of the daily one.
        Without fmt/template, Claude reads the request and picks the format, the
        cover and the feature/plan it is about."""
        from app.generate.request import interpret

        try:
            if fmt is None or template is None:
                rp = await interpret(request, self.claude)
                fmt, template, subject = fmt or rp.format, template or rp.template, subject or rp.subject
                log.info("/post plan: %s %s %s", fmt, template, subject)
            plan = content.custom_plan(request, fmt, template, subject)
            return await self._generate(d, plan, extra=True, subject=plan.subjects[0], request=request)
        except Exception as e:
            log.exception("custom post failed")
            await self.notifier.alert(f"Postarea la cerere a eșuat: {e}")
            raise

    def plan_for(self, post: Post) -> content.DayPlan | None:
        """The plan a post was made from (your request, for /post ones)."""
        if post.theme == content.CUSTOM:
            subject = post.subject if post.request else None
            return content.custom_plan(post.request or post.subject, post.format or "single", post.template, subject)
        return content.theme_plan(post.theme)

    async def _generate(self, d: date, plan: content.DayPlan, *, base: Post | None = None,
                        feedback: str | None = None, image_only: bool = False, extra: bool = False,
                        subject: str | None = None, request: str | None = None) -> Post:
        async with self._lock:
            subject = base.subject if base else (subject or self.pick_subject(plan))
            with db.session() as s:
                if base:
                    post = s.get(Post, base.id)
                    post.revision += 1
                    post.status = PostStatus.generating
                else:
                    post = Post(for_date=d, theme=plan.theme, subject=subject, template=plan.template,
                                status=PostStatus.generating, extra=extra, format=plan.format, request=request)
                    s.add(post)
                s.flush()
                post_id = post.id
                fmt = post.format or "single"
                prev_content = dict(post.content or {})
                prev_photo = post.photo
                usage = dict(post.usage or {})
            try:
                await self._build(d, plan, post_id, subject, base, feedback, image_only,
                                  prev_content, prev_photo, usage, fmt)
            except Exception:
                # Leave nothing stuck in "generating": drop a new row, restore a regenerated one.
                with db.session() as s:
                    post = s.get(Post, post_id)
                    if base is None:
                        s.delete(post)
                    else:
                        post.revision = base.revision
                        post.status = PostStatus.draft
                raise
            post = self.get(post_id)
        sent = await self.notifier.send_draft(post, self.slot_for(d).astimezone(self.s.tz))
        if sent:
            with db.session() as s:
                p = s.get(Post, post_id)
                p.telegram_chat_id, p.telegram_message_ids = sent
            post = self.get(post_id)
        return post

    async def _build(self, d: date, plan: content.DayPlan, post_id: int, subject: str, base: Post | None,
                     feedback: str | None, image_only: bool, prev_content: dict[str, Any],
                     prev_photo: dict[str, Any] | None, usage: dict[str, Any], fmt: str = "single") -> None:
        if image_only and prev_content:
            draft = PostDraft.model_validate(prev_content)
            issues = list(base.review_issues if base else [])
        else:
            draft, issues, u = await self._write(plan, subject, feedback, prev_content or None, fmt)
            usage[f"rev{(base.revision + 1) if base else 1}"] = u

        photo_info, photo_path = None, None
        if plan.template == "photo":
            photo_info, photo_path = await self._photo(draft, exclude_current=prev_photo if image_only else None)
        elif plan.template == "feature" and image_only:
            draft = self._next_screen(subject, draft)

        if fmt == "single" and draft.slides:
            draft = draft.model_copy(update={"slides": []})
        known = set(content.all_screens())
        if any(sl.screen and sl.screen not in known for sl in draft.slides):  # never render a missing file
            draft = draft.model_copy(update={"slides": [
                sl.model_copy(update={"screen": sl.screen if sl.screen in known else None}) for sl in draft.slides]})
        out = self.s.media_dir / f"{d:%Y-%m-%d}-p{post_id}-v{(base.revision + 1) if base else 1}.jpg"
        images = await render_set(plan.template, plan.theme, subject, draft.model_dump(), out, photo_path,
                                  credit_line(photo_info), with_slides=fmt in ("carousel", "reel"))
        if fmt == "reel":
            media = [await make_reel(images, out.with_suffix(".mp4"))]
        else:
            media = images

        with db.session() as s:
            post = s.get(Post, post_id)
            post.content = draft.model_dump()
            post.review_issues = issues
            post.photo = photo_info
            post.image_path = str(out)
            post.image_url = None
            post.media_paths = [str(m) for m in media]
            post.media_urls = []
            post.usage = usage
            post.status = PostStatus.draft

    async def _write(self, plan: content.DayPlan, subject: str, feedback: str | None,
                     previous: dict[str, Any] | None, fmt: str = "single") -> tuple[PostDraft, list[str], dict[str, Any]]:
        recent = [{"date": p.for_date.isoformat(), "theme": p.theme, "headline": p.content.get("headline", ""),
                   "hook": p.content.get("hook", "")} for p in self.recent() if p.content]
        brief = Brief(theme=plan.theme, label=plan.label, template=plan.template, guidance=plan.guidance,
                      subject=subject, subject_detail=self.subject_detail(plan, subject), recent=recent,
                      feedback=feedback, previous=previous, format=fmt, rules=self.rules(),
                      past_feedback=self.past_feedback(), examples=self.best_examples(),
                      rejected=self.rejected_hooks(plan.theme), season=content.season(self.today().month),
                      stories=content.stories(), screens=self._slide_screens(plan, subject))
        if self.claude is None:
            raise GenerationError("Claude client not configured (ANTHROPIC_API_KEY)")
        draft, u1 = await generate_draft(brief, self.claude)
        draft = normalize(draft)
        issues, u2 = await review(draft, plan.template, self.claude if self.review_with_claude else None, fmt)
        usage = {"write": u1, "review": u2}
        if issues:
            # One automatic correction pass; whatever remains is shown to you.
            log.info("review issues, retrying once: %s", issues)
            brief.review_issues = issues
            brief.previous = draft.model_dump()
            draft2, u3 = await generate_draft(brief, self.claude)
            draft2 = normalize(draft2)
            issues2, u4 = await review(draft2, plan.template, self.claude if self.review_with_claude else None,
                                      fmt)
            usage.update(fix=u3, review2=u4)
            if len(issues2) <= len(issues):
                draft, issues = draft2, issues2
        return draft, issues, usage

    @staticmethod
    def _slide_screens(plan: content.DayPlan, subject: str) -> dict[str, list[str]]:
        """A feature post's slides use that feature's screens; other posts may use any."""
        feat = content.feature(subject) if plan.template == "feature" else None
        if feat and feat.get("screens"):
            return {feat["name"]: list(feat["screens"])}
        return content.screens_by_feature()

    async def _photo(self, draft: PostDraft, exclude_current: dict | None) -> tuple[dict[str, Any], Path]:
        if self.images is None:
            raise ImageError("image provider not configured (PIXABAY_API_KEY or PEXELS_API_KEY)")
        exclude = self.used_photo_ids()
        if exclude_current:
            exclude.add(exclude_current["source_id"])
        query = draft.photo_query or "gym members training"
        photo = await self.images.find(query, self.s.media_dir / "photos", exclude)
        info = {"source": photo.source, "source_id": photo.source_id, "credit": photo.credit,
                "page_url": photo.page_url, "query": query}
        return info, photo.path

    def _next_screen(self, subject: str, draft: PostDraft) -> PostDraft:
        screens = (content.feature(subject) or {}).get("screens") or content.all_screens()
        i = screens.index(draft.screen) if draft.screen in screens else -1
        return draft.model_copy(update={"screen": screens[(i + 1) % len(screens)]})

    def slot_for(self, d: date) -> datetime:
        """When an approved post for `d` goes out: its slot, or now if that passed."""
        return max(self.slot(d), self.now() + timedelta(minutes=1))

    # --------------------------------------------------------------- actions
    async def regenerate(self, post_id: int, feedback: str | None = None, image_only: bool = False) -> Post:
        post = self.get(post_id)
        if not post or post.status != PostStatus.draft:
            raise ValueError(f"post {post_id} is not a draft")
        plan = self.plan_for(post)
        if plan is None:
            raise ValueError(f"theme {post.theme} is no longer in the calendar")
        if feedback:
            with db.session() as s:
                p = s.get(Post, post_id)
                p.feedback = [*(p.feedback or []), feedback.strip()]
        await self.notifier.mark(post, "→ regenerez")
        try:
            return await self._generate(post.for_date, plan, base=post, feedback=feedback, image_only=image_only)
        except Exception as e:
            with db.session() as s:
                p = s.get(Post, post_id)
                p.status = PostStatus.draft
            await self.notifier.alert(f"Regenerarea #{post_id} a eșuat: {e}")
            raise

    async def approve(self, post_id: int) -> Post:
        with db.session() as s:
            post = s.get(Post, post_id)
            if post.status != PostStatus.draft:
                return post
            post.status = PostStatus.approved
            post.scheduled_for = self.slot_for(post.for_date)
            post.next_attempt_at = post.scheduled_for
        post = self.get(post_id)
        local = as_utc(post.scheduled_for).astimezone(self.s.tz)
        await self.notifier.mark(post, f"→ se publică {local:%d.%m %H:%M}" + (" (DRY RUN)" if self.s.dry_run else ""))
        return post

    async def skip(self, post_id: int) -> Post:
        with db.session() as s:
            post = s.get(Post, post_id)
            post.status = PostStatus.skipped
        post = self.get(post_id)
        await self.notifier.mark(post)
        return post

    # ------------------------------------------------------------ publishing
    async def publish_due(self) -> list[Post]:
        if self.paused:
            return []
        now = self.now()
        with db.session() as s:
            due = [p.id for p in s.scalars(select(Post).where(Post.status == PostStatus.approved))
                   if as_utc(p.next_attempt_at or p.scheduled_for) <= now]
        done = []
        for pid in due:
            done.append(await self.publish(pid))
        return done

    def _log(self, post_id: int, platform: str, action: str, ok: bool, detail: str = "") -> None:
        with db.session() as s:
            s.add(PublishLog(post_id=post_id, platform=platform, action=action, ok=ok, detail=detail[:4000]))

    async def publish(self, post_id: int) -> Post:
        async with self._publish_lock:
            with db.session() as s:
                post = s.get(Post, post_id)
                if post.status not in (PostStatus.approved, PostStatus.publishing):
                    return post
                post.status = PostStatus.publishing
                post.attempts += 1
            post = self.get(post_id)
            try:
                await self._publish_platforms(post)
            except Exception as e:
                return await self._publish_failed(post_id, e)
            with db.session() as s:
                p = s.get(Post, post_id)
                p.status = PostStatus.published
                p.last_error = None
            post = self.get(post_id)
            links = []
            if post.fb_post_id:
                links.append(f"FB {post.fb_post_id}")
            if post.ig_media_id:
                links.append(f"IG {post.ig_media_id}")
            await self.notifier.mark(post, ("(DRY RUN, nimic trimis) " if self.s.dry_run else "") + ", ".join(links))
            return post

    async def _publish_platforms(self, post: Post) -> None:
        c = post.content
        caption_ig = c["caption_ig"] + "\n\n" + " ".join(c["hashtags_ig"])
        caption_fb = c["caption_fb"] + ("\n\n" + " ".join(c["hashtags_fb"]) if c["hashtags_fb"] else "")
        targets = self.s.publish_targets

        if self.s.dry_run:
            self._log(post.id, "all", "dry_run", True, f"would publish {post.image_path} to {targets}")
            with db.session() as s:
                p = s.get(Post, post.id)
                p.fb_post_id = p.fb_post_id or ("dry-run" if "facebook" in targets else None)
                p.ig_media_id = p.ig_media_id or ("dry-run" if "instagram" in targets else None)
            return

        if self.meta is None or self.store is None:
            raise MetaError("Meta client or media storage not configured", retryable=False)

        paths = post.media_paths or [post.image_path]
        if len(post.media_urls or []) != len(paths):
            urls = []
            for path in paths:
                urls.append(await self.store.put(Path(path), f"posts/{Path(path).name}"))
            with db.session() as s:
                p = s.get(Post, post.id)
                p.media_urls = urls
                p.image_url = urls[0]
            self._log(post.id, "storage", "upload", True, " ".join(urls))
            post = self.get(post.id)
        fmt = post.format or "single"
        alt = c.get("alt_text") or None

        # Each platform is idempotent: a stored id means it is already live.
        if "instagram" in targets and not post.ig_media_id:
            limit = await self.meta.publishing_limit()
            if limit["quota_usage"] >= limit["quota_total"]:
                raise MetaError(f"Instagram publishing limit reached ({limit})")
            if not post.ig_container_id:
                if fmt == "carousel":
                    cid = await self.meta.create_ig_carousel(post.media_urls, caption_ig, alt)
                elif fmt == "reel":
                    cid = await self.meta.create_ig_reel(post.media_urls[0], caption_ig)
                else:
                    cid = await self.meta.create_ig_container(post.image_url, caption_ig, alt)
                with db.session() as s:
                    s.get(Post, post.id).ig_container_id = cid
                self._log(post.id, "instagram", "container", True, cid)
                post = self.get(post.id)
            try:
                # A reel is processed on Meta's side: give it longer.
                await self.meta.wait_ig_container(post.ig_container_id, timeout_s=600 if fmt == "reel" else 120)
            except MetaError as e:
                if e.retryable:  # expired container: make a new one next attempt
                    with db.session() as s:
                        s.get(Post, post.id).ig_container_id = None
                raise
            media_id = await self.meta.publish_ig_container(post.ig_container_id)
            with db.session() as s:
                s.get(Post, post.id).ig_media_id = media_id
            self._log(post.id, "instagram", "publish", True, media_id)

        if "facebook" in targets and not post.fb_post_id:
            if fmt == "carousel":
                fb_id = await self.meta.publish_facebook_carousel(post.media_urls, caption_fb)
            elif fmt == "reel":
                fb_id = await self.meta.publish_facebook_video(post.media_urls[0], caption_fb)
            else:
                fb_id = await self.meta.publish_facebook_photo(post.image_url, caption_fb)
            with db.session() as s:
                s.get(Post, post.id).fb_post_id = fb_id
            self._log(post.id, "facebook", "publish", True, fb_id)

    async def _publish_failed(self, post_id: int, e: Exception) -> Post:
        retryable = getattr(e, "retryable", True)
        self._log(post_id, "all", "error", False, str(e))
        with db.session() as s:
            p = s.get(Post, post_id)
            p.last_error = str(e)
            if retryable and p.attempts < self.s.max_publish_attempts:
                p.status = PostStatus.approved
                p.next_attempt_at = self.now() + RETRY_DELAYS[min(p.attempts - 1, len(RETRY_DELAYS) - 1)]
                final = False
            else:
                p.status = PostStatus.failed
                final = True
            attempts, nxt = p.attempts, p.next_attempt_at
        log.error("publish #%s failed (attempt %s): %s", post_id, attempts, e)
        post = self.get(post_id)
        if final:
            await self.notifier.alert(f"Publicarea #{post_id} a eșuat definitiv după {attempts} încercări: {e}")
            await self.notifier.mark(post)
        else:
            local = as_utc(nxt).astimezone(self.s.tz)
            await self.notifier.alert(f"Publicarea #{post_id} a eșuat (încercarea {attempts}), reîncerc la {local:%H:%M}: {e}")
        return post

    # -------------------------------------------------------------- insights
    async def collect_metrics(self, days: int = 30) -> int:
        """Daily: refresh reach and interactions of everything published in the
        last `days` days. Returns how many posts were updated."""
        if self.meta is None or self.s.dry_run:
            return 0
        since = self.today() - timedelta(days=days)
        with db.session() as s:
            posts = list(s.scalars(select(Post).where(Post.status == PostStatus.published, Post.for_date >= since)))
        updated = 0
        for post in posts:
            m: dict[str, Any] = dict(post.metrics or {})
            if post.ig_media_id and post.ig_media_id != "dry-run":
                try:
                    m["ig"] = await self.meta.ig_insights(post.ig_media_id)
                except MetaError as e:
                    log.warning("IG insights #%s: %s", post.id, e)
            if post.fb_post_id and post.fb_post_id != "dry-run":
                try:
                    m["fb"] = await self.meta.fb_post_stats(post.fb_post_id)
                except MetaError as e:
                    log.warning("FB stats #%s: %s", post.id, e)
            if m != (post.metrics or {}):
                with db.session() as s:
                    p = s.get(Post, post.id)
                    p.metrics = m
                    p.metrics_at = self.now()
                updated += 1
        log.info("metrics updated for %d of %d posts", updated, len(posts))
        return updated

    def report_text(self, days: int = 30) -> str:
        """What worked in the last `days` days: by theme, by format, and the top posts."""
        since = self.today() - timedelta(days=days)
        with db.session() as s:
            posts = [p for p in s.scalars(select(Post).where(Post.status == PostStatus.published,
                                                              Post.for_date >= since)) if p.metrics]
        if not posts:
            return (f"📊 Nicio postare cu statistici în ultimele {days} zile. Statisticile se adună zilnic "
                    f"la 21:00 pentru postările publicate (nu în DRY RUN).")
        lines = [f"📊 Ultimele {days} zile · {len(posts)} postări cu statistici", ""]

        def group(key) -> list[str]:
            out = []
            buckets: dict[str, list[Post]] = {}
            for p in posts:
                buckets.setdefault(key(p), []).append(p)
            for name, ps in sorted(buckets.items(), key=lambda kv: -avg([score(p) or 0 for p in kv[1]])):
                out.append(f"• {name}: {len(ps)} post., reach mediu {avg([reach(p) for p in ps]):.0f}, "
                           f"interacțiuni medii {avg([interactions(p) for p in ps]):.1f}, "
                           f"rată {avg([score(p) or 0 for p in ps]) * 100:.1f}%")
            return out

        lines += ["Pe teme:", *group(lambda p: p.theme), "", "Pe format:", *group(lambda p: p.format or "single"), ""]
        top = sorted(posts, key=lambda p: score(p) or 0, reverse=True)[:3]
        lines.append("Cele mai bune:")
        for p in top:
            lines.append(f"• #{p.id} {p.for_date:%d.%m} {p.theme}/{p.format}: „{p.content.get('headline', '')}” "
                         f"· reach {reach(p)}, salvări {saves(p)}, distribuiri {shares(p)}")
        lines.append("\nRata = interacțiuni / reach. Salvările și distribuirile contează cel mai mult.")
        return "\n".join(lines)

    # --------------------------------------------------------------- reports
    def status_text(self) -> str:
        with db.session() as s:
            posts = list(s.scalars(select(Post).order_by(Post.id.desc()).limit(8)))
        from app.approve.telegram import STATUS_LABEL
        lines = ["⏸ PAUZĂ ACTIVĂ" if self.paused else "▶️ Program automat activ",
                 "DRY RUN (nu public nimic)" if self.s.dry_run else "Publicare reală activă", ""]
        for p in posts:
            when = as_utc(p.scheduled_for).astimezone(self.s.tz).strftime(" → %d.%m %H:%M") if p.scheduled_for else ""
            lines.append(f"#{p.id} {p.for_date:%d.%m} {p.theme}{' (extra)' if p.extra else ''}: "
                         f"{STATUS_LABEL[p.status]}{when}"
                         + (f"\n   eroare: {p.last_error[:150]}" if p.last_error and p.status != PostStatus.published else ""))
        nxt = self.today() + timedelta(days=1)
        lines.append(f"\nGenerare zilnică {self.s.generate_time:%H:%M}, publicare {self.s.publish_time:%H:%M}.")
        plan = content.day_plan(nxt)
        lines.append(f"Mâine ({nxt:%d.%m}): {plan.label if plan else 'fără postare'}")
        return "\n".join(lines)

    async def meta_check_text(self) -> str:
        if self.meta is None:
            return "Meta nu e configurat (META_ACCESS_TOKEN)."
        try:
            info = await self.meta.check()
        except MetaError as e:
            return f"❌ Meta: {e}"
        lines = [f"✅ Token: {info['token_owner'].get('name')} ({info['token_owner'].get('id')})",
                 f"Pagina: {info['page']['name']} ({info['page']['id']})",
                 f"Instagram business: {info['instagram_business_account']}"]
        if "ig_matches_config" in info:
            lines.append("ID Instagram corect în config" if info["ig_matches_config"] else "⚠️ META_IG_USER_ID diferă!")
            lim = info["publishing_limit"]
            lines.append(f"Limită IG: {lim['quota_usage']}/{lim['quota_total']} în 24h")
        return "\n".join(lines)

    async def health_check(self) -> None:
        """Daily: alert early if the Meta token stopped working."""
        if self.meta is None or self.s.dry_run:
            return
        try:
            await self.meta.check()
        except MetaError as e:
            await self.notifier.alert(f"Verificarea zilnică Meta a eșuat: {e}")
