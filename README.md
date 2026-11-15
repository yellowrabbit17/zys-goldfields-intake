# ZYS GoldFields PRO LLC — Conservation Grant Hunter, E-Signature & Zero-Call Intake Portal

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/yellowrabbit17/zys-goldfields-intake)

End-to-end automated California conservation grant underwriting, Tier-1 prospect qualification, 1-page cold-outreach pitch generation, California UETA / ESIGN e-signature execution (`2 CFR § 200.459` compliant), "Sign-to-Unlock" County Resource Conservation District (RCD) / USDA NRCS submission packaging, persistent PostgreSQL storage, primary custom domain routing (`https://grants.zysgoldfields.pro`), and warm-up rate-limited Day 4 / Day 9 follow-up automation.

## Production Hardening Upgrades

1. **Neon Lakebase Postgres & Dual-Transport Storage (`db.py`, `neon.ts`, `intake_app.py`)**:
   - **Pooled App Queries vs. Direct Migrations**: Uses `DATABASE_URL_POOLED` (`-pooler`) for regular runtime reads/writes and direct unpooled `DATABASE_URL` (`_strip_pooler_host`) for schema DDL / startup migrations. Falls back cleanly to local SQLite (`grants.db`) only when neither is configured.
   - **Automatic TCP + Neon Serverless HTTPS `/sql` Fallback**: Connects via `psycopg2-binary` over PostgreSQL TCP when available and seamlessly switches to Neon Serverless HTTPS `/sql` transport when TCP port `5432` is blocked or proxied (`NEON_SQL_TRANSPORT=auto|http`).
   - **Automatic Migrations & Cold-DB Bootstrapping**: Runs schema DDL and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations automatically on startup, and seeds `grants`, `landowner_profiles`, `california_prospects`, and `prospect_outreach_status` on fresh databases so signed agreements, intake packets, and outreach tracking survive every redeploy.
   - **Resilient Download Fallback**: Even if ephemeral container local disk files (`packets/*.md`) are cleared on restart, `/api/packet/<id>`, `/api/preview/<id>`, and `/api/agreement/<id>` reconstruct exact Markdown dossiers on-the-fly from persisted PostgreSQL records (while enforcing the Sign-to-Unlock gate).

2. **Neon Managed Better Auth (`neon_auth.py`, `/auth`, `/admin`, `/api/auth/*`, `/api/me`)**:
   - **Same-Origin Auth Proxy (`/api/auth/*`)**: Proxies authentication requests cleanly to `NEON_AUTH_BASE_URL`, normalizing first-party cookies across local HTTP dev and production HTTPS (`grants.zysgoldfields.pro`) without exposing third-party cookie domains.
   - **EdDSA JWT & Session Verification (`/api/me`)**: Cryptographically verifies EdDSA (`Ed25519`) Bearer JWTs against `NEON_AUTH_JWKS_URL` (`PyJWT` + `PyJWKClient`) and validates active Neon Auth session cookies for staff console access (`/auth`, `/admin`, `/api/intakes`, `/api/audits`, `/api/prospects`) alongside `ADMIN_API_TOKEN`.
   - **Zero-Friction Landowner Flow Preserved**: Landowner 1-click claim links (`/claim?prospect_id=...`), Stage-A previews, and California UETA Stage-B e-signatures remain frictionless without requiring landowner account creation.

3. **Off-Host Redundant Backups & Keep-Alive (`notifier.py`, `intake_app.py`)**:
   - **Transactional Email with Attachments**: Immediately dispatches `signed_advisory_agreement.md`, `esignature_audit_record.json`, and `client_submission_packet.md` to your admin inbox via **Resend** (HTTPS API) or **SMTP** from `grants@zysgoldfields.pro`.
   - **Persistent Backup Webhook**: Optional HMAC-SHA256 signed JSON webhook (`X-ZYS-Signature: sha256=<hex>`) to Make / Zapier / n8n / Google Apps Script.
   - **24/7 Keep-Alive Endpoint (`/healthz`)** and **Diagnostic Check (`/api/health`, `/api/test-email`)**: Fast zero-DB `GET`/`HEAD /healthz` plus rich health diagnostics reporting database backend persistence, pooled/direct driver state, Neon Auth status, active `BASE_URL`, `DAILY_SEND_LIMIT`, and email readiness.

