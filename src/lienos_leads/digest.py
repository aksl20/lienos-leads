"""Digest quotidien (HTML + texte) et export CSV du tracker."""

from __future__ import annotations

import csv
import html
import logging
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from .config import DigestConfig, secret
from .models import Lead, Segment

log = logging.getLogger(__name__)

SEGMENT_LABELS = {
    Segment.MISSION: "Missions freelance — candidater aujourd'hui",
    Segment.OFFRE_EMPLOI: "Offres d'emploi — proposer une mission en attendant le recrutement",
    Segment.LEVEE: "Levées de fonds et actualités — approche à froid",
}


def _group(leads: list[Lead]) -> dict[Segment, list[Lead]]:
    groups: dict[Segment, list[Lead]] = {s: [] for s in SEGMENT_LABELS}
    for lead in sorted(leads, key=lambda l: l.score or 0, reverse=True):
        groups[lead.segment].append(lead)
    return groups


def render_text(leads: list[Lead], stats: dict, day: date) -> str:
    lines = [f"Digest prospects Lienos — {day:%d/%m/%Y}",
             f"{len(leads)} leads au-dessus du seuil ; {stats_line(stats)}", ""]
    for segment, items in _group(leads).items():
        if not items:
            continue
        lines.append(f"== {SEGMENT_LABELS[segment]} ({len(items)})")
        for l in items:
            company = l.company or "?"
            lines.append(f"[{l.score}] {l.title} — {company}  (id {l.fingerprint[:8]})")
            if l.explanation:
                lines.append(f"    Pourquoi : {l.explanation}")
            if l.approach:
                lines.append(f"    Angle : {l.approach}")
            if l.url:
                lines.append(f"    {l.url}")
        lines.append("")
    lines.append("Trier : leads status <id> a_contacter | plus_tard | rejete --note \"motif\"")
    return "\n".join(lines)


def stats_line(stats: dict) -> str:
    return (f"{stats.get('collected', 0)} collectés, {stats.get('new', 0)} nouveaux, "
            f"{stats.get('excluded', 0)} exclus, {stats.get('scored', 0)} notés"
            + (f" dont {stats['llm_fallbacks']} par l'heuristique (appel LLM en échec)"
               if stats.get("llm_fallbacks") else ""))


def render_html(leads: list[Lead], stats: dict, day: date) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        f"<title>Digest prospects {day:%d/%m/%Y}</title>",
        "<style>body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#1f2328;"
        "max-width:760px;margin:24px auto;padding:0 16px}h1{font-size:20px}"
        "h2{font-size:16px;margin-top:28px;border-bottom:1px solid #d0d7de;padding-bottom:4px}"
        ".lead{margin:14px 0;padding:10px 12px;border:1px solid #d0d7de;border-radius:8px}"
        ".score{display:inline-block;min-width:34px;text-align:center;font-weight:600;"
        "border-radius:6px;padding:0 6px;margin-right:6px;background:#dbeafe;color:#1e3a8a}"
        ".muted{color:#57606a;font-size:13px}a{color:#0969da}</style></head><body>",
        f"<h1>Digest prospects Lienos — {day:%d/%m/%Y}</h1>",
        f"<p class='muted'>{len(leads)} leads au-dessus du seuil · {e(stats_line(stats))}</p>",
    ]
    for segment, items in _group(leads).items():
        if not items:
            continue
        parts.append(f"<h2>{e(SEGMENT_LABELS[segment])} ({len(items)})</h2>")
        for l in items:
            title = e(l.title)
            if l.url:
                title = f"<a href='{e(l.url, quote=True)}'>{title}</a>"
            meta = " · ".join(x for x in [l.company, l.location, l.source,
                                          f"TJM {l.daily_rate:.0f} €" if l.daily_rate else ""] if x)
            parts.append("<div class='lead'>")
            parts.append(f"<div><span class='score'>{l.score}</span><strong>{title}</strong></div>")
            parts.append(f"<div class='muted'>{e(meta)} · id <code>{l.fingerprint[:8]}</code></div>")
            if l.explanation:
                parts.append(f"<div><b>Pourquoi :</b> {e(l.explanation)}</div>")
            if l.approach:
                parts.append(f"<div><b>Angle :</b> {e(l.approach)}</div>")
            parts.append("</div>")
    if not leads:
        parts.append("<p>Aucun lead au-dessus du seuil aujourd'hui.</p>")
    parts.append("<p class='muted'>Trier : <code>leads status &lt;id&gt; a_contacter | plus_tard | "
                 "rejete --note \"motif\"</code></p></body></html>")
    return "\n".join(parts)


def write_files(leads: list[Lead], stats: dict, cfg: DigestConfig, day: date) -> tuple[Path, Path]:
    out = Path(cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"digest-{day:%Y-%m-%d}.html"
    text_path = out / f"digest-{day:%Y-%m-%d}.txt"
    html_path.write_text(render_html(leads, stats, day), encoding="utf-8")
    text_path.write_text(render_text(leads, stats, day), encoding="utf-8")
    return html_path, text_path


def send_email(leads: list[Lead], stats: dict, cfg: DigestConfig, day: date) -> None:
    if not cfg.send_email:
        return
    msg = EmailMessage()
    msg["Subject"] = f"[Lienos] {len(leads)} prospects — {day:%d/%m}"
    msg["From"] = secret("SMTP_USER", required=True)
    msg["To"] = cfg.to or msg["From"]
    msg.set_content(render_text(leads, stats, day))
    msg.add_alternative(render_html(leads, stats, day), subtype="html")
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context()) as s:
        s.login(secret("SMTP_USER", required=True), secret("SMTP_PASSWORD", required=True))
        s.send_message(msg)
    log.info("Digest envoyé à %s", msg["To"])


EXPORT_COLUMNS = ["id", "first_seen", "status", "status_note", "score", "segment", "source",
                  "company", "title", "url"]


def export_csv(rows: list[dict], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig : s'ouvre bien dans Excel
        writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "id": row["id"][:8]})
    return p
