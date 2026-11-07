"""Zero-Call Digital Client Intake Portal (intake_app.py).

Serves a single-page web intake portal for ZYS GoldFields PRO LLC (Conservation & Agricultural
Grant Advisory) and automatically generates client_submission_packet.md via admin_packager.py
upon submission.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    build_budget_engineering_table,
    generate_submission_packet,
    resolve_local_agency_contacts,
)
from db import DEFAULT_DB_PATH, fetch_all_grants, fetch_all_intakes, fetch_all_prospects, init_db
from matcher import DEFAULT_CONFIG_PATH, load_matcher_config
from models import ClientIntake

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("intake_app")

DEFAULT_HOST = os.environ.get("INTAKE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PORT", os.environ.get("INTAKE_PORT", "8080")))
PACKETS_ARCHIVE_DIR = Path(__file__).resolve().parent / "packets"

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


def ensure_database_ready(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Ensure SQLite tables exist and bootstrap live grants if running on a cold cloud container."""
    init_db(db_path)
    grants = fetch_all_grants(db_path)
    if not grants:
        logger.info("Cold container detected (0 grants in grants.db); running live grant ingestion...")
        from scraper import ingest_all_grants

        ingest_all_grants(db_path=db_path)


def render_portal_html() -> str:
    """Render the single-page HTML/CSS/JS Zero-Call Digital Client Intake Portal."""
    cfg = load_matcher_config(DEFAULT_CONFIG_PATH)
    fee_str = cfg.fee_label()
    public_mode_js = "true" if os.environ.get("PUBLIC_MODE", "0") == "1" else "false"
    county_options_html = "".join(f'<option value="{c}"></option>' for c in ALL_CA_COUNTIES)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{cfg.firm_name} — Digital Property Grant Intake</title>
  <meta name="description" content="2-minute zero-call digital property intake portal for {cfg.firm_name} ({cfg.firm_subtitle}). Submit parcel details asynchronously for conservation grant filing under {fee_str} no-win/no-fee terms." />
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
      max-width: 820px;
      margin: 0 auto;
      padding: 40px 20px 72px;
    }}

    header.masthead {{
      border-bottom: 1px solid var(--border-stone);
      padding-bottom: 24px;
      margin-bottom: 32px;
    }}

    .firm-lockup {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 14px;
    }}

    .firm-name {{
      font-family: 'Newsreader', Georgia, serif;
      font-weight: 600;
      font-size: 1.15rem;
      letter-spacing: 0.02em;
      color: var(--ink-primary);
    }}

    .firm-practice {{
      font-size: 0.82rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent-ochre);
    }}

    h1 {{
      font-family: 'Newsreader', Georgia, serif;
      font-weight: 500;
      font-size: clamp(1.85rem, 3.6vw, 2.45rem);
      line-height: 1.18;
      letter-spacing: -0.015em;
      margin: 0 0 12px;
      color: var(--ink-primary);
    }}

    .lead-copy {{
      margin: 0;
      color: var(--ink-secondary);
      max-width: 66ch;
      font-size: 0.98rem;
    }}

    .assurance-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 20px;
      margin-top: 18px;
      padding-top: 16px;
      border-top: 1px solid var(--border-stone);
      font-size: 0.84rem;
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
      padding: clamp(22px, 4vw, 38px);
    }}

    .sheet-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 24px;
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
      font-weight: 500;
      padding: 6px 12px;
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
      gap: 18px 20px;
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
      gap: 6px;
    }}

    label {{
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--ink-primary);
    }}

    .label-hint {{
      font-weight: 400;
      color: var(--ink-muted);
      font-size: 0.78rem;
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
      font-size: 0.93rem;
      line-height: 1.45;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}

    input.mono-input {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.88rem;
    }}

    textarea {{
      min-height: 92px;
      resize: vertical;
    }}

    input:focus,
    textarea:focus {{
      outline: none;
      border-color: var(--border-focus);
      box-shadow: 0 0 0 3px hsla(154, 42%, 28%, 0.12);
    }}

    .terms-box {{
      margin-top: 22px;
      padding: 16px 18px;
      background-color: var(--surface-subtle);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
    }}

    .checkbox-row {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      cursor: pointer;
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
      font-size: 0.86rem;
      color: var(--ink-secondary);
      line-height: 1.55;
    }}

    .terms-text strong {{
      color: var(--ink-primary);
    }}

    .submit-row {{
      margin-top: 24px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}

    .btn-primary {{
      width: 100%;
      padding: 13px 20px;
      background-color: var(--accent-pine);
      color: #fff;
      border: none;
      border-radius: var(--radius-sm);
      font-family: inherit;
      font-size: 0.96rem;
      font-weight: 600;
      letter-spacing: 0.01em;
      cursor: pointer;
      transition: background-color 0.16s ease, transform 0.08s ease;
    }}

    .btn-primary:hover {{
      background-color: var(--accent-pine-hover);
    }}

    .btn-primary:active {{
      transform: translateY(1px);
    }}

    .btn-primary:disabled {{
      opacity: 0.65;
      cursor: wait;
    }}

    .alert-banner {{
      display: none;
      padding: 12px 15px;
      border-radius: var(--radius-sm);
      font-size: 0.88rem;
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
      margin-top: 30px;
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
      margin-bottom: 20px;
    }}

    .summary-dl {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 14px;
      margin: 0 0 22px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border-stone);
    }}

    .summary-item dt {{
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--ink-muted);
      margin-bottom: 3px;
    }}

    .summary-item dd {{
      margin: 0;
      font-weight: 600;
      font-size: 0.94rem;
      color: var(--ink-primary);
    }}

    .summary-item dd.mono {{
      font-family: 'JetBrains Mono', monospace;
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
      padding: 9px 16px;
      border-radius: var(--radius-sm);
      font-size: 0.85rem;
      font-weight: 600;
    }}

    .packet-preview {{
      background: hsl(40, 20%, 97%);
      border: 1px solid var(--border-stone);
      border-radius: var(--radius-sm);
      padding: 16px;
      max-height: 420px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.79rem;
      line-height: 1.55;
      white-space: pre-wrap;
      color: var(--ink-primary);
    }}

    footer.site-footer {{
      margin-top: 36px;
      text-align: center;
      font-size: 0.8rem;
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
      <h1>Digital Property Grant Intake</h1>
      <p class="lead-copy">
        Complete this 2-minute property intake to generate your matched conservation grant narrative,
        engineered line-item budget, and County Resource Conservation District (RCD) filing handoff—100%
        online with zero phone calls or meetings required.
      </p>
      <div class="assurance-strip">
        <span><strong>$0 Upfront Cost</strong> · Strictly No-Win, No-Fee</span>
        <span><strong>{fee_str} Contingent Success Fee</strong> · Payable Only Upon Award</span>
        <span><strong>Asynchronous RCD / NRCS Handoff</strong> · No Phone Calls</span>
      </div>
    </header>

    <main>
      <section class="sheet" aria-labelledby="form-heading">
        <div class="sheet-toolbar">
          <h2 id="form-heading">Parcel &amp; Conservation Scope</h2>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <a id="btn-repo-zip" href="/api/repo-archive.zip" download="zys-goldfields-intake.zip" class="btn-ghost" style="text-decoration:none;">
              Download GitHub Repo (.zip)
            </a>
            <button type="button" id="btn-prefill-vance" class="btn-ghost">
              Load Sample Parcel (Vance — El Dorado 45 ac)
            </button>
          </div>
        </div>

        <form id="intake-form" action="/submit" method="post" novalidate>
          <div class="form-grid">
            <div class="field-group">
              <label for="input-name">Landowner / Entity Name</label>
              <input type="text" id="input-name" name="name" required placeholder="e.g., Elena &amp; Marcus Vance" />
            </div>

            <div class="field-group">
              <label for="input-email">Email Address <span class="label-hint">(for digital packet delivery)</span></label>
              <input type="email" id="input-email" name="email" required placeholder="name@domain.com" />
            </div>

            <div class="field-group">
              <label for="input-county">California County</label>
              <input type="text" id="input-county" name="county" list="ca-counties" required placeholder="e.g., El Dorado" />
              <datalist id="ca-counties">
                {county_options_html}
              </datalist>
            </div>

            <div class="field-group">
              <label for="input-apn">Parcel APN Number</label>
              <input type="text" id="input-apn" name="apn" class="mono-input" required placeholder="e.g., 089-140-027-000" />
            </div>

            <div class="field-group field-full">
              <label for="input-acreage">Total Parcel Acreage</label>
              <input type="number" id="input-acreage" name="acreage" class="mono-input" min="1" max="100000" step="0.1" required placeholder="e.g., 45" />
            </div>

            <div class="field-group field-full">
              <label for="input-condition">Current Land Condition</label>
              <textarea id="input-condition" name="land_condition" required placeholder="Describe existing vegetation, oak/forest mortality, brush fuel load, streambank/riparian condition, or soil erosion..."></textarea>
            </div>

            <div class="field-group field-full">
              <label for="input-goals">Project Goals</label>
              <textarea id="input-goals" name="project_goals" required placeholder="Describe desired improvements: reforestation, RPF forest management plan, fuels thinning, riparian culvert replacement, solar irrigation, or conservation easement..."></textarea>
            </div>
          </div>

          <div class="terms-box">
            <label class="checkbox-row" for="checkbox-terms">
              <input type="checkbox" id="checkbox-terms" name="agreed_to_terms" value="true" required />
              <span class="terms-text">
                <strong>I agree to the {cfg.firm_name} {fee_str} No-Win / No-Fee Advisory Terms:</strong>
                I authorize {cfg.firm_name} ({cfg.firm_subtitle}) to prepare my conservation grant submission
                packet and coordinate technical handoff with my County Resource Conservation District (RCD)
                and local USDA NRCS Service Center. I owe <strong>$0 upfront</strong> and <strong>$0 if no grant
                is awarded</strong>; a contingent <strong>{fee_str} success fee</strong> applies solely upon
                official grant award execution.
              </span>
            </label>
          </div>

          <div id="form-error" class="alert-banner" role="alert"></div>

          <div class="submit-row">
            <button type="submit" id="btn-submit-intake" class="btn-primary">
              Submit Digital Intake &amp; Generate Filing Packet
            </button>
          </div>
        </form>
      </section>

      <section id="confirmation-panel" class="confirmation-panel" aria-live="polite">
        <div id="status-banner" class="status-header">
          Submission Packet Generated &amp; Ready for County RCD / NRCS Dispatch
        </div>

        <dl class="summary-dl">
          <div class="summary-item">
            <dt>Primary Matched Grant</dt>
            <dd id="res-grant">—</dd>
          </div>
          <div class="summary-item">
            <dt>Engineered Grant Request</dt>
            <dd id="res-request" class="mono">—</dd>
          </div>
          <div class="summary-item">
            <dt>Application Deadline</dt>
            <dd id="res-deadline">—</dd>
          </div>
          <div class="summary-item">
            <dt>Assigned Local Handoff Target</dt>
            <dd id="res-rcd">—</dd>
          </div>
        </dl>

        <div class="packet-actions">
          <a href="/api/packet/latest" id="btn-download-packet" class="btn-download" download="client_submission_packet.md">
            Download client_submission_packet.md
          </a>
          <button type="button" id="btn-copy-packet" class="btn-ghost">
            Copy Complete Packet Markdown
          </button>
        </div>

        <div id="packet-preview" class="packet-preview"></div>
      </section>
    </main>

    <footer class="site-footer">
      &copy; {cfg.firm_name} &middot; {cfg.firm_subtitle} &middot; 100% Digital Asynchronous Landowner Onboarding
    </footer>
  </div>

  <script>
    const PUBLIC_MODE = {public_mode_js};
    const form = document.getElementById('intake-form');
    const errorEl = document.getElementById('form-error');
    const submitBtn = document.getElementById('btn-submit-intake');
    const confirmPanel = document.getElementById('confirmation-panel');
    const previewEl = document.getElementById('packet-preview');
    const prefillBtn = document.getElementById('btn-prefill-vance');
    const repoZipBtn = document.getElementById('btn-repo-zip');
    const copyBtn = document.getElementById('btn-copy-packet');

    // Auto-populate fields when a prospect clicks their personalized ?name=...&email=...&county=...&acres=... link
    const urlParams = new URLSearchParams(window.location.search);
    if (PUBLIC_MODE) {{
      if (repoZipBtn) repoZipBtn.style.display = 'none';
    }}
    if (PUBLIC_MODE || urlParams.has('name') || urlParams.has('org')) {{
      prefillBtn.style.display = 'none';
    }}
    if (urlParams.get('name') || urlParams.get('org')) {{
      document.getElementById('input-name').value = urlParams.get('name') || urlParams.get('org');
    }}
    if (urlParams.get('email')) {{
      document.getElementById('input-email').value = urlParams.get('email');
    }}
    if (urlParams.get('county')) {{
      document.getElementById('input-county').value = urlParams.get('county');
    }}
    if (urlParams.get('acres')) {{
      document.getElementById('input-acreage').value = urlParams.get('acres');
    }}
    if (urlParams.get('condition')) {{
      document.getElementById('input-condition').value = urlParams.get('condition');
    }}
    if (urlParams.get('goals')) {{
      document.getElementById('input-goals').value = urlParams.get('goals');
    }}

    prefillBtn.addEventListener('click', () => {{
      document.getElementById('input-name').value = 'Elena & Marcus Vance';
      document.getElementById('input-email').value = 'vance.family@sierraridgeoak.com';
      document.getElementById('input-county').value = 'El Dorado';
      document.getElementById('input-apn').value = '089-140-027-000';
      document.getElementById('input-acreage').value = '45';
      document.getElementById('input-condition').value =
        '45-acre degraded blue oak, black oak, and ponderosa pine woodland in El Dorado County experiencing post-drought canopy mortality, invasive brush fuel accumulation, and seasonal gully soil erosion.';
      document.getElementById('input-goals').value =
        'Complete a Registered Professional Forester (RPF) Forest Management Plan, mechanical understory fuels thinning, native oak and pine reforestation, gully erosion stabilization, and wildlife habitat restoration.';
      document.getElementById('checkbox-terms').checked = true;
      errorEl.className = 'alert-banner';
      errorEl.textContent = '';
    }});

    copyBtn.addEventListener('click', async () => {{
      if (!previewEl.textContent) return;
      await navigator.clipboard.writeText(previewEl.textContent);
      const orig = copyBtn.textContent;
      copyBtn.textContent = 'Copied to Clipboard';
      setTimeout(() => {{ copyBtn.textContent = orig; }}, 1800);
    }});

    form.addEventListener('submit', async (ev) => {{
      ev.preventDefault();
      errorEl.className = 'alert-banner';
      errorEl.textContent = '';

      const payload = {{
        name: document.getElementById('input-name').value.trim(),
        email: document.getElementById('input-email').value.trim(),
        county: document.getElementById('input-county').value.trim(),
        state: 'CA',
        apn: document.getElementById('input-apn').value.trim(),
        acreage: parseFloat(document.getElementById('input-acreage').value || '0'),
        land_condition: document.getElementById('input-condition').value.trim(),
        project_goals: document.getElementById('input-goals').value.trim(),
        agreed_to_terms: document.getElementById('checkbox-terms').checked
      }};

      submitBtn.disabled = true;
      submitBtn.textContent = 'Matching Active Grants & Engineering Packet...';

      try {{
        const resp = await fetch('/api/intake', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        const data = await resp.json();
        if (!resp.ok) {{
          throw new Error(data.error || 'Please check all required parcel fields and terms agreement.');
        }}

        document.getElementById('res-grant').textContent = data.matched_grant_title;
        document.getElementById('res-request').textContent = data.engineered_grant_request;
        document.getElementById('res-deadline').textContent = data.deadline;
        document.getElementById('res-rcd').textContent = data.rcd_target;
        previewEl.textContent = data.packet_markdown;

        confirmPanel.classList.add('visible');
        confirmPanel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }} catch (err) {{
        errorEl.textContent = err.message;
        errorEl.className = 'alert-banner error';
      }} finally {{
        submitBtn.disabled = false;
        submitBtn.textContent = 'Submit Digital Intake & Generate Filing Packet';
      }}
    }});
  </script>
</body>
</html>
"""


def _process_intake_payload(raw_data: dict[str, Any]) -> dict[str, Any]:
    """Validate intake payload, run admin_packager, and return structured response JSON."""
    intake = ClientIntake(
        name=str(raw_data.get("name", "")),
        email=str(raw_data.get("email", "")),
        county=str(raw_data.get("county", "")),
        state=str(raw_data.get("state") or "CA"),
        apn=str(raw_data.get("apn", "")),
        acreage=float(raw_data.get("acreage") or 0),
        land_condition=str(raw_data.get("land_condition", "")),
        project_goals=str(raw_data.get("project_goals", "")),
        agreed_to_terms=bool(raw_data.get("agreed_to_terms")),
    )

    cfg = load_matcher_config(DEFAULT_CONFIG_PATH)
    saved, matches, packet_path = generate_submission_packet(
        intake=intake,
        db_path=DEFAULT_DB_PATH,
        config_path=DEFAULT_CONFIG_PATH,
        output_path=DEFAULT_PACKET_PATH,
        persist_intake=True,
    )
    primary = matches[0]
    _, total_cost, grant_req, _, fee_dollars = build_budget_engineering_table(
        saved, primary.grant, fee_percent=cfg.success_fee_percent
    )
    contacts = resolve_local_agency_contacts(saved.county, saved.state)
    packet_md = packet_path.read_text(encoding="utf-8")
    if saved.id is not None:
        PACKETS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archived_file = PACKETS_ARCHIVE_DIR / f"client_submission_packet_{saved.id}.md"
        archived_file.write_text(packet_md, encoding="utf-8")

    return {
        "status": "ok",
        "intake_id": saved.id,
        "client_name": saved.name,
        "apn": saved.apn,
        "county": saved.county,
        "matched_grant_id": primary.grant.id,
        "matched_grant_title": primary.grant.title,
        "probability_score": f"{primary.score}/100",
        "score_rationale": primary.score_rationale,
        "agency": primary.grant.agency,
        "deadline": primary.grant.deadline,
        "engineered_grant_request": f"${grant_req:,} (of ${total_cost:,} total project budget)",
        "contingent_fee_upon_award": f"${fee_dollars:,}",
        "rcd_target": f"{contacts['rcd_name']} / {contacts['nrcs_office']}",
        "packet_path": str(packet_path),
        "packet_download_url": f"/api/packet/{saved.id}" if saved.id else "/api/packet/latest",
        "packet_markdown": packet_md,
    }


class IntakePortalHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP request handler for the Zero-Call Digital Client Intake Portal."""

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_html(HTTPStatus.OK, render_portal_html())
            return

        if parsed.path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "healthy",
                    "service": "ZYS GoldFields PRO Intake Portal",
                    "public_mode": os.environ.get("PUBLIC_MODE", "0") == "1",
                },
            )
            return

        if parsed.path == "/api/prospects":
            prospects = [p.model_dump() for p in fetch_all_prospects(DEFAULT_DB_PATH)]
            self._send_json(HTTPStatus.OK, {"count": len(prospects), "prospects": prospects})
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
                "client_submission_packet.md",
                "config.json",
                "db.py",
                "grants.db",
                "intake_app.py",
                "matcher.py",
                "models.py",
                "outreach_pitches.md",
                "prospect_hunter.py",
                "render.yaml",
                "requirements.txt",
                "run_pipeline.py",
                "runtime.txt",
                "scraper.py",
                "seed_profiles.py",
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
            intakes = [
                {
                    **item.model_dump(),
                    "packet_url": f"/api/packet/{item.id}",
                }
                for item in fetch_all_intakes(DEFAULT_DB_PATH)
            ]
            self._send_json(HTTPStatus.OK, {"count": len(intakes), "intakes": intakes})
            return

        if parsed.path == "/api/packet/latest":
            if not DEFAULT_PACKET_PATH.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No submission packet generated yet."})
                return
            data = DEFAULT_PACKET_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="client_submission_packet.md"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path.startswith("/api/packet/"):
            tail = parsed.path.removeprefix("/api/packet/").strip("/")
            if tail.isdigit():
                target = PACKETS_ARCHIVE_DIR / f"client_submission_packet_{int(tail)}.md"
                if target.exists():
                    data = target.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="client_submission_packet_{int(tail)}.md"',
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_bytes = self.rfile.read(length) if length > 0 else b""

        if parsed.path == "/api/intake":
            try:
                body = json.loads(raw_bytes.decode("utf-8") or "{}")
                result = _process_intake_payload(body)
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

        if parsed.path == "/submit":
            try:
                form = parse_qs(raw_bytes.decode("utf-8"), keep_blank_values=True)
                flat = {
                    k: (v[0] if v else "")
                    for k, v in form.items()
                }
                flat["agreed_to_terms"] = flat.get("agreed_to_terms", "").lower() in ("true", "on", "1", "yes")
                result = _process_intake_payload(flat)
                confirmation_html = (
                    f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Submission Confirmed</title></head>"
                    f"<body style='font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 20px;'>"
                    f"<h1>Submission Packet Generated</h1>"
                    f"<p><strong>Matched Grant:</strong> {result['matched_grant_title']}</p>"
                    f"<p><strong>Engineered Request:</strong> {result['engineered_grant_request']}</p>"
                    f"<p><a href='{result['packet_download_url']}'>Download client_submission_packet.md</a> | <a href='/'>Back to Portal</a></p>"
                    f"<pre style='background:#f6f5f0;padding:16px;overflow:auto;'>{result['packet_markdown']}</pre>"
                    f"</body></html>"
                )
                self._send_html(HTTPStatus.OK, confirmation_html)
            except Exception as exc:
                self._send_html(HTTPStatus.BAD_REQUEST, f"<h1>Validation Error</h1><p>{exc}</p><p><a href='/'>Go Back</a></p>")
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Start the ThreadingHTTPServer for the digital intake portal."""
    ensure_database_ready(DEFAULT_DB_PATH)
    server = ThreadingHTTPServer((host, port), IntakePortalHandler)
    logger.info("ZYS GoldFields PRO Intake Portal listening on http://%s:%d", host, port)
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
