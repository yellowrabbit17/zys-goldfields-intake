"""Admin Notification & Persistent Backup Dispatcher (notifier.py).

Render's free tier uses an ephemeral filesystem: grants.db, signed agreements, and packets are wiped on
every redeploy or restart. This module copies every intake event OFF the instance the moment it happens:

1. Admin email with attachments (signed_advisory_agreement.md, esignature_audit_record.json,
   client_submission_packet.md) via Resend (HTTPS API) or any SMTP server.
2. JSON POST of the full record + file contents to a persistent backup webhook (e.g., Make / Zapier /
   n8n / Google Apps Script / Pipedream / your own API). Requests are HMAC-SHA256 signed when
   BACKUP_WEBHOOK_SECRET is set (header: X-ZYS-Signature: sha256=<hex>).
3. A local append-only backups/submissions.jsonl journal (best effort; survives restarts only on
   persistent disks, but is useful locally and for debugging).

Secrets are read ONLY from environment variables, never from config.json:
  RESEND_API_KEY | SMTP_USERNAME, SMTP_PASSWORD | BACKUP_WEBHOOK_SECRET
Non-secret settings come from config.json "notifications" and can be overridden by env vars:
  NOTIFICATION_PROVIDER, ADMIN_NOTIFICATION_EMAIL, NOTIFICATION_FROM_EMAIL, SMTP_HOST, SMTP_PORT,
  SMTP_USE_TLS, BACKUP_WEBHOOK_URL
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import smtplib
import threading
from typing import Any

import requests

from models import MatcherConfig

logger = logging.getLogger("notifier")

BACKUP_JOURNAL_PATH = Path(__file__).resolve().parent / "backups" / "submissions.jsonl"
RESEND_API_URL = "https://api.resend.com/emails"
_JOURNAL_LOCK = threading.Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_settings(cfg: MatcherConfig) -> dict[str, Any]:
    """Merge config.json notification settings with environment variable overrides and secrets."""
    n = cfg.notifications
    settings = {
        "provider": (os.environ.get("NOTIFICATION_PROVIDER") or n.provider or "auto").strip().lower(),
        "admin_email": (os.environ.get("ADMIN_NOTIFICATION_EMAIL") or n.admin_email).strip(),
        "from_email": (os.environ.get("NOTIFICATION_FROM_EMAIL") or n.from_email).strip(),
        "smtp_host": (os.environ.get("SMTP_HOST") or n.smtp_host).strip(),
        "smtp_port": int(os.environ.get("SMTP_PORT") or n.smtp_port),
        "smtp_use_tls": _env_bool("SMTP_USE_TLS", n.smtp_use_tls),
        "smtp_username": os.environ.get("SMTP_USERNAME", "").strip(),
        "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
        "resend_api_key": os.environ.get("RESEND_API_KEY", "").strip(),
        "backup_webhook_url": (os.environ.get("BACKUP_WEBHOOK_URL") or n.backup_webhook_url).strip(),
        "backup_webhook_secret": os.environ.get("BACKUP_WEBHOOK_SECRET", ""),
        "notify_on_preview": n.notify_on_preview,
        "timeout": n.timeout_seconds,
    }
    if settings["provider"] == "auto":
        if settings["resend_api_key"]:
            settings["provider"] = "resend"
        elif settings["smtp_host"]:
            settings["provider"] = "smtp"
        else:
            settings["provider"] = "none"
    return settings


def notification_status(cfg: MatcherConfig) -> dict[str, Any]:
    """Non-sensitive readiness summary (safe to expose to an admin health check)."""
    s = resolve_settings(cfg)
    return {
        "email_provider": s["provider"],
        "email_ready": bool(
            s["admin_email"]
            and s["from_email"]
            and (
                (s["provider"] == "resend" and s["resend_api_key"])
                or (s["provider"] == "smtp" and s["smtp_host"])
            )
        ),
        "backup_webhook_ready": bool(s["backup_webhook_url"]),
        "webhook_signed": bool(s["backup_webhook_secret"]),
    }


def _append_journal(entry: dict[str, Any]) -> None:
    try:
        BACKUP_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _JOURNAL_LOCK, BACKUP_JOURNAL_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - best effort only
        logger.warning("Could not append local backup journal: %s", exc)


def _send_via_resend(s: dict[str, Any], subject: str, body: str, attachments: dict[str, str]) -> str:
    payload = {
        "from": s["from_email"],
        "to": [s["admin_email"]],
        "subject": subject,
        "text": body,
        "attachments": [
            {"filename": name, "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
            for name, content in attachments.items()
        ],
    }
    resp = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {s['resend_api_key']}", "Content-Type": "application/json"},
        json=payload,
        timeout=s["timeout"],
    )
    resp.raise_for_status()
    return f"resend:{resp.json().get('id', 'sent')}"


def _send_via_smtp(s: dict[str, Any], subject: str, body: str, attachments: dict[str, str]) -> str:
    msg = EmailMessage()
    msg["From"] = s["from_email"]
    msg["To"] = s["admin_email"]
    msg["Subject"] = subject
    msg.set_content(body)
    for name, content in attachments.items():
        subtype = "json" if name.endswith(".json") else "markdown"
        maintype = "application" if subtype == "json" else "text"
        msg.add_attachment(content.encode("utf-8"), maintype=maintype, subtype=subtype, filename=name)

    if s["smtp_port"] == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(s["smtp_host"], s["smtp_port"], timeout=s["timeout"])
    else:
        server = smtplib.SMTP(s["smtp_host"], s["smtp_port"], timeout=s["timeout"])
    with server:
        if s["smtp_port"] != 465 and s["smtp_use_tls"]:
            server.starttls()
        if s["smtp_username"]:
            server.login(s["smtp_username"], s["smtp_password"])
        server.send_message(msg)
    return "smtp:sent"


def _post_backup_webhook(s: dict[str, Any], envelope: dict[str, Any]) -> str:
    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "ZYS-GoldFields-Backup/1.0"}
    if s["backup_webhook_secret"]:
        sig = hmac.new(s["backup_webhook_secret"].encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-ZYS-Signature"] = f"sha256={sig}"
    resp = requests.post(s["backup_webhook_url"], data=body, headers=headers, timeout=s["timeout"])
    resp.raise_for_status()
    return f"webhook:{resp.status_code}"


def _compose_email(event: str, record: dict[str, Any], cfg: MatcherConfig) -> tuple[str, str]:
    signed = event == "agreement_signed"
    who = record.get("client_name", "Unknown client")
    apn = record.get("apn", "")
    county = record.get("county", "")
    if signed:
        subject = f"[SIGNED] {who} — APN {apn} ({county} County) — {record.get('matched_grant_title', '')}"
    else:
        subject = f"[LEAD PREVIEW] {who} — APN {apn} ({county} County)"
    lines = [
        f"{cfg.firm_name} — automated intake backup ({event})",
        "",
        f"Client:            {who} <{record.get('client_email', '')}>",
        f"Prospect ID:       {record.get('prospect_id') or '(organic / no magic link)'}",
        f"EIN:               {record.get('ein') or '(not provided)'}",
        f"Parcel:            APN {apn}, {record.get('acreage', '')} ac, {county} County, CA",
        f"Matched grant:     {record.get('matched_grant_title', '')} ({record.get('matched_grant_id', '')})",
        f"Eligibility:       {record.get('eligibility_tier', '')}",
        f"Internal score:    {record.get('probability_score', '')}",
        f"Grant request:     {record.get('engineered_grant_request', '')}",
        f"Deferred fee:      {record.get('contingent_fee_upon_award', '')} (waived if not awarded)",
    ]
    if signed:
        lines += [
            "",
            "E-signature audit trail (California UETA / ESIGN):",
            f"  Signer:          {record.get('signer_full_name', '')} — {record.get('signer_title', '')}",
            f"  UTC timestamp:   {record.get('signature_timestamp_utc', '')}",
            f"  IP address:      {record.get('signer_ip_address', '')}",
            f"  User-Agent:      {record.get('signer_user_agent', '')}",
            f"  SHA-256:         {record.get('contract_sha256_hash', '')}",
        ]
    lines += [
        "",
        "Render's filesystem is ephemeral — this email and its attachments are the system of record.",
    ]
    return subject, "\n".join(lines)


def dispatch_intake_backup(
    event: str,
    record: dict[str, Any],
    attachments: dict[str, str],
    cfg: MatcherConfig,
) -> dict[str, str]:
    """Synchronously email the admin, POST the backup webhook, and append the local journal.

    Never raises: each channel's outcome is returned so a delivery failure cannot break client intake.
    """
    s = resolve_settings(cfg)
    results: dict[str, str] = {}
    envelope = {
        "event": event,
        "dispatched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firm": cfg.firm_name,
        "record": record,
        "files": attachments,
    }

    if event != "agreement_signed" and not s["notify_on_preview"]:
        results["skipped"] = "notify_on_preview=false"
        return results

    # 1. Admin email
    if s["provider"] in ("resend", "smtp") and s["admin_email"] and s["from_email"]:
        subject, body = _compose_email(event, record, cfg)
        try:
            sender = _send_via_resend if s["provider"] == "resend" else _send_via_smtp
            results["email"] = sender(s, subject, body, attachments)
        except Exception as exc:  # noqa: BLE001 - delivery must never break intake
            results["email"] = f"error:{type(exc).__name__}: {exc}"
            logger.error("Admin email delivery failed for %s: %s", record.get("client_name"), exc)
    else:
        results["email"] = "not_configured"

    # 2. Persistent backup webhook
    if s["backup_webhook_url"]:
        try:
            results["webhook"] = _post_backup_webhook(s, envelope)
        except Exception as exc:  # noqa: BLE001
            results["webhook"] = f"error:{type(exc).__name__}: {exc}"
            logger.error("Backup webhook delivery failed for %s: %s", record.get("client_name"), exc)
    else:
        results["webhook"] = "not_configured"

    # 3. Local journal (record only; file bodies omitted to keep the journal small)
    _append_journal({**{k: v for k, v in envelope.items() if k != "files"}, "delivery": results})
    logger.info("Intake backup dispatched (%s) for %s: %s", event, record.get("client_name"), results)
    return results


def dispatch_intake_backup_async(
    event: str,
    record: dict[str, Any],
    attachments: dict[str, str],
    cfg: MatcherConfig,
) -> threading.Thread:
    """Fire-and-forget wrapper so the client's browser never waits on SMTP/webhook latency."""
    t = threading.Thread(
        target=dispatch_intake_backup,
        args=(event, record, attachments, cfg),
        name=f"zys-backup-{event}",
        daemon=True,
    )
    t.start()
    return t
