"""Chargement de la configuration : réglages dans config.toml, secrets dans l'environnement."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import Segment


@dataclass
class FranceTravailConfig:
    enabled: bool = True
    keywords: list[str] = field(default_factory=lambda: ["data scientist", "machine learning"])
    departements: list[str] = field(default_factory=list)  # vide = toute la France
    published_since_days: int = 1  # valeurs acceptées par l'API : 1, 3, 7, 14, 31
    max_results_per_keyword: int = 150


@dataclass
class RssConfig:
    enabled: bool = True
    feeds: dict[str, str] = field(default_factory=dict)  # nom -> URL
    include_keywords: list[str] = field(default_factory=list)  # signal : au moins un mot présent
    topic_keywords: list[str] = field(default_factory=list)  # thème : au moins un aussi, si renseigné


@dataclass
class MaltMailConfig:
    enabled: bool = False
    imap_host: str = "imap.zoho.eu"
    mailbox: str = "INBOX"
    sender_contains: str = "malt"
    lookback_days: int = 2


@dataclass
class LinkedinMailConfig:
    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    mailbox: str = "INBOX"
    sender: str = "hit-reply@linkedin.com"  # InMails ; les autres expéditeurs n'ont pas le texte
    lookback_days: int = 3


@dataclass
class ApifyConfig:
    enabled: bool = False
    actor: str = "koatiromeo~french-it-contract-market"
    input: dict = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)  # un run par mot-clé ; vide = input tel quel
    min_daily_rate: float = 600


@dataclass
class ScoringConfig:
    model: str = "claude-haiku-5-5"
    threshold: int = 70
    threshold_levee: int = 55  # une levée n'a pas de mission à noter : la grille la plafonne vers 65
    max_leads_per_run: int = 150
    profile: str = ""  # description du positionnement envoyée au LLM

    def threshold_for(self, segment: Segment) -> int:
        return self.threshold_levee if segment == Segment.LEVEE else self.threshold


@dataclass
class DigestConfig:
    output_dir: str = "output"
    send_email: bool = False
    smtp_host: str = "smtp.zoho.eu"
    smtp_port: int = 465
    to: str = ""


@dataclass
class Config:
    db_path: str = "data/leads.sqlite"
    recontact_cooldown_days: int = 90
    excluded_companies: list[str] = field(default_factory=list)
    france_travail: FranceTravailConfig = field(default_factory=FranceTravailConfig)
    rss: RssConfig = field(default_factory=RssConfig)
    malt_mail: MaltMailConfig = field(default_factory=MaltMailConfig)
    linkedin_mail: LinkedinMailConfig = field(default_factory=LinkedinMailConfig)
    apify: ApifyConfig = field(default_factory=ApifyConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        sections = {
            "france_travail": FranceTravailConfig,
            "rss": RssConfig,
            "malt_mail": MaltMailConfig,
            "linkedin_mail": LinkedinMailConfig,
            "apify": ApifyConfig,
            "scoring": ScoringConfig,
            "digest": DigestConfig,
        }
        kwargs = {k: v for k, v in data.items() if k not in sections}
        for name, klass in sections.items():
            kwargs[name] = klass(**data.get(name, {}))
        return cls(**kwargs)


def secret(name: str, required: bool = False) -> str:
    """Lit un secret dans l'environnement (jamais dans config.toml)."""
    value = os.environ.get(name, "")
    if required and not value:
        raise RuntimeError(f"Variable d'environnement manquante : {name}")
    return value


def load_dotenv(path: str | Path = ".env") -> None:
    """Petit chargeur .env sans dépendance : KEY=VALUE, lignes # ignorées."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
