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


@dataclass(frozen=True)
class DayPlan:
    weekday: int
    theme: str
    label: str
    template: str
    guidance: str
    subjects: list[str]


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


def all_screens() -> list[str]:
    out: list[str] = []
    for f in product()["features"]:
        for s in f.get("screens", []):
            if s not in out:
                out.append(s)
    return out
