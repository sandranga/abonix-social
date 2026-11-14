"""Approval over Telegram. Long polling (getUpdates), so the server needs no
public endpoint. Only chats in TELEGRAM_ALLOWED_CHAT_IDS can act."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app.config import get_settings
from app.images.providers import credit_line
from app.models import Post, PostStatus

if TYPE_CHECKING:  # pragma: no cover
    from app.pipeline import Pipeline

log = logging.getLogger(__name__)

PHOTO_CAPTION_MAX = 1024
MESSAGE_MAX = 4096

STATUS_LABEL = {
    PostStatus.generating: "⏳ Se generează",
    PostStatus.draft: "📝 Așteaptă aprobarea",
    PostStatus.approved: "✅ Aprobat",
    PostStatus.publishing: "📤 Se publică",
    PostStatus.published: "🎉 Publicat",
    PostStatus.failed: "❌ Eșuat",
    PostStatus.skipped: "⏭ Sărit",
    PostStatus.expired: "⌛ Expirat",
}

HELP = """Comenzi:
/azi – generează acum draftul pentru azi
/genereaza AAAA-LL-ZZ – generează draftul pentru o dată
/extra – încă o postare azi, chiar dacă cea de azi e deja aprobată/publicată
/extra joi – postare extra cu tema altei zile (luni … sâmbătă)
/post TEXT – o postare scrisă acum după cererea ta. Spune ce vrei: reel,
  carusel, o poză, o funcționalitate cu capturi din aplicație, un plan cu preț…
  de ex. /post reel despre tableta de la intrare, cu capturi din aplicație
/raport – ce a mers în ultimele 30 de zile (reach, salvări, distribuiri)
/regula TEXT – o regulă pe care o respect la fiecare postare
/reguli – regulile tale · /sterge_regula N – șterge regula N
/status – ultimele postări și ce urmează
/pauza – oprește generarea și publicarea automată
/reia – pornește din nou
/verifica – verifică token-ul Meta și limita Instagram
/help – lista asta

