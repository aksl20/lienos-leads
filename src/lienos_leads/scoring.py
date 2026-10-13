"""Scoring des leads par un LLM (API Claude), selon la grille du plan.

Le modèle note chaque critère ; le score final est recalculé ici (somme bornée par
les poids), pour ne pas dépendre de l'arithmétique du modèle. Les dernières décisions
d'Axel (leads retenus, rejets motivés) sont ajoutées au prompt : c'est la boucle
d'apprentissage.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from .config import ScoringConfig, secret
from .models import Lead, normalize

log = logging.getLogger(__name__)

WEIGHTS = {
    "adequation_technique": 30,
    "fraicheur": 20,
    "capacite_a_payer": 15,
    "pas_d_equipe_ia": 15,
    "format_freelance": 10,
    "geographie": 10,
}

GRID = """Grille (note chaque critère entre 0 et son maximum) :
- adequation_technique (30) : vision par ordinateur, pathologie numérique, MIL, SSL, multi-omics,
  apprentissage fédéré, LLM/RAG en production, data science appliquée. 0 si hors IA/data.
- fraicheur (20) : signal de moins de 14 jours = 20 ; 15 à 45 jours = 10 ; plus ancien = 0.
- capacite_a_payer (15) : levée ≥ 2 M€, ETI ou grand groupe, TJM affiché ≥ 650 € = 15.
- pas_d_equipe_ia (15) : l'entreprise semble ne pas avoir d'équipe IA interne (première offre data, etc.).
- format_freelance (10) : mission freelance, CDD, ou offre manifestement difficile à pourvoir.
- geographie (10) : Île-de-France ou full remote = 10 ; France hors IDF = 5 ; étranger = 0."""

SYSTEM = """Tu es l'assistant de prospection d'un consultant freelance en IA. Tu évalues des signaux
(missions, offres d'emploi, levées de fonds) pour décider s'ils valent une prise de contact.
Tu réponds uniquement par un objet JSON valide, sans texte autour."""

USER_TEMPLATE = """Profil du consultant :
{profile}

{grid}

Décisions récentes du consultant sur des leads passés (à utiliser pour calibrer ta notation) :
{feedback}

Lead à évaluer :
{lead}

Réponds avec ce JSON :
{{"entreprise": "<nom de l'entreprise concernée, ou chaîne vide>",
  "criteres": {{"adequation_technique": 0, "fraicheur": 0, "capacite_a_payer": 0,
               "pas_d_equipe_ia": 0, "format_freelance": 0, "geographie": 0}},
  "explication": "<une phrase : pourquoi ce score>",
  "angle": "<une phrase : l'accroche à utiliser pour un premier message>"}}"""

JSON_RE = re.compile(r"\{.*\}", re.S)


def lead_payload(lead: Lead) -> str:
    data = {
        "type": lead.segment.value,
        "source": lead.source,
        "titre": lead.title,
        "entreprise": lead.company,
        "lieu": lead.location,
        "publie_il_y_a_jours": round(lead.age_days(), 1) if lead.age_days() is not None else None,
        "tjm": lead.daily_rate,
        "description": lead.description[:3000],
        "infos_entreprise": {k: v for k, v in lead.company_info.items() if k != "dirigeants"},
        "details": {k: v for k, v in lead.extra.items() if k != "all_links"},
    }
    return json.dumps(data, ensure_ascii=False, default=str, indent=1)


def format_feedback(examples: list[dict]) -> str:
    if not examples:
        return "(aucune pour l'instant)"
    lines = []
    for ex in examples:
        note = f" — motif : {ex['status_note']}" if ex.get("status_note") else ""
        lines.append(f"- [{ex['status']}] {ex['title']} ({ex.get('company') or '?'}), "
                     f"score donné {ex.get('score')}{note}")
    return "\n".join(lines)


def parse_response(text: str) -> dict:
    match = JSON_RE.search(text)
    if not match:
        raise ValueError("pas de JSON dans la réponse")
    data = json.loads(match.group(0))
    criteria = data.get("criteres") or {}
    details = {}
    for key, maximum in WEIGHTS.items():
        try:
            details[key] = max(0, min(maximum, int(round(float(criteria.get(key, 0))))))
        except (TypeError, ValueError):
            details[key] = 0
    return {
        "company": (data.get("entreprise") or "").strip(),
        "details": details,
        "score": sum(details.values()),
        "explanation": (data.get("explication") or "").strip(),
        "approach": (data.get("angle") or "").strip(),
    }


# --- Score heuristique : sert sans clé API (essais, tests) ------------------
TECH_TERMS = ["machine learning", "deep learning", "computer vision", "vision par ordinateur",
              "pathologie", "omics", "genomique", "llm", "rag", "ia generative", "data scientist",
              "intelligence artificielle", "nlp", "pytorch", "mlops"]
IDF_CODES = ("75", "77", "78", "91", "92", "93", "94", "95")


def heuristic_score(lead: Lead) -> dict:
    text = normalize(f"{lead.title} {lead.description}")
    hits = sum(1 for t in TECH_TERMS if normalize(t) in text)
    age = lead.age_days()
    location = normalize(lead.location)
    cp = str(lead.extra.get("code_postal") or lead.company_info.get("code_postal") or "")
    details = {
        "adequation_technique": min(30, hits * 10),
        "fraicheur": 20 if age is not None and age <= 14 else (10 if age is not None and age <= 45 else 5),
        "capacite_a_payer": 15 if (lead.daily_rate or 0) >= 650
        or lead.company_info.get("categorie") in ("ETI", "GE") else 7,
        "pas_d_equipe_ia": 8,
        "format_freelance": 10 if lead.segment.value == "mission" else 5,
        "geographie": 10 if cp.startswith(IDF_CODES) or "remote" in text or "paris" in location else 5,
    }
    return {"company": lead.company, "details": details, "score": sum(details.values()),
            "explanation": "Score heuristique (pas de clé API configurée).", "approach": ""}


class Scorer:
    def __init__(self, cfg: ScoringConfig, feedback: list[dict], client=None):
        self.cfg = cfg
        self.feedback = format_feedback(feedback)
        self.client = client
        self.fallbacks = 0  # leads notés par l'heuristique faute de réponse exploitable du LLM
        if self.client is None and secret("ANTHROPIC_API_KEY"):
            import anthropic
            self.client = anthropic.Anthropic()

    @property
    def uses_llm(self) -> bool:
        return self.client is not None

    def _score_one(self, lead: Lead) -> dict:
        if not self.uses_llm:
            return heuristic_score(lead)
        prompt = USER_TEMPLATE.format(
            profile=self.cfg.profile.strip(), grid=GRID, feedback=self.feedback,
            lead=lead_payload(lead),
        )
        for attempt in range(2):
            try:
                msg = self.client.messages.create(
                    model=self.cfg.model,
                    # La réflexion du modèle est décomptée de max_tokens : à 600, le JSON
                    # arrivait tronqué ou absent.
                    max_tokens=4000,
                    output_config={"effort": "low"},
                    system=SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                return parse_response(text)
            except Exception as exc:  # réseau, quota, JSON invalide : on réessaie une fois
                log.warning("Scoring de %r, essai %d échoué : %s", lead.title[:60], attempt + 1, exc)
        result = heuristic_score(lead)
        result["explanation"] = "Score heuristique (échec de l'appel LLM)."
        result["fallback"] = True
        return result

    def score(self, leads: list[Lead], workers: int = 4) -> list[Lead]:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(self._score_one, leads))
        self.fallbacks += sum(1 for r in results if r.get("fallback"))
        for lead, result in zip(leads, results):
            if not lead.company and result["company"]:
                lead.company = result["company"]
            lead.score = result["score"]
            lead.score_details = result["details"]
            lead.explanation = result["explanation"]
            lead.approach = result["approach"]
        return leads
