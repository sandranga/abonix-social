from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import Settings  # noqa: E402
from app.generate.schemas import PostDraft, ReviewResult  # noqa: E402


def good_draft(**over: Any) -> PostDraft:
    base = dict(
        headline="Nu mai pierde reînnoiri",
        subline="Abonix le amintește membrilor înainte să le expire abonamentul.",
        points=[], points_after=[],
        caption_ig=("Câți membri ți-au expirat luna trecută fără să-i suni? Cu Abonix pleacă singure "
                    "memento-uri pe email și SMS cu 7 și 3 zile înainte. Planul Growth costă 215 RON pe lună. "
                    "Detalii și prețuri: link în bio."),
        caption_fb="Memento-uri automate pe email și SMS. Detalii: link în bio.",
        hashtags_ig=["#salafitness", "#gestiunesala", "#abonamente", "#fitnessromania", "#abonix"],
        hashtags_fb=["#abonix"],
        alt_text="Ecranul de setări al memento-urilor din Abonix.",
        screen="memento-setari.webp", photo_query="ballet dancers studio", accent="indigo",
        hook="memento-urile automate salvează reînnoirile",
    )
    base.update(over)
    return PostDraft(**base)


class FakeMessages:
    def __init__(self, outputs: list[Any]):
        self.outputs = outputs
        self.calls: list[dict] = []

    async def parse(self, **kw):
        self.calls.append(kw)
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return SimpleNamespace(
            parsed_output=out, stop_reason="end_turn", stop_details=None, model=kw["model"], _request_id="req_test",
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
        )


class FakeClaude:
    """Mimics AsyncAnthropic: beta.messages.parse writes, messages.parse reviews."""

    def __init__(self, drafts: list[PostDraft] | None = None, review: ReviewResult | None = None):
        self.beta = SimpleNamespace(messages=FakeMessages(drafts or [good_draft()]))
        self.messages = FakeMessages([review or ReviewResult(ok=True, issues=[])])


class FakeNotifier:
    def __init__(self):
        self.alerts: list[str] = []
        self.infos: list[str] = []
        self.drafts: list[int] = []
        self.marks: list[tuple[int, str, str]] = []

    async def alert(self, text): self.alerts.append(text)
    async def info(self, text): self.infos.append(text)

    async def send_draft(self, post, when):
        self.drafts.append(post.id)
        return 111, [1000 + post.id * 10, 1001 + post.id * 10]

    async def mark(self, post, note=""): self.marks.append((post.id, post.status.value, note))


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(_env_file=None, data_dir=tmp_path, dry_run=True, anthropic_api_key="x")
    s.media_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(s.db_url)
    return s
