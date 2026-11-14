"""Automated Outreach Batch & Day 4 / Day 9 Follow-Up Scheduler (outreach_scheduler.py).

Implements:
1. Template parsing from `outreach_pitches.md` (Day 0 Initial Brief) and
   `followup_templates.md` (Day 4 & Day 9 follow-ups), dynamically rewriting
   magic links to the configured `BASE_URL` (e.g. https://portal.zysgoldfields.pro)
   and unescaping KaTeX dollar signs (`\$` -> `$`) into crisp plain-text & HTML emails.
2. Safe domain warm-up batch rate limiting via `DAILY_SEND_LIMIT` (default: 8/day).
3. Automated Day 4 (>= 92h after Day 0) and Day 9 (>= 212h after Day 0 & >= 72h after Day 4)
   progression with priority ordering (Day 9 -> Day 4 -> Day 0), max 1 email per prospect/run.
4. Claimed/signed/unsubscribed suppression via `client_intakes` and `prospect_outreach_status`,
   plus atomic `(prospect_id, sequence_step)` reservation locks in `outreach_send_log` so no
   prospect is ever emailed twice for the same cadence step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import logging
from pathlib import Path
import re
from typing import Any, Callable

from db import (
    DEFAULT_DB_PATH,
    database_backend_info,
    fetch_all_prospects,
    fetch_claimed_and_signed_identities,
    fetch_outreach_send_logs,
    fetch_outreach_statuses,
    fetch_sent_steps_map,
    finalize_outreach_send,
    get_daily_send_count_utc,
    release_outreach_reservation,
    reserve_outreach_send,
    sync_outreach_status_from_prospects,
)
from matcher import DEFAULT_CONFIG_PATH, DEFAULT_FOLLOWUP_PATH, DEFAULT_OUTPUT_PATH, load_matcher_config
from models import (
    MatcherConfig,
    ProspectRecord,
    build_claim_url,
    resolve_base_url,
    resolve_daily_send_limit,
    rewrite_portal_urls,
)
from notifier import notification_status, send_transactional_email

logger = logging.getLogger("outreach_scheduler")

DAY4_MIN_HOURS_AFTER_DAY0 = 92   # ~4 days minus 4h daily-cron jitter allowance
DAY9_MIN_HOURS_AFTER_DAY0 = 212  # ~9 days minus 4h daily-cron jitter allowance
DAY9_MIN_HOURS_AFTER_DAY4 = 72   # >= 3 days after Day 4 follow-up

TERMINAL_OR_SUPPRESSED_STATUSES = {
    "claimed",
    "signed",
    "unsubscribed",
    "bounced",
    "completed",
}


def _parse_iso_utc(raw: str) -> datetime | None:
    val = (raw or "").strip()
    if not val:
        return None
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _strip_subject_markdown(raw_subject: str) -> str:
    txt = raw_subject.replace(r"\$", "$").replace(r"\|", "|")
    txt = re.sub(r"\*\*(.*?)\*\*", r"\1", txt)
    txt = re.sub(r"\*(.*?)\*", r"\1", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _extract_subject_and_body(section_md: str) -> tuple[str, str] | None:
    """Split a '**Subject:** ...' block from its email body markdown."""
    m = re.search(r"(?m)^\*\*Subject:\*\*\s*(.+)$", section_md)
    if not m:
        return None
    subject = _strip_subject_markdown(m.group(1))
    body = section_md[m.end() :].strip()
    # Trim section boundary trailing dividers cleanly without removing internal CAN-SPAM '---'
    body = re.sub(r"(?:\n\s*---+\s*)+$", "", body).strip()
    return subject, body


def parse_initial_pitches(pitches_path: Path | str = DEFAULT_OUTPUT_PATH) -> dict[str, dict[str, str]]:
    """Parse Day 0 initial cold-outreach emails from outreach_pitches.md keyed by prospect_id."""
    fp = Path(pitches_path)
    if not fp.exists():
        return {}
    text = fp.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)(?=^## Tier-1 Outreach Brief #\d+:)", text)
    parsed: dict[str, dict[str, str]] = {}

    for block in blocks:
        if not block.startswith("## Tier-1 Outreach Brief #"):
            continue
        pid_match = re.search(r"/claim\?prospect_id=([A-Za-z0-9_-]+)", block)
        if not pid_match:
            continue
        prospect_id = pid_match.group(1).strip()
        letter_match = re.search(r"(?m)^### Cold-Outreach Letter[^\n]*\n+", block)
        if not letter_match:
            continue
        extracted = _extract_subject_and_body(block[letter_match.end() :])
        if not extracted:
            continue
        subject, body_md = extracted
        parsed[prospect_id] = {
            "prospect_id": prospect_id,
            "subject": subject,
            "body_md": body_md,
        }
    return parsed


def parse_followup_sequences(followup_path: Path | str = DEFAULT_FOLLOWUP_PATH) -> dict[str, dict[str, dict[str, str]]]:
    """Parse Day 4 and Day 9 follow-up emails from followup_templates.md keyed by prospect_id."""
    fp = Path(followup_path)
    if not fp.exists():
        return {}
    text = fp.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)(?=^## Follow-Up Sequences #\d+:)", text)
    parsed: dict[str, dict[str, dict[str, str]]] = {}

    for block in blocks:
        if not block.startswith("## Follow-Up Sequences #"):
            continue
        pid_match = re.search(r"/claim\?prospect_id=([A-Za-z0-9_-]+)", block)
        if not pid_match:
            continue
        prospect_id = pid_match.group(1).strip()

        day4_marker = re.search(r"(?m)^### Day 4 Follow-Up Email[^\n]*\n+", block)
        day9_marker = re.search(r"(?m)^### Day 9 Follow-Up Email[^\n]*\n+", block)
        entry: dict[str, dict[str, str]] = {}

        if day4_marker:
            end_pos = day9_marker.start() if day9_marker else len(block)
            day4_extracted = _extract_subject_and_body(block[day4_marker.end() : end_pos])
            if day4_extracted:
                entry["day4_followup"] = {
                    "prospect_id": prospect_id,
                    "subject": day4_extracted[0],
                    "body_md": day4_extracted[1],
                }
        if day9_marker:
            day9_extracted = _extract_subject_and_body(block[day9_marker.end() :])
            if day9_extracted:
                entry["day9_followup"] = {
                    "prospect_id": prospect_id,
                    "subject": day9_extracted[0],
                    "body_md": day9_extracted[1],
                }
        if entry:
            parsed[prospect_id] = entry
    return parsed


def _inline_md_to_text(line: str) -> str:
    txt = line.replace(r"\$", "$").replace(r"\|", "|")
    txt = re.sub(r"\*\*\[([^\]]+)\]\((https?://[^)]+)\)\*\*", r"\1: \2", txt)
    txt = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", txt)
    txt = re.sub(r"\*\*(.*?)\*\*", r"\1", txt)
    txt = re.sub(r"\*(.*?)\*", r"\1", txt)
    txt = txt.replace("`", "")
    return txt.rstrip()


def _inline_md_to_html(text: str) -> str:
    """Convert a line of Markdown inline syntax to safe HTML."""
    # First tokenize links so their inner text & URLs are formatted cleanly
    parts: list[str] = []
    pattern = re.compile(r"(\*\*\[([^\]]+)\]\((https?://[^)]+)\)\*\*|\[([^\]]+)\]\((https?://[^)]+)\))")
    pos = 0
    for m in pattern.finditer(text):
        before = text[pos : m.start()]
        if before:
            parts.append(_format_inline_emphasis_html(before))
        if m.group(2) is not None:
            label = html.escape(m.group(2))
            url = html.escape(m.group(3), quote=True)
            is_cta = "/claim" in m.group(3)
            if is_cta:
                parts.append(
                    f'<a href="{url}" style="display:inline-block;background:#1f4e37;color:#ffffff;'
                    f'text-decoration:none;font-weight:600;padding:11px 18px;border-radius:6px;">{label}</a>'
                )
            else:
                parts.append(f'<a href="{url}" style="color:#1f4e37;font-weight:600;">{label}</a>')
        else:
            label = html.escape(m.group(4) or "")
            url = html.escape(m.group(5) or "", quote=True)
            parts.append(f'<a href="{url}" style="color:#1f4e37;font-weight:600;">{label}</a>')
        pos = m.end()
    tail = text[pos:]
    if tail:
        parts.append(_format_inline_emphasis_html(tail))
    return "".join(parts)


def _format_inline_emphasis_html(snippet: str) -> str:
    escaped = html.escape(snippet)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)
    return escaped


def render_email_from_template(
    subject_raw: str,
    body_md: str,
    active_base_url: str,
    cfg: MatcherConfig | None = None,
) -> tuple[str, str, str]:
    """Rewrite URLs to active BASE_URL, unescape '$', and render (subject, text_body, html_body)."""
    clean_subject = rewrite_portal_urls(_strip_subject_markdown(subject_raw), active_base_url, cfg=cfg)
    raw_md = rewrite_portal_urls(body_md, active_base_url, cfg=cfg)
    raw_md = raw_md.replace(r"\$", "$").replace(r"\|", "|")

    lines = raw_md.splitlines()
    text_lines: list[str] = []
    html_blocks: list[str] = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;'
        'font-size:15px;line-height:1.6;color:#1b2e24;max-width:660px;margin:0 auto;padding:12px 4px;">'
    ]
    in_list = False
    in_footer = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html_blocks.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            close_list()
            text_lines.append("")
            continue
        if stripped in ("---", "----"):
            close_list()
            text_lines.append("-" * 60)
            html_blocks.append('<hr style="border:none;border-top:1px solid #d9d4c7;margin:20px 0 14px;" />')
            in_footer = True
            continue
        if stripped.startswith("#### "):
            close_list()
            heading = stripped[5:].strip()
            text_lines.append(_inline_md_to_text(heading))
            html_blocks.append(
                f'<h4 style="margin:18px 0 8px;font-size:15px;color:#143625;">{_inline_md_to_html(heading)}</h4>'
            )
            continue
        if stripped.startswith("### "):
            close_list()
            heading = stripped[4:].strip()
            text_lines.append(_inline_md_to_text(heading))
            html_blocks.append(
                f'<h3 style="margin:20px 0 10px;font-size:17px;color:#143625;">{_inline_md_to_html(heading)}</h3>'
            )
            continue
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            text_lines.append(f"  * {_inline_md_to_text(item)}")
            if not in_list:
                html_blocks.append('<ul style="margin:8px 0 14px;padding-left:20px;">')
                in_list = True
            html_blocks.append(f'<li style="margin-bottom:6px;">{_inline_md_to_html(item)}</li>')
            continue

        close_list()
        text_lines.append(_inline_md_to_text(stripped))
        if in_footer:
            html_blocks.append(
                f'<p style="margin:4px 0;font-size:12px;line-height:1.45;color:#5a6960;">{_inline_md_to_html(stripped)}</p>'
            )
        elif stripped.startswith("👉"):
            cta_clean = stripped.lstrip("👉").strip()
            html_blocks.append(
                f'<div style="margin:18px 0;">{_inline_md_to_html(cta_clean)}</div>'
            )
        else:
            html_blocks.append(f'<p style="margin:0 0 12px;">{_inline_md_to_html(stripped)}</p>')

    close_list()
    html_blocks.append("</div>")

    # Collapse >2 consecutive blank lines in plain text
    plain_text = re.sub(r"\n{3,}", "\n\n", "\n".join(text_lines)).strip() + "\n"
    html_text = "\n".join(html_blocks)
    return clean_subject, plain_text, html_text


def _choose_next_step_for_prospect(
    prospect: ProspectRecord,
    status_row: dict[str, Any] | None,
    sent_steps: set[str],
    now: datetime,
) -> tuple[str | None, int]:
    """Return `(sequence_step, priority_rank)` or `(None, 99)` if not eligible now.

    Priority rank:
      0 = day9_followup
      1 = day4_followup
      2 = day0_initial
    """
    st = status_row or {}
    cur_status = str(st.get("status") or "pending").lower()
    if cur_status in TERMINAL_OR_SUPPRESSED_STATUSES:
        return None, 99
    if st.get("claimed_at") or st.get("signed_at") or st.get("unsubscribed_at"):
        return None, 99

    day0_dt = _parse_iso_utc(str(st.get("initial_sent_at") or ""))
    day4_dt = _parse_iso_utc(str(st.get("day4_sent_at") or ""))
    day9_dt = _parse_iso_utc(str(st.get("day9_sent_at") or ""))

    if day0_dt is None and "day0_initial" not in sent_steps:
        return "day0_initial", 2

    if day0_dt is not None and day4_dt is None and "day4_followup" not in sent_steps:
        if (now - day0_dt) >= timedelta(hours=DAY4_MIN_HOURS_AFTER_DAY0):
            return "day4_followup", 1
        return None, 99

    if day0_dt is not None and day4_dt is not None and day9_dt is None and "day9_followup" not in sent_steps:
        if (now - day0_dt) >= timedelta(hours=DAY9_MIN_HOURS_AFTER_DAY0) and (now - day4_dt) >= timedelta(
            hours=DAY9_MIN_HOURS_AFTER_DAY4
        ):
            return "day9_followup", 0
        return None, 99

    return None, 99


def run_outreach_batch(
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    pitches_path: Path | str = DEFAULT_OUTPUT_PATH,
    followup_path: Path | str = DEFAULT_FOLLOWUP_PATH,
    daily_limit: int | None = None,
    max_sends: int | None = None,
    dry_run: bool = False,
    now_utc: datetime | None = None,
    base_url_override: str | None = None,
    email_sender: Callable[..., tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Execute one daily warm-up batch across Day 9, Day 4, and Day 0 outreach cadences."""
    cfg = load_matcher_config(config_path)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_utc = now.strftime("%Y-%m-%d")

    active_base_url = resolve_base_url(cfg, override_base_url=base_url_override)
    effective_limit = max(1, int(daily_limit if daily_limit is not None else resolve_daily_send_limit(cfg)))

    prospects = fetch_all_prospects(db_path=db_path)
    if not prospects:
        return {
            "status": "no_prospects",
            "dry_run": dry_run,
            "timestamp_utc": now_iso,
            "date_utc": today_utc,
            "base_url": active_base_url,
            "db_backend": database_backend_info(db_path),
            "daily_send_limit": effective_limit,
            "sent_today_before": 0,
            "sent_this_run": 0,
            "sent_today_after": 0,
            "sent": [],
            "planned": [],
            "errors": [],
        }

    sync_outreach_status_from_prospects(prospects, db_path=db_path)
    status_map = fetch_outreach_statuses(db_path=db_path)
    claimed_ids, claimed_emails = fetch_claimed_and_signed_identities(db_path=db_path)
    sent_steps_map = fetch_sent_steps_map(db_path=db_path)
    sent_today_before = get_daily_send_count_utc(today_utc, db_path=db_path)

    remaining_quota = max(0, effective_limit - sent_today_before)
    if max_sends is not None:
        remaining_quota = min(remaining_quota, max(0, int(max_sends)))

    initial_templates = parse_initial_pitches(pitches_path)
    followup_templates = parse_followup_sequences(followup_path)

    suppressed_count = 0
    missing_template_count = 0
    eligible_queue: list[tuple[int, str, ProspectRecord, dict[str, str]]] = []
    queue_counts = {"day9_followup": 0, "day4_followup": 0, "day0_initial": 0}

    # Preserve rank ordering CA-T1-001 -> CA-T1-002 -> ...
    ordered_prospects = sorted(
        prospects,
        key=lambda p: (-p.probability_score, -p.estimated_acreage, p.prospect_id),
    )

    for p in ordered_prospects:
        if p.prospect_id.upper() in claimed_ids or p.contact_email.lower() in claimed_emails:
            suppressed_count += 1
            continue

        st_row = status_map.get(p.prospect_id)
        if st_row and str(st_row.get("status") or "").lower() in TERMINAL_OR_SUPPRESSED_STATUSES:
            suppressed_count += 1
            continue

        sent_steps = sent_steps_map.get(p.prospect_id, set())
        step, priority = _choose_next_step_for_prospect(p, st_row, sent_steps, now)
        if step is None:
            continue

        if step == "day0_initial":
            tpl = initial_templates.get(p.prospect_id)
        else:
            tpl = followup_templates.get(p.prospect_id, {}).get(step)
        if not tpl:
            missing_template_count += 1
            continue

        queue_counts[step] += 1
        eligible_queue.append((priority, p.prospect_id, p, tpl))

    # Process Day 9 -> Day 4 -> Day 0, keeping Tier-1 prospect rank within each priority band
    rank_index = {p.prospect_id: idx for idx, p in enumerate(ordered_prospects)}
    eligible_queue.sort(key=lambda item: (item[0], rank_index.get(item[1], 99999)))

    notif_diag = notification_status(cfg)
    if not dry_run and email_sender is None and not notif_diag["email_ready"]:
        return {
            "status": "missing_email_credentials",
            "error": "Email provider is not ready (set RESEND_API_KEY and NOTIFICATION_FROM_EMAIL).",
            "dry_run": False,
            "timestamp_utc": now_iso,
            "date_utc": today_utc,
            "base_url": active_base_url,
            "db_backend": database_backend_info(db_path),
            "daily_send_limit": effective_limit,
            "sent_today_before": sent_today_before,
            "remaining_quota_before": remaining_quota,
            "sent_this_run": 0,
            "sent_today_after": sent_today_before,
            "suppressed_claimed_or_signed": suppressed_count,
            "queue_counts": queue_counts,
            "sent": [],
            "planned": [],
            "errors": [],
        }

    if remaining_quota <= 0 and not dry_run:
        return {
            "status": "quota_exhausted",
            "dry_run": False,
            "timestamp_utc": now_iso,
            "date_utc": today_utc,
            "base_url": active_base_url,
            "db_backend": database_backend_info(db_path),
            "daily_send_limit": effective_limit,
            "sent_today_before": sent_today_before,
            "remaining_quota_before": 0,
            "sent_this_run": 0,
            "sent_today_after": sent_today_before,
            "suppressed_claimed_or_signed": suppressed_count,
            "queue_counts": queue_counts,
            "sent": [],
            "planned": [],
            "errors": [],
        }

    sender_fn = email_sender or (
        lambda **kwargs: send_transactional_email(cfg, **kwargs)
    )
    reply_to_addr = (cfg.unsubscribe_email or cfg.notifications.admin_email or notif_diag.get("admin_email") or "").strip()
    list_unsub = f"<mailto:{reply_to_addr}?subject=UNSUBSCRIBE>" if reply_to_addr else ""

    sent_items: list[dict[str, Any]] = []
    planned_items: list[dict[str, Any]] = []
    error_items: list[dict[str, Any]] = []
    candidates_to_process = eligible_queue[:remaining_quota]

    for _, pid, prospect, tpl in candidates_to_process:
        step = "day0_initial"
        if tpl is followup_templates.get(pid, {}).get("day4_followup"):
            step = "day4_followup"
        elif tpl is followup_templates.get(pid, {}).get("day9_followup"):
            step = "day9_followup"

        magic_link = build_claim_url(prospect.prospect_id, cfg, base_url=active_base_url)
        subject, text_body, html_body = render_email_from_template(
            tpl["subject"],
            tpl["body_md"],
            active_base_url=active_base_url,
            cfg=cfg,
        )

        item_summary = {
            "prospect_id": prospect.prospect_id,
            "organization_name": prospect.organization_name,
            "recipient_email": prospect.contact_email,
            "outreach_segment": prospect.outreach_segment,
            "sequence_step": step,
            "subject": subject,
            "magic_link": magic_link,
        }

        if dry_run:
            planned_items.append(item_summary)
            continue

        log_id = reserve_outreach_send(
            prospect_id=prospect.prospect_id,
            sequence_step=step,
            recipient_email=prospect.contact_email,
            subject=subject,
            provider=str(notif_diag.get("email_provider") or "resend"),
            sent_at_utc=now_iso,
            sent_date_utc=today_utc,
            magic_link=magic_link,
            db_path=db_path,
        )
        if log_id is None:
            continue

        extra_headers = {"List-Unsubscribe": list_unsub} if list_unsub else None
        try:
            provider_name, provider_msg_id = sender_fn(
                to_email=prospect.contact_email,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                reply_to=reply_to_addr,
                headers=extra_headers,
            )
            finalize_outreach_send(
                log_id=log_id,
                prospect_id=prospect.prospect_id,
                sequence_step=step,
                provider=provider_name,
                provider_message_id=provider_msg_id,
                sent_at_utc=now_iso,
                db_path=db_path,
            )
            sent_items.append(
                {
                    **item_summary,
                    "log_id": log_id,
                    "provider": provider_name,
                    "provider_message_id": provider_msg_id,
                    "sent_at_utc": now_iso,
                }
            )
        except Exception as exc:  # noqa: BLE001 - continue remaining warm-up batch on single failure
            err_msg = f"{type(exc).__name__}: {exc}"
            release_outreach_reservation(
                log_id=log_id,
                prospect_id=prospect.prospect_id,
                error_message=err_msg,
                updated_at_utc=now_iso,
                db_path=db_path,
            )
            logger.error("Outreach send failed for %s (%s): %s", prospect.prospect_id, step, err_msg)
            error_items.append({**item_summary, "error": err_msg})

    return {
        "status": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "timestamp_utc": now_iso,
        "date_utc": today_utc,
        "base_url": active_base_url,
        "db_backend": database_backend_info(db_path),
        "daily_send_limit": effective_limit,
        "sent_today_before": sent_today_before,
        "remaining_quota_before": remaining_quota,
        "sent_this_run": len(sent_items),
        "sent_today_after": sent_today_before + len(sent_items),
        "suppressed_claimed_or_signed": suppressed_count,
        "missing_templates": missing_template_count,
        "queue_counts": queue_counts,
        "sent": sent_items,
        "planned": planned_items,
        "errors": error_items,
    }


