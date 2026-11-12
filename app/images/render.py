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
}

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
)


def screen_kind(screen: str | None) -> str:
    if not screen:
        return "desktop"
    path = ASSETS_DIR / "screens" / screen
    with Image.open(path) as im:
        return "phone" if im.height > im.width else "desktop"


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
        if ctx["accent"] == "dark":
            ctx["accent"] = "indigo"
    return ctx


def render_html(template: str, ctx: dict[str, Any]) -> str:
    return _env.get_template(f"{template}.html").render(**ctx)


async def html_to_jpeg(html: str, out: Path, quality: int = 90) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written to disk (not set_content) so file:// assets resolve.
    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "post.html"
        page_file.write_text(html, encoding="utf-8")
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--allow-file-access-from-files"])
            try:
                page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
                await page.goto(page_file.as_uri(), wait_until="networkidle")
                await page.evaluate("document.fonts.ready")
                png = await page.screenshot(type="png", full_page=False)
            finally:
                await browser.close()
    img = Image.open(io.BytesIO(png)).convert("RGB")  # JPEG, no alpha (Instagram requirement)
    img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    log.info("rendered %s", out)
    return out


async def render_post(template: str, theme: str, subject: str, draft: dict[str, Any], out: Path,
                      photo_path: Path | None = None, photo_credit: str | None = None) -> Path:
    ctx = build_context(template, theme, subject, draft, photo_path, photo_credit)
    return await html_to_jpeg(render_html(template, ctx), out)
