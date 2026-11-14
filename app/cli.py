"""Manual commands for setup and the verification steps in the plan.

  python -m app.cli samples              render every template with sample text (no API)
  python -m app.cli draft [--date D]     generate one draft locally, no Telegram, no publishing
  python -m app.cli week                 7 drafts, one per weekday (plan step 3)
  python -m app.cli meta-check           plan step 1: token sees the Page and IG account
  python -m app.cli meta-test IMAGE      plan step 2: publish one test JPEG (asks first)
  python -m app.cli status
  python -m app.cli sync-check [--abonix PATH] [--mark]
                                         compare product.yaml with the Abonix repo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from app import content, db
from app.config import get_settings


def _print_draft(post) -> None:
    c = post.content
    print(f"\n=== #{post.id} {post.for_date} [{post.theme} / {post.subject} / {post.format}]")
    for m in post.media_paths or [post.image_path]:
        print(f"    {m}")
    print(f"TITLU: {c['headline']}\nSUBTITLU: {c['subline']}")
    for i, sl in enumerate(c.get("slides") or [], start=2):
        print(f"SLIDE {i}: {sl['title']} — {sl['body']}")
    if c.get("points"):
        print("PUNCTE:", " | ".join(c["points"]), ("|| " + " | ".join(c["points_after"])) if c.get("points_after") else "")
    print(f"\nIG:\n{c['caption_ig']}\n{' '.join(c['hashtags_ig'])}")
    print(f"\nFB:\n{c['caption_fb']}\n{' '.join(c['hashtags_fb'])}")
    if post.review_issues:
        print("\n⚠️  " + "\n⚠️  ".join(post.review_issues))
    print("usage:", json.dumps(post.usage))


async def cmd_samples(_args) -> None:
    from app.images.render import html_to_jpegs, render_post, slide_pages
    from app.images.video import make_reel
    from app.samples import SAMPLES, SLIDES, placeholder_photo

    out = get_settings().data_dir / "samples"
    photo = placeholder_photo(out / "placeholder.jpg")
    for i, smp in enumerate(SAMPLES):
        p = out / f"{i}-{smp['template']}.jpg"
        await render_post(smp["template"], smp["theme"], smp["subject"], smp["draft"], p,
                          photo if smp["template"] == "photo" else None,
                          "Fotograf" if smp["template"] == "photo" else None)
        print(p)
    # A carousel's slides after the cover, and the same slides as a reel.
    cover = out / "0-feature.jpg"
    slides = await html_to_jpegs(slide_pages("feature", {**SAMPLES[0]["draft"], "slides": SLIDES}, out / "carousel"))
    for s in slides:
        print(s)
    print(await make_reel([cover, *slides], out / "reel.mp4"))


async def _local_pipeline():
    from app.main import build_pipeline
    from app.pipeline import NullNotifier

    db.init_db()
    p, _ = build_pipeline()
    p.notifier = NullNotifier()  # local runs never message Telegram
    return p


async def cmd_draft(args) -> None:
    p = await _local_pipeline()
    d = date.fromisoformat(args.date) if args.date else p.today()
    post = await p.generate_for(d, force=True)
    if post:
        _print_draft(post)


async def cmd_week(_args) -> None:
    p = await _local_pipeline()
    start = p.today()
    for i in range(7):
        d = start + timedelta(days=i)
        if content.day_plan(d) is None:
            print(f"\n=== {d} ({d:%A}): fără postare")
            continue
        post = await p.generate_for(d, force=True)
        _print_draft(post)


async def cmd_meta_check(_args) -> None:
    from app.publish.meta import get_meta

    print(json.dumps(await get_meta().check(), indent=2, ensure_ascii=False))


async def cmd_meta_test(args) -> None:
    from app.publish.meta import get_meta
    from app.publish.storage import get_store

    s = get_settings()
    img = Path(args.image)
    url = args.url or await get_store().put(img, f"tests/{img.name}")
    print("public URL:", url)
    if input("Publish this as a TEST post on the real Page and Instagram? Type 'da': ").strip() != "da":
        return
    meta = get_meta()
    caption = "Test Abonix Social — se șterge în câteva minute."
    if "instagram" in s.publish_targets:
        cid = await meta.create_ig_container(url, caption)
        await meta.wait_ig_container(cid)
        print("IG media:", await meta.publish_ig_container(cid), "(delete it manually from the app)")
    if "facebook" in s.publish_targets:
        fb = await meta.publish_facebook_photo(url, caption)
        print("FB post:", fb)
        if input("Delete the Facebook test post now? Type 'da': ").strip() == "da":
            await meta.delete(fb)
            print("deleted")


async def cmd_status(_args) -> None:
    p = await _local_pipeline()
    print(p.status_text())


async def cmd_sync_check(args) -> None:
    from app import sync_check

    repo = Path(args.abonix) if args.abonix else sync_check.default_repo()
    try:
        r = sync_check.run(repo)
    except FileNotFoundError as e:
        print(e)
        sys.exit(2)
    print(sync_check.format_report(r))
    if args.mark:
        if not r.ok:
            print("\nNu mut synced_with_abonix: rezolvă întâi diferențele de mai sus.")
            sys.exit(1)
        sync_check.mark(r.head)
        print(f"\nsynced_with_abonix = {r.head} în product.yaml.")
    elif not r.ok:
        sys.exit(1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("samples")
    d = sub.add_parser("draft")
    d.add_argument("--date")
    sub.add_parser("week")
    sub.add_parser("meta-check")
    t = sub.add_parser("meta-test")
    t.add_argument("image")
    t.add_argument("--url", help="skip upload, use an already public URL")
    sub.add_parser("status")
    sc = sub.add_parser("sync-check")
    sc.add_argument("--abonix", help="path to the Abonix repo (default: ../Abonix)")
    sc.add_argument("--mark", action="store_true",
                    help="after updating product.yaml: record the current Abonix commit as checked")
    args = ap.parse_args()
    fn = {"samples": cmd_samples, "draft": cmd_draft, "week": cmd_week, "meta-check": cmd_meta_check,
          "meta-test": cmd_meta_test, "status": cmd_status, "sync-check": cmd_sync_check}[args.cmd]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
