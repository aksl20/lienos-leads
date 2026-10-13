# lienos-leads

Veille et scoring de prospects pour l'activité freelance de Lienos (Axel).
Vue d'ensemble, installation et routine quotidienne : voir README.md.

## Commandes

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest                       # 11 tests, aucun appel réseau
.venv/bin/leads -v run --no-email      # -v se place avant la sous-commande
.venv/bin/leads list --days 1 --min-score 70
```

Le Python système du poste est en 3.9 : toujours passer par `.venv` (3.11 minimum).

## Conventions

- Code, commentaires, journaux et messages en français ; identifiants en anglais.
- Secrets uniquement dans `.env` (lu par `load_dotenv()`), jamais dans `config.toml`, le code,
  les fixtures ou les journaux. Les jetons vont en en-tête HTTP, pas dans l'URL : httpx
  journalise les URL complètes en mode `-v`.
- Une source = un module dans `sources/` avec `fetch(cfg, client)` et `to_lead(item)`.
  Une source en panne ne bloque pas le digest (`pipeline.collect`).
- Tests avec respx, sans réseau. Les réponses réelles servent de fixtures dans `tests/fixtures/`,
  après retrait des données personnelles (noms de recruteurs, e-mails, téléphones).
- RGPD : au plus 3 dirigeants (nom et fonction), aucune coordonnée de recruteur conservée.
- Les appels payants (Apify) se font avec un plafond explicite et après accord d'Axel.

## État (13 octobre 2026)

| Brique | État |
| --- | --- |
| Free-Work via Apify | Validé sur données réelles ; activé dans `config.toml` |
| Scoring LLM (`claude-haiku-5-5`) | Validé sur 18 missions réelles |
| Enrichissement (Recherche d'entreprises, BODACC) | Fonctionne ; un résultat trouvé par nom n'est gardé que si le nom concorde |
| Run quotidien en local (`leads run --no-email`) | Testé le 13 octobre 2026 en mode incrémental |
| France Travail | Bloqué : accès à l'API restreint, demande à faire |
| Envoi du digest par e-mail (SMTP Zoho) | Validé le 13 octobre 2026 ; `send_email = true` |
| GitHub Actions | Dépôt git local prêt ; dépôt GitHub privé, secrets et variable `CONFIG_TOML` à créer |
| RSS, alertes Malt | Jamais testés en réel, désactivés |

### Free-Work (Apify)

- Acteur `koatiromeo~french-it-contract-market`, 0,008 $ par mission livrée, plus 0,10 $ par
  benchmark de TJM : garder `computeRateBenchmark = false`.
- Entrée réelle : `sources`, `query`, `maxMissions`, `sinceDays`, `contractorOnly`,
  `onlyNewSinceLastRun`. `query` exige tous les mots à la fois.
- Sortie réelle : `rate_min`, `rate_max`, `rate_period`, `currency`, `city`, `region`,
  `contracts`, `skills`, `remote`, `published_at`. Fixture : `tests/fixtures/apify_freework_sample.json`.
- Le plancher `min_daily_rate` se compare à `rate_max` ; les missions sans TJM sont gardées.
- `config.toml` est en réglages quotidiens : incrémental, `sinceDays = 3`, `maxMissions = 100`.
- Run quotidien du 13 octobre 2026 : 8 missions livrées (0,06 $), 7 au-dessus du plancher, toutes
  déjà en base, donc 0 nouveau lead. L'acteur n'avait pas mémorisé les runs non incrémentaux.
- L'acteur est lancé en asynchrone (`/runs`, attente par `waitForFinish`, lecture du jeu de
  données), avec abandon au bout de 900 s : l'appel synchrone d'Apify est limité à 300 s et le
  run incrémental du 13 octobre en avait pris 183.
- Ne pas tester en mode incrémental hors d'un vrai `leads run` : les missions livrées sont
  mémorisées par l'acteur et ne reviendraient plus. Pour un essai, `onlyNewSinceLastRun = false`
  et `maxMissions = 1`.
- Quatre runs au total, environ 0,55 $.

### Scoring

- `claude-haiku-5-5` réfléchit par défaut et cette réflexion est décomptée de `max_tokens` :
  à 600, le JSON arrivait tronqué. Réglé à 4000 avec `output_config.effort = "low"`.
- Premier lot réel : 7 missions sur 18 au-dessus du seuil de 70.

### France Travail

- L'API Offres d'emploi n'est plus en libre-service (accès « Restreint », demande via
  https://francetravail.io/contact). Le scope `api_offresdemploiv2 o2dsoffre` est refusé.
- L'application d'Axel n'a que les scopes fiches métiers ROME, statistiques et événements.
- `france_travail.py` n'a jamais été confronté à une réponse réelle.

## À valider

1. France Travail : champs réels comparés à `to_lead()`, pagination (200 / 206 / 204,
   `Content-Range`), fixture `tests/fixtures/france_travail_sample.json` et test respx.
   En attente de l'accès.
2. Enrichissement sur des entreprises issues de France Travail.
3. Flux RSS (URL Maddyness, Google Alerts), alertes Malt par IMAP.
4. Premier run GitHub Actions (`workflow_dispatch`) une fois le dépôt privé créé.

## Défauts connus

- Enrichissement : les sociétés étrangères et les plateformes ne sont pas trouvées, et un nom
  commercial différent de la raison sociale est désormais ignoré plutôt que mal apparié.
- `.github/workflows/daily.yml` échoue volontairement sans variable `CONFIG_TOML` : après toute
  modification de `config.toml`, recoller son contenu dans la variable du dépôt GitHub.
- Le cron du workflow (5 h 45 UTC) correspond à 7 h 45 à Paris en heure d'été seulement.
- Sur Free-Work, l'« entreprise » est presque toujours un intermédiaire (ESN, cabinet) :
  l'enrichissement et le critère « pas d'équipe IA interne » portent sur lui, pas sur le client final.
- Un lead déjà en base n'est jamais renoté par `leads run` ; il n'existe pas de commande de renotation.
- BODACC : `registre` est une liste (`["845219153", "845 219 153"]`), interrogée par
  `registre="{siren}"` (vérifié sur données réelles).
