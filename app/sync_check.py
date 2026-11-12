"""Compares the product sheet with the Abonix repo, so you know when it is out
of date. It only reports: the feature texts stay hand-written on purpose,
because product.yaml is what keeps Claude from inventing features.

Checks:
  prices     pricing.json vs product.yaml (plans, limits, included SMS/emails,
             Personal Trainer, SMS overage)
  features   which plan has which feature (pricing.json flags vs `plans:` in
             product.yaml), plus any new flag in pricing.json
  screens    frontend/public/tour vs assets/screens: new, missing or changed
  commits    Abonix commits since `synced_with_abonix` in product.yaml
"""

from __future__ import annotations

import filecmp
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app import content
from app.config import ASSETS_DIR, CONTENT_DIR, ROOT

PRICING = Path("frontend/src/pages/pricing/pricing.json")
TOUR = Path("frontend/public/tour")

# pricing.json feature flag -> product.yaml feature keys that follow it.
# None = a flag with no feature of its own in the sheet (covered elsewhere).
FLAG_FEATURES: dict[str, list[str] | None] = {
    "memberManagement": ["members"],
    "smartDashboard": ["dashboard"],
    "paymentHistory": None,
    "membershipPlans": ["plans"],
    "qrCheckIn": ["checkin"],
    "csvImportExport": ["csv"],
    "promotions": ["promotions"],
    "automaticReminders": ["reminders"],
    "manualReminders": None,
    "emailReminders": None,
    "smsReminders": None,
    "marketing": ["marketing"],
    "memberApp": ["member_app", "member_invites", "join_requests"],
    "earnings": ["earnings"],
    "classes": ["classes"],
    "kioskCheckIn": ["kiosk"],
}


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)  # the sheet disagrees with Abonix
    notes: list[str] = field(default_factory=list)     # worth a look, not wrong by itself
    commits: list[str] = field(default_factory=list)
    head: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def default_repo() -> Path:
    return ROOT.parent / "Abonix"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout.strip()


def check_prices(pricing: dict, product: dict) -> list[str]:
    out: list[str] = []

    def cmp(what: str, theirs, ours) -> None:
        if theirs != ours:
            out.append(f"{what}: Abonix {theirs}, product.yaml {ours}")

    ours = {p["key"]: p for p in product["plans"]}
    for plan in pricing["plans"]:
        key = plan["key"]
        if key not in ours:
            out.append(f"Plan nou în Abonix: {key} ({plan.get('priceRon')} RON)")
            continue
        o = ours[key]
        cmp(f"{key}: preț", plan.get("priceRon"), o.get("price_ron"))
        cmp(f"{key}: limită membri", plan.get("memberLimit"), o.get("member_limit"))
        cmp(f"{key}: SMS incluse", plan.get("includedSms", 0), o.get("included_sms", 0))
        cmp(f"{key}: emailuri incluse", plan.get("includedEmails", 0), o.get("included_emails", 0))
        cmp(f"{key}: memento-uri", plan.get("includesReminders"), o.get("includes_reminders"))
    for key in ours.keys() - {p["key"] for p in pricing["plans"]}:
        out.append(f"Planul {key} nu mai există în Abonix")

    pt, opt = pricing.get("personalTrainer", {}), product.get("personal_trainer", {})
    cmp("Personal Trainer: preț", pt.get("priceRon"), opt.get("price_ron"))
    cmp("Personal Trainer: limită clienți", pt.get("clientLimit"), opt.get("client_limit"))
    cmp("Personal Trainer: utilizatori", pt.get("users"), opt.get("users"))
    cmp("Personal Trainer: preț memento-uri", pt.get("remindersAddonRon"), opt.get("reminders_addon_ron"))
    cmp("Personal Trainer: SMS memento-uri", pt.get("remindersAddonSms"), opt.get("reminders_addon_sms"))
    cmp("SMS peste limită (EUR)", pricing.get("smsOverageFeeEur"), product.get("sms_overage_eur"))
    return out


