"""Checks a draft before it reaches you: hard rules in code, then a cheap
Claude pass for tone, diacritics and promises the product does not make."""

from __future__ import annotations

import logging
import re
from typing import Any

import anthropic

from app import content
from app.config import get_settings
from app.generate.schemas import PostDraft, ReviewResult

log = logging.getLogger(__name__)

CEDILLA = {"ş": "ș", "ţ": "ț", "Ş": "Ș", "Ţ": "Ț"}
FORBIDDEN = [
    r"\brevoluționar\w*", r"\bdisruptiv\w*", r"\bnr\.?\s*1\b", r"\bgarantat\w*",
    r"\bnelimitat\w*", r"\binteligenț\w* artificial\w*",
    r"\bgratuit\w*", r"\bgratis\b", r"\blun[ăa] gratuit\w*", r"\bperioad[ăa] de prob[ăa]",
]
# "gratuit" is allowed when it is about the member's side (no app/account needed).
GRATUIT_OK_CONTEXT = re.compile(r"(check-in|cod(ul)? QR|fără aplicație|fără cont)", re.I)
# Case-sensitive: "ai" is an ordinary Romanian word ("ai nevoie").
FORBIDDEN_CASE = [r"\bAI\b"]
PRICE_RE = re.compile(r"(\d{2,4})\s*(?:RON|lei)\b", re.I)
# Captions carry no links: the call to action is always "link în bio".
LINK_RE = re.compile(r"https?://\S+|\bwww\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:net|ro|com|eu|org|io|app)\b\S*", re.I)
BIO_RE = re.compile(r"link(ul)? (din|în|in) bio", re.I)


def words(s: str) -> int:
    return len([w for w in re.split(r"\s+", s.strip()) if w])


def fix_cedilla(text: str) -> str:
    for bad, good in CEDILLA.items():
        text = text.replace(bad, good)
    return text


def normalize(d: PostDraft) -> PostDraft:
    """Silent, safe fixes: cedilla -> comma-below, hashtag formatting."""
    data = d.model_dump()
    for k, v in data.items():
        if isinstance(v, str):
            data[k] = fix_cedilla(v).strip()
        elif isinstance(v, list):
            data[k] = [fix_cedilla(x).strip() if isinstance(x, str) else x for x in v]

    def tags(lst: list[str]) -> list[str]:
        out = []
        for t in lst:
            t = "#" + re.sub(r"[^\wăâîșțĂÂÎȘȚ]", "", t.lstrip("#"))
            if len(t) > 1 and t.lower() not in [o.lower() for o in out]:
                out.append(t)
        return out

    data["hashtags_ig"] = tags(data["hashtags_ig"])
    data["hashtags_fb"] = tags(data["hashtags_fb"])
    for key in ("hashtags_ig", "hashtags_fb"):
        if "#abonix" not in [t.lower() for t in data[key]]:
            data[key].append("#abonix")
    return PostDraft.model_validate(data)


