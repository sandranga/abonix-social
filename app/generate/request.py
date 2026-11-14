"""Turns a /post request into a plan: which format, which cover template and
which feature / plan / number it is about. One cheap Claude call (the review
model); if it fails, keywords anywhere in the request decide."""

from __future__ import annotations

import logging
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from app import content
from app.config import get_settings

log = logging.getLogger(__name__)

Format = Literal["single", "carousel", "reel"]
Template = Literal["feature", "quote", "split", "photo", "offer", "number"]


class RequestPlan(BaseModel):
    format: Format = Field(description="single = o imagine, carousel = mai multe slide-uri, reel = video scurt.")
    template: Template = Field(description=(
        "Coperta: feature = captură reală din aplicație (alege-o când e vorba de o funcționalitate sau "
        "cererea pomenește aplicația, ecrane, capturi); photo = fotografie de stoc (când cere o poză, oameni, "
        "un tip de club); offer = card cu prețul unui plan; number = o cifră din facts; split = înainte/după "
        "(Excel/caiet vs Abonix); quote = doar text mare (anunțuri, sfaturi, citate)."))
    subject: Optional[str] = Field(default=None, description=(
        "feature: cheia funcționalității; offer: cheia planului; number: cheia cifrei. Altfel null."))


SYSTEM = """\
Primești cererea editorului pentru o postare Abonix pe Facebook și Instagram și alegi
cum o facem. Respectă exact ce cere: dacă cere reel, video sau clip -> reel; carusel,
slide-uri, mai multe imagini -> carousel; altfel single. Dacă cererea e despre o
funcționalitate sau pomenește aplicația, capturi, ecrane -> template feature cu cheia
ei. Dacă cere o poză, fotografie, oameni -> photo. Preț, plan, abonament Abonix -> offer.
Alege doar chei din listele de mai jos."""


def _choices() -> str:
    feats = "\n".join(f"- {f['key']}: {f['name']}" for f in content.product()["features"])
    plans = "\n".join(f"- {p['key']}: {p['name']} ({p['price_ron']} RON)" for p in content.product()["plans"])
    facts = "\n".join(f"- {k}: {v.get('number', '')} {v.get('caption', '')}" for k, v in content.facts().items())
    return (f"Funcționalități (template feature):\n{feats}\n\nPlanuri (template offer):\n{plans}\n"
            f"- personal_trainer: Personal Trainer\n\nCifre (template number):\n{facts}")


def _valid(plan: RequestPlan) -> RequestPlan:
    """Fall back to a text cover when the chosen key doesn't exist."""
    ok = {
        "feature": lambda k: content.feature(k) is not None,
        "offer": lambda k: content.offer(k) is not None,
        "number": lambda k: k in content.facts(),
    }
    check = ok.get(plan.template)
    if check and not (plan.subject and check(plan.subject)):
        log.warning("request plan: unknown %s %r, using a text cover", plan.template, plan.subject)
        return plan.model_copy(update={"template": "quote", "subject": None})
    if not check:
        plan = plan.model_copy(update={"subject": None})
    return plan


MUSIC_WORDS = ("muzic", "melodi", "piesa", "piese", "cantec", "music", "song", "sunet", "sound",
               "audio", "trending", "trend", "coloana sonora")


def wants_music(request: str | None) -> bool:
    """Did the request ask for music? It can only be added in the Instagram app."""
    if not request:
        return False
    plain = request.lower().translate(str.maketrans("ăâîșşțţ", "aaisstt"))
    return any(w in plain for w in MUSIC_WORDS)


def guess(request: str) -> RequestPlan:
    """No Claude: keywords anywhere in the request."""
    plain = request.lower().translate(str.maketrans("ăâîșşțţ", "aaisstt"))
    if any(w in plain for w in ("reel", "video", "clip")):
        fmt = "reel"
    elif any(w in plain for w in ("carusel", "carousel", "slide")):
        fmt = "carousel"
    else:
        fmt = "single"
    if any(w in plain for w in ("foto", "poza", "fotografie", "imagine cu oameni")):
        return RequestPlan(format=fmt, template="photo")
    # The feature whose name shares the most words with the request (at least
    # two, or all of a short name); the first one wins a tie.
    best, best_score = None, 0
    for f in content.product()["features"]:
        name = f["name"].lower().translate(str.maketrans("ăâîșşțţ", "aaisstt"))
        words = [w for w in name.replace(",", " ").split() if len(w) > 4]
        score = sum(w in plain for w in words)
        if f["key"] in plain.split():
            score += 2
        if score > best_score and (score >= 2 or score == len(words)):
            best, best_score = f["key"], score
    if best:
        return RequestPlan(format=fmt, template="feature", subject=best)
    return RequestPlan(format=fmt, template="quote")


async def interpret(request: str, api: anthropic.AsyncAnthropic | None) -> RequestPlan:
    if api is None:
        return guess(request)
    try:
        resp = await api.messages.parse(
            model=get_settings().claude_review_model,
            max_tokens=1000,
            system=SYSTEM + "\n\n" + _choices(),
            messages=[{"role": "user", "content": f"Cererea editorului: «{request}»"}],
            output_format=RequestPlan,
            output_config={"effort": "low"},
        )
        if resp.parsed_output is None:
            return guess(request)
        return _valid(resp.parsed_output)
    except (anthropic.APIError, ValueError) as e:
        log.warning("request plan failed (%s), guessing from keywords", e)
        return guess(request)
