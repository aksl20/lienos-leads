"""Tests de bout en bout avec toutes les API simulées (aucun appel réseau réel)."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

from lienos_leads.config import Config
from lienos_leads.enrich import BODACC_URL, SEARCH_URL as ENTREPRISES_URL, Enricher
from lienos_leads.models import Lead, Segment, Status
from lienos_leads.pipeline import run
from lienos_leads.scoring import Scorer, parse_response
from lienos_leads.sources import france_travail, freework_apify, linkedin_mail, malt_mail, rss
from lienos_leads.store import Store

NOW = datetime.now(timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"

OFFRES = {
    "resultats": [
        {
            "id": "190ABC",
            "intitule": "Data scientist vision par ordinateur (H/F)",
            "description": "Deep learning, PyTorch, imagerie médicale, pathologie numérique.",
            "dateCreation": (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "entreprise": {"nom": "MedImaging SAS"},
            "lieuTravail": {"libelle": "75 - PARIS 13", "codePostal": "75013"},
            "typeContrat": "CDI",
        },
        {
            "id": "190DEF",
            "intitule": "Data scientist (H/F)",
            "description": "Machine learning sur données de vente.",
            "dateCreation": (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "entreprise": {"nom": "Faillite Industries"},
            "lieuTravail": {"libelle": "69 - LYON", "codePostal": "69003"},
            "typeContrat": "CDI",
        },
    ]
}

RSS_FEED = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Alerte</title>
<item><title>PathoAI lève 5 millions d'euros pour son diagnostic par IA</title>
<link>https://www.google.com/url?rct=j&amp;url=https://example.org/pathoai-leve</link>
<description>La startup parisienne de pathologie numérique recrute.</description>
<pubDate>{NOW:%a, %d %b %Y %H:%M:%S} +0000</pubDate></item>
<item><title>Recette de la tarte aux pommes</title><link>https://example.org/tarte</link>
<description>Rien à voir.</description></item>
</channel></rss>"""


def entreprise(siren: str, nom: str, etat: str = "A", cp: str = "75013") -> dict:
    return {"results": [{
        "siren": siren, "nom_complet": nom, "activite_principale": "72.19Z",
        "categorie_entreprise": "PME", "tranche_effectif_salarie": "12", "etat_administratif": etat,
        "siege": {"libelle_commune": "PARIS", "code_postal": cp},
        "dirigeants": [{"type_dirigeant": "personne physique", "nom": "MARTIN", "prenoms": "Claire",
                        "qualite": "Président"}],
    }]}


class FakeLLM:
    """Imite client.messages.create de la SDK Anthropic."""

    def __init__(self):
        self.prompts = []
        self.messages = self

    def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        good = "pathologie" in prompt.lower()
        payload = {
            "entreprise": "PathoAI" if "PathoAI" in prompt else "",
            "criteres": {"adequation_technique": 30 if good else 5, "fraicheur": 20,
                         "capacite_a_payer": 12, "pas_d_equipe_ia": 10, "format_freelance": 5,
                         "geographie": 10 if good else 5},
            "explication": "Besoin vision médicale explicite." if good else "Peu lié à l'IA avancée.",
            "angle": "Citer la publication npj Precision Oncology.",
        }
        text = "Voici l'évaluation :\n" + json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FT_CLIENT_ID", "id")
    monkeypatch.setenv("FT_CLIENT_SECRET", "secret")
    c = Config()
    c.db_path = str(tmp_path / "leads.sqlite")
    c.digest.output_dir = str(tmp_path / "out")
    c.france_travail.keywords = ["data scientist"]
    c.rss.feeds = {"alerte_ia_sante": "https://feeds.example.org/alerte"}
    c.rss.include_keywords = ["lève", "millions"]
    return c


