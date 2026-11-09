"""Zero-Call Digital Client Intake & California E-Signature Portal (intake_app.py).

Serves a single-page web intake portal for ZYS GoldFields PRO LLC (Conservation & Agricultural
Grant Advisory) incorporating:
- Scrollable "Conservation Advisory & Origination Agreement" governed by the California Uniform
  Electronic Transactions Act (UETA) and federal ESIGN Act, with explicit 2 CFR § 200.459
  federal/state grant compliance protection, 5-business-day payment trigger, 1.5% monthly late fee,
  and Sacramento County, California venue / prevailing-party attorney fee provisions.
- Frictionless 1-click E-Signature block (Full Legal Name, Title, and "I agree to the ZYS GoldFields
  PRO LLC Advisory Agreement.") capturing UTC Timestamp, Client IP Address, User-Agent browser
  string, and SHA-256 hash of the signed contract text in grants.db.
- Two-stage "Sign-to-Unlock" Final Filing Gate via admin_packager.py:
  * Stage A (Client Preview Summary): Shows matched grant, engineered budget total, and probability
    score while masking the RCD Co-Sponsor Memorandum, grant solicitation ID, and copy-paste
    narrative until agreement_signed == True in grants.db.
  * Stage B (Unlocked RCD & Agency Filing Dossier): Unlocks client_submission_packet.md (with
    appended Notice of Authorized Advisory Representation) and generates signed_advisory_agreement.md.
"""

from __future__ import annotations

import argparse
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
import io
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import zipfile

from pydantic import ValidationError

from admin_packager import (
    DEFAULT_PACKET_PATH,
    DEFAULT_PREVIEW_PATH,
    build_budget_engineering_table,
    generate_submission_packet,
    resolve_local_agency_contacts,
)
from db import (
    DEFAULT_DB_PATH,
    fetch_all_esignature_audits,
    fetch_all_grants,
    fetch_all_intakes,
    fetch_all_prospects,
    fetch_client_intake_by_id,
    init_db,
)
from legal_agreement import (
    AGREEMENT_VERSION,
    DEFAULT_SIGNED_AGREEMENT_PATH,
    compute_contract_sha256,
    render_canonical_contract_text,
)
from matcher import DEFAULT_CONFIG_PATH, load_matcher_config
from models import ClientIntake, eligibility_tier_label
from notifier import dispatch_intake_backup_async, notification_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("intake_app")

DEFAULT_HOST = os.environ.get("INTAKE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PORT", os.environ.get("INTAKE_PORT", "8080")))
PACKETS_ARCHIVE_DIR = Path(__file__).resolve().parent / "packets"
DEFAULT_CSV_PATH = Path(__file__).resolve().parent / "california_prospects.csv"

ALL_CA_COUNTIES = (
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa", "Contra Costa",
    "Del Norte", "El Dorado", "Fresno", "Glenn", "Humboldt", "Imperial", "Inyo",
    "Kern", "Kings", "Lake", "Lassen", "Los Angeles", "Madera", "Marin", "Mariposa",
    "Mendocino", "Merced", "Modoc", "Mono", "Monterey", "Napa", "Nevada", "Orange",
    "Placer", "Plumas", "Riverside", "Sacramento", "San Benito", "San Bernardino",
    "San Diego", "San Francisco", "San Joaquin", "San Luis Obispo", "San Mateo",
    "Santa Barbara", "Santa Clara", "Santa Cruz", "Shasta", "Sierra", "Siskiyou",
    "Solano", "Sonoma", "Stanislaus", "Sutter", "Tehama", "Trinity", "Tulare",
    "Tuolumne", "Ventura", "Yolo", "Yuba",
)


def lookup_prospect_by_id(prospect_id: str, csv_path: Path | str = DEFAULT_CSV_PATH) -> dict[str, Any] | None:
    """Look up a California prospect by ID (e.g. CA-T1-001 or CA-PROS-070) from CSV or SQLite fallback."""
    pid = str(prospect_id or "").strip()
    if not pid:
        return None
    target_csv = Path(csv_path)
    if target_csv.exists():
        try:
            with target_csv.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    r_id = row.get("prospect_id", "").strip()
                    if r_id.lower() == pid.lower():
                        return row
        except Exception as exc:
            logger.warning("Error reading prospects CSV for prospect_id=%s: %s", pid, exc)
    try:
        all_prospects = fetch_all_prospects(DEFAULT_DB_PATH)
        for p in all_prospects:
            if p.prospect_id.lower() == pid.lower():
                return p.model_dump()
    except Exception as exc:
        logger.warning("Error searching prospects in DB for prospect_id=%s: %s", pid, exc)
    return None


