"""Hand-written sample drafts, one per template. Used by `cli.py samples` to
check the templates without calling any API."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SAMPLES: list[dict] = [
    {
        "template": "feature", "theme": "feature", "subject": "reminders",
        "draft": {"headline": "Nu mai pierde reînnoiri", "subline": "Abonix le amintește membrilor înainte să le expire abonamentul.",
                  "screen": "memento-setari.webp", "accent": "indigo", "points": []},
    },
    {
        "template": "feature", "theme": "feature", "subject": "member_app",
        "draft": {"headline": "Abonamentul, în buzunarul lor", "subline": "Membrii își văd data expirării direct pe telefon.",
                  "screen": "app-acasa.webp", "accent": "sky",
                  "points": ["Fără magazin de aplicații", "Plăți și intrări la vedere", "Progres spre zilele bonus"]},
    },
    {
        "template": "quote", "theme": "tip", "subject": "x",
        "draft": {"headline": "Scrie-le cu 7 zile înainte, nu după", "subline": "Un memento la timp aduce înapoi mai mulți membri decât o reducere.",
                  "accent": "dark"},
    },
    {
        "template": "split", "theme": "problem_solution", "subject": "x",
        "draft": {"headline": "Cine expiră săptămâna asta?", "subline": "Aceeași întrebare, două răspunsuri.", "accent": "indigo",
                  "points": ["Cauți prin foi de calcul", "Suni membrii pe rând", "Uiți câte unul mereu"],
                  "points_after": ["Lista e gata dimineața", "Memento-uri trimise automat", "Nimeni nu e uitat"]},
    },
    {
        "template": "photo", "theme": "club_type", "subject": "studio de dans",
        "draft": {"headline": "Pentru studiourile de dans", "subline": "Clase, rezervări și abonamente, fără caiet și fără WhatsApp.",
                  "accent": "indigo", "points": ["Calendarul claselor cu locuri rămase", "Înscriere prin link sau cod QR"]},
    },
    {
        "template": "offer", "theme": "offer", "subject": "growth",
        "draft": {"headline": "Totul automat, un preț fix", "subline": "Memento-uri, aplicație pentru membri și clase de grup.",
                  "accent": "indigo", "points": ["250 SMS incluse lunar", "Aplicație pentru membri", "Clase de grup și rezervări"]},
    },
    {
        "template": "offer", "theme": "offer", "subject": "personal_trainer",
        "draft": {"headline": "Pentru antrenorii personali", "subline": "Clienții, ședințele și plățile tale, într-un singur loc.",
                  "accent": "sky", "points": ["Pachete de ședințe cu contor", "Clienții se programează singuri", "Aplicație cu numele tău"]},
    },
    {
        "template": "number", "theme": "number", "subject": "sms_300",
        "draft": {"headline": "Mesajul tău încape întreg", "subline": "Aproape dublul limitei standard de 160 de caractere.", "accent": "dark"},
    },
]


SLIDES = [
    {"title": "Membrii uită, nu pleacă", "body": "Cei mai mulți nu renunță la sală. Doar le expiră abonamentul și nimeni nu le amintește."},
    {"title": "Memento cu 7 zile înainte", "body": "Abonix trimite singur email sau SMS cu 7 și 3 zile înainte de expirare, apoi în ziua expirării."},
    {"title": "Tu doar vezi cine a reînnoit", "body": "Fără telefoane date pe rând și fără liste în Excel."},
]


def placeholder_photo(path: Path) -> Path:
    """A synthetic 'photo' (gradient + shapes) so the photo template can be
    checked without a stock-photo key."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = 1200, 1600
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(200 - 120 * t), int(150 - 90 * t), int(120 + 40 * t)))
    for i, (x, r) in enumerate([(300, 180), (760, 240), (520, 120)]):
        d.ellipse([x - r, 500 + i * 160 - r, x + r, 500 + i * 160 + r], fill=(240, 200, 170))
    img.save(path, "JPEG", quality=85)
    return path