def mock_apis(router: respx.Router) -> None:
    router.post(france_travail.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok"}))
    router.get(france_travail.SEARCH_URL).mock(return_value=httpx.Response(200, json=OFFRES))
    router.get("https://feeds.example.org/alerte").mock(
        return_value=httpx.Response(200, text=RSS_FEED))

    def entreprises(request):
        q = request.url.params["q"]
        if "Faillite" in q:
            return httpx.Response(200, json=entreprise("999999999", "FAILLITE INDUSTRIES", cp="69003"))
        if "PathoAI" in q:
            return httpx.Response(200, json=entreprise("222222222", "PATHOAI"))
        return httpx.Response(200, json=entreprise("111111111", "MEDIMAGING SAS"))

    router.get(ENTREPRISES_URL).mock(side_effect=entreprises)

    def bodacc(request):
        collective = "999999999" in request.url.params["where"]
        return httpx.Response(200, json={"total_count": 1 if collective else 0, "results": []})

    router.get(BODACC_URL).mock(side_effect=bodacc)


def test_full_run_selects_dedupes_and_excludes(cfg):
    llm = FakeLLM()
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        with httpx.Client() as client:
            stats = run(cfg, client=client, llm_client=llm, send=False, today=date(2026, 10, 12))

    assert stats["collected"] == 3          # 2 offres + 1 article retenu (la tarte est filtrée)
    assert stats["excluded"] == 1           # procédure collective au BODACC
    assert stats["selected"] == 2           # offre vision médicale + levée PathoAI
    html = open(stats["digest_html"], encoding="utf-8").read()
    assert "MedImaging SAS" in html and "PathoAI" in html
    assert "Faillite" not in html
    assert "https://example.org/pathoai-leve" in html  # lien Google Alerts déballé

    # Deuxième passage le même jour : tout est déjà connu, rien de nouveau
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        with httpx.Client() as client:
            stats2 = run(cfg, client=client, llm_client=llm, send=False)
    assert stats2["new"] == 0


def test_feedback_loop_reaches_prompt_and_cooldown(cfg):
    store = Store(cfg.db_path)
    lead = Lead(source="t", segment=Segment.OFFRE_EMPLOI, title="ML engineer", company="MedImaging SAS")
    lead.siren = "111111111"
    lead.score = 80
    store.save(lead)
    assert store.set_status(lead.fingerprint[:8], Status.CONTACTE, "appel prévu")
    assert store.recently_contacted("111111111", 90)
    rejected = Lead(source="t", segment=Segment.MISSION, title="Data analyst Excel", company="X")
    rejected.score = 72
    store.save(rejected)
    store.set_status(rejected.fingerprint, Status.REJETE, "pas de ML, juste du reporting")
    examples = store.feedback_examples()
    store.close()

    llm = FakeLLM()
    scorer = Scorer(cfg.scoring, examples, client=llm)
    scorer.score([Lead(source="t", segment=Segment.MISSION, title="Mission IA", company="Y")])
    assert "pas de ML, juste du reporting" in llm.prompts[0]

    # MedImaging a été contactée : sa nouvelle offre est exclue au run suivant
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        with httpx.Client() as client:
            stats = run(cfg, client=client, llm_client=llm, send=False)
    html = open(stats["digest_html"], encoding="utf-8").read()
    assert "MedImaging" not in html


def test_parse_response_clamps_scores():
    text = '{"entreprise": "A", "criteres": {"adequation_technique": 99, "fraicheur": -3, ' \
           '"capacite_a_payer": "12"}, "explication": "x", "angle": "y"}'
    result = parse_response(text)
    assert result["details"]["adequation_technique"] == 30
    assert result["details"]["fraicheur"] == 0
    assert result["score"] == 42


def test_heuristic_without_api_key(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    scorer = Scorer(cfg.scoring, [])
    assert not scorer.uses_llm
    lead = Lead(source="t", segment=Segment.MISSION, title="Mission deep learning computer vision",
                location="Paris", published_at=NOW, daily_rate=700)
    scorer.score([lead])
    assert lead.score >= 70


def test_france_travail_pagination(cfg):
    cfg.france_travail.max_results_per_keyword = 300
    page = {"resultats": [dict(OFFRES["resultats"][0], id=f"P{i}") for i in range(150)]}
    last = {"resultats": [dict(OFFRES["resultats"][0], id="LAST")]}
    with respx.mock as router:
        router.post(france_travail.TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok"}))
        router.get(france_travail.SEARCH_URL).mock(side_effect=[
            httpx.Response(206, json=page), httpx.Response(200, json=last)])
        with httpx.Client() as client:
            leads = france_travail.fetch(cfg.france_travail, client)
    assert len(leads) == 151


def test_malt_mail_parser():
    raw = (
        "From: Malt <noreply@malt.fr>\r\nSubject: =?utf-8?q?Nouveau_projet_=3A_Expert_LLM_RAG?=\r\n"
        "Date: Mon, 12 Oct 2026 08:00:00 +0200\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Un client cherche un expert RAG pour 3 mois.\r\n"
        "Voir le projet : https://www.malt.fr/client/projects/abc123\r\n"
        "Gérer mes alertes : https://www.malt.fr/settings/notifications\r\n"
    ).encode()
    lead = malt_mail.parse_message(raw)
    assert lead.title == "Nouveau projet : Expert LLM RAG"
    assert lead.url == "https://www.malt.fr/client/projects/abc123"
    assert lead.segment == Segment.MISSION


def test_rss_filter_and_unwrap():
    leads = rss.parse_feed("alerte", RSS_FEED, ["lève"])
    assert [l.url for l in leads] == ["https://example.org/pathoai-leve"]


def test_freework_apify_replays_real_sample(cfg, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "apify-token")
    cfg.apify.enabled = True
    cfg.apify.input = {"sources": ["free-work"], "maxMissions": 30}
    items = json.loads((FIXTURES / "apify_freework_sample.json").read_text(encoding="utf-8"))
    run = {"id": "RUN1", "defaultDatasetId": "DS1"}
    with respx.mock as router:
        start = router.post(freework_apify.START_URL.format(actor=cfg.apify.actor)).mock(
            return_value=httpx.Response(201, json={"data": {**run, "status": "READY"}}))
        poll = router.get(freework_apify.RUN_URL.format(run_id="RUN1")).mock(side_effect=[
            httpx.Response(200, json={"data": {**run, "status": "RUNNING"}}),
            httpx.Response(200, json={"data": {**run, "status": "SUCCEEDED"}})])
        router.get(freework_apify.ITEMS_URL.format(dataset_id="DS1")).mock(
            return_value=httpx.Response(200, json=items))
        with httpx.Client() as client:
            leads = freework_apify.fetch(cfg.apify, client)
    assert poll.call_count == 2             # attend la fin du run avant de lire le jeu de données
    assert json.loads(start.calls.last.request.content) == cfg.apify.input
    for call in router.calls:               # le jeton ne doit pas finir dans les journaux
        assert "apify-token" not in str(call.request.url)

    all_leads = [freework_apify.to_lead(item) for item in items]
    assert len(all_leads) == 29
    assert all(l.title and l.company and l.url and l.published_at for l in all_leads)
    assert sum(l.daily_rate is not None for l in all_leads) == 17
    # 12 missions sous le plancher de 600 € écartées, les 12 sans TJM affiché gardées
    assert len(leads) == 17
    assert all(l.daily_rate is None or l.daily_rate >= 600 for l in leads)

    fourchette = next(l for l in all_leads if l.extra["tjm_min"] == 600)
    assert fourchette.daily_rate == 730    # haut de la fourchette « 600-730 EUR/j »
    assert fourchette.source == "apify:free-work" and fourchette.segment == Segment.MISSION
    assert any(l.location == "Paris, Île-de-France" for l in all_leads)
    assert any(l.location == "Île-de-France" for l in all_leads)  # ville = région, pas de doublon


def test_enrich_ignores_first_result_with_another_name(cfg):
    store = Store(cfg.db_path)
    with respx.mock as router:
        router.get(ENTREPRISES_URL).mock(side_effect=[
            httpx.Response(200, json=entreprise("333333333", "H&R TELECOM")),
            httpx.Response(200, json=entreprise("444444444", "NSI FRANCE (NSI FRANCE)")),
        ])
        with httpx.Client() as client:
            enricher = Enricher(client, store, 90)
            assert enricher.lookup("R&S TELECOM") is None
            assert enricher.lookup("NSI France")["siren"] == "444444444"
    store.close()


def test_llm_failures_are_reported_as_fallbacks(cfg):
    class BrokenLLM(FakeLLM):
        def create(self, **kwargs):
            raise RuntimeError("401 invalid x-api-key")

    scorer = Scorer(cfg.scoring, [], client=BrokenLLM())
    lead = Lead(source="t", segment=Segment.MISSION, title="Mission IA", company="Y")
    scorer.score([lead])
    assert scorer.fallbacks == 1 and "heuristique" in lead.explanation


def test_freework_apify_one_run_per_query_survives_a_failure(cfg, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "apify-token")
    cfg.apify.enabled = True
    cfg.apify.input = {"sources": ["free-work"], "maxMissions": 50}
    cfg.apify.queries = ["machine learning", "LLM", "deep learning"]
    items = json.loads((FIXTURES / "apify_freework_sample.json").read_text(encoding="utf-8"))

    def start(request):
        query = json.loads(request.content)["query"]
        status = "FAILED" if query == "deep learning" else "SUCCEEDED"
        return httpx.Response(201, json={"data": {
            "id": f"run-{query}", "defaultDatasetId": f"ds-{query}", "status": status}})

    with respx.mock as router:
        route = router.post(freework_apify.START_URL.format(actor=cfg.apify.actor)).mock(side_effect=start)
        # Les deux mots-clés renvoient des missions en commun : elles ne comptent qu'une fois
        router.get(freework_apify.ITEMS_URL.format(dataset_id="ds-machine learning")).mock(
            return_value=httpx.Response(200, json=items[:20]))
        router.get(freework_apify.ITEMS_URL.format(dataset_id="ds-LLM")).mock(
            return_value=httpx.Response(200, json=items[10:]))
        with httpx.Client() as client:
            leads = freework_apify.fetch(cfg.apify, client)
    assert route.call_count == 3
    assert len(leads) == 17    # même résultat que la fixture complète, malgré le run en échec


def test_rss_real_sample_needs_funding_signal_and_topic():
    content = (FIXTURES / "rss_frenchweb_sample.xml").read_bytes()
    signal, topic = ["lève", "millions"], ["IA", "santé", "cardiologie"]
    assert len(rss.parse_feed("frenchweb", content, [])) == 6
    kept = rss.parse_feed("frenchweb", content, signal, topic)
    titles = " | ".join(l.title for l in kept)
    assert "EDISON IA" in titles and "IMPLICITY" in titles
    assert "CARBONFARM" not in titles          # levée sans rapport avec l'IA ou la santé
    assert "consommateurs" not in titles       # parle d'IA mais pas de financement
    assert all(l.published_at and l.url.startswith("https://www.frenchweb.fr/") for l in kept)
    # « IA » est un mot entier : il ne doit pas être trouvé dans « matériel » ou « sociaux »
    assert not rss.parse_feed("frenchweb", content, ["sécuriser"], ["IA"])


def test_same_funding_round_from_two_outlets_is_kept_once(cfg):
    cfg.france_travail.enabled = False
    cfg.rss.feeds = {"a": "https://feeds.example.org/a", "b": "https://feeds.example.org/b"}
    item = ("<item><title>{t}</title><link>https://example.org/{u}</link>"
            "<description>Pathologie numérique.</description></item>")
    feed = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{}</channel></rss>'
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        router.get("https://feeds.example.org/a").mock(return_value=httpx.Response(
            200, text=feed.format(item.format(t="PathoAI lève 5 millions", u="a"))))
        router.get("https://feeds.example.org/b").mock(return_value=httpx.Response(
            200, text=feed.format(item.format(t="La startup PathoAI lève 5 millions d'euros", u="b"))))
        with httpx.Client() as client:
            stats = run(cfg, client=client, llm_client=FakeLLM(), send=False)
    assert stats["collected"] == 2 and stats["selected"] == 1 and stats["excluded"] == 1


def test_homonym_is_not_a_hard_exclusion(cfg):
    store = Store(cfg.db_path)
    ceased = entreprise("555555555", "PRIMO", etat="C")
    with respx.mock(assert_all_called=False) as router:
        router.get(ENTREPRISES_URL).mock(side_effect=[
            httpx.Response(200, json={**ceased, "total_results": 12}),
            httpx.Response(200, json={**ceased, "total_results": 1}),
        ])
        with httpx.Client() as client:
            homonym = Lead(source="t", segment=Segment.LEVEE, title="PRIMO lève 7 M€", company="PRIMO")
            Enricher(client, store, 90).apply(homonym)
            unique = Lead(source="t", segment=Segment.LEVEE, title="PRIMO lève 7 M€", company="PRIMO")
            Enricher(client, store, 90).apply(unique)
    store.close()
    assert homonym.excluded_reason is None and not homonym.company_info["correspondance_sure"]
    assert unique.excluded_reason == "entreprise cessée (SIRENE)"


def test_levee_has_its_own_threshold(cfg):
    assert cfg.scoring.threshold_for(Segment.LEVEE) == 55
    assert cfg.scoring.threshold_for(Segment.MISSION) == cfg.scoring.threshold_for(Segment.OFFRE_EMPLOI) == 70


def test_linkedin_inmail_parser():
    sample = (FIXTURES / "linkedin_inmail_sample.eml").read_text(encoding="utf-8")
    lead = linkedin_mail.parse_message(sample.format(template="email_hire_inmail_01").encode())
    assert lead.title == "Mission freelance ML Engineer – imagerie médicale, 6 mois"
    assert lead.url.startswith("https://www.linkedin.com/messaging/thread/2-")
    assert lead.description.startswith("Bonjour Axel,") and lead.description.endswith("Camille")
    assert "Se désabonner" not in lead.description and "midToken" not in lead.description
    assert lead.source == "linkedin" and lead.published_at.year == 2026

    # Un second message dans la même conversation ne crée pas un second lead
    again = sample.replace("Subject: =?UTF-8?Q?Mission", "Subject: =?UTF-8?Q?Relance_Mission")
    assert linkedin_mail.parse_message(again.format(template="email_hire_inmail_01").encode()).uid == lead.uid

    # Réponse dans une conversation déjà engagée, ou e-mail LinkedIn sans lien de conversation
    reply = sample.format(template="email_hire_inmail_reply_to_member_01").encode()
    assert linkedin_mail.parse_message(reply) is None
    digest = b"From: LinkedIn <messages-noreply@linkedin.com>\r\nSubject: Votre reseau\r\n\r\n2 new connections"
    assert linkedin_mail.parse_message(digest) is None
