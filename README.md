# lienos-leads

Veille et scoring automatiques de prospects pour l'activité freelance de Lienos.
Chaque jour ouvré, le script collecte des signaux (missions, offres d'emploi, levées de fonds),
les enrichit, les note avec Claude et envoie un digest des leads ≥ 70/100.
La prise de contact reste manuelle.

```
sources ─► dédoublonnage ─► enrichissement + exclusions ─► scoring LLM ─► SQLite ─► digest
                                                               ▲                     │
                                                               └── statuts et rejets ┘
```

## Installation (5 minutes)

```bash
git clone <votre dépôt privé> && cd lienos-leads
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.toml config.toml   # réglages
cp .env.example .env                 # secrets
pytest                               # 10 tests, API simulées
leads run --no-email                 # premier essai : digest dans output/
```

Sans `ANTHROPIC_API_KEY`, un score heuristique remplace le LLM : utile pour tester la chaîne.

## Brancher les sources, dans l'ordre du plan

| Semaine | Source | À faire | Réglage |
| --- | --- | --- | --- |
| 1 | Alertes Malt | Activer les alertes projets Malt vers contact@lienos.fr ; mot de passe d'application Zoho | `[malt_mail] enabled = true`, `IMAP_*` |
| 1 | Digest e-mail | Mot de passe d'application Zoho (SMTP) | `[digest] send_email = true`, `SMTP_*` |
| 1 | Free-Work (option) | Relire les CGU Free-Work, créer un compte Apify | `[apify] enabled = true`, `APIFY_TOKEN` |
| 2 | France Travail | Créer une application sur francetravail.io, souscrire « Offres d'emploi v2 » | `FT_CLIENT_ID`, `FT_CLIENT_SECRET` |
| 3 | Levées de fonds | Créer des Google Alerts en mode « Flux RSS » ; vérifier l'URL du flux Maddyness | `[rss.feeds]` |

L'enrichissement (API Recherche d'entreprises, BODACC) est sans clé.

## Routine quotidienne

```bash
leads list --days 1 --min-score 70          # ou lire le digest reçu par e-mail
leads status 5c87a908 a_contacter
leads status 76249d4c rejete --note "pas de besoin ML, juste du reporting"
leads status 5c87a908 contacte              # l'entreprise sort des radars 90 jours
leads export                                # output/tracker.csv (s'ouvre dans Excel)
```

Les rejets motivés et les leads retenus sont réinjectés dans le prompt de scoring
(12 décisions les plus récentes) : plus vous motivez vos rejets, plus le tri s'affine.

Statuts : `nouveau`, `a_contacter`, `plus_tard`, `contacte`, `repondu`, `rejete`.

## Planification

**GitHub Actions (dépôt privé obligatoire, la base y est commitée).**
`.github/workflows/daily.yml` tourne à 7 h 45 heure de Paris du lundi au vendredi.
Renseigner les secrets du `.env` dans *Settings → Secrets and variables → Actions* et
coller votre `config.toml` dans la variable `CONFIG_TOML` (obligatoire).
Avant de changer des statuts en local : `git pull`, puis `git push` juste après,
pour ne pas entrer en conflit avec le run suivant.

**Ou en local (cron).** `45 7 * * 1-5 cd ~/lienos-leads && .venv/bin/leads run`

## Grille de scoring

| Critère | Poids |
| --- | --- |
| Adéquation technique (vision, MIL, SSL, omics, fédéré, LLM/RAG) | 30 |
| Fraîcheur du signal (< 14 jours) | 20 |
| Capacité à payer (levée ≥ 2 M€, ETI, TJM ≥ 650 €) | 15 |
| Pas d'équipe IA interne | 15 |
| Format compatible freelance | 10 |
| Géographie (IDF ou remote) | 10 |

Le LLM note chaque critère ; le total est recalculé et borné dans `scoring.py`.
Exclusions avant scoring : entreprise cessée (SIRENE), procédure collective (BODACC),
contactée il y a moins de 90 jours, liste `excluded_companies`.

## RGPD

Données stockées : entreprise, intitulé, lien, score ; au plus 3 dirigeants (nom et fonction,
issus du registre public). Aucune collecte LinkedIn. Pensez à tenir un registre du traitement
et à purger les contacts sans réponse après 12 mois.

## Structure

```
src/lienos_leads/
  sources/france_travail.py  API Offres d'emploi v2 (OAuth2, pagination 206)
  sources/rss.py             Google Alerts, Maddyness, tout flux RSS
  sources/malt_mail.py       Alertes Malt par IMAP (lecture seule)
  sources/freework_apify.py  Free-Work via Apify (optionnel)
  enrich.py                  Recherche d'entreprises + BODACC + exclusions
  scoring.py                 Prompt, grille, boucle d'apprentissage, repli heuristique
  digest.py                  Digest HTML/texte, envoi SMTP, export CSV
  pipeline.py                Orchestration
  store.py                   SQLite : dédoublonnage, statuts
  cli.py                     leads run | status | list | export
```