4. **Custom Domain Support, CORS, Session Cookies & Proxy Caching (`models.py`, `matcher.py`, `prospect_hunter.py`, `intake_app.py`)**:
   - **Primary Domain `https://grants.zysgoldfields.pro`**: `BASE_URL` defaults cleanly to `https://grants.zysgoldfields.pro` across all `/claim?prospect_id=...` magic links, generated templates, outreach batches, and canonical `<link>` tags. (Note: add `https://grants.zysgoldfields.pro` and `https://zys-goldfields-intake.onrender.com` to Neon Auth Trusted Domains so browser origins on production domains are accepted.)
   - **CORS, Cookie/Session & Cloudflare/Proxy Headers**: Supports `OPTIONS` preflight and `Access-Control-Allow-*` / `Vary: Origin` CORS headers scoped to `https://grants.zysgoldfields.pro` and `https://zys-goldfields-intake.onrender.com`, issues signed `HttpOnly; SameSite=Lax; Secure` session cookies over reverse-proxy HTTPS, extracts client IPs via `CF-Connecting-IP` / `X-Forwarded-For`, and emits `Cache-Control: no-cache, no-store, must-revalidate, max-age=0` + `CDN-Cache-Control: no-store` on dynamic HTML and API routes.
   - **1-Click Pre-Filled Experience (`/claim?prospect_id=CA-T1-###`)**: Pre-fills Organization Name, County, EIN, Acreage, Conservation Focus, and Top Matched Grant from PostgreSQL/`california_prospects.csv`. Prospects verify their parcel APN, e-sign, and unlock their RCD filing packet in 1 click.

5. **Automated Day 4 & Day 9 Outreach Scheduler + Warm-Up Rate Limiting (`outreach_scheduler.py`, `.github/workflows/outreach_cron.yml`)**:
   - **Protected Cron Endpoint (`/api/cron/run-outreach`)**: Strictly secured by `ADMIN_API_TOKEN` (`Authorization: Bearer ...`), triggered daily at `16:00 UTC` (8 AM PST / 9 AM PDT) via GitHub Actions with Render cold-start retries.
   - **Safe Domain Warm-Up Rate Limiting (`DAILY_SEND_LIMIT`)**: Caps total daily combined Day 0 / Day 4 / Day 9 emails (default `5`/day) and tracks exact per-day UTC send volume in `outreach_send_log`.
   - **Priority Cadence, Idempotency & Suppression**: Processes `Day 9 Follow-Up` -> `Day 4 Follow-Up` -> `Day 0 Initial Brief`, enforces atomic `(prospect_id, sequence_step)` database locks so no prospect is ever emailed twice for the same cadence step, and automatically suppresses any prospect who claims/signs on the portal or unsubscribes (`/api/outreach/mark`).

6. **Statutory Tiering, Land Trust Segmentation & CAN-SPAM Compliance (`matcher.py`, `legal_agreement.py`)**:
   - Client correspondence translates internal scores into `"Tier-1 Statutory Eligibility Match (4/4 Core Criteria Met)"`, segments `Land Trust` (`Channel_Partner`) vs. Sanctuaries/Ranches (`Direct_Client`), phrases the 7% fee as a `"7% Deferred Administrative Origination & Packaging Fee (100% Waived if Grant is Not Awarded)"` (`2 CFR § 200.459`), and embeds the CAN-SPAM physical address (`8698 Elk Grove Blvd Ste 1-279 Elk Grove CA 95624`) and unsubscribe disclosures in `outreach_pitches.md` and `followup_templates.md`.

