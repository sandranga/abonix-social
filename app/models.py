from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PostStatus(str, enum.Enum):
    generating = "generating"
    draft = "draft"            # waiting for approval in Telegram
    approved = "approved"      # queued for the publish time
    publishing = "publishing"
    published = "published"
    failed = "failed"          # gave up after max attempts
    skipped = "skipped"        # you pressed "Sar peste"
    expired = "expired"        # never approved, superseded by a newer draft


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    for_date: Mapped[date] = mapped_column(Date, index=True)
    theme: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(200))
    template: Mapped[str] = mapped_column(String(40))
    status: Mapped[PostStatus] = mapped_column(Enum(PostStatus), default=PostStatus.generating, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # Extra posts (/extra) come on top of the daily one: they never block it,
    # and the daily one never blocks them.
    extra: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # single (one image), carousel (several images) or reel (a video made of the slides)
    format: Mapped[str] = mapped_column(String(20), default="single", server_default="single")

    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    review_issues: Mapped[list[str]] = mapped_column(JSON, default=list)
    photo: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # stock photo id, photographer...
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # the cover (first slide)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Every file that gets published, in order: the slides (carousel) or the video (reel).
    media_paths: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    media_urls: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    # Instructions you gave when regenerating (reply to a draft), oldest first.
    feedback: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    # Meta Insights: {"ig": {...}, "fb": {...}}, refreshed daily for 30 days.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    metrics_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    telegram_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fb_post_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ig_container_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ig_media_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # token counts per call
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    logs: Mapped[list[PublishLog]] = relationship(back_populates="post", cascade="all, delete-orphan")


class PublishLog(Base):
    __tablename__ = "publish_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(40))
    ok: Mapped[bool] = mapped_column()
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    post: Mapped[Post] = relationship(back_populates="logs")


class KV(Base):
    """Small key/value store (Telegram update offset, pause flag...)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