def get_outreach_dashboard_summary(db_path: Path | str = DEFAULT_DB_PATH, log_limit: int = 50) -> dict[str, Any]:
    """Return status breakdown and recent outreach_send_log entries for admin monitoring."""
    cfg = load_matcher_config(DEFAULT_CONFIG_PATH)
    prospects = fetch_all_prospects(db_path=db_path)
    if prospects:
        sync_outreach_status_from_prospects(prospects, db_path=db_path)
    statuses = fetch_outreach_statuses(db_path=db_path)
    counts: dict[str, int] = {}
    for row in statuses.values():
        st = str(row.get("status") or "pending")
        counts[st] = counts.get(st, 0) + 1
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_limit = resolve_daily_send_limit(cfg)
    sent_today = get_daily_send_count_utc(today_utc, db_path=db_path)

    return {
        "status": "ok",
        "base_url": resolve_base_url(cfg),
        "db_backend": database_backend_info(db_path),
        "daily_send_limit": daily_limit,
        "sent_today_utc": sent_today,
        "remaining_today_utc": max(0, daily_limit - sent_today),
        "total_prospects": len(statuses),
        "status_counts": counts,
        "prospect_statuses": list(statuses.values()),
        "recent_logs": fetch_outreach_send_logs(limit=log_limit, db_path=db_path),
    }
