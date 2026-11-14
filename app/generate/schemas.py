"""What Claude returns. Kept free of length constraints on purpose: structured
outputs guarantee the shape, `review.py` enforces lengths and facts so a
too-long headline produces a readable issue instead of a schema error."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Accent = Literal["indigo", "sky", "dark"]


class Slide(BaseModel):
    title: str = Field(description="Titlul slide-ului. Maxim 6 cuvinte.")
    body: str = Field(description="Textul slide-ului. Maxim 25 de cuvinte, o singură idee.")


class PostDraft(BaseModel):
    headline: str = Field(description="Titlul de pe imagine. Maxim 6 cuvinte.")
    subline: str = Field(description="Subtitlul de pe imagine. Maxim 12 cuvinte.")
    points: list[str] = Field(
        default_factory=list,
        description=(
            "Puncte scurte pe imagine. feature/offer/photo: 0-3 beneficii, max 7 cuvinte fiecare. "
            "split: exact 3 puncte pentru partea «înainte» (Excel/caiet). Altfel gol."
        ),
    )
    points_after: list[str] = Field(
        default_factory=list,
        description="Doar pentru split: exact 3 puncte pentru partea «cu Abonix». Altfel gol.",
    )
    caption_ig: str = Field(description="Caption Instagram, fără hashtag-uri (se adaugă separat).")
    caption_fb: str = Field(description="Caption Facebook, fără hashtag-uri (se adaugă separat).")
    hashtags_ig: list[str] = Field(description="5-10 hashtag-uri, fiecare începe cu #.")
    hashtags_fb: list[str] = Field(description="0-3 hashtag-uri, fiecare începe cu #.")
    alt_text: str = Field(description="Descrierea imaginii pentru nevăzători, o frază.")
    screen: Optional[str] = Field(
        default=None, description="Doar pentru feature: numele fișierului de captură ales din lista dată."
    )
    photo_query: Optional[str] = Field(
        default=None, description="Doar pentru photo: căutare în engleză pentru o fotografie de stoc, fără text."
    )
    accent: Accent = Field(default="indigo", description="Varianta de culoare a imaginii.")
    hook: str = Field(description="Unghiul/ideea postării într-o frază, pentru evitarea repetițiilor.")
    slides: list[Slide] = Field(
        default_factory=list,
        description=(
            "Doar pentru carousel și reel: 2-4 slide-uri după copertă, fiecare cu o singură idee, "
            "în ordinea în care se citesc. Slide-ul final cu „link în bio” se adaugă automat. Altfel gol."
        ),
    )


class ReviewResult(BaseModel):
    ok: bool = Field(description="true dacă draftul poate fi publicat așa cum e.")
    issues: list[str] = Field(
        default_factory=list, description="Probleme concrete, în română, câte una pe element. Gol dacă ok."
    )