## Architecture & Data Flow

```mermaid
flowchart TD
    A["Public Scraper & Prospect Hunter"] --> B[("Neon Lakebase Postgres (Pooled + Direct) / SQLite Fallback")]
    B --> C["california_prospects & prospect_outreach_status"]
    D["GitHub Actions Daily Cron (16:00 UTC)"] -->|"POST /api/cron/run-outreach (Bearer Token)"| E["outreach_scheduler.py (DAILY_SEND_LIMIT Warm-Up)"]
    E -->|"Day 0 / Day 4 / Day 9 via Resend"| F["Prospect Inbox (Magic Link /claim?prospect_id=...)"]
    F -->|"1-Click Visit"| G["Digital Intake Portal (grants.zysgoldfields.pro)"]
    G -->|"Stage A: Unsigned"| H["Locked Client Preview Summary"]
    G -->|"Stage B: CA UETA E-Signature"| I["Unlocked RCD Packet & Signed Agreement"]
    I -->|"Suppresses Follow-Ups + Persists"| B
    I --> J["notifier.py -> Admin Email + Webhook"]
    K["Advisory Staff (/auth, /admin)"] -->|"Same-Origin Proxy /api/auth/*"| L["Neon Managed Better Auth + EdDSA JWKS"]
    L -->|"/api/me + Protected Routes"| B
```

## Environment Configuration (`.env.example`)

Copy `.env.example` to `.env` or `.env.local` for local testing, or configure these variables in Render Dashboard -> **Environment** (and `ADMIN_API_TOKEN` + `PORTAL_BASE_URL` in GitHub Repo Settings -> **Secrets and variables -> Actions**):

| Variable | Description | Example / Default |
| :--- | :--- | :--- |
| `PUBLIC_MODE` | Enables production security on admin API routes | `1` |
| `BASE_URL` | Primary custom domain URL for magic links and canonical metadata | `https://grants.zysgoldfields.pro` |
| `DATABASE_URL` | Direct unpooled Neon PostgreSQL URI used for startup DDL & migrations | `postgresql://.../neondb?sslmode=require` |
| `DATABASE_URL_POOLED` | Pooled (`-pooler`) Neon PostgreSQL URI used for application queries | `postgresql://...-pooler.../neondb?sslmode=require` |
| `NEON_AUTH_BASE_URL` | Neon Managed Better Auth URL (`/api/auth/*` proxy & `/api/me` JWKS issuer) | `https://...neonauth.../neondb/auth` |
| `ADMIN_API_TOKEN` | Bearer token protecting `/api/cron/run-outreach`, `/api/outreach-status`, `/api/intakes`, `/api/audits` | `zys_admin_...` |
| `DAILY_SEND_LIMIT` | Safe daily domain warm-up cap across Day 0/4/9 batches | `5` |
| `NOTIFICATION_PROVIDER` | Email provider (`resend`, `smtp`, or `auto`) | `resend` |
| `RESEND_API_KEY` | Resend HTTPS API key | `re_...` |
| `ADMIN_NOTIFICATION_EMAIL` | Destination admin inbox | `Ksandzhar@gmail.com` |
| `NOTIFICATION_FROM_EMAIL` | Verified sender email address | `grants@zysgoldfields.pro` |
| `BACKUP_WEBHOOK_URL` | Optional persistent JSON webhook | `https://hook.us1.make.com/...` |
| `BACKUP_WEBHOOK_SECRET` | Optional HMAC-SHA256 secret for webhook requests | `whsec_...` |

## Build, Verification & Running

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
npm install
npm run build
./venv/bin/python run_pipeline.py
```

To run the portal server locally (loads `.env.local` if present):
```bash
npm start
# or directly:
./venv/bin/python intake_app.py --serve --host 0.0.0.0 --port 8080
```

