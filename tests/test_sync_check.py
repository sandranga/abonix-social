import json
import shutil
import subprocess

import pytest

from app import content, sync_check
from app.config import ASSETS_DIR


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def pricing_from_sheet() -> dict:
    """A pricing.json that matches product.yaml exactly."""
    product = content._yaml("product.yaml")
    feats = {f["key"]: set(f["plans"]) for f in product["features"]}
    plans = []
    for p in product["plans"]:
        flags = {}
        for flag, keys in sync_check.FLAG_FEATURES.items():
            flags[flag] = bool(keys) and p["key"] in feats[keys[0]]
        plans.append({"key": p["key"], "priceRon": p["price_ron"], "memberLimit": p["member_limit"],
                      "includesReminders": p["includes_reminders"], "includedSms": p.get("included_sms", 0),
                      "includedEmails": p.get("included_emails", 0), "features": flags})
    pt = product["personal_trainer"]
    return {"smsOverageFeeEur": product["sms_overage_eur"],
            "personalTrainer": {"priceRon": pt["price_ron"], "clientLimit": pt["client_limit"], "users": pt["users"],
                                "remindersAddonRon": pt["reminders_addon_ron"],
                                "remindersAddonSms": pt["reminders_addon_sms"]},
            "plans": plans}


@pytest.fixture
def abonix(tmp_path, monkeypatch):
    """A tiny Abonix repo: pricing.json + tour screenshots matching ours, one commit."""
    repo = tmp_path / "Abonix"
    (repo / sync_check.PRICING).parent.mkdir(parents=True)
    (repo / sync_check.PRICING).write_text(json.dumps(pricing_from_sheet()), encoding="utf-8")
    shutil.copytree(ASSETS_DIR / "screens", repo / sync_check.TOUR)
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "start")
    base = git(repo, "rev-parse", "--short", "HEAD")
    real = content._yaml("product.yaml")
    monkeypatch.setattr(content, "_yaml", lambda name: {**real, "synced_with_abonix": base}
                        if name == "product.yaml" else real)
    return repo


def commit(repo, msg):
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-am", msg)


def test_in_sync(abonix):
    r = sync_check.run(abonix)
    assert r.ok and r.notes == [] and r.commits == []


def test_price_change_is_a_problem(abonix):
    p = json.loads((abonix / sync_check.PRICING).read_text(encoding="utf-8"))
    p["plans"][1]["priceRon"] = 229
    (abonix / sync_check.PRICING).write_text(json.dumps(p), encoding="utf-8")
    r = sync_check.run(abonix)
    assert not r.ok and any("growth: preț: Abonix 229" in x for x in r.problems)


def test_new_flag_and_plan_change(abonix):
    p = json.loads((abonix / sync_check.PRICING).read_text(encoding="utf-8"))
    for plan in p["plans"]:
        plan["features"]["waitlist"] = plan["key"] != "essential"
    p["plans"][0]["features"]["classes"] = True  # classes now on Essential too
    (abonix / sync_check.PRICING).write_text(json.dumps(p), encoding="utf-8")
    r = sync_check.run(abonix)
    assert any("„waitlist”" in n and "growth, business" in n for n in r.notes)
    assert any("„classes”" in x and "essential" in x for x in r.problems)


def test_screens_new_and_changed(abonix):
    tour = abonix / sync_check.TOUR
    (tour / "nou.webp").write_bytes(b"new")
    (tour / "panou.webp").write_bytes(b"changed")
    r = sync_check.run(abonix)
    assert any("nou.webp" in n for n in r.notes)
    assert any("panou.webp s-a schimbat" in x for x in r.problems)


def test_lists_commits_since_marker(abonix):
    commit(abonix, "Let members pay online")
    commit(abonix, "Fix a typo")
    r = sync_check.run(abonix)
    assert [c.split(" ", 2)[2] for c in r.commits] == ["Fix a typo", "Let members pay online"]
    assert "2 commit-uri" in sync_check.format_report(r)


def test_mark_rewrites_only_the_marker(tmp_path, monkeypatch):
    sheet = tmp_path / "product.yaml"
    sheet.write_text("# keep me\nsynced_with_abonix: aaaaaaa\nname: Abonix\n", encoding="utf-8")
    monkeypatch.setattr(sync_check, "CONTENT_DIR", tmp_path)
    sync_check.mark("bbbbbbb")
    assert sheet.read_text(encoding="utf-8") == "# keep me\nsynced_with_abonix: bbbbbbb\nname: Abonix\n"


def test_missing_repo_is_explained(tmp_path):
    with pytest.raises(FileNotFoundError, match="--abonix"):
        sync_check.run(tmp_path / "nope")
