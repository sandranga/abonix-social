"""One Claude call per draft: brand book + product sheet + today's theme in,
a validated `PostDraft` out."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app import content
from app.config import get_settings
from app.generate.schemas import PostDraft

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class GenerationError(RuntimeError):
    pass


@dataclass
class Brief:
    """Everything that varies per draft."""

    theme: str
    label: str
    template: str
    guidance: str
    subject: str
    subject_detail: dict[str, Any] | None = None
    recent: list[dict[str, str]] = field(default_factory=list)
    feedback: str | None = None
    previous: dict[str, Any] | None = None
    review_issues: list[str] = field(default_factory=list)
    format: str = "single"
    rules: list[str] = field(default_factory=list)            # your standing rules (/regula)
    past_feedback: list[str] = field(default_factory=list)    # instructions you gave on other drafts
    examples: list[dict[str, str]] = field(default_factory=list)  # approved posts that did well
    rejected: list[str] = field(default_factory=list)         # hooks of drafts you skipped
    season: dict[str, Any] | None = None
    stories: list[dict[str, Any]] = field(default_factory=list)


SYSTEM_RULES = """\
Ești copywriterul de social media al Abonix. Scrii o postare pe zi pentru pagina de
Facebook și contul de Instagram Abonix, în limba română.

Reguli:
- Respectă brand book-ul și folosește DOAR informațiile din fișa produsului. Dacă o
  informație nu e acolo, nu o scrie. Nu inventa funcționalități, prețuri, reduceri,
  clienți, rezultate sau statistici.
- Prețurile și cifrele de pe imagine le pune template-ul din fișa produsului; în
  caption poți menționa prețul exact din fișă.
- headline: maxim 6 cuvinte. subline: maxim 12 cuvinte. Se citesc pe telefon dintr-o privire.
- caption_ig: 300-900 de caractere, cu un hook în prima frază, paragrafe scurte și un
  call to action la final care trimite la „link în bio”. Fără hashtag-uri în text.
- caption_fb: poate fi la fel sau puțin diferit, tot cu „link în bio” la final.
- Niciun link în caption-uri: fără https://, www., „abonix.net” sau alte adrese web,
  fără număr de telefon. Doar „link în bio”.
