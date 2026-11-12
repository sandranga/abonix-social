"""The daily flow: theme -> Claude text -> review -> image -> render ->
Telegram approval -> scheduled, idempotent publishing to Facebook + Instagram."""

from __future__ import annotations

import asyncio
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
from app.images.render import render_post
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
        """Rotation: the subject of this theme used longest ago (never used first)."""
        with db.session() as s:
            rows = s.execute(
                select(Post.subject, Post.for_date)
                .where(Post.theme == plan.theme, Post.status.notin_([PostStatus.skipped, PostStatus.expired]))
            ).all()
        last_used: dict[str, date] = {}
        for subj, d in rows:
            if subj not in last_used or d > last_used[subj]:
                last_used[subj] = d
        return min(plan.subjects, key=lambda x: (x in last_used, last_used.get(x, date.min), plan.subjects.index(x)))

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

    async def _generate(self, d: date, plan: content.DayPlan, *, base: Post | None = None,
                        feedback: str | None = None, image_only: bool = False, extra: bool = False) -> Post:
        async with self._lock:
            subject = base.subject if base else self.pick_subject(plan)
            with db.session() as s:
                if base:
                    post = s.get(Post, base.id)
                    post.revision += 1
                    post.status = PostStatus.generating
                else:
                    post = Post(for_date=d, theme=plan.theme, subject=subject, template=plan.template,
                                status=PostStatus.generating, extra=extra)
                    s.add(post)
                s.flush()
                post_id = post.id
                prev_content = dict(post.content or {})
                prev_photo = post.photo
                usage = dict(post.usage or {})
            try:
                await self._build(d, plan, post_id, subject, base, feedback, image_only,
                                  prev_content, prev_photo, usage)
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
                     prev_photo: dict[str, Any] | None, usage: dict[str, Any]) -> None:
        if image_only and prev_content:
            draft = PostDraft.model_validate(prev_content)
            issues = list(base.review_issues if base else [])
        else:
            draft, issues, u = await self._write(plan, subject, feedback, prev_content or None)
            usage[f"rev{(base.revision + 1) if base else 1}"] = u

        photo_info, photo_path = None, None
        if plan.template == "photo":
            photo_info, photo_path = await self._photo(draft, exclude_current=prev_photo if image_only else None)
        elif plan.template == "feature" and image_only:
            draft = self._next_screen(subject, draft)

        out = self.s.media_dir / f"{d:%Y-%m-%d}-p{post_id}-v{(base.revision + 1) if base else 1}.jpg"
        await render_post(plan.template, plan.theme, subject, draft.model_dump(), out, photo_path,
                          credit_line(photo_info))

        with db.session() as s:
            post = s.get(Post, post_id)
            post.content = draft.model_dump()
            post.review_issues = issues
            post.photo = photo_info
            post.image_path = str(out)
            post.image_url = None
            post.usage = usage
            post.status = PostStatus.draft

    async def _write(self, plan: content.DayPlan, subject: str, feedback: str | None,
                     previous: dict[str, Any] | None) -> tuple[PostDraft, list[str], dict[str, Any]]:
        recent = [{"date": p.for_date.isoformat(), "theme": p.theme, "headline": p.content.get("headline", ""),
                   "hook": p.content.get("hook", "")} for p in self.recent() if p.content]
        brief = Brief(theme=plan.theme, label=plan.label, template=plan.template, guidance=plan.guidance,
                      subject=subject, subject_detail=self.subject_detail(plan, subject), recent=recent,
                      feedback=feedback, previous=previous)
        if self.claude is None:
            raise GenerationError("Claude client not configured (ANTHROPIC_API_KEY)")
        draft, u1 = await generate_draft(brief, self.claude)
        draft = normalize(draft)
        issues, u2 = await review(draft, plan.template, self.claude if self.review_with_claude else None)
        usage = {"write": u1, "review": u2}
        if issues:
            # One automatic correction pass; whatever remains is shown to you.
            log.info("review issues, retrying once: %s", issues)
            brief.review_issues = issues
            brief.previous = draft.model_dump()
            draft2, u3 = await generate_draft(brief, self.claude)
            draft2 = normalize(draft2)
            issues2, u4 = await review(draft2, plan.template, self.claude if self.review_with_claude else None)
            usage.update(fix=u3, review2=u4)
            if len(issues2) <= len(issues):
                draft, issues = draft2, issues2
        return draft, issues, usage

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
        plan = content.theme_plan(post.theme)
        if plan is None:
            raise ValueError(f"theme {post.theme} is no longer in the calendar")
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

        if not post.image_url:
            key = f"posts/{Path(post.image_path).name}"
            url = await self.store.put(Path(post.image_path), key)
            with db.session() as s:
                s.get(Post, post.id).image_url = url
            self._log(post.id, "storage", "upload", True, url)
            post = self.get(post.id)

        # Each platform is idempotent: a stored id means it is already live.
        if "instagram" in targets and not post.ig_media_id:
            limit = await self.meta.publishing_limit()
            if limit["quota_usage"] >= limit["quota_total"]:
                raise MetaError(f"Instagram publishing limit reached ({limit})")
            if not post.ig_container_id:
                cid = await self.meta.create_ig_container(post.image_url, caption_ig)
                with db.session() as s:
                    s.get(Post, post.id).ig_container_id = cid
                self._log(post.id, "instagram", "container", True, cid)
                post = self.get(post.id)
            try:
                await self.meta.wait_ig_container(post.ig_container_id)
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