def check_features(pricing: dict, product: dict) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    ours = {f["key"]: set(f.get("plans", [])) for f in product["features"]}
    flags = sorted({flag for p in pricing["plans"] for flag in p.get("features", {})})
    for flag in flags:
        if flag not in FLAG_FEATURES:
            on = [p["key"] for p in pricing["plans"] if p["features"].get(flag)]
            notes.append(f"Funcționalitate nouă în pricing.json: „{flag}” (planuri: {', '.join(on) or '-'}). "
                         f"Adaug-o în product.yaml și în FLAG_FEATURES din app/sync_check.py.")
            continue
        theirs = {p["key"] for p in pricing["plans"] if p["features"].get(flag)}
        for key in FLAG_FEATURES[flag] or []:
            if key not in ours:
                problems.append(f"Lipsește funcționalitatea „{key}” din product.yaml (flag {flag})")
            elif ours[key] != theirs:
                problems.append(f"„{key}”: Abonix o are pe {sorted(theirs)}, product.yaml spune {sorted(ours[key])}")
    return problems, notes


def check_screens(tour: Path, screens: Path, used: list[str]) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    theirs = {p.name for p in tour.glob("*.webp")}
    ours = {p.name for p in screens.glob("*.webp")}
    for name in sorted(theirs - ours):
        notes.append(f"Captură nouă în Abonix: {name} (copiaz-o în assets/screens/ și leag-o de o funcționalitate)")
    for name in sorted(ours - theirs):
        (problems if name in used else notes).append(
            f"Captura {name} nu mai există în Abonix" + (" și e folosită în product.yaml" if name in used else ""))
    for name in sorted(theirs & ours):
        if not filecmp.cmp(tour / name, screens / name, shallow=False):
            problems.append(f"Captura {name} s-a schimbat în Abonix (copiaz-o din nou din {TOUR.as_posix()}/)")
    return problems, notes


def run(repo: Path) -> Report:
    if not (repo / PRICING).is_file():
        raise FileNotFoundError(f"Nu găsesc repo-ul Abonix la {repo} (lipsește {PRICING.as_posix()}). "
                                f"Folosește --abonix CALE.")
    r = Report()
    pricing = json.loads((repo / PRICING).read_text(encoding="utf-8"))
    product = content._yaml("product.yaml")  # fresh read, not the cached one
    r.problems += check_prices(pricing, product)
    p, n = check_features(pricing, product)
    r.problems += p
    r.notes += n
    used = [s for f in product["features"] for s in f.get("screens", [])]
    p, n = check_screens(repo / TOUR, ASSETS_DIR / "screens", used)
    r.problems += p
    r.notes += n

    r.head = _git(repo, "rev-parse", "--short", "HEAD")
    since = product.get("synced_with_abonix")
    if since:
        try:
            log = _git(repo, "log", "--no-merges", "--format=%h %cs %s", f"{since}..HEAD")
            r.commits = [line for line in log.splitlines() if line]
        except subprocess.CalledProcessError:
            r.notes.append(f"Commit-ul {since} din synced_with_abonix nu există în repo-ul Abonix.")
    else:
        r.notes.append("product.yaml nu are synced_with_abonix; rulează cu --mark după ce o verifici.")
    return r


def mark(head: str) -> None:
    """Record the Abonix commit the sheet now matches."""
    path = CONTENT_DIR / "product.yaml"
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^synced_with_abonix:.*$", f"synced_with_abonix: {head}", text)
    if n != 1:
        raise ValueError("product.yaml needs exactly one `synced_with_abonix:` line")
    path.write_text(new, encoding="utf-8")


def format_report(r: Report) -> str:
    lines = []
    if r.problems:
        lines.append(f"✗ {len(r.problems)} diferențe între product.yaml și Abonix:")
        lines += [f"  - {p}" for p in r.problems]
    else:
        lines.append("✓ Prețurile, planurile și capturile se potrivesc cu Abonix.")
    if r.notes:
        lines.append("\nDe verificat:")
        lines += [f"  - {n}" for n in r.notes]
    if r.commits:
        lines.append(f"\n{len(r.commits)} commit-uri în Abonix de la ultima verificare "
                     f"(verifică dacă vreunul e o funcționalitate de adăugat în product.yaml):")
        lines += [f"  {c}" for c in r.commits]
    else:
        lines.append("\nNiciun commit nou în Abonix de la ultima verificare.")
    lines.append(f"\nAbonix HEAD: {r.head}")
    return "\n".join(lines)