def ensure_database_ready(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Ensure SQLite tables exist and bootstrap live grants if running on a cold cloud container."""
    init_db(db_path)
    grants = fetch_all_grants(db_path)
    if not grants:
        logger.info("Cold container detected (0 grants in grants.db); running live grant ingestion...")
        from scraper import ingest_all_grants

        ingest_all_grants(db_path=db_path)


def render_portal_html(prospect: dict[str, Any] | None = None) -> str:
    """Render the single-page HTML/CSS/JS Zero-Call Digital Client Intake & California E-Signature Portal."""
    cfg = load_matcher_config(DEFAULT_CONFIG_PATH)
    fee_str = cfg.fee_label()
    fee_name = cfg.fee_name()
    public_mode_js = "true" if os.environ.get("PUBLIC_MODE", "0") == "1" else "false"
    county_options_html = "".join(f'<option value="{c}"></option>' for c in ALL_CA_COUNTIES)

    prefill_name = str((prospect or {}).get("organization_name") or "")
    prefill_email = str((prospect or {}).get("contact_email") or "")
    prefill_county = str((prospect or {}).get("county") or "")
    prefill_acreage = str((prospect or {}).get("estimated_acreage") or "")
    prefill_ein = str((prospect or {}).get("ein") or "")
    prefill_prospect_id = str((prospect or {}).get("prospect_id") or "")
    prefill_matched_grant = str((prospect or {}).get("top_matched_grant") or "")
    prefill_focus = str((prospect or {}).get("conservation_focus") or "")
    prefill_json = json.dumps(prospect) if prospect else "null"

    if prospect and prefill_matched_grant:
        magic_banner = f"""<div class="magic-link-banner" style="background:var(--success-bg); border:1px solid hsla(152, 48%, 20%, 0.25); border-radius:var(--radius-sm); padding:16px 20px; margin-bottom:20px;">
          <div style="font-size:0.75rem; text-transform:uppercase; letter-spacing:0.07em; font-weight:700; color:var(--success-ink); margin-bottom:4px;">
            1-Click Pre-Screened Grant Allocation &middot; Tier-1 Statutory Eligibility Match (4/4 Core Criteria Met)
          </div>
          <div style="font-size:1.05rem; font-weight:600; color:var(--ink-primary); margin-bottom:4px;">
            Matched Program: <strong>{html.escape(prefill_matched_grant)}</strong>
          </div>
          <div style="font-size:0.83rem; color:var(--ink-secondary);">
            Candidate ID: <code style="font-family:'JetBrains Mono', monospace; background:var(--surface-subtle); padding:2px 6px; border-radius:3px;">{html.escape(prefill_prospect_id)}</code> &middot; Entity: <strong>{html.escape(prefill_name)}</strong> &middot; County: {html.escape(prefill_county)} &middot; Verified EIN: <code style="font-family:'JetBrains Mono', monospace; background:var(--surface-subtle); padding:2px 6px; border-radius:3px;">{html.escape(prefill_ein)}</code>
          </div>
          <div style="font-size:0.82rem; color:var(--accent-pine); margin-top:8px; font-weight:600;">
            &rarr; Verify your parcel APN below, e-sign the advisory agreement ($0 upfront), and unlock your complete RCD &amp; Agency filing dossier in 1 click.
          </div>
        </div>"""
    else:
        magic_banner = ""

    sample_contract_preview = render_canonical_contract_text(
        firm_name=cfg.firm_name,
        firm_subtitle=cfg.firm_subtitle,
        fee_percent=cfg.success_fee_percent,
        client_name=prefill_name or "[Landowner / Entity Name Entered Above]",
        client_email=prefill_email or "[Client Email Entered Above]",
        county=prefill_county or "[California County Entered Above]",
        state="CA",
        apn="[Parcel APN Entered Above]",
        acreage=float(prefill_acreage) if (prefill_acreage and prefill_acreage.replace(".", "", 1).isdigit()) else 45.0,
        matched_grant_title=prefill_matched_grant or "[Matched Conservation / Agricultural Grant Program]",
        engineered_grant_request=30375,
        contingent_fee_dollars=2126,
    )
    sample_contract_escaped = html.escape(sample_contract_preview)
    template_sha256 = compute_contract_sha256(sample_contract_preview)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{cfg.firm_name} — Digital Property Grant Intake &amp; California UETA E-Signature Portal</title>
  <meta name="description" content="2-minute zero-call digital property intake and California UETA / federal ESIGN e-signature portal for {cfg.firm_name} ({cfg.firm_subtitle})." />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg-paper: hsl(40, 28%, 96%);
      --surface-sheet: hsl(40, 38%, 99%);
      --surface-subtle: hsl(40, 22%, 93%);
      --ink-primary: hsl(154, 36%, 14%);
      --ink-secondary: hsl(152, 14%, 33%);
      --ink-muted: hsl(150, 10%, 46%);
      --accent-pine: hsl(154, 42%, 22%);
      --accent-pine-hover: hsl(154, 46%, 17%);
      --accent-ochre: hsl(38, 64%, 36%);
      --border-stone: hsl(38, 18%, 83%);
      --border-focus: hsl(154, 42%, 28%);
      --danger-bg: hsl(4, 68%, 96%);
      --danger-ink: hsl(4, 62%, 34%);
      --warning-bg: hsl(38, 86%, 94%);
      --warning-ink: hsl(32, 78%, 26%);
      --success-bg: hsl(146, 36%, 94%);
      --success-ink: hsl(152, 48%, 20%);
      --shadow-sheet: 0 1px 2px hsla(154, 30%, 12%, 0.04), 0 12px 32px -8px hsla(154, 28%, 12%, 0.08);
      --radius-sm: 6px;
      --radius-md: 10px;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      padding: 0;
      background-color: var(--bg-paper);
      color: var(--ink-primary);
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 15px;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }}

    .page-shell {{
      max-width: 860px;
      margin: 0 auto;
      padding: 36px 20px 72px;
    }}

    header.masthead {{
      border-bottom: 1px solid var(--border-stone);
      padding-bottom: 22px;
      margin-bottom: 28px;
    }}

    .firm-lockup {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 12px;
    }}

    .firm-name {{
      font-family: 'Newsreader', Georgia, serif;
      font-weight: 600;
      font-size: 1.18rem;
      letter-spacing: 0.02em;
      color: var(--ink-primary);
    }}

    .firm-practice {{
      font-size: 0.8rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent-ochre);
    }}

    h1 {{
      font-family: 'Newsreader', Georgia, serif;
      font-weight: 500;
      font-size: clamp(1.8rem, 3.5vw, 2.35rem);
      line-height: 1.18;
      letter-spacing: -0.015em;
      margin: 0 0 10px;
      color: var(--ink-primary);
    }}

    .lead-copy {{
      margin: 0;
      color: var(--ink-secondary);
      max-width: 68ch;
      font-size: 0.96rem;
    }}

    .assurance-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 18px;
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--border-stone);
      font-size: 0.82rem;
      color: var(--ink-secondary);
    }}

    .assurance-strip span strong {{
      color: var(--ink-primary);
      font-weight: 600;
    }}

    .sheet {{
      background: var(--surface-sheet);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-md);
      box-shadow: var(--shadow-sheet);
      padding: clamp(22px, 4vw, 36px);
    }}

    .sheet-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 22px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border-stone);
    }}

    .sheet-toolbar h2 {{
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
      letter-spacing: -0.01em;
    }}

    .btn-ghost {{
      background: transparent;
      border: 1px solid var(--border-stone);
      color: var(--ink-secondary);
      font-family: inherit;
      font-size: 0.8rem;
      font-weight: 600;
      padding: 7px 12px;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: background-color 0.15s ease, color 0.15s ease, border-color 0.15s ease;
    }}

    .btn-ghost:hover {{
      background: var(--surface-subtle);
      color: var(--ink-primary);
      border-color: var(--ink-muted);
    }}

    .form-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px 18px;
    }}

    .field-full {{
      grid-column: 1 / -1;
    }}

    @media (max-width: 640px) {{
      .form-grid {{
        grid-template-columns: 1fr;
      }}
    }}

    .field-group {{
      display: flex;
      flex-direction: column;
      gap: 5px;
    }}

    label {{
      font-size: 0.83rem;
      font-weight: 600;
      color: var(--ink-primary);
    }}

    .label-hint {{
      font-weight: 400;
      color: var(--ink-muted);
      font-size: 0.77rem;
      margin-left: 4px;
    }}

    input[type="text"],
    input[type="email"],
    input[type="number"],
    textarea {{
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      background-color: #fff;
      color: var(--ink-primary);
      font-family: inherit;
      font-size: 0.92rem;
      line-height: 1.45;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}

    input.mono-input {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.87rem;
    }}

    textarea {{
      min-height: 86px;
      resize: vertical;
    }}

    input:focus,
    textarea:focus {{
      outline: none;
      border-color: var(--border-focus);
      box-shadow: 0 0 0 3px hsla(154, 42%, 28%, 0.12);
    }}

    .agreement-section {{
      margin-top: 28px;
      padding-top: 22px;
      border-top: 1px solid var(--border-stone);
    }}

    .agreement-header {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }}

    .agreement-header h3 {{
      margin: 0;
      font-size: 1rem;
      font-weight: 600;
      color: var(--ink-primary);
    }}

    .agreement-badge {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.73rem;
      color: var(--accent-pine);
      background: var(--success-bg);
      padding: 3px 8px;
      border-radius: 4px;
      font-weight: 500;
    }}

    .compliance-highlights {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}

    .compliance-pill {{
      background: var(--surface-subtle);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      padding: 9px 12px;
      font-size: 0.78rem;
      color: var(--ink-secondary);
      line-height: 1.45;
    }}

    .compliance-pill strong {{
      display: block;
      color: var(--ink-primary);
      font-size: 0.8rem;
      margin-bottom: 2px;
    }}

    .scrollable-contract {{
      background: #fff;
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      padding: 14px 16px;
      max-height: 250px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.76rem;
      line-height: 1.55;
      white-space: pre-wrap;
      color: var(--ink-primary);
      margin-bottom: 16px;
    }}

    .esign-box {{
      padding: 18px;
      background-color: var(--surface-subtle);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
    }}

    .esign-signatory-attestation {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      background: #fff;
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      padding: 12px 16px;
      margin-bottom: 14px;
    }}

    @media (max-width: 640px) {{
      .esign-signatory-attestation {{
        grid-template-columns: 1fr;
      }}
    }}

    .esign-sig-col {{
      display: flex;
      flex-direction: column;
      gap: 3px;
    }}

    .esign-sig-label {{
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--ink-muted);
      font-weight: 600;
    }}

    .esign-sig-val {{
      font-size: 0.92rem;
      color: var(--ink-primary);
      font-weight: 600;
    }}

    .esign-sig-val.empty {{
      color: var(--ink-muted);
      font-style: italic;
      font-weight: 400;
      font-size: 0.84rem;
    }}

    .checkbox-row {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      cursor: pointer;
      padding-top: 6px;
    }}

    .checkbox-row input[type="checkbox"] {{
      width: 18px;
      height: 18px;
      margin-top: 3px;
      accent-color: var(--accent-pine);
      flex-shrink: 0;
      cursor: pointer;
    }}

    .terms-text {{
      font-size: 0.85rem;
      color: var(--ink-secondary);
      line-height: 1.52;
    }}

    .terms-text strong {{
      color: var(--ink-primary);
    }}

    .submit-row {{
      margin-top: 22px;
      display: grid;
      grid-template-columns: 1fr 1.45fr;
      gap: 12px;
    }}

    @media (max-width: 680px) {{
      .submit-row {{
        grid-template-columns: 1fr;
      }}
    }}

    .btn-primary {{
      width: 100%;
      padding: 13px 18px;
      background-color: var(--accent-pine);
      color: #fff;
      border: none;
      border-radius: var(--radius-sm);
      font-family: inherit;
      font-size: 0.92rem;
      font-weight: 600;
      letter-spacing: 0.01em;
      cursor: pointer;
      transition: background-color 0.16s ease, transform 0.08s ease;
    }}

    .btn-primary:hover {{
      background-color: var(--accent-pine-hover);
    }}

    .btn-secondary {{
      width: 100%;
      padding: 13px 16px;
      background-color: #fff;
      color: var(--ink-primary);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      font-family: inherit;
      font-size: 0.86rem;
      font-weight: 600;
      cursor: pointer;
      transition: background-color 0.15s ease, border-color 0.15s ease;
    }}

    .btn-secondary:hover {{
      background-color: var(--surface-subtle);
      border-color: var(--ink-muted);
    }}

    .btn-primary:disabled,
    .btn-secondary:disabled {{
      opacity: 0.65;
      cursor: wait;
    }}

    .alert-banner {{
      display: none;
      padding: 12px 15px;
      border-radius: var(--radius-sm);
      font-size: 0.87rem;
      margin-top: 16px;
    }}

    .alert-banner.error {{
      display: block;
      background: var(--danger-bg);
      color: var(--danger-ink);
      border: 1px solid hsla(4, 62%, 34%, 0.25);
    }}

    .confirmation-panel {{
      display: none;
      margin-top: 28px;
      background: var(--surface-sheet);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-md);
      box-shadow: var(--shadow-sheet);
      padding: clamp(22px, 4vw, 34px);
    }}

    .confirmation-panel.visible {{
      display: block;
    }}

    .status-header {{
      background: var(--success-bg);
      color: var(--success-ink);
      padding: 12px 16px;
      border-radius: var(--radius-sm);
      font-size: 0.88rem;
      font-weight: 600;
      margin-bottom: 18px;
    }}

    .status-header.locked {{
      background: var(--warning-bg);
      color: var(--warning-ink);
      border: 1px solid hsla(32, 78%, 26%, 0.25);
    }}

    .summary-dl {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(195px, 1fr));
      gap: 14px;
      margin: 0 0 18px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--border-stone);
    }}

    .summary-item dt {{
      font-size: 0.74rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--ink-muted);
      margin-bottom: 3px;
    }}

    .summary-item dd {{
      margin: 0;
      font-weight: 600;
      font-size: 0.91rem;
      color: var(--ink-primary);
      word-break: break-word;
    }}

    .summary-item dd.mono {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.82rem;
    }}

    .audit-strip {{
      display: none;
      background: var(--surface-subtle);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      padding: 12px 14px;
      margin-bottom: 16px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.76rem;
      color: var(--ink-secondary);
      line-height: 1.55;
    }}

    .audit-strip.visible {{
      display: block;
    }}

    .packet-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 16px;
    }}

    .btn-download {{
      display: inline-block;
      text-decoration: none;
      background: var(--accent-pine);
      color: #fff;
      padding: 9px 15px;
      border-radius: var(--radius-sm);
      font-size: 0.83rem;
      font-weight: 600;
    }}

    .btn-download.secondary {{
      background: hsl(38, 64%, 32%);
    }}

    .packet-preview {{
      background: hsl(40, 20%, 97%);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      padding: 16px;
      max-height: 440px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.78rem;
      line-height: 1.55;
      white-space: pre-wrap;
      color: var(--ink-primary);
    }}

    footer.site-footer {{
      margin-top: 34px;
      text-align: center;
      font-size: 0.79rem;
      color: var(--ink-muted);
    }}
  </style>
</head>
<body>
  <div class="page-shell">
    <header class="masthead">
      <div class="firm-lockup">
        <span class="firm-name">{cfg.firm_name}</span>
        <span class="firm-practice">{cfg.firm_subtitle}</span>
      </div>
      <h1>Digital Property Grant Intake &amp; E-Signature</h1>
      <p class="lead-copy">
        Complete this 2-minute property intake to preview your matched conservation grant &amp; engineered
        budget, and e-sign the California UETA / ESIGN Advisory Agreement to unlock your complete County
        Resource Conservation District (RCD) filing dossier—100% online with zero phone calls.
      </p>
      <div class="assurance-strip">
        <span><strong>$0 Upfront Cost</strong> · Strictly 100% Waived if Not Awarded</span>
        <span><strong>2 CFR § 200.459 Compliant</strong> · Paid Strictly From Separate General Funds Upon Award</span>
        <span><strong>California UETA &amp; ESIGN Audit Trail</strong> · Cryptographic SHA-256 Hash</span>
      </div>
    </header>

    <main>
      <section class="sheet" aria-labelledby="form-heading">
        <div class="sheet-toolbar">
          <h2 id="form-heading">1. Landowner, Signer &amp; Parcel Intake</h2>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <a id="btn-repo-zip" href="/api/repo-archive.zip" download="zys-goldfields-intake.zip" class="btn-ghost" style="text-decoration:none;">
              Download GitHub Repo (.zip)
            </a>
            <button type="button" id="btn-prefill-vance" class="btn-ghost">
              Load Sample Parcel &amp; Signer (Elena &amp; Marcus Vance — El Dorado 45 ac)
            </button>
          </div>
        </div>

        {magic_banner}

        <form id="intake-form" action="/submit" method="post" novalidate>
          <div class="form-grid">
            <div class="field-group">
              <label for="input-name">Landowner / Entity Name</label>
              <input type="text" id="input-name" name="name" required value="{html.escape(prefill_name)}" placeholder="e.g., Elena &amp; Marcus Vance" />
            </div>

            <div class="field-group">
              <label for="input-email">Email Address <span class="label-hint">(for dossier &amp; signed receipt)</span></label>
              <input type="email" id="input-email" name="email" required value="{html.escape(prefill_email)}" placeholder="name@domain.com" />
            </div>

            <div class="field-group">
              <label for="input-signer-name">Full Legal Name of Signer <span class="label-hint">(Required to Unlock Dossier)</span></label>
              <input type="text" id="input-signer-name" name="signer_full_name" required placeholder="e.g., Elena Vance" />
            </div>

            <div class="field-group">
              <label for="input-signer-title">Title / Legal Capacity <span class="label-hint">(e.g., Managing Co-Owner, Executive Director)</span></label>
              <input type="text" id="input-signer-title" name="signer_title" required placeholder="e.g., Managing Co-Owner &amp; Steward" />
            </div>

            <div class="field-group">
              <label for="input-county">California County</label>
              <input type="text" id="input-county" name="county" list="ca-counties" required value="{html.escape(prefill_county)}" placeholder="e.g., El Dorado" />
              <datalist id="ca-counties">
                {county_options_html}
              </datalist>
            </div>

            <div class="field-group">
              <label for="input-ein">Employer Identification Number (EIN / Tax ID)</label>
              <input type="text" id="input-ein" name="ein" class="mono-input" value="{html.escape(prefill_ein)}" placeholder="e.g., 94-1234567" />
            </div>

            <div class="field-group">
              <label for="input-apn">Parcel APN Number</label>
              <input type="text" id="input-apn" name="apn" class="mono-input" required placeholder="e.g., 089-140-027-000" />
            </div>

            <div class="field-group">
              <label for="input-acreage">Total Parcel Acreage</label>
              <input type="number" id="input-acreage" name="acreage" class="mono-input" min="1" max="100000" step="0.1" required value="{html.escape(str(prefill_acreage))}" placeholder="e.g., 45" />
            </div>

            <input type="hidden" id="input-prospect-id" name="prospect_id" value="{html.escape(prefill_prospect_id)}" />

            <div class="field-group field-full">
              <label for="input-condition">Current Land Condition</label>
              <textarea id="input-condition" name="land_condition" required placeholder="Describe existing vegetation, oak/forest mortality, brush fuel load, streambank/riparian condition, or soil erosion..."></textarea>
            </div>

            <div class="field-group field-full">
              <label for="input-goals">Project Goals</label>
              <textarea id="input-goals" name="project_goals" required placeholder="Describe desired improvements: reforestation, RPF forest management plan, fuels thinning, riparian culvert replacement, solar irrigation, or conservation easement..."></textarea>
            </div>
          </div>

          <div class="agreement-section">
            <div class="agreement-header">
              <h3>2. Conservation Advisory &amp; Origination Agreement</h3>
              <span class="agreement-badge">{AGREEMENT_VERSION} · Cal. Civ. Code § 1633.1 / 15 U.S.C. § 7001</span>
            </div>

            <div class="compliance-highlights">
              <div class="compliance-pill">
                <strong>2 CFR § 200.459 Compliance</strong>
                {fee_name} applies only upon verified award receipt, payable strictly from Client's separate general/operating funds—never deducted from restricted grant funds.
              </div>
              <div class="compliance-pill">
                <strong>Payment Trigger &amp; 5-Day Settlement</strong>
                Invoiced upon official Agency Award Notice (or RCD Grant Agreement), payable within 5 business days of initial tranche disbursement ($0 upfront).
              </div>
              <div class="compliance-pill">
                <strong>CAL FIRE RPF &amp; RCD Coordination</strong>
                Forester and RCD technical coordination conducted under applicable professional standards; awards remain within sole agency discretion.
              </div>
              <div class="compliance-pill">
                <strong>Remedies &amp; Sacramento Venue</strong>
                1.5% monthly late fee after 5 business days; exclusive venue in Sacramento County, CA with prevailing-party attorney fees (Cal. Civ. Code § 1717).
              </div>
            </div>

            <div id="scrollable-agreement-box" class="scrollable-contract" tabindex="0" aria-label="Conservation Advisory and Origination Agreement">{sample_contract_escaped}</div>

            <div class="esign-box">
              <div class="esign-signatory-attestation">
                <div class="esign-sig-col">
                  <span class="esign-sig-label">Authorized Electronic Signatory</span>
                  <span id="esign-preview-name" class="esign-sig-val mono empty">(Enter Full Legal Name in Section 1 above)</span>
                </div>
                <div class="esign-sig-col">
                  <span class="esign-sig-label">Title / Legal Capacity</span>
                  <span id="esign-preview-title" class="esign-sig-val mono empty">(Enter Title / Capacity in Section 1 above)</span>
                </div>
              </div>

              <label class="checkbox-row" for="checkbox-terms">
                <input type="checkbox" id="checkbox-terms" name="agreed_to_terms" value="true" required />
                <span class="terms-text">
                  <strong>I agree to the ZYS GoldFields PRO LLC Advisory Agreement.</strong>
                  By entering my Full Legal Name and Title in the intake form above and checking this box, I execute a legally binding
                  electronic signature under the California Uniform Electronic Transactions Act (UETA) and federal
                  ESIGN Act, agreeing to the <strong>{fee_name}</strong> paid strictly from
                  separate general operating non-grant funds (<strong>2 CFR § 200.459 compliant</strong>) within <strong>5 business
                  days</strong> of initial grant tranche disbursement ($0 upfront).
                </span>
              </label>
            </div>
          </div>

          <div id="form-error" class="alert-banner" role="alert"></div>

          <div class="submit-row">
            <button type="button" id="btn-preview-only" class="btn-secondary">
              Stage A: Preview Match &amp; Budget Summary (Locked)
            </button>
            <button type="submit" id="btn-submit-intake" class="btn-primary">
              Stage B: E-Sign &amp; Unlock Full RCD Filing Dossier
            </button>
          </div>
        </form>
      </section>

      <section id="confirmation-panel" class="confirmation-panel" aria-live="polite">
        <div id="status-banner" class="status-header">
          UNLOCKED — E-Signature Verified &amp; Complete RCD Filing Dossier Ready
        </div>

        <dl class="summary-dl">
          <div class="summary-item">
            <dt>Filing Gate Status</dt>
            <dd id="res-gate-status" class="mono">—</dd>
          </div>
          <div class="summary-item">
            <dt>Primary Matched Grant</dt>
            <dd id="res-grant">—</dd>
          </div>
          <div class="summary-item">
            <dt>Solicitation ID</dt>
            <dd id="res-grant-id" class="mono">—</dd>
          </div>
          <div class="summary-item">
            <dt>Engineered Grant Request</dt>
            <dd id="res-request" class="mono">—</dd>
          </div>
          <div class="summary-item">
            <dt>Statutory Eligibility Assessment</dt>
            <dd id="res-score" class="mono">—</dd>
          </div>
          <div class="summary-item">
            <dt>Assigned Local Handoff Target</dt>
            <dd id="res-rcd">—</dd>
          </div>
        </dl>

        <div id="audit-strip" class="audit-strip"></div>

        <div class="packet-actions">
          <a href="/api/packet/latest" id="btn-download-packet" class="btn-download" download="client_submission_packet.md">
            Download client_submission_packet.md
          </a>
          <a href="/api/agreement/latest" id="btn-download-agreement" class="btn-download secondary" download="signed_advisory_agreement.md">
            Download signed_advisory_agreement.md
          </a>
          <button type="button" id="btn-copy-packet" class="btn-ghost">
            Copy Dossier Markdown
          </button>
        </div>

        <div id="packet-preview" class="packet-preview"></div>
      </section>
    </main>

    <footer class="site-footer">
      &copy; {cfg.firm_name} &middot; {cfg.firm_subtitle} &middot; California UETA &amp; ESIGN Compliant ({template_sha256[:16]}…)
    </footer>
  </div>

  <script>
    const PUBLIC_MODE = {public_mode_js};
    const PROSPECT_PREFILL = {prefill_json};
    const form = document.getElementById('intake-form');
    const errorEl = document.getElementById('form-error');
    const submitBtn = document.getElementById('btn-submit-intake');
    const previewOnlyBtn = document.getElementById('btn-preview-only');
    const confirmPanel = document.getElementById('confirmation-panel');
    const statusBanner = document.getElementById('status-banner');
    const auditStrip = document.getElementById('audit-strip');
    const previewEl = document.getElementById('packet-preview');
    const prefillBtn = document.getElementById('btn-prefill-vance');
    const repoZipBtn = document.getElementById('btn-repo-zip');
    const copyBtn = document.getElementById('btn-copy-packet');
    const dlPacketBtn = document.getElementById('btn-download-packet');
    const dlAgreementBtn = document.getElementById('btn-download-agreement');

    const urlParams = new URLSearchParams(window.location.search);
    if (PUBLIC_MODE) {{
      if (repoZipBtn) repoZipBtn.style.display = 'none';
    }}
    if (PUBLIC_MODE || urlParams.has('name') || urlParams.has('org') || PROSPECT_PREFILL) {{
      prefillBtn.style.display = 'none';
    }}
    if (PROSPECT_PREFILL) {{
      const p = PROSPECT_PREFILL;
      if (p.organization_name) {{
        document.getElementById('input-name').value = p.organization_name;
        document.getElementById('input-signer-name').value = p.organization_name;
      }}
      if (p.contact_email) document.getElementById('input-email').value = p.contact_email;
      if (p.county) document.getElementById('input-county').value = p.county;
      if (p.estimated_acreage) document.getElementById('input-acreage').value = p.estimated_acreage;
      if (p.ein) document.getElementById('input-ein').value = p.ein;
      if (p.prospect_id) document.getElementById('input-prospect-id').value = p.prospect_id;
      if (p.conservation_focus) {{
        if (!document.getElementById('input-condition').value) {{
          document.getElementById('input-condition').value =
            `Working conservation parcel in ${{p.county}} County (${{p.estimated_acreage}} acres). Focus: ${{p.conservation_focus}}.`;
        }}
        if (!document.getElementById('input-goals').value) {{
          document.getElementById('input-goals').value =
            `Implement resource stewardship under ${{p.top_matched_grant || 'active conservation grant programs'}}, focusing on ${{p.conservation_focus}} and local RCD technical coordination.`;
        }}
      }}
    }}
    if (urlParams.get('name') || urlParams.get('org')) {{
      const nm = urlParams.get('name') || urlParams.get('org');
      document.getElementById('input-name').value = nm;
      document.getElementById('input-signer-name').value = nm;
    }}
    if (urlParams.get('email')) {{
      document.getElementById('input-email').value = urlParams.get('email');
    }}
    if (urlParams.get('county')) {{
      document.getElementById('input-county').value = urlParams.get('county');
    }}
    if (urlParams.get('ein')) {{
      document.getElementById('input-ein').value = urlParams.get('ein');
    }}
    if (urlParams.get('acres')) {{
      document.getElementById('input-acreage').value = urlParams.get('acres');
    }}
    if (urlParams.get('signer_name')) {{
      document.getElementById('input-signer-name').value = urlParams.get('signer_name');
    }}
    if (urlParams.get('signer_title')) {{
      document.getElementById('input-signer-title').value = urlParams.get('signer_title');
    }}
    if (urlParams.get('condition')) {{
      document.getElementById('input-condition').value = urlParams.get('condition');
    }}
    if (urlParams.get('goals')) {{
      document.getElementById('input-goals').value = urlParams.get('goals');
    }}

    const signerNameInput = document.getElementById('input-signer-name');
    const signerTitleInput = document.getElementById('input-signer-title');
    const esignPreviewName = document.getElementById('esign-preview-name');
    const esignPreviewTitle = document.getElementById('esign-preview-title');

    function syncEsignPreview() {{
      if (!esignPreviewName || !esignPreviewTitle) return;
      const nm = signerNameInput ? signerNameInput.value.trim() : '';
      const ttl = signerTitleInput ? signerTitleInput.value.trim() : '';
      if (nm) {{
        esignPreviewName.textContent = nm;
        esignPreviewName.classList.remove('empty');
      }} else {{
        esignPreviewName.textContent = '(Enter Full Legal Name in Section 1 above)';
        esignPreviewName.classList.add('empty');
      }}
      if (ttl) {{
        esignPreviewTitle.textContent = ttl;
        esignPreviewTitle.classList.remove('empty');
      }} else {{
        esignPreviewTitle.textContent = '(Enter Title / Capacity in Section 1 above)';
        esignPreviewTitle.classList.add('empty');
      }}
    }}

    if (signerNameInput) signerNameInput.addEventListener('input', syncEsignPreview);
    if (signerTitleInput) signerTitleInput.addEventListener('input', syncEsignPreview);
    syncEsignPreview();

    prefillBtn.addEventListener('click', () => {{
      document.getElementById('input-name').value = 'Elena & Marcus Vance';
      document.getElementById('input-email').value = 'vance.family@sierraridgeoak.com';
      document.getElementById('input-county').value = 'El Dorado';
      document.getElementById('input-ein').value = '94-8271039';
      document.getElementById('input-apn').value = '089-140-027-000';
      document.getElementById('input-acreage').value = '45';
      document.getElementById('input-condition').value =
        '45-acre degraded blue oak, black oak, and ponderosa pine woodland in El Dorado County experiencing post-drought canopy mortality, invasive brush fuel accumulation, and seasonal gully soil erosion.';
      document.getElementById('input-goals').value =
        'Complete a Registered Professional Forester (RPF) Forest Management Plan, mechanical understory fuels thinning, native oak and pine reforestation, gully erosion stabilization, and wildlife habitat restoration.';
      document.getElementById('input-signer-name').value = 'Elena Vance';
      document.getElementById('input-signer-title').value = 'Managing Co-Owner & Steward, Sierra Ridge Oak Woodland';
      document.getElementById('checkbox-terms').checked = true;
      errorEl.className = 'alert-banner';
      errorEl.textContent = '';
      syncEsignPreview();
    }});

    copyBtn.addEventListener('click', async () => {{
      if (!previewEl.textContent) return;
      await navigator.clipboard.writeText(previewEl.textContent);
      const orig = copyBtn.textContent;
      copyBtn.textContent = 'Copied to Clipboard';
      setTimeout(() => {{ copyBtn.textContent = orig; }}, 1800);
    }});

    async function submitIntakeRequest(previewOnly) {{
      errorEl.className = 'alert-banner';
      errorEl.textContent = '';

      const payload = {{
        name: document.getElementById('input-name').value.trim(),
        email: document.getElementById('input-email').value.trim(),
        county: document.getElementById('input-county').value.trim(),
        state: 'CA',
        apn: document.getElementById('input-apn').value.trim(),
        acreage: parseFloat(document.getElementById('input-acreage').value || '0'),
        ein: document.getElementById('input-ein').value.trim(),
        prospect_id: document.getElementById('input-prospect-id').value.trim(),
        land_condition: document.getElementById('input-condition').value.trim(),
        project_goals: document.getElementById('input-goals').value.trim(),
        signer_full_name: document.getElementById('input-signer-name').value.trim(),
        signer_title: document.getElementById('input-signer-title').value.trim(),
        agreed_to_terms: previewOnly ? false : document.getElementById('checkbox-terms').checked,
        preview_only: previewOnly
      }};

      submitBtn.disabled = true;
      previewOnlyBtn.disabled = true;

      try {{
        const resp = await fetch('/api/intake', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        const data = await resp.json();
        if (!resp.ok) {{
          throw new Error(data.error || 'Please check all required parcel fields and e-signature boxes.');
        }}

        document.getElementById('res-grant').textContent = data.matched_grant_title;
        document.getElementById('res-grant-id').textContent = data.matched_grant_id;
        document.getElementById('res-request').textContent = data.engineered_grant_request;
        document.getElementById('res-score').textContent = data.eligibility_tier || data.probability_score;
        document.getElementById('res-rcd').textContent = data.rcd_target;
        previewEl.textContent = data.packet_markdown;

        if (data.agreement_signed) {{
          statusBanner.className = 'status-header';
          statusBanner.textContent =
            'STAGE B UNLOCKED — E-Signature Verified & Complete RCD Filing Dossier Ready for Dispatch';
          document.getElementById('res-gate-status').textContent = 'UNLOCKED (Stage B)';
          auditStrip.className = 'audit-strip visible';
          auditStrip.textContent =
            `UETA/ESIGN Audit Trail | Signer: ${{data.signer_full_name}} (${{data.signer_title}}) | ` +
            `UTC: ${{data.signature_timestamp_utc}} | IP: ${{data.signer_ip_address}} | ` +
            `SHA-256: ${{data.contract_sha256_hash}}`;
          dlPacketBtn.href = data.packet_download_url || '/api/packet/latest';
          dlPacketBtn.textContent = 'Download Unlocked client_submission_packet.md';
          dlAgreementBtn.style.display = 'inline-block';
          dlAgreementBtn.href = data.signed_agreement_download_url || '/api/agreement/latest';
        }} else {{
          statusBanner.className = 'status-header locked';
          statusBanner.textContent =
            'STAGE A PREVIEW (LOCKED) — E-Sign Agreement Above to Unlock Solicitation ID, Narrative & RCD Memo';
          document.getElementById('res-gate-status').textContent = 'LOCKED PREVIEW (Stage A)';
          auditStrip.className = 'audit-strip';
          auditStrip.textContent = '';
          dlPacketBtn.href = data.packet_download_url || '/api/packet/latest';
          dlPacketBtn.textContent = 'Download Stage A Preview Summary (.md)';
          dlAgreementBtn.style.display = 'none';
        }}

        confirmPanel.classList.add('visible');
        confirmPanel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }} catch (err) {{
        errorEl.textContent = err.message;
        errorEl.className = 'alert-banner error';
      }} finally {{
        submitBtn.disabled = false;
        previewOnlyBtn.disabled = false;
      }}
    }}

    previewOnlyBtn.addEventListener('click', () => submitIntakeRequest(true));
    form.addEventListener('submit', (ev) => {{
      ev.preventDefault();
      submitIntakeRequest(false);
    }});
  </script>
</body>
</html>
"""


def _process_intake_payload(
    raw_data: dict[str, Any],
    *,
    client_ip: str = "127.0.0.1",
    user_agent: str = "ZYS-GoldFields-Intake-Portal/1.0",
) -> dict[str, Any]:
    """Validate intake payload, enforce the Sign-to-Unlock filing gate, persist data, and dispatch notifications."""
    preview_only = bool(raw_data.get("preview_only", False)) or str(raw_data.get("stage", "")).lower() == "preview"
    agreed_to_terms = bool(raw_data.get("agreed_to_terms", False))

    if not preview_only and not agreed_to_terms:
        raise ValueError(
            "You must check 'I agree to the ZYS GoldFields PRO LLC Advisory Agreement' "
            "or select Stage A Preview."
        )

    name_val = str(raw_data.get("name", "")).strip()
    signer_full_name = str(raw_data.get("signer_full_name") or "").strip()
    signer_title = str(raw_data.get("signer_title") or "").strip()
    prospect_id_val = str(raw_data.get("prospect_id") or "").strip()
    ein_val = str(raw_data.get("ein") or "").strip()

    if not preview_only:
        if not signer_full_name:
            signer_full_name = name_val
        if not signer_title:
            signer_title = "Property Owner & Authorized Representative"

    intake = ClientIntake(
        prospect_id=prospect_id_val,
        ein=ein_val,
        name=name_val,
        email=str(raw_data.get("email", "")),
        county=str(raw_data.get("county", "")),
        state=str(raw_data.get("state") or "CA"),
        apn=str(raw_data.get("apn", "")),
        acreage=float(raw_data.get("acreage") or 0),
        land_condition=str(raw_data.get("land_condition", "")),
        project_goals=str(raw_data.get("project_goals", "")),
        agreed_to_terms=agreed_to_terms and not preview_only,
        agreement_signed=agreed_to_terms and not preview_only,
        signer_full_name=signer_full_name,
        signer_title=signer_title,
        signer_ip_address=client_ip,
        signer_user_agent=user_agent,
    )

    cfg = load_matcher_config(DEFAULT_CONFIG_PATH)
    target_output = DEFAULT_PREVIEW_PATH if preview_only else DEFAULT_PACKET_PATH
    saved, matches, packet_path = generate_submission_packet(
        intake=intake,
        db_path=DEFAULT_DB_PATH,
        config_path=DEFAULT_CONFIG_PATH,
        output_path=target_output,
        signed_agreement_output_path=DEFAULT_SIGNED_AGREEMENT_PATH,
        persist_intake=True,
    )
    primary = matches[0]
    tier_label = eligibility_tier_label(primary.probability_breakdown, primary.score)
    _, total_cost, grant_req, _, fee_dollars = build_budget_engineering_table(
        saved, primary.grant, fee_percent=cfg.success_fee_percent
    )
    contacts = resolve_local_agency_contacts(saved.county, saved.state)
    packet_md = packet_path.read_text(encoding="utf-8")

    if not saved.agreement_signed:
        if cfg.notifications.notify_on_preview:
            preview_record = {
                "event": "preview_generated",
                "intake_id": saved.id,
                "prospect_id": saved.prospect_id,
                "ein": saved.ein,
                "client_name": saved.name,
                "client_email": saved.email,
                "county": saved.county,
                "apn": saved.apn,
                "acreage": saved.acreage,
                "matched_grant": primary.grant.title,
                "matched_grant_id": primary.grant.id,
                "engineered_grant_request": grant_req,
                "eligibility_tier": tier_label,
                "probability_score": primary.score,
            }
            dispatch_intake_backup_async(
                event="preview_generated",
                record=preview_record,
                attachments=[("client_preview_summary.md", packet_md.encode("utf-8"), "text/markdown")],
                cfg=cfg,
            )

        return {
            "status": "ok",
            "stage": "preview_locked",
            "agreement_signed": False,
            "intake_id": saved.id,
            "prospect_id": saved.prospect_id,
            "ein": saved.ein,
            "client_name": saved.name,
            "apn": saved.apn,
            "county": saved.county,
            "matched_grant_id": "[LOCKED — E-SIGNATURE REQUIRED]",
            "matched_grant_title": primary.grant.title,
            "probability_score": f"{primary.score}/100",
            "eligibility_tier": tier_label,
            "score_rationale": primary.score_rationale,
            "agency": primary.grant.agency,
            "deadline": primary.grant.deadline,
            "engineered_grant_request": f"${grant_req:,} (of ${total_cost:,} total project budget)",
            "contingent_fee_upon_award": f"${fee_dollars:,}",
            "rcd_target": "[LOCKED — E-Sign Advisory Agreement to Unlock County RCD Co-Sponsor Memo]",
            "packet_path": str(packet_path),
            "packet_download_url": f"/api/preview/{saved.id}" if saved.id else "/api/preview/latest",
            "packet_markdown": packet_md,
        }

    signed_agreement_md = (
        DEFAULT_SIGNED_AGREEMENT_PATH.read_text(encoding="utf-8")
        if DEFAULT_SIGNED_AGREEMENT_PATH.exists()
        else ""
    )

    audit_json = json.dumps(
        {
            "intake_id": saved.id,
            "prospect_id": saved.prospect_id,
            "ein": saved.ein,
            "client_name": saved.name,
            "client_email": saved.email,
            "county": saved.county,
            "state": saved.state,
            "apn": saved.apn,
            "acreage": saved.acreage,
            "signer_full_name": saved.signer_full_name,
            "signer_title": saved.signer_title,
            "signature_timestamp_utc": saved.signature_timestamp_utc,
            "signer_ip_address": saved.signer_ip_address,
            "signer_user_agent": saved.signer_user_agent,
            "contract_sha256_hash": saved.contract_sha256_hash,
            "agreement_version": AGREEMENT_VERSION,
            "eligibility_tier": tier_label,
            "probability_score": primary.score,
            "matched_grant_title": primary.grant.title,
            "matched_grant_id": primary.grant.id,
            "engineered_grant_request": grant_req,
            "total_project_budget": total_cost,
            "contingent_fee_dollars": fee_dollars,
        },
        indent=2,
    )

    signed_record = {
        "event": "agreement_signed",
        "intake_id": saved.id,
        "prospect_id": saved.prospect_id,
        "ein": saved.ein,
        "client_name": saved.name,
        "client_email": saved.email,
        "county": saved.county,
        "apn": saved.apn,
        "acreage": saved.acreage,
        "matched_grant": primary.grant.title,
        "matched_grant_id": primary.grant.id,
        "engineered_grant_request": grant_req,
        "contingent_fee_dollars": fee_dollars,
        "contract_sha256_hash": saved.contract_sha256_hash,
        "signature_timestamp_utc": saved.signature_timestamp_utc,
        "signer_ip": saved.signer_ip_address,
        "signer_user_agent": saved.signer_user_agent,
        "signer_full_name": saved.signer_full_name,
        "signer_title": saved.signer_title,
        "eligibility_tier": tier_label,
        "probability_score": primary.score,
    }

    attachments = [
        ("signed_advisory_agreement.md", signed_agreement_md.encode("utf-8"), "text/markdown"),
        ("client_submission_packet.md", packet_md.encode("utf-8"), "text/markdown"),
        ("esignature_audit_record.json", audit_json.encode("utf-8"), "application/json"),
    ]

    dispatch_intake_backup_async(
        event="agreement_signed",
        record=signed_record,
        attachments=attachments,
        cfg=cfg,
    )

    return {
        "status": "ok",
        "stage": "unlocked_filing_dossier",
        "agreement_signed": True,
        "intake_id": saved.id,
        "prospect_id": saved.prospect_id,
        "ein": saved.ein,
        "client_name": saved.name,
        "apn": saved.apn,
        "county": saved.county,
        "matched_grant_id": primary.grant.id,
        "matched_grant_title": primary.grant.title,
        "probability_score": f"{primary.score}/100",
        "eligibility_tier": tier_label,
        "score_rationale": primary.score_rationale,
        "agency": primary.grant.agency,
        "deadline": primary.grant.deadline,
        "engineered_grant_request": f"${grant_req:,} (of ${total_cost:,} total project budget)",
        "contingent_fee_upon_award": f"${fee_dollars:,}",
        "rcd_target": f"{contacts['rcd_name']} / {contacts['nrcs_office']}",
        "signer_full_name": saved.signer_full_name,
        "signer_title": saved.signer_title,
        "agreement_version": AGREEMENT_VERSION,
        "signature_timestamp_utc": saved.signature_timestamp_utc,
        "signer_ip_address": saved.signer_ip_address,
        "signer_user_agent": saved.signer_user_agent,
        "contract_sha256_hash": saved.contract_sha256_hash,
        "packet_path": str(packet_path),
        "packet_download_url": f"/api/packet/{saved.id}" if saved.id else "/api/packet/latest",
        "signed_agreement_path": saved.signed_agreement_path,
        "signed_agreement_download_url": (
            f"/api/agreement/{saved.id}" if saved.id else "/api/agreement/latest"
        ),
        "signed_agreement_markdown": signed_agreement_md,
        "packet_markdown": packet_md,
    }


def _unlock_existing_intake(
    raw_data: dict[str, Any],
    *,
    client_ip: str = "127.0.0.1",
    user_agent: str = "ZYS-GoldFields-Intake-Portal/1.0",
) -> dict[str, Any]:
    """Unlock an existing Stage-A intake record once the client completes the e-signature block."""
    intake_id = int(raw_data.get("intake_id") or 0)
    if intake_id <= 0:
        raise ValueError("A valid intake_id is required to sign and unlock.")
    agreed = bool(raw_data.get("agreed_to_terms", False))
    if not agreed:
        raise ValueError("You must check 'I agree to the ZYS GoldFields PRO LLC Advisory Agreement' to unlock.")

    existing = fetch_client_intake_by_id(intake_id, DEFAULT_DB_PATH)
    if existing is None:
        raise ValueError(f"Intake #{intake_id} not found in grants.db.")

    signer_full_name = str(raw_data.get("signer_full_name") or existing.name).strip()
    signer_title = str(raw_data.get("signer_title") or "Property Owner & Authorized Representative").strip()

    signed_intake = existing.model_copy(
        update={
            "agreed_to_terms": True,
            "agreement_signed": True,
            "signer_full_name": signer_full_name,
            "signer_title": signer_title,
            "signer_ip_address": client_ip,
            "signer_user_agent": user_agent,
        }
    )

    cfg = load_matcher_config(DEFAULT_CONFIG_PATH)
    saved, matches, packet_path = generate_submission_packet(
        intake=signed_intake,
        db_path=DEFAULT_DB_PATH,
        config_path=DEFAULT_CONFIG_PATH,
        output_path=DEFAULT_PACKET_PATH,
        signed_agreement_output_path=DEFAULT_SIGNED_AGREEMENT_PATH,
        persist_intake=True,
    )
    primary = matches[0]
    tier_label = eligibility_tier_label(primary.probability_breakdown, primary.score)
    _, total_cost, grant_req, _, fee_dollars = build_budget_engineering_table(
        saved, primary.grant, fee_percent=cfg.success_fee_percent
    )
    contacts = resolve_local_agency_contacts(saved.county, saved.state)
    packet_md = packet_path.read_text(encoding="utf-8")
    signed_agreement_md = (
        DEFAULT_SIGNED_AGREEMENT_PATH.read_text(encoding="utf-8")
        if DEFAULT_SIGNED_AGREEMENT_PATH.exists()
        else ""
    )

    audit_json = json.dumps(
        {
            "intake_id": saved.id,
            "prospect_id": saved.prospect_id,
            "ein": saved.ein,
            "client_name": saved.name,
            "client_email": saved.email,
            "county": saved.county,
            "state": saved.state,
            "apn": saved.apn,
            "acreage": saved.acreage,
            "signer_full_name": saved.signer_full_name,
            "signer_title": saved.signer_title,
            "signature_timestamp_utc": saved.signature_timestamp_utc,
            "signer_ip_address": saved.signer_ip_address,
            "signer_user_agent": saved.signer_user_agent,
            "contract_sha256_hash": saved.contract_sha256_hash,
            "agreement_version": AGREEMENT_VERSION,
            "eligibility_tier": tier_label,
            "probability_score": primary.score,
            "matched_grant_title": primary.grant.title,
            "matched_grant_id": primary.grant.id,
            "engineered_grant_request": grant_req,
            "total_project_budget": total_cost,
            "contingent_fee_dollars": fee_dollars,
        },
        indent=2,
    )

    signed_record = {
        "event": "agreement_signed",
        "intake_id": saved.id,
        "prospect_id": saved.prospect_id,
        "ein": saved.ein,
        "client_name": saved.name,
        "client_email": saved.email,
        "county": saved.county,
        "apn": saved.apn,
        "acreage": saved.acreage,
        "matched_grant": primary.grant.title,
        "matched_grant_id": primary.grant.id,
        "engineered_grant_request": grant_req,
        "contingent_fee_dollars": fee_dollars,
        "contract_sha256_hash": saved.contract_sha256_hash,
        "signature_timestamp_utc": saved.signature_timestamp_utc,
        "signer_ip": saved.signer_ip_address,
        "signer_user_agent": saved.signer_user_agent,
        "signer_full_name": saved.signer_full_name,
        "signer_title": saved.signer_title,
        "eligibility_tier": tier_label,
        "probability_score": primary.score,
    }

    attachments = [
        ("signed_advisory_agreement.md", signed_agreement_md.encode("utf-8"), "text/markdown"),
        ("client_submission_packet.md", packet_md.encode("utf-8"), "text/markdown"),
        ("esignature_audit_record.json", audit_json.encode("utf-8"), "application/json"),
    ]

    dispatch_intake_backup_async(
        event="agreement_signed",
        record=signed_record,
        attachments=attachments,
        cfg=cfg,
    )

    return {
        "status": "ok",
        "stage": "unlocked_filing_dossier",
        "agreement_signed": True,
        "intake_id": saved.id,
        "prospect_id": saved.prospect_id,
        "ein": saved.ein,
        "client_name": saved.name,
        "apn": saved.apn,
        "county": saved.county,
        "matched_grant_id": primary.grant.id,
        "matched_grant_title": primary.grant.title,
        "probability_score": f"{primary.score}/100",
        "eligibility_tier": tier_label,
        "score_rationale": primary.score_rationale,
        "agency": primary.grant.agency,
        "deadline": primary.grant.deadline,
        "engineered_grant_request": f"${grant_req:,} (of ${total_cost:,} total project budget)",
        "contingent_fee_upon_award": f"${fee_dollars:,}",
        "rcd_target": f"{contacts['rcd_name']} / {contacts['nrcs_office']}",
        "signer_full_name": saved.signer_full_name,
        "signer_title": saved.signer_title,
        "agreement_version": AGREEMENT_VERSION,
        "signature_timestamp_utc": saved.signature_timestamp_utc,
        "signer_ip_address": saved.signer_ip_address,
        "signer_user_agent": saved.signer_user_agent,
        "contract_sha256_hash": saved.contract_sha256_hash,
        "packet_path": str(packet_path),
        "packet_download_url": f"/api/packet/{saved.id}",
        "signed_agreement_path": saved.signed_agreement_path,
        "signed_agreement_download_url": f"/api/agreement/{saved.id}",
        "signed_agreement_markdown": signed_agreement_md,
        "packet_markdown": packet_md,
    }


class IntakePortalHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP request handler for the Zero-Call Digital Client Intake & E-Signature Portal."""

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _extract_client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "127.0.0.1"

    def _extract_user_agent(self) -> str:
        return self.headers.get("User-Agent", "ZYS-GoldFields-Client-Browser/1.0").strip()

    def _check_admin_auth(self) -> bool:
        token = os.environ.get("ADMIN_API_TOKEN", "").strip()
        is_public = os.environ.get("PUBLIC_MODE", "0") == "1" or bool(os.environ.get("RENDER"))
        if not is_public and not token:
            return True
        auth_header = self.headers.get("Authorization", "").strip()
        if token and auth_header.startswith("Bearer ") and auth_header[7:].strip() == token:
            return True
        qs = parse_qs(urlparse(self.path).query)
        if token and qs.get("token", [""])[0] == token:
            return True
        return False

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html_body: str) -> None:
        body = html_body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_markdown_file(self, file_path: Path, download_name: str) -> None:
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/healthz", "/claim"):
            self.send_response(HTTPStatus.OK)
            if parsed.path == "/healthz":
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "3")
            else:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return

        if parsed.path == "/api/health":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_html(HTTPStatus.OK, render_portal_html())
            return

        if parsed.path == "/healthz":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if parsed.path == "/claim":
            qs = parse_qs(parsed.query)
            prospect_id = qs.get("prospect_id", [""])[0].strip()
            prospect = lookup_prospect_by_id(prospect_id) if prospect_id else None
            self._send_html(HTTPStatus.OK, render_portal_html(prospect=prospect))
            return

        if parsed.path == "/api/health":
            cfg = load_matcher_config(DEFAULT_CONFIG_PATH)
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "healthy",
                    "service": "ZYS GoldFields PRO Intake & E-Signature Portal",
                    "agreement_version": AGREEMENT_VERSION,
                    "public_mode": os.environ.get("PUBLIC_MODE", "0") == "1",
                    "notifications": notification_status(cfg),
                },
            )
            return

        if parsed.path == "/api/prospects":
            if not self._check_admin_auth():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Admin authentication required in public mode."})
                return
            prospects = [p.model_dump() for p in fetch_all_prospects(DEFAULT_DB_PATH)]
            self._send_json(HTTPStatus.OK, {"count": len(prospects), "prospects": prospects})
            return

        if parsed.path == "/api/audits":
            if not self._check_admin_auth():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Admin authentication required in public mode."})
                return
            audits = [a.model_dump() for a in fetch_all_esignature_audits(DEFAULT_DB_PATH)]
            self._send_json(HTTPStatus.OK, {"count": len(audits), "audits": audits})
            return

        if parsed.path == "/api/repo-archive.zip":
            if os.environ.get("PUBLIC_MODE", "0") == "1":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint disabled in public mode."})
                return
            repo_root = Path(__file__).resolve().parent
            export_names = (
                ".gitignore",
                "Procfile",
                "README.md",
                "admin_packager.py",
                "california_prospects.csv",
                "client_preview_summary.md",
                "client_submission_packet.md",
                "config.json",
                "db.py",
                "followup_templates.md",
                "grants.db",
                "intake_app.py",
                "legal_agreement.py",
                "matcher.py",
                "models.py",
                "notifier.py",
                "outreach_pitches.md",
                "prospect_hunter.py",
                "render.yaml",
                "requirements.txt",
                "run_pipeline.py",
                "runtime.txt",
                "scraper.py",
                "seed_profiles.py",
                "signed_advisory_agreement.md",
                "start_public_tunnel.sh",
            )
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for rel_name in export_names:
                    fp = repo_root / rel_name
                    if fp.exists():
                        zf.write(fp, arcname=rel_name)
                for pkt in sorted(PACKETS_ARCHIVE_DIR.glob("*.md")):
                    zf.write(pkt, arcname=f"packets/{pkt.name}")
            zip_bytes = buf.getvalue()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="zys-goldfields-intake.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)
            return

        if parsed.path == "/api/intakes":
            if not self._check_admin_auth():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Admin authentication required in public mode."})
                return
            intakes = [
                {
                    **item.model_dump(),
                    "packet_url": f"/api/packet/{item.id}" if item.agreement_signed else f"/api/preview/{item.id}",
                    "signed_agreement_url": f"/api/agreement/{item.id}" if item.agreement_signed else None,
                }
                for item in fetch_all_intakes(DEFAULT_DB_PATH)
            ]
            self._send_json(HTTPStatus.OK, {"count": len(intakes), "intakes": intakes})
            return

        if parsed.path == "/api/packet/latest":
            if (os.environ.get("PUBLIC_MODE", "0") == "1" or os.environ.get("RENDER")) and not self._check_admin_auth():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Latest packet downloads disabled in public mode."})
                return
            if not DEFAULT_PACKET_PATH.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No unlocked submission packet generated yet."})
                return
            self._send_markdown_file(DEFAULT_PACKET_PATH, "client_submission_packet.md")
            return

        if parsed.path.startswith("/api/packet/"):
            tail = parsed.path.removeprefix("/api/packet/").strip("/")
            if tail.isdigit():
                intake_id = int(tail)
                intake_rec = fetch_client_intake_by_id(intake_id, DEFAULT_DB_PATH)
                if intake_rec is not None and not intake_rec.agreement_signed:
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "error": (
                                f"Sign-to-Unlock Gate Active: Intake #{intake_id} has not executed the "
                                "ZYS GoldFields PRO LLC Advisory Agreement (agreement_signed == False)."
                            )
                        },
                    )
                    return
                target = PACKETS_ARCHIVE_DIR / f"client_submission_packet_{intake_id}.md"
                if target.exists():
                    self._send_markdown_file(target, f"client_submission_packet_{intake_id}.md")
                    return

        if parsed.path == "/api/preview/latest":
            if (os.environ.get("PUBLIC_MODE", "0") == "1" or os.environ.get("RENDER")) and not self._check_admin_auth():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Latest preview downloads disabled in public mode."})
                return
            if not DEFAULT_PREVIEW_PATH.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No preview summary generated yet."})
                return
            self._send_markdown_file(DEFAULT_PREVIEW_PATH, "client_preview_summary.md")
            return

        if parsed.path.startswith("/api/preview/"):
            tail = parsed.path.removeprefix("/api/preview/").strip("/")
            if tail.isdigit():
                target = PACKETS_ARCHIVE_DIR / f"client_preview_summary_{int(tail)}.md"
                if target.exists():
                    self._send_markdown_file(target, f"client_preview_summary_{int(tail)}.md")
                    return

        if parsed.path == "/api/agreement/latest":
            if (os.environ.get("PUBLIC_MODE", "0") == "1" or os.environ.get("RENDER")) and not self._check_admin_auth():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Latest agreement downloads disabled in public mode."})
                return
            if not DEFAULT_SIGNED_AGREEMENT_PATH.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No signed advisory agreement generated yet."})
                return
            self._send_markdown_file(DEFAULT_SIGNED_AGREEMENT_PATH, "signed_advisory_agreement.md")
            return

        if parsed.path.startswith("/api/agreement/"):
            tail = parsed.path.removeprefix("/api/agreement/").strip("/")
            if tail.isdigit():
                target = PACKETS_ARCHIVE_DIR / f"signed_advisory_agreement_{int(tail)}.md"
                if target.exists():
                    self._send_markdown_file(target, f"signed_advisory_agreement_{int(tail)}.md")
                    return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_bytes = self.rfile.read(length) if length > 0 else b""
        client_ip = self._extract_client_ip()
        user_agent = self._extract_user_agent()

        if parsed.path == "/api/intake":
            try:
                body = json.loads(raw_bytes.decode("utf-8") or "{}")
                result = _process_intake_payload(body, client_ip=client_ip, user_agent=user_agent)
                self._send_json(HTTPStatus.OK, result)
            except ValidationError as verr:
                first_msg = verr.errors()[0].get("msg", "Invalid intake input.")
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": first_msg})
            except ValueError as val_err:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(val_err)})
            except Exception as exc:
                logger.exception("Error processing /api/intake")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if parsed.path == "/api/sign":
            try:
                body = json.loads(raw_bytes.decode("utf-8") or "{}")
                result = _unlock_existing_intake(body, client_ip=client_ip, user_agent=user_agent)
                self._send_json(HTTPStatus.OK, result)
            except ValueError as val_err:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(val_err)})
            except Exception as exc:
                logger.exception("Error processing /api/sign")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if parsed.path == "/submit":
            try:
                form = parse_qs(raw_bytes.decode("utf-8"), keep_blank_values=True)
                flat = {k: (v[0] if v else "") for k, v in form.items()}
                flat["agreed_to_terms"] = flat.get("agreed_to_terms", "").lower() in ("true", "on", "1", "yes")
                result = _process_intake_payload(flat, client_ip=client_ip, user_agent=user_agent)
                confirmation_html = (
                    f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Submission Confirmed</title></head>"
                    f"<body style='font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 20px;'>"
                    f"<h1>Submission Dossier Unlocked</h1>"
                    f"<p><strong>Matched Grant:</strong> {result['matched_grant_title']}</p>"
                    f"<p><strong>Statutory Eligibility Assessment:</strong> {result.get('eligibility_tier', result['probability_score'])}</p>"
                    f"<p><strong>Engineered Request:</strong> {result['engineered_grant_request']}</p>"
                    f"<p><strong>SHA-256 Contract Hash:</strong> <code>{result.get('contract_sha256_hash', '')}</code></p>"
                    f"<p><a href='{result['packet_download_url']}'>Download client_submission_packet.md</a> | "
                    f"<a href='{result.get('signed_agreement_download_url', '/api/agreement/latest')}'>Download signed_advisory_agreement.md</a> | "
                    f"<a href='/'>Back to Portal</a></p>"
                    f"<pre style='background:#f6f5f0;padding:16px;overflow:auto;'>{result['packet_markdown']}</pre>"
                    f"</body></html>"
                )
                self._send_html(HTTPStatus.OK, confirmation_html)
            except Exception as exc:
                self._send_html(HTTPStatus.BAD_REQUEST, f"<h1>Validation Error</h1><p>{exc}</p><p><a href='/'>Go Back</a></p>")
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Start the ThreadingHTTPServer for the digital intake & e-signature portal."""
    ensure_database_ready(DEFAULT_DB_PATH)
    server = ThreadingHTTPServer((host, port), IntakePortalHandler)
    logger.info("ZYS GoldFields PRO Intake & E-Signature Portal listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down intake portal server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ZYS GoldFields PRO Zero-Call Digital Client Intake Portal.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (defaults to $PORT or 8080)")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