def rule_issues(d: PostDraft, template: str) -> list[str]:
    issues: list[str] = []
    if words(d.headline) > 6:
        issues.append(f"Titlul are {words(d.headline)} cuvinte (max 6): „{d.headline}”")
    if words(d.subline) > 12:
        issues.append(f"Subtitlul are {words(d.subline)} cuvinte (max 12).")
    for p in d.points + d.points_after:
        if words(p) > 8:
            issues.append(f"Punct prea lung (max 7 cuvinte): „{p}”")
    if template == "split" and (len(d.points) != 3 or len(d.points_after) != 3):
        issues.append("Split are nevoie de exact 3 puncte pe fiecare parte.")
    if template in ("feature", "offer") and len(d.points) > 3:
        issues.append("Maxim 3 puncte pe imagine.")
    if template == "photo" and not d.photo_query:
        issues.append("Lipsește photo_query pentru fotografie.")
    if not (150 <= len(d.caption_ig) <= 2000):
        issues.append(f"Caption Instagram are {len(d.caption_ig)} caractere (ținta 300-900, max 2000).")
    if len(d.caption_fb) > 3000:
        issues.append("Caption Facebook prea lung.")
    if not (3 <= len(d.hashtags_ig) <= 12):
        issues.append(f"{len(d.hashtags_ig)} hashtag-uri pe Instagram (5-10).")

    for where, cap in (("caption IG", d.caption_ig), ("caption FB", d.caption_fb)):
        for m in LINK_RE.finditer(cap):
            issues.append(f"Link în {where}: „{m.group(0)}”. Fără linkuri, doar „link în bio”.")
        if not BIO_RE.search(cap):
            issues.append(f"{where} nu trimite la „link în bio”.")

    texts = {
        "titlu": d.headline, "subtitlu": d.subline, "caption IG": d.caption_ig,
        "caption FB": d.caption_fb, "puncte": " | ".join(d.points + d.points_after),
    }
    allowed = content.allowed_prices()
    for where, t in texts.items():
        for m in PRICE_RE.finditer(t):
            if int(m.group(1)) not in allowed:
                issues.append(f"Preț necunoscut în {where}: „{m.group(0)}” (permise: {sorted(allowed)} RON)")
        for pat in FORBIDDEN:
            for m in re.finditer(pat, t, re.I):
                if "gratu" in m.group(0).lower() and GRATUIT_OK_CONTEXT.search(t):
                    continue
                issues.append(f"Formulare interzisă în {where}: „{m.group(0)}”")
        for pat in FORBIDDEN_CASE:
            for m in re.finditer(pat, t):
                issues.append(f"Formulare interzisă în {where}: „{m.group(0)}”")
    return issues


REVIEW_SYSTEM = """\
Ești editorul care verifică postările Abonix înainte de publicare. Verifică draftul
față de brand book și fișa produsului. Semnalează DOAR probleme reale:
- informații care nu sunt în fișa produsului (funcționalități, prețuri, oferte, cifre inventate);
- promisiuni pe care produsul nu le face, exagerări, formulări interzise;
- greșeli de română sau diacritice lipsă (ex. „sala” în loc de „sală” unde e cazul, „in” în loc de „în”);
- ton nepotrivit sau text greu de înțeles.
Nu semnala preferințe stilistice minore. Dacă e în regulă, ok=true și issues gol.
"""


async def claude_review(d: PostDraft, api: anthropic.AsyncAnthropic) -> tuple[ReviewResult, dict[str, Any]]:
    s = get_settings()
    system = [{
        "type": "text",
        "text": REVIEW_SYSTEM + "\n<brand_book>\n" + content.brand_md() + "\n</brand_book>\n<fisa_produsului>\n"
        + content.product_yaml_text() + "\n</fisa_produsului>",
        "cache_control": {"type": "ephemeral"},
    }]
    resp = await api.messages.parse(
        model=s.claude_review_model,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": "Draftul:\n" + d.model_dump_json(indent=1)}],
        output_format=ReviewResult,
        output_config={"effort": "low"},
    )
    if resp.parsed_output is None:
        return ReviewResult(ok=True, issues=["(verificarea automată nu a răspuns)"]), {}
    u = resp.usage
    return resp.parsed_output, {"model": resp.model, "input": u.input_tokens, "output": u.output_tokens}


async def review(d: PostDraft, template: str, api: anthropic.AsyncAnthropic | None) -> tuple[list[str], dict[str, Any]]:
    issues = rule_issues(d, template)
    usage: dict[str, Any] = {}
    if api is not None:
        try:
            r, usage = await claude_review(d, api)
            if not r.ok:
                issues += [f"Editor: {i}" for i in r.issues]
        except anthropic.APIError as e:  # review is advisory; never block on it
            log.warning("review call failed: %s", e)
            issues.append("(verificarea automată a eșuat, citește cu atenție)")
    return issues, usage