- Nu repeta unghiul, hook-ul sau titlul ultimelor postări primite.
- Diacritice corecte cu virgulă: ș ț Ș Ț.
"""


def _system_blocks() -> list[dict[str, Any]]:
    # Stable prefix, cached: identical bytes every call.
    text = (
        SYSTEM_RULES
        + "\n<brand_book>\n" + content.brand_md() + "\n</brand_book>\n"
        + "\n<fisa_produsului>\n" + content.product_yaml_text() + "\n</fisa_produsului>\n"
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_user_message(b: Brief) -> str:
    parts = [
        f"Tema zilei: {b.label} (theme={b.theme}, template={b.template}).",
        f"Indicații: {b.guidance}",
        f"Subiectul de azi: {b.subject}",
    ]
    if b.subject_detail:
        parts.append("Detalii despre subiect:\n" + json.dumps(b.subject_detail, ensure_ascii=False, indent=1))
    if b.template == "feature":
        parts.append(
            "Alege `screen` din capturile acestei funcționalități: "
            + ", ".join((b.subject_detail or {}).get("screens", []))
        )
    if b.format == "carousel":
        parts.append(
            "Format: carusel. Coperta e imaginea obișnuită (headline, subline). Scrie în `slides` 2-4 "
            "slide-uri care continuă ideea, câte una pe slide (title max 6 cuvinte, body max 25), "
            "într-o ordine care te face să dai mai departe: problema, cum se rezolvă, ce câștigi. "
            "Slide-ul final cu „link în bio” se adaugă automat; nu-l scrie."
        )
    elif b.format == "reel":
        parts.append(
            "Format: reel (video scurt). Coperta (headline, subline) e primul cadru și trebuie să oprească "
            "scroll-ul. Scrie în `slides` 2-4 cadre scurte (title max 6 cuvinte, body max 20), câte o idee, "
            "citibile în 3 secunde fiecare. Cadrul final cu „link în bio” se adaugă automat."
        )
    else:
        parts.append("Format: o singură imagine. Lasă `slides` gol.")
    if b.season:
        parts.append(f"Context sezonier ({b.season['name']}), folosește-l doar dacă se potrivește "
                     f"natural cu subiectul: {b.season['hint']}")
    if b.stories:
        parts.append(
            "Povești reale cu acord (le poți folosi exact cum sunt, fără să adaugi nimic; numele clubului "
            "doar dacă name_ok e true):\n" + json.dumps(b.stories, ensure_ascii=False, indent=1)
        )
    if b.rules:
        parts.append("Regulile permanente ale editorului (respectă-le mereu):\n- " + "\n- ".join(b.rules))
    if b.past_feedback:
        parts.append(
            "Instrucțiuni date recent de editor la alte postări. Aplică-le pe cele generale "
            "(ton, lungime, stil), nu pe cele legate strict de o anumită postare:\n- "
            + "\n- ".join(b.past_feedback)
        )
    if b.examples:
        lines = [f"- [{e['theme']}] {e['headline']}: {e['caption']}" for e in b.examples]
        parts.append("Postări aprobate care au mers bine (stilul de urmat, nu le copia):\n" + "\n".join(lines))
    if b.rejected:
        parts.append("Unghiuri respinse recent de editor (evită-le):\n- " + "\n- ".join(b.rejected))
    if b.recent:
        lines = [f"- {r['date']} [{r['theme']}] {r['headline']} — {r['hook']}" for r in b.recent]
        parts.append("Ultimele postări (nu le repeta):\n" + "\n".join(lines))
    if b.previous:
        parts.append(
            "Varianta anterioară a acestei postări (scrie una nouă, diferită):\n"
            + json.dumps(b.previous, ensure_ascii=False, indent=1)
        )
    if b.review_issues:
        parts.append("Corectează aceste probleme găsite la verificare:\n- " + "\n- ".join(b.review_issues))
    if b.feedback:
        parts.append(f"Instrucțiuni de la editor (prioritare): {b.feedback}")
    parts.append("Scrie postarea.")
    return "\n\n".join(parts)


def _usage(resp: Any) -> dict[str, int]:
    u = resp.usage
    return {
        "input": u.input_tokens or 0,
        "output": u.output_tokens or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def client() -> anthropic.AsyncAnthropic:
    s = get_settings()
    return anthropic.AsyncAnthropic(api_key=s.anthropic_api_key or None, max_retries=3)


async def generate_draft(b: Brief, api: anthropic.AsyncAnthropic | None = None) -> tuple[PostDraft, dict[str, Any]]:
    s = get_settings()
    api = api or client()
    kwargs: dict[str, Any] = {}
    if s.claude_fallbacks:
        kwargs = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    try:
        resp = await api.beta.messages.parse(
            model=s.claude_model,
            max_tokens=16000,
            system=_system_blocks(),
            messages=[{"role": "user", "content": build_user_message(b)}],
            output_format=PostDraft,
            output_config={"effort": s.claude_effort},
            **kwargs,
        )
    except anthropic.APIStatusError as e:
        raise GenerationError(f"Claude API {e.status_code}: {e.message} (request {e.request_id})") from e
    except anthropic.APIConnectionError as e:
        raise GenerationError(f"Claude API unreachable: {e}") from e

    if resp.stop_reason == "refusal":
        raise GenerationError(f"Claude refused: {getattr(resp.stop_details, 'explanation', '')}")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise GenerationError(f"No usable output (stop_reason={resp.stop_reason}, request {resp._request_id})")
    log.info("draft generated by %s, usage=%s", resp.model, _usage(resp))
    return resp.parsed_output, {"model": resp.model, **_usage(resp)}