La un draft: apasă un buton sau răspunde (reply) la mesajul lui cu instrucțiuni,
de ex. „titlul mai scurt, fără emoji”, și îl regenerez cu ele. Țin minte
instrucțiunile și le aplic și la postările următoare când sunt generale."""


ZILE = ["luni", "marți", "miercuri", "joi", "vineri", "sâmbătă", "duminică"]


def parse_weekday(arg: str) -> int | None:
    """'joi', 'Marti', 'sâmbătă', 'sambata' -> 0..6; anything else -> None."""
    plain = str.maketrans("ăâîșşțţ", "aaisstt")
    a = arg.strip().lower().translate(plain)
    return next((i for i, z in enumerate(ZILE) if z.translate(plain) == a), None)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class TelegramAPI:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = http or httpx.AsyncClient(timeout=70)

    async def call(self, method: str, files: dict[str, Any] | None = None, **params: Any) -> Any:
        data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                for k, v in params.items() if v is not None}
        if files:
            r = await self.http.post(f"{self.base}/{method}", data=data, files=files)
        else:
            r = await self.http.post(f"{self.base}/{method}", data=data)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method}: {body.get('description')}")
        return body["result"]


def keyboard(post: Post) -> dict[str, Any]:
    tag = f"{post.id}:{post.revision}"
    rows = [
        [{"text": "✅ Aprob", "callback_data": f"a:{tag}"}, {"text": "⏭ Sar peste", "callback_data": f"s:{tag}"}],
        [{"text": "🔄 Regenerează", "callback_data": f"r:{tag}"}],
    ]
    if post.template in ("photo", "feature"):
        rows[1].append({"text": "🖼 Altă imagine", "callback_data": f"i:{tag}"})
    return {"inline_keyboard": rows}


class TelegramNotifier:
    """What the pipeline calls to talk to you."""

    def __init__(self, api: TelegramAPI, chat_ids: list[int]):
        self.api = api
        self.chat_ids = chat_ids

    async def alert(self, text: str) -> None:
        for chat in self.chat_ids:
            try:
                await self.api.call("sendMessage", chat_id=chat, text=_truncate("⚠️ " + text, MESSAGE_MAX))
            except Exception as e:  # never let an alert crash the caller
                log.error("telegram alert failed: %s", e)

    async def info(self, text: str) -> None:
        for chat in self.chat_ids:
            await self.api.call("sendMessage", chat_id=chat, text=_truncate(text, MESSAGE_MAX))

    async def send_draft(self, post: Post, when: datetime | None) -> tuple[int, list[int]] | None:
        if not self.chat_ids:
            log.warning("no TELEGRAM_ALLOWED_CHAT_IDS; draft %s not sent", post.id)
            return None
        chat = self.chat_ids[0]
        c = post.content
        fmt_label = {"carousel": " · CARUSEL", "reel": " · REEL"}.get(post.format or "single", "")
        head = [f"Draft #{post.id} (v{post.revision}){' · EXTRA' if post.extra else ''}{fmt_label}"
                f" · {ZILE[post.for_date.weekday()]} {post.for_date:%d.%m}",
                f"Temă: {post.theme} · {post.subject}"]
        if when:
            head.append(f"Se publică la: {when:%d.%m %H:%M} după aprobare")
        if post.review_issues:
            head.append("\n⚠️ De verificat:\n• " + "\n• ".join(post.review_issues))
        if credit_line(post.photo):
            head.append(f"\nFoto: {credit_line(post.photo)}")
        ids: list[int] = []
        fmt = post.format or "single"
        caption = _truncate("\n".join(head), PHOTO_CAPTION_MAX)
        media = [Path(m) for m in (post.media_paths or [post.image_path])]
        if fmt == "reel":
            with open(media[0], "rb") as f:
                msg = await self.api.call("sendVideo", chat_id=chat, caption=caption, supports_streaming="true",
                                          files={"video": (media[0].name, f, "video/mp4")})
            ids.append(msg["message_id"])
        elif fmt == "carousel" and len(media) > 1:
            handles = [open(m, "rb") for m in media[:10]]
            try:
                album = [{"type": "photo", "media": f"attach://p{i}", **({"caption": caption} if i == 0 else {})}
                         for i in range(len(handles))]
                msgs = await self.api.call("sendMediaGroup", chat_id=chat, media=album,
                                           files={f"p{i}": (media[i].name, h, "image/jpeg")
                                                  for i, h in enumerate(handles)})
            finally:
                for h in handles:
                    h.close()
            ids += [m["message_id"] for m in msgs]
        else:
            with open(post.image_path, "rb") as f:
                msg = await self.api.call("sendPhoto", chat_id=chat, caption=caption,
                                          files={"photo": (Path(post.image_path).name, f, "image/jpeg")})
            ids.append(msg["message_id"])
        ig = c["caption_ig"] + "\n\n" + " ".join(c["hashtags_ig"])
        fb = c["caption_fb"] + ("\n\n" + " ".join(c["hashtags_fb"]) if c["hashtags_fb"] else "")
        text = f"📸 Instagram:\n{ig}\n\n📘 Facebook:\n{fb}"
        if len(text) > MESSAGE_MAX:
            m = await self.api.call("sendMessage", chat_id=chat, text=_truncate(f"📸 Instagram:\n{ig}", MESSAGE_MAX))
            ids.append(m["message_id"])
            text = _truncate(f"📘 Facebook:\n{fb}", MESSAGE_MAX)
        m = await self.api.call("sendMessage", chat_id=chat, text=text, reply_markup=keyboard(post))
        ids.append(m["message_id"])
        return chat, ids

    async def mark(self, post: Post, note: str = "") -> None:
        """Replace the buttons under a draft with its status."""
        if not post.telegram_chat_id or not post.telegram_message_ids:
            return
        last = post.telegram_message_ids[-1]
        label = STATUS_LABEL.get(post.status, post.status.value)
        try:
            await self.api.call("editMessageReplyMarkup", chat_id=post.telegram_chat_id, message_id=last,
                                reply_markup={"inline_keyboard": []})
            await self.api.call("sendMessage", chat_id=post.telegram_chat_id, reply_to_message_id=last,
                                text=f"{label} · #{post.id} {note}".strip())
        except Exception as e:
            log.warning("could not update telegram message: %s", e)


class TelegramBot:
    """Polls Telegram and routes buttons/commands to the pipeline."""

    def __init__(self, api: TelegramAPI, pipeline: Pipeline, allowed: list[int]):
        self.api = api
        self.pipeline = pipeline
        self.allowed = set(allowed)
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        offset = self.pipeline.kv_get("telegram_offset")
        offset = int(offset) if offset else None
        await self.api.call("setMyCommands", commands=[
            {"command": c, "description": d} for c, d in [
                ("azi", "Generează draftul de azi"), ("extra", "Încă o postare azi"),
                ("post", "O postare după cererea ta"),
                ("status", "Ultimele postări"), ("raport", "Ce a mers în ultimele 30 de zile"),
                ("reguli", "Regulile tale pentru postări"),
                ("pauza", "Oprește automatizarea"), ("reia", "Pornește automatizarea"),
                ("verifica", "Verifică Meta"), ("help", "Ajutor")]
        ])
        log.info("telegram bot polling")
        while True:
            try:
                updates = await self.api.call("getUpdates", offset=offset, timeout=50,
                                              allowed_updates=["message", "callback_query"])
            except Exception as e:
                log.warning("getUpdates failed: %s; retrying in 10s", e)
                await asyncio.sleep(10)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self.pipeline.kv_set("telegram_offset", str(offset))
                # Generation takes ~a minute; keep polling meanwhile.
                task = asyncio.create_task(self._safe_handle(u))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _safe_handle(self, u: dict[str, Any]) -> None:
        try:
            await self.handle(u)
        except Exception as e:
            log.exception("update handling failed")
            chat = (u.get("message") or u.get("callback_query", {}).get("message") or {}).get("chat", {}).get("id")
            if chat in self.allowed:
                try:
                    await self.api.call("sendMessage", chat_id=chat, text=f"❌ Eroare: {e}")
                except Exception:
                    pass

    async def handle(self, u: dict[str, Any]) -> None:
        if "callback_query" in u:
            return await self.on_callback(u["callback_query"])
        msg = u.get("message")
        if not msg:
            return
        chat = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if chat not in self.allowed:
            if text.startswith("/start"):
                await self.api.call("sendMessage", chat_id=chat, text=(
                    f"Salut! ID-ul acestui chat este {chat}.\n"
                    f"Pune-l în TELEGRAM_ALLOWED_CHAT_IDS în .env și repornește containerul."))
            return
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to and text and not text.startswith("/"):
            post = self.pipeline.find_by_message(reply_to)
            if post:
                await self.api.call("sendMessage", chat_id=chat, text=f"🔄 Regenerez #{post.id} cu instrucțiunile tale…")
                await self.pipeline.regenerate(post.id, feedback=text)
                return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help"):
            await self.api.call("sendMessage", chat_id=chat, text=HELP)
        elif cmd == "/azi":
            await self.api.call("sendMessage", chat_id=chat, text="⏳ Generez draftul de azi…")
            await self.pipeline.generate_for(self.pipeline.today(), force=True)
        elif cmd == "/genereaza":
            try:
                d = datetime.strptime(arg.strip(), "%Y-%m-%d").date()
            except ValueError:
                await self.api.call("sendMessage", chat_id=chat, text="Folosește: /genereaza 2026-11-12")
                return
            await self.api.call("sendMessage", chat_id=chat, text=f"⏳ Generez draftul pentru {d}…")
            await self.pipeline.generate_for(d, force=True)
        elif cmd == "/extra":
            wd = None
            if arg.strip():
                wd = parse_weekday(arg)
                if wd is None:
                    await self.api.call("sendMessage", chat_id=chat,
                                        text="Folosește: /extra sau /extra joi (luni … sâmbătă)")
                    return
            label = f" cu tema de {ZILE[wd]}" if wd is not None else ""
            await self.api.call("sendMessage", chat_id=chat, text=f"⏳ Generez o postare extra{label}…")
            await self.pipeline.generate_extra(self.pipeline.today(), wd)
        elif cmd == "/post":
            if len(arg.strip()) < 5:
                await self.api.call("sendMessage", chat_id=chat, text=(
                    "Scrie ce postare vrei, de ex.\n"
                    "/post anunță programul redus de Crăciun, 24–26 decembrie\n"
                    "/post carusel 3 greșeli când ții abonamentele în Excel\n"
                    "/post reel de ce memento-urile cresc reînnoirile\n"
                    "/post foto pentru cluburile de înot, înscrierile de toamnă"))
                return
            await self.api.call("sendMessage", chat_id=chat, text="⏳ Citesc cererea și scriu postarea…")
            await self.pipeline.generate_custom(self.pipeline.today(), arg.strip())
        elif cmd == "/raport":
            await self.api.call("sendMessage", chat_id=chat, text=_truncate(self.pipeline.report_text(), MESSAGE_MAX))
        elif cmd == "/regula":
            if not arg.strip():
                await self.api.call("sendMessage", chat_id=chat,
                                    text="Folosește: /regula fără emoji în titlu\nO respect la fiecare postare.")
                return
            rules = self.pipeline.add_rule(arg)
            await self.api.call("sendMessage", chat_id=chat, text=f"✅ Regula {len(rules)} salvată: {arg.strip()}")
        elif cmd == "/reguli":
            rules = self.pipeline.rules()
            text = ("Regulile tale:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))
                    + "\n\nȘterge una cu /sterge_regula N") if rules else \
                "Nicio regulă încă. Adaugă una cu /regula TEXT, de ex. /regula fără emoji în titlu"
            await self.api.call("sendMessage", chat_id=chat, text=text)
        elif cmd == "/sterge_regula":
            try:
                removed = self.pipeline.remove_rule(int(arg.strip()))
            except ValueError:
                await self.api.call("sendMessage", chat_id=chat, text="Folosește: /sterge_regula N (vezi /reguli)")
                return
            await self.api.call("sendMessage", chat_id=chat, text=f"🗑 Am șters regula: {removed}")
        elif cmd == "/status":
            await self.api.call("sendMessage", chat_id=chat, text=self.pipeline.status_text())
        elif cmd == "/pauza":
            self.pipeline.kv_set("paused", "1")
            await self.api.call("sendMessage", chat_id=chat, text="⏸ Pauză: nu generez și nu public automat.")
        elif cmd == "/reia":
            self.pipeline.kv_set("paused", "")
            await self.api.call("sendMessage", chat_id=chat, text="▶️ Am reluat programul automat.")
        elif cmd == "/verifica":
            await self.api.call("sendMessage", chat_id=chat, text=await self.pipeline.meta_check_text())
        else:
            await self.api.call("sendMessage", chat_id=chat, text="Nu știu comanda asta.\n\n" + HELP)

    async def on_callback(self, cq: dict[str, Any]) -> None:
        chat = cq["message"]["chat"]["id"]
        if chat not in self.allowed:
            await self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text="Nu ai acces.")
            return
        try:
            action, post_id, rev = cq["data"].split(":")
            post_id, rev = int(post_id), int(rev)
        except ValueError:
            await self.api.call("answerCallbackQuery", callback_query_id=cq["id"])
            return
        post = self.pipeline.get(post_id)
        if not post or post.revision != rev or post.status != PostStatus.draft:
            await self.api.call("answerCallbackQuery", callback_query_id=cq["id"],
                                text="Draftul ăsta nu mai e activ.", show_alert=True)
            return
        texts = {"a": "Aprobat ✅", "s": "Sărit ⏭", "r": "Regenerez 🔄", "i": "Caut altă imagine 🖼"}
        await self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text=texts.get(action, ""))
        if action == "a":
            await self.pipeline.approve(post_id)
        elif action == "s":
            await self.pipeline.skip(post_id)
        elif action == "r":
            await self.pipeline.regenerate(post_id)
        elif action == "i":
            await self.pipeline.regenerate(post_id, image_only=True)


def build(pipeline: Pipeline) -> tuple[TelegramNotifier, TelegramBot] | tuple[None, None]:
    s = get_settings()
    if not s.telegram_bot_token:
        return None, None
    api = TelegramAPI(s.telegram_bot_token)
    return TelegramNotifier(api, s.telegram_allowed_chat_ids), TelegramBot(api, pipeline, s.telegram_allowed_chat_ids)
