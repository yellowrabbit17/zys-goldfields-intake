# ZYS GoldFields PRO LLC — Conservation Grant Hunter, E-Signature & Zero-Call Intake Portal

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/yellowrabbit17/zys-goldfields-intake)

End-to-end automated California conservation grant underwriting, Tier-1 prospect qualification, 1-page cold-outreach pitch generation, California UETA / ESIGN e-signature execution (`2 CFR § 200.459` compliant), and "Sign-to-Unlock" County Resource Conservation District (RCD) / USDA NRCS submission packaging.

## Key Capabilities

1. **Live Federal & California Grant Ingestion (`scraper.py`)**:
   - Queries the public **Grants.gov REST API** and **California Climate Investments (CCI)** open funding programs into `grants.db` (`48` active conservation opportunities).
2. **4-Pillar Grant Award Probability Scoring Engine (`matcher.py` & `config.json`)**:
   - Evaluates every candidate on a strict `0–100` scale and enforces `min_probability_score: 75`:
     - **Acreage & Threshold Fit (30 pts)**: Statutory agency parcel minimums/maximums (e.g., CAL FIRE CFIP `20–5,000 ac` window, SALC agricultural easement scale, USDA ACEP landscape thresholds).
     - **Entity & Tax-Exempt Eligibility (25 pts)**: Verified federal IRS Form 990 `501(c)(3)` EIN standing, commercial agricultural producer eligibility, or County RCD / Land Trust co-sponsorship readiness.
     - **Ecological & Geographic Priority Fit (25 pts)**: County fire-severity zones, coastal sea-level-rise watersheds, Pacific Flyway wetland corridors, and prime farmland conversion-risk zones.
     - **Funding-to-Scale Ratio & Co-Match Feasibility (20 pts)**: Cost-share / bargain-sale / in-kind match feasibility relative to parcel acreage and organizational scale.
3. **Tier-1 Ranked California Prospects & Outreach Briefs (`prospect_hunter.py`)**:
   - `california_prospects.csv` & `outreach_pitches.md`: **57 qualified Tier-1 California wildlife sanctuaries, conservation ranches, and land trusts** (`score >= 75/100`, ranked `98/100` down to `76/100`), with weak or statutorily non-compliant leads automatically excluded.
4. **California UETA / ESIGN Contingency Agreement & `2 CFR § 200.459` Protection (`legal_agreement.py`, `intake_app.py`)**:
   - Embeds a scrollable **"Conservation Advisory & Origination Agreement"** governed by the **California Uniform Electronic Transactions Act (UETA)** (`Cal. Civ. Code § 1633.1 et seq.`) and federal **ESIGN Act** (`15 U.S.C. § 7001 et seq.`).
   - **Federal & State Grant Compliance (`2 CFR § 200.459`)**: Explicitly stipulates that the **7% Advisory Success Fee** is paid strictly from the Client's separate general/operating funds upon official Agency Award Notice (payable within **5 business days** of initial tranche disbursement, with a **1.5% monthly late fee** and **Sacramento County, California** venue / prevailing-party attorney fees under `Cal. Civ. Code § 1717`), and is never billed to or deducted from restricted grant funds.
   - **Cryptographic Audit Trail (`grants.db` & `signed_advisory_agreement.md`)**: Captures the signer's Full Legal Name, Title, UTC Timestamp, Client IP Address, User-Agent browser string, and `SHA-256` hash of the signed contract text.
5. **"Sign-to-Unlock" Final Filing Gate (`admin_packager.py`)**:
   - **Stage A — Client Preview Summary (`client_preview_summary.md`)**: Displays the matched grant title, engineered budget total, and probability score while masking the grant solicitation ID, copy-paste narrative, and County RCD Co-Sponsor Memorandum until `agreement_signed == True` in `grants.db`.
   - **Stage B — Unlocked RCD & Agency Filing Dossier (`client_submission_packet.md`)**: Unlocks immediately upon e-signature completion and appends **Part C: Notice of Authorized Advisory Representation & E-Signature Certificate**.

## 1-Click Render Free Deployment (`render.yaml`)

1. Click the **Deploy to Render** button above, or open [https://dashboard.render.com](https://dashboard.render.com) -> **New +** -> **Blueprint** and connect this repository.
2. Render automatically detects `render.yaml`, installs `requirements.txt`, boots `intake_app.py` with `PUBLIC_MODE=1`, and serves the pre-scored `grants.db` and `california_prospects.csv` over HTTPS.

## Local Execution

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python run_pipeline.py
./venv/bin/python intake_app.py --host 0.0.0.0 --port 8080
```
