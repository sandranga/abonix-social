"""Loads the brand book, product sheet, theme calendar and facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

import yaml

from app.config import CONTENT_DIR


@lru_cache
def brand_md() -> str:
    return (CONTENT_DIR / "brand.md").read_text(encoding="utf-8")


def _yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONTENT_DIR / name).read_text(encoding="utf-8"))


@lru_cache
def product() -> dict[str, Any]:
    return _yaml("product.yaml")


@lru_cache
def product_yaml_text() -> str:
    return (CONTENT_DIR / "product.yaml").read_text(encoding="utf-8")


@lru_cache
def calendar() -> dict[str, Any]:
    return _yaml("calendar.yaml")


@lru_cache
def facts() -> dict[str, dict[str, str]]:
    return _yaml("facts.yaml")["facts"]


FORMATS = ("single", "carousel", "reel")


@dataclass(frozen=True)
class DayPlan:
    weekday: int
    theme: str
    label: str
    template: str
    guidance: str
    subjects: list[str]
    format: str = "single"


CUSTOM = "custom"


def custom_plan(request: str, fmt: str = "single", template: str = "quote",
                subject: str | None = None) -> DayPlan:
    """A one-off plan for /post: your request is the brief. `subject` is the
    feature / plan / number key when the cover needs one."""
    guidance = (
        "Postare cerută acum de editor. Cererea lui, de respectat întocmai: «" + request.strip() + "». "
        "Respectă brand book-ul și fișa produsului ca la orice postare: dacă cererea presupune ceva "
        "ce nu e în fișă (funcționalități, prețuri, oferte, cifre), nu inventa; scrie doar ce e adevărat."
    )
    # How this kind of cover is written, borrowed from the calendar day that uses it.
    day = next((weekday_plan(wd) for wd in range(7)
                if weekday_plan(wd) and weekday_plan(wd).template == template), None)
    if day and template != "quote":
        guidance += " Cum se scrie acest tip de imagine: " + day.guidance
    if template == "photo" and "photo_query" not in guidance:
        guidance += (" Scrie și `photo_query`: o căutare în engleză pentru o fotografie de stoc reală, "
                     "fără text în ea, potrivită cu subiectul.")
    return DayPlan(weekday=-1, theme=CUSTOM, label="Postare la cerere", template=template,
                   guidance=guidance, subjects=[subject or request.strip()], format=fmt)


def season(month: int) -> dict[str, Any] | None:
    """The seasonal context for a month (calendar.yaml -> seasons)."""
    return next((s for s in calendar().get("seasons") or [] if month in s.get("months", [])), None)


@lru_cache
def stories() -> list[dict[str, Any]]:
    """Real club stories that may be used: only those with written consent."""
    path = CONTENT_DIR / "stories.yaml"
    if not path.is_file():
        return []
    return [s for s in (_yaml("stories.yaml").get("stories") or []) if s.get("consent") is True]


def day_plan(d: date) -> DayPlan | None:
    """The theme for a date, or None on days without a post."""
    return weekday_plan(d.weekday())


def theme_plan(theme: str) -> DayPlan | None:
    """The calendar entry for a theme (extra posts can borrow another day's)."""
    for wd in range(7):
        plan = weekday_plan(wd)
        if plan and plan.theme == theme:
            return plan
    return None


def weekday_plan(wd: int) -> DayPlan | None:
    """The theme for a weekday (0 = Monday), or None if it has no post."""
    entry = calendar()["days"].get(wd)
    if not entry or entry.get("theme") == "none":
        return None
    return DayPlan(
        weekday=wd,
        theme=entry["theme"],
        label=entry["label"],
        template=entry["template"],
        guidance=entry["guidance"].strip(),
        subjects=list(entry.get("subjects") or []),
        format=entry.get("format", "single"),
    )


def feature(key: str) -> dict[str, Any] | None:
    return next((f for f in product()["features"] if f["key"] == key), None)


def offer(key: str) -> dict[str, Any] | None:
    """A plan (essential/growth/business) or the personal trainer offer."""
    if key == "personal_trainer":
        pt = dict(product()["personal_trainer"])
        pt["key"] = key
        pt["limit_label"] = f"până la {pt['client_limit']} de clienți"
        return pt
    plan = next((p for p in product()["plans"] if p["key"] == key), None)
    if plan:
        plan = dict(plan)
        plan["limit_label"] = f"până la {plan['member_limit']} de membri"
    return plan


def allowed_prices() -> set[int]:
    """Every RON amount that may legitimately appear in a post."""
    p = product()
    prices = {plan["price_ron"] for plan in p["plans"]}
    pt = p["personal_trainer"]
    prices |= {pt["price_ron"], pt["reminders_addon_ron"], pt["price_ron"] + pt["reminders_addon_ron"]}
    return prices


def screens_by_feature() -> dict[str, list[str]]:
    """Feature name -> its screenshots, for choosing one per slide."""
    return {f["name"]: list(f.get("screens", [])) for f in product()["features"] if f.get("screens")}


def all_screens() -> list[str]:
    out: list[str] = []
    for f in product()["features"]:
        for s in f.get("screens", []):
            if s not in out:
                out.append(s)
    return out
