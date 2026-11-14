from pathlib import Path

from app.approve.telegram import TelegramBot, TelegramNotifier, keyboard
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

    async def make_reel(images, out, music=None):
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


async def test_post_command_hands_the_whole_request_over(settings, tmp_path, monkeypatch):
    from app import pipeline as pipeline_mod
    from app.generate.request import RequestPlan

    async def make_reel(images, out, music=None):
        out.write_bytes(b"mp4")
        return out

    monkeypatch.setattr(pipeline_mod, "make_reel", make_reel)

    claude = FakeClaude(request_plan=RequestPlan(format="reel", template="feature", subject="kiosk"))
    p, n = make(settings, tmp_path, claude=claude)
    api = FakeAPI()
    bot = TelegramBot(api, p, [42])

    await bot.handle({"message": {"chat": {"id": 42}, "text": "/post"}})
    assert "Scrie ce postare vrei" in api.calls[-1][1]["text"]

    request = "vreau un reel despre tableta de la intrare, cu capturi din aplicație"
    await bot.handle({"message": {"chat": {"id": 42}, "text": "/post " + request}})
    read = next(c for c in claude.messages.calls if c["output_format"].__name__ == "RequestPlan")
    # the request was read as a whole, "reel" in the middle included
    assert request in read["messages"][0]["content"]
    post = p.get(n.drafts[-1])
    assert (post.format, post.template, post.subject, post.request) == ("reel", "feature", "kiosk", request)


async def test_manual_publish_button(settings, tmp_path):
    from app.models import PostStatus

    p, _ = make(settings, tmp_path, claude=FakeClaude())
    post = await p.generate_for(MONDAY)  # a carousel
    api = FakeAPI()
    bot = TelegramBot(api, p, [42])
    buttons = [b["callback_data"] for row in keyboard(post)["inline_keyboard"] for b in row]
    assert f"m:{post.id}:{post.revision}" in buttons

    await bot.on_callback({"id": "cb", "message": {"chat": {"id": 42}}, "data": f"m:{post.id}:{post.revision}"})
    docs = next(c for c in api.calls if c[0] == "sendMediaGroup")
    assert [m["type"] for m in docs[1]["media"]] == ["document"] * 4  # full quality, in carousel order
    texts = [c[1]["text"] for c in api.calls if c[0] == "sendMessage"]
    assert any(post.content["caption_ig"] in t for t in texts) and any(post.content["caption_fb"] in t for t in texts)
    assert any("Pași" in t for t in texts)

    assert p.get(post.id).status == PostStatus.manual
    assert await p.publish_due() == []                       # the bot never publishes it
    assert (await p.generate_for(MONDAY)).id == post.id      # it counts as the day's post


async def test_music_request_says_so_and_keeps_the_reel_silent(settings, tmp_path, monkeypatch):
    from app import pipeline as pipeline_mod
    from app.generate.request import RequestPlan, wants_music

    assert wants_music("un reel cu o melodie din trending") and wants_music("cu muzică pe fundal")
    assert not wants_music("un carusel despre memento-uri")

    used = []

    async def make_reel(images, out, music=None):
        used.append(music)
        out.write_bytes(b"mp4")
        return out

    monkeypatch.setattr(pipeline_mod, "make_reel", make_reel)
    monkeypatch.setattr(pipeline_mod, "pick_music", lambda seed: Path("assets/music/track.mp3"))
    claude = FakeClaude(request_plan=RequestPlan(format="reel", template="feature", subject="kiosk"))
    p, n = make(settings, tmp_path, claude=claude)
    api = FakeAPI()
    bot = TelegramBot(api, p, [42])
    await bot.handle({"message": {"chat": {"id": 42}, "text": "/post un reel despre kiosk, cu o melodie din trending"}})
    first = [c[1]["text"] for c in api.calls if c[0] == "sendMessage"][0]
    assert "Muzica nu se poate pune prin API" in first  # said before anything is generated
    assert used == [None]                                # no library track under the music you'll add

    post = p.get(n.drafts[-1])
    api2 = FakeAPI()
    await TelegramNotifier(api2, [42]).send_draft(post, None)
    assert "📲 Public eu din aplicație" in api2.calls[0][1]["caption"]  # the draft repeats it
