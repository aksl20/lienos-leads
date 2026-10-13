"""Enrichissement des entreprises (API Recherche d'entreprises) et exclusions dures.

Les deux API utilisées sont ouvertes et sans clé :
- https://recherche-entreprises.api.gouv.fr (SIRENE + RNE, mise à jour quotidienne)
- BODACC sur bodacc-datadila.opendatasoft.com (procédures collectives)
"""

from __future__ import annotations

import logging
import time

import httpx

from .models import Lead, normalize
from .store import Store

log = logging.getLogger(__name__)

SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"
BODACC_URL = (
    "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "annonces-commerciales/records"
)
MIN_INTERVAL = 0.15  # l'API Recherche d'entreprises limite à environ 7 requêtes par seconde


def _same_company(query: str, result: dict) -> bool:
    """Le premier résultat d'une recherche par nom n'est pas forcément la bonne entreprise."""
    wanted = f" {normalize(query)} "
    names = (result.get("nom_complet"), result.get("nom_raison_sociale"), result.get("sigle"))
    return any(wanted in f" {normalize(n)} " or f" {normalize(n)} " in wanted for n in names if n)


class Enricher:
    def __init__(self, client: httpx.Client, store: Store, cooldown_days: int,
                 excluded_companies: list[str] | None = None):
        self.client = client
        self.store = store
        self.cooldown_days = cooldown_days
        self.excluded = [normalize(c) for c in (excluded_companies or []) if c.strip()]
        self._cache: dict[str, dict | None] = {}
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def lookup(self, company: str, siret: str | None = None) -> dict | None:
        query = (siret or "")[:9] or company
        if not query.strip():
            return None
        if query in self._cache:
            return self._cache[query]
        self._throttle()
        try:
            resp = self.client.get(SEARCH_URL, params={"q": query, "per_page": 1, "page": 1})
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except httpx.HTTPError as exc:
            log.warning("Recherche d'entreprises indisponible pour %r : %s", query, exc)
            results = []
        info = None
        if results and not siret and not _same_company(company, results[0]):
            log.info("Recherche d'entreprises : %r ne correspond pas à %r, ignoré",
                     results[0].get("nom_complet"), company)
            results = []
        if results:
            r = results[0]
            siege = r.get("siege") or {}
            info = {
                "siren": r.get("siren"),
                "nom": r.get("nom_complet"),
                "naf": r.get("activite_principale"),
                "categorie": r.get("categorie_entreprise"),
                "tranche_effectif": r.get("tranche_effectif_salarie"),
                "date_creation": r.get("date_creation"),
                "etat": r.get("etat_administratif"),
                "commune": siege.get("libelle_commune"),
                "code_postal": siege.get("code_postal"),
                # Minimisation RGPD : fonction et nom seulement, 3 dirigeants maximum
                "dirigeants": [
                    {"nom": f"{d.get('prenoms', '')} {d.get('nom', '')}".strip(),
                     "qualite": d.get("qualite")}
                    for d in (r.get("dirigeants") or [])[:3]
                    if d.get("type_dirigeant") == "personne physique"
                ],
            }
        self._cache[query] = info
        return info

    def in_collective_procedure(self, siren: str) -> bool:
        """Procédure collective (sauvegarde, redressement, liquidation) publiée au BODACC."""
        self._throttle()
        try:
            resp = self.client.get(BODACC_URL, params={
                "where": f'familleavis="collective" AND registre="{siren}"',
                "limit": 1,
            })
            resp.raise_for_status()
            return resp.json().get("total_count", 0) > 0
        except httpx.HTTPError as exc:
            log.warning("BODACC indisponible pour %s : %s", siren, exc)
            return False  # en cas de doute on garde le lead ; Axel valide de toute façon

    def apply(self, lead: Lead) -> Lead:
        """Complète le lead et pose excluded_reason si une exclusion dure s'applique."""
        name = normalize(lead.company)
        if name and any(ex and ex in name for ex in self.excluded):
            lead.excluded_reason = "entreprise exclue (liste de config)"
            return lead
        if lead.company or lead.extra.get("siret"):
            info = self.lookup(lead.company, lead.extra.get("siret"))
            if info:
                lead.company_info = info
                lead.siren = info["siren"]
                if info.get("etat") == "C":
                    lead.excluded_reason = "entreprise cessée (SIRENE)"
                    return lead
                if lead.siren and self.in_collective_procedure(lead.siren):
                    lead.excluded_reason = "procédure collective au BODACC"
                    return lead
        if self.store.recently_contacted(lead.company_key, self.cooldown_days):
            lead.excluded_reason = f"déjà contactée depuis moins de {self.cooldown_days} jours"
        return lead
