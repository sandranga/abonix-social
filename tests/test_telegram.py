from pathlib import Path

from app.approve.telegram import TelegramBot, TelegramNotifier
from tests.conftest import FakeClaude
from tests.test_pipeline import MONDAY, THURSDAY, make


class FakeAPI:
    def __init__(self):
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.next_id = 100

    async def call(self, method, files=None, **params):
        self.calls.append((method, params, files))
        if method == "sendMediaGroup":
            out = [{"message_id": self.next_id + i} for i in range(len(params["media"]))]
            self.next_id += len(out)
            return out
        self.next_id += 1
        return {"message_id": self.next_id}

    def methods(self):
        return [c[0] for c in self.calls]


async def test_carousel_draft_is_an_album_then_buttons(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    post = await p.generate_for(MONDAY)
    api = FakeAPI()
    chat, ids = await TelegramNotifier(api, [42]).send_draft(post, None)
    album = next(c for c in api.calls if c[0] == "sendMediaGroup")
    assert len(album[1]["media"]) == 4 and album[1]["media"][0]["media"] == "attach://p0"
    assert "CARUSEL" in album[1]["media"][0]["caption"] and "caption" not in album[1]["media"][1]
    assert set(album[2]) == {"p0", "p1", "p2", "p3"}
    assert api.methods()[-1] == "sendMessage" and "reply_markup" in api.calls[-1][1]
    assert len(ids) == 5 and chat == 42


async def test_reel_draft_is_a_video(settings, tmp_path, monkeypatch):
    from app import pipeline as pipeline_mod

    async def make_reel(images, out):
        out.write_bytes(b"mp4")
        return out

    monkeypatch.setattr(pipeline_mod, "make_reel", make_reel)
    p, _ = make(settings, tmp_path)
    post = await p.generate_for(MONDAY.replace(day=10))  # Tuesday
    api = FakeAPI()
    await TelegramNotifier(api, [42]).send_draft(post, None)
    assert api.methods()[0] == "sendVideo" and "REEL" in api.calls[0][1]["caption"]


async def test_single_draft_is_a_photo(settings, tmp_path):
    p, _ = make(settings, tmp_path)
    post = await p.generate_for(THURSDAY)
    api = FakeAPI()
    await TelegramNotifier(api, [42]).send_draft(post, None)
    assert api.methods()[0] == "sendPhoto"


async def test_rule_and_report_commands(settings, tmp_path):
    p, _ = make(settings, tmp_path, claude=FakeClaude())
    api = FakeAPI()
    bot = TelegramBot(api, p, [42])

    async def say(text):
        await bot.handle({"message": {"chat": {"id": 42}, "text": text}})
        return api.calls[-1][1]["text"]

    assert "Regula 1 salvată" in await say("/regula fără emoji în titlu")
    assert "1. fără emoji în titlu" in await say("/reguli")
    assert "Am șters regula" in await say("/sterge_regula 1")
    assert "Folosește" in await say("/sterge_regula 7")
    assert "Nicio regulă" in await say("/reguli")
    assert "Nicio postare cu statistici" in await say("/raport")


async def test_reply_feedback_is_stored(settings, tmp_path):
    p, _ = make(settings, tmp_path, claude=FakeClaude())
    post = await p.generate_for(MONDAY)
    api = FakeAPI()
    bot = TelegramBot(api, p, [42])
    await bot.handle({"message": {"chat": {"id": 42}, "text": "mai scurt",
                                  "reply_to_message": {"message_id": post.telegram_message_ids[-1]}}})
    assert p.get(post.id).feedback == ["mai scurt"]


def test_parse_post_request():
    from app.approve.telegram import parse_post_request

    assert parse_post_request("carusel 3 greșeli în Excel") == ("carousel", "quote", "3 greșeli în Excel")
    assert parse_post_request("Reel de ce memento-urile merg") == ("reel", "quote", "de ce memento-urile merg")
    assert parse_post_request("foto cluburi de înot") == ("single", "photo", "cluburi de înot")
    assert parse_post_request("anunță programul de Crăciun") == ("single", "quote", "anunță programul de Crăciun")
    assert parse_post_request("carusel") == ("single", "quote", "carusel")  # a format word alone is the request


async def test_post_command(settings, tmp_path):
    claude = FakeClaude()
    p, n = make(settings, tmp_path, claude=claude)
    api = FakeAPI()
    bot = TelegramBot(api, p, [42])

    await bot.handle({"message": {"chat": {"id": 42}, "text": "/post"}})
    assert "Scrie ce postare vrei" in api.calls[-1][1]["text"]

    await bot.handle({"message": {"chat": {"id": 42}, "text": "/post carusel 3 greșeli când ții abonamentele în Excel"}})
    assert "un carusel" in api.calls[-1][1]["text"]
    post = p.get(n.drafts[-1])
    assert post.theme == "custom" and post.extra and post.format == "carousel" and post.template == "quote"
    assert post.subject == "3 greșeli când ții abonamentele în Excel"
    sent = claude.beta.messages.calls[-1]["messages"][0]["content"]
    assert "«3 greșeli când ții abonamentele în Excel»" in sent and "Format: carusel" in sent
