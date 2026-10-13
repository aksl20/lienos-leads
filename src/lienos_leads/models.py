"""Modèle de données commun à toutes les sources."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class Segment(str, Enum):
    MISSION = "mission"          # mission publiée sur une plateforme freelance
    OFFRE_EMPLOI = "offre_emploi"  # offre d'emploi (signal de besoin)
    LEVEE = "levee"              # levée de fonds / actualité entreprise


class Status(str, Enum):
    NOUVEAU = "nouveau"
    A_CONTACTER = "a_contacter"
    PLUS_TARD = "plus_tard"
    CONTACTE = "contacte"
    REPONDU = "repondu"
    REJETE = "rejete"


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation : sert au dédoublonnage."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class Lead(BaseModel):
    source: str                      # ex. "france_travail", "maddyness", "malt"
    segment: Segment
    title: str
    url: str = ""
    company: str = ""
    description: str = ""
    location: str = ""
    published_at: datetime | None = None
    daily_rate: float | None = None  # TJM affiché, en euros
    extra: dict = Field(default_factory=dict)

    # Rempli par l'enrichissement
    siren: str | None = None
    company_info: dict = Field(default_factory=dict)
    excluded_reason: str | None = None

    # Rempli par le scoring
    score: int | None = None
    score_details: dict = Field(default_factory=dict)
    explanation: str = ""
    approach: str = ""

    # Identifiant figé à la collecte : le LLM peut ensuite compléter « company »
    # sans que le lead change d'identité (sinon il serait renoté chaque jour).
    uid: str = ""

    def model_post_init(self, __context) -> None:
        if not self.uid:
            self.uid = self._compute_fingerprint()

    def _compute_fingerprint(self) -> str:
        """Même entreprise + même intitulé = même lead, quelle que soit la source
        (une mission vue sur deux sites ne compte qu'une fois)."""
        key = f"{normalize(self.company)}|{normalize(self.title)}"
        if not normalize(self.company):
            key = f"url|{self.url or normalize(self.title)}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        return self.uid

    @property
    def company_key(self) -> str:
        return self.siren or normalize(self.company)

    def age_days(self, now: datetime | None = None) -> float | None:
        if not self.published_at:
            return None
        now = now or datetime.now(timezone.utc)
        published = self.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return (now - published).total_seconds() / 86400
