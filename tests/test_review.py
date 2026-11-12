from app.generate.review import normalize, rule_issues
from tests.conftest import good_draft


def test_good_draft_has_no_issues():
    assert rule_issues(good_draft(), "feature") == []


def test_unknown_price_is_flagged():
    d = good_draft(caption_ig=good_draft().caption_ig + " Acum doar 199 lei!")
    assert any("199 lei" in i for i in rule_issues(d, "feature"))


def test_forbidden_words():
    d = good_draft(subline="Memento-uri nelimitate și prima lună gratuită.")
    issues = " ".join(rule_issues(d, "feature"))
    assert "nelimitate" in issues and "gratuit" in issues


def test_gratuit_allowed_about_member_side():
    d = good_draft(subline="Check-in-ul e gratuit pentru membri, fără aplicație.")
    assert not any("gratuit" in i for i in rule_issues(d, "feature"))


def test_ai_is_case_sensitive():
    assert rule_issues(good_draft(subline="Ai nevoie de ordine în abonamente?"), "feature") == []
    assert any("AI" in i for i in rule_issues(good_draft(subline="Gestiune cu AI pentru săli."), "feature"))


def test_headline_length():
    d = good_draft(headline="Un titlu mult prea lung pentru o imagine de feed")
    assert any("Titlul" in i for i in rule_issues(d, "feature"))


def test_split_needs_three_each():
    d = good_draft(points=["a", "b"], points_after=["x", "y", "z"])
    assert any("Split" in i for i in rule_issues(d, "split"))


def test_normalize_fixes_cedilla_and_hashtags():
    d = normalize(good_draft(headline="Ştii cine expiră aşa?", hashtags_ig=["sala fitness", "#Sala", "#sala"],
                             hashtags_fb=[]))
    assert d.headline == "Știi cine expiră așa?"
    assert d.hashtags_ig == ["#salafitness", "#Sala", "#abonix"]
    assert d.hashtags_fb == ["#abonix"]
