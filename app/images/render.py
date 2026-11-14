"""HTML template -> 1080x1350 JPEG. Text, prices, logo and screenshots are
put on by the template, so diacritics and numbers always come out right."""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from PIL import Image
from playwright.async_api import async_playwright

from app import content
from app.config import ASSETS_DIR, TEMPLATES_DIR

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1080, 1350
TEMPLATES = {"feature", "quote", "split", "photo", "offer", "number"}

KICKERS = {
    "feature": "Funcționalitate",
    "tip": "Sfatul săptămânii",
    "problem_solution": "Înainte și după",
    "club_type": "Pentru cluburi",
    "offer": "Prețuri",
    "number": "Pe scurt",
    "custom": "Abonix",
}

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
)


def screen_kind(screen: str | None) -> str:
    """desktop (landscape) -> browser frame, phone (tall) -> phone frame,
    tablet (portrait but not phone-tall, like the entrance tablet) -> tablet frame."""
    if not screen:
        return "desktop"
    path = ASSETS_DIR / "screens" / screen
    with Image.open(path) as im:
        ratio = im.height / im.width
    if ratio <= 1:
        return "desktop"
    return "phone" if ratio > 1.6 else "tablet"


def build_context(template: str, theme: str, subject: str, draft: dict[str, Any], photo_path: Path | None = None,
                  photo_credit: str | None = None) -> dict[str, Any]:
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template}")
    ctx: dict[str, Any] = {
        "assets": ASSETS_DIR.as_uri(),
        "template": template,
        "accent": draft.get("accent") or "indigo",
        "kicker": KICKERS.get(theme, "Abonix"),
        "headline": draft["headline"],
        "subline": draft["subline"],
        "points": draft.get("points") or [],
        "points_after": draft.get("points_after") or [],
    }
    if template == "feature":
        feat = content.feature(subject) or {}
        screens = feat.get("screens") or content.all_screens()
        screen = draft.get("screen") if draft.get("screen") in screens else screens[0]
        ctx.update(screen=screen, screen_kind=screen_kind(screen))
        ctx["kicker"] = feat.get("name", ctx["kicker"])
    elif template == "offer":
        off = content.offer(subject)
        if not off:
            raise ValueError(f"unknown offer {subject}")
        ctx["offer"] = off
    elif template == "number":
        ctx["fact"] = content.facts()[subject]
    elif template == "photo":
        if photo_path is None:
            raise ValueError("photo template needs a photo")
        ctx["photo_src"] = photo_path.resolve().as_uri()
        ctx["photo_credit"] = photo_credit
        if ctx["accent"] in ("dark", "gradient"):  # the photo gets a solid brand tint
            ctx["accent"] = "indigo"
    return ctx


def render_html(template: str, ctx: dict[str, Any]) -> str:
    return _env.get_template(f"{template}.html").render(**ctx)


async def html_to_jpegs(pages: list[tuple[str, Path]], quality: int = 90) -> list[Path]:
    """Renders several HTML pages to JPEGs with one browser."""
    outs = []
    # Written to disk (not set_content) so file:// assets resolve.
    with tempfile.TemporaryDirectory() as tmp:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--allow-file-access-from-files"])
            try:
                page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
                for i, (html, out) in enumerate(pages):
                    page_file = Path(tmp) / f"page{i}.html"
                    page_file.write_text(html, encoding="utf-8")
                    await page.goto(page_file.as_uri(), wait_until="networkidle")
                    await page.evaluate("document.fonts.ready")
                    png = await page.screenshot(type="png", full_page=False)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    img = Image.open(io.BytesIO(png)).convert("RGB")  # JPEG, no alpha (Instagram requirement)
                    img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
                    log.info("rendered %s", out)
                    outs.append(out)
            finally:
                await browser.close()
    return outs


async def html_to_jpeg(html: str, out: Path, quality: int = 90) -> Path:
    return (await html_to_jpegs([(html, out)], quality))[0]


async def render_post(template: str, theme: str, subject: str, draft: dict[str, Any], out: Path,
                      photo_path: Path | None = None, photo_credit: str | None = None) -> Path:
    ctx = build_context(template, theme, subject, draft, photo_path, photo_credit)
    return await html_to_jpeg(render_html(template, ctx), out)


def slide_pages(theme: str, draft: dict[str, Any], out_stem: Path) -> list[tuple[str, Path]]:
    """The slides after the cover: one per `slides` item, then the "link în bio" slide."""
    slides = draft.get("slides") or []
    total = len(slides) + 2  # cover + slides + final
    accent = draft.get("accent") or "indigo"
    base = {"assets": ASSETS_DIR.as_uri(), "accent": accent, "kicker": KICKERS.get(theme, "Abonix"), "total": total}
    pages = []
    for i, sl in enumerate(slides, start=2):
        screen = sl.get("screen")
        ctx = {**base, "template": "slide" + (" has-shot" if screen else ""), "index": i, "title": sl["title"],
               "body": sl["body"], "screen": screen, "screen_kind": screen_kind(screen) if screen else None}
        pages.append((render_html("slide", ctx), out_stem.with_name(f"{out_stem.name}-{i}.jpg")))
    ctx = {**base, "template": "cta", "index": total}
    pages.append((render_html("cta", ctx), out_stem.with_name(f"{out_stem.name}-{total}.jpg")))
    return pages


async def render_set(template: str, theme: str, subject: str, draft: dict[str, Any], cover: Path,
                     photo_path: Path | None = None, photo_credit: str | None = None,
                     with_slides: bool = False) -> list[Path]:
    """The cover, plus (for carousels and reels) the slides, all with one browser."""
    ctx = build_context(template, theme, subject, draft, photo_path, photo_credit)
    pages = [(render_html(template, ctx), cover)]
    if with_slides:
        pages += slide_pages(theme, draft, cover.with_suffix(""))
    return await html_to_jpegs(pages)
