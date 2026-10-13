"""Ligne de commande : leads run | status | list | export."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from .config import Config, load_dotenv
from .digest import export_csv
from .models import Status
from .pipeline import run
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leads", description="Prospection Lienos")
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Collecter, noter et produire le digest du jour")
    p_run.add_argument("--no-email", action="store_true", help="Écrire le digest sans l'envoyer")

    p_status = sub.add_parser("status", help="Changer le statut d'un lead (id du digest)")
    p_status.add_argument("lead_id")
    p_status.add_argument("status", choices=[s.value for s in Status])
    p_status.add_argument("--note", default="", help="Motif, surtout pour un rejet")

    p_list = sub.add_parser("list", help="Lister les leads récents")
    p_list.add_argument("--days", type=int, default=7)
    p_list.add_argument("--min-score", type=int, default=0)
    p_list.add_argument("--status", choices=[s.value for s in Status])

    p_export = sub.add_parser("export", help="Exporter le tracker en CSV")
    p_export.add_argument("--out", default="output/tracker.csv")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = Config.load(args.config)

    if args.cmd == "run":
        stats = run(cfg, send=not args.no_email)
        mode = "LLM" if stats["llm"] else "heuristique (pas de ANTHROPIC_API_KEY)"
        if stats["llm_fallbacks"]:
            mode += f", {stats['llm_fallbacks']} en repli heuristique après échec de l'appel"
        print(f"{stats['selected']} leads retenus sur {stats['new']} nouveaux "
              f"(scoring {mode}). Digest : {stats['digest_html']}")
        return 0

    store = Store(cfg.db_path)
    try:
        if args.cmd == "status":
            if not store.set_status(args.lead_id, Status(args.status), args.note):
                print(f"Aucun lead unique ne correspond à l'id « {args.lead_id} ».", file=sys.stderr)
                return 1
            print(f"{args.lead_id} → {args.status}")
        elif args.cmd == "list":
            since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
            for row in store.leads_since(since, args.min_score):
                if args.status and row["status"] != args.status:
                    continue
                print(f"{row['id'][:8]}  {row['score'] or 0:>3}  {row['status']:<11} "
                      f"{(row['company'] or '?')[:28]:<28}  {row['title'][:70]}")
        elif args.cmd == "export":
            path = export_csv(store.all_leads(), args.out)
            print(f"Tracker exporté : {path}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
