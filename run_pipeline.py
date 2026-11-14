"""End-to-end execution and verification runner for ZYS GoldFields PRO LLC Grant Hunter."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import requests

from admin_packager import DEFAULT_PACKET_PATH, DEFAULT_PREVIEW_PATH
from db import (
    DEFAULT_DB_PATH,
    _adapt_sql,
    database_backend_info,
    fetch_all_prospects,
    fetch_outreach_statuses,
    get_database_url,
    init_db,
    insert_client_intake,
    mark_prospect_outreach_state,
    sync_outreach_status_from_prospects,
    upsert_prospects,
)
from intake_app import PACKETS_ARCHIVE_DIR, IntakePortalHandler
from legal_agreement import DEFAULT_SIGNED_AGREEMENT_PATH
from matcher import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_FOLLOWUP_PATH,
    DEFAULT_OUTPUT_PATH,
    NO_CALL_NEXT_STEP,
    load_matcher_config,
    run_matcher,
)
from models import ClientIntake, build_claim_url, resolve_base_url, rewrite_portal_urls
from outreach_scheduler import get_outreach_dashboard_summary, run_outreach_batch
from prospect_hunter import (
    CA_COUNTIES,
    DEFAULT_CSV_PATH,
    build_california_prospects_dataset,
)
from scraper import ingest_all_grants
from seed_profiles import run_seed


def verify_persistence_domain_and_outreach_scheduler() -> None:
    """Verify PostgreSQL SQL adapter/backend resolution, dynamic BASE_URL magic links, and outreach cadence."""
    # 1. PostgreSQL SQL placeholder & DSN adapter tests
    sample_sql = "SELECT '?' AS literal_q, status FROM grants WHERE id = ? AND note = 'it''s ?'"
    pg_sql = _adapt_sql(sample_sql, is_postgres=True)
    assert pg_sql == "SELECT '?' AS literal_q, status FROM grants WHERE id = %s AND note = 'it''s ?'", pg_sql

    old_db_url = os.environ.get("DATABASE_URL")
    old_base_url = os.environ.get("BASE_URL")
    try:
        os.environ.pop("DATABASE_URL", None)
        assert get_database_url(DEFAULT_DB_PATH) is None
        assert database_backend_info(DEFAULT_DB_PATH)["backend"] == "sqlite"

        os.environ["DATABASE_URL"] = "postgres://zys_user:secret@ep-cool-oak.us-west-2.aws.neon.tech/neondb?sslmode=require"
        norm_dsn = get_database_url(DEFAULT_DB_PATH)
        assert norm_dsn is not None and norm_dsn.startswith("postgresql://"), norm_dsn
        pg_info = database_backend_info(DEFAULT_DB_PATH)
        assert pg_info["backend"] == "postgresql"
        assert pg_info["persistent"] is True
        assert pg_info["host"] == "ep-cool-oak.us-west-2.aws.neon.tech"
        os.environ.pop("DATABASE_URL", None)

        # 2. Custom domain BASE_URL resolution & URL rewriting
        os.environ.pop("BASE_URL", None)
        cfg_default = load_matcher_config(DEFAULT_CONFIG_PATH)
        assert resolve_base_url(cfg_default) == "https://zys-goldfields-intake.onrender.com"
        assert build_claim_url("CA-T1-001", cfg_default) == "https://zys-goldfields-intake.onrender.com/claim?prospect_id=CA-T1-001"

        os.environ["BASE_URL"] = "https://portal.zysgoldfields.pro/"
        cfg_custom = load_matcher_config(DEFAULT_CONFIG_PATH)
        assert cfg_custom.public_intake_url == "https://portal.zysgoldfields.pro"
        assert resolve_base_url(cfg_custom) == "https://portal.zysgoldfields.pro"
        assert build_claim_url("CA-T1-001", cfg_custom) == "https://portal.zysgoldfields.pro/claim?prospect_id=CA-T1-001"
        rewritten = rewrite_portal_urls(
            "Verify parcel: https://zys-goldfields-intake.onrender.com/claim?prospect_id=CA-T1-001",
            "https://portal.zysgoldfields.pro",
            cfg=cfg_custom,
        )
        assert rewritten == "Verify parcel: https://portal.zysgoldfields.pro/claim?prospect_id=CA-T1-001"

        # 3. Isolated outreach scheduler cadence, warm-up rate limit, idempotency & claim suppression
        sample_prospects = fetch_all_prospects(DEFAULT_DB_PATH)[:10]
        assert len(sample_prospects) == 10

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_db = Path(tmp_dir) / "test_outreach.db"
            # Verify isolated temp file ignores global DATABASE_URL so tests never mutate remote PG
            os.environ["DATABASE_URL"] = "postgres://should:ignore@example.com/db"
            assert get_database_url(tmp_db) is None
            os.environ.pop("DATABASE_URL", None)
            init_db(tmp_db, force=True)
            upsert_prospects(sample_prospects, db_path=tmp_db, reset_existing=True)
            sync_outreach_status_from_prospects(sample_prospects, db_path=tmp_db)

            sent_log: list[dict[str, str]] = []

            def fake_sender(
                *,
                to_email: str,
                subject: str,
                text_body: str,
                html_body: str = "",
                reply_to: str = "",
                headers: dict[str, str] | None = None,
            ) -> tuple[str, str]:
                assert r"\$" not in text_body, f"Found unescaped '\\$' in plain-text outreach email to {to_email}"
                assert r"\$" not in html_body, f"Found unescaped '\\$' in HTML outreach email to {to_email}"
                assert "https://portal.zysgoldfields.pro/claim?prospect_id=" in text_body
                assert headers and "List-Unsubscribe" in headers
                msg_id = f"mock_msg_{len(sent_log) + 1:03d}"
                sent_log.append({"to": to_email, "subject": subject, "id": msg_id})
                return ("mock-resend", msg_id)

            t0 = datetime(2026, 11, 1, 16, 0, 0, tzinfo=timezone.utc)
            # Day 0 Batch 1: warm-up cap 3 emails/day -> sends Day 0 to CA-T1-001, 002, 003
            r1 = run_outreach_batch(
                db_path=tmp_db,
                config_path=DEFAULT_CONFIG_PATH,
                pitches_path=DEFAULT_OUTPUT_PATH,
                followup_path=DEFAULT_FOLLOWUP_PATH,
                daily_limit=3,
                now_utc=t0,
                base_url_override="https://portal.zysgoldfields.pro",
                email_sender=fake_sender,
            )
            assert r1["status"] == "ok"
            assert r1["sent_this_run"] == 3
            assert r1["sent_today_after"] == 3
            assert [x["prospect_id"] for x in r1["sent"]] == [
                sample_prospects[0].prospect_id,
                sample_prospects[1].prospect_id,
                sample_prospects[2].prospect_id,
            ]
            assert all(x["sequence_step"] == "day0_initial" for x in r1["sent"])

            # Same-day re-run must hit daily warm-up quota and send 0
            r1_dup = run_outreach_batch(
                db_path=tmp_db,
                config_path=DEFAULT_CONFIG_PATH,
                pitches_path=DEFAULT_OUTPUT_PATH,
                followup_path=DEFAULT_FOLLOWUP_PATH,
                daily_limit=3,
                now_utc=t0 + timedelta(hours=2),
                base_url_override="https://portal.zysgoldfields.pro",
                email_sender=fake_sender,
            )
            assert r1_dup["status"] == "quota_exhausted"
            assert r1_dup["sent_this_run"] == 0

            # Suppress prospect #1 via portal intake signature & prospect #2 via unsubscribe
            p0 = sample_prospects[0]
            p1 = sample_prospects[1]
            insert_client_intake(
                ClientIntake(
                    name=p0.organization_name,
                    email=p0.contact_email,
                    county=p0.county,
                    state="CA",
                    apn="019-140-001-000",
                    acreage=p0.estimated_acreage,
                    land_condition="Restoring riparian corridor and working rangeland.",
                    project_goals="NRCS & CCI carbon farming and rangeland soil health.",
                    agreed_to_terms=True,
                    agreement_signed=True,
                    signer_full_name="Test Signer",
                    signer_title="Managing Director",
                    prospect_id=p0.prospect_id,
                    ein=p0.ein,
                ),
                db_path=tmp_db,
            )
            assert mark_prospect_outreach_state(p1.prospect_id, "unsubscribed", db_path=tmp_db) is True

            # Before 92 hours (T0 + 80h): Day 4 must NOT be queued yet -> only Day 0 fires for #4
            r_early = run_outreach_batch(
                db_path=tmp_db,
                config_path=DEFAULT_CONFIG_PATH,
                pitches_path=DEFAULT_OUTPUT_PATH,
                followup_path=DEFAULT_FOLLOWUP_PATH,
                daily_limit=3,
                max_sends=1,
                now_utc=t0 + timedelta(hours=80),
                base_url_override="https://portal.zysgoldfields.pro",
                email_sender=fake_sender,
            )
            assert r_early["sent_this_run"] == 1
            assert r_early["sent"][0]["prospect_id"] == sample_prospects[3].prospect_id
            assert r_early["sent"][0]["sequence_step"] == "day0_initial"

            # At Day 4 (T0 + 96h >= 92h): prospect #3 MUST get Day 4 follow-up first,
            # while #1 (signed) and #2 (unsubscribed) remain suppressed!
            r_day4 = run_outreach_batch(
                db_path=tmp_db,
                config_path=DEFAULT_CONFIG_PATH,
                pitches_path=DEFAULT_OUTPUT_PATH,
                followup_path=DEFAULT_FOLLOWUP_PATH,
                daily_limit=3,
                max_sends=2,
                now_utc=t0 + timedelta(hours=96),
                base_url_override="https://portal.zysgoldfields.pro",
                email_sender=fake_sender,
            )
            assert r_day4["sent_this_run"] == 2
            assert r_day4["sent"][0]["prospect_id"] == sample_prospects[2].prospect_id
            assert r_day4["sent"][0]["sequence_step"] == "day4_followup"
            assert r_day4["sent"][1]["prospect_id"] == sample_prospects[4].prospect_id
            assert r_day4["sent"][1]["sequence_step"] == "day0_initial"

            # At Day 9 (T0 + 216h >= 212h and >= 72h post Day 4): prospect #3 MUST get Day 9 first!
            r_day9 = run_outreach_batch(
                db_path=tmp_db,
                config_path=DEFAULT_CONFIG_PATH,
                pitches_path=DEFAULT_OUTPUT_PATH,
                followup_path=DEFAULT_FOLLOWUP_PATH,
                daily_limit=2,
                now_utc=t0 + timedelta(hours=216),
                base_url_override="https://portal.zysgoldfields.pro",
                email_sender=fake_sender,
            )
            assert r_day9["sent_this_run"] == 2
            assert r_day9["sent"][0]["prospect_id"] == sample_prospects[2].prospect_id
            assert r_day9["sent"][0]["sequence_step"] == "day9_followup"

            # Ensure neither signed p0 nor unsubscribed p1 ever received a follow-up after Day 0
            p0_emails = [m for m in sent_log if m["to"] == p0.contact_email.lower()]
            p1_emails = [m for m in sent_log if m["to"] == p1.contact_email.lower()]
            assert len(p0_emails) == 1 and len(p1_emails) == 1

            summary = get_outreach_dashboard_summary(tmp_db)
            assert summary["status_counts"].get("signed", 0) >= 1
            assert summary["status_counts"].get("unsubscribed", 0) >= 1
            assert summary["status_counts"].get("completed", 0) >= 1
    finally:
        if old_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_db_url
        if old_base_url is None:
            os.environ.pop("BASE_URL", None)
        else:
            os.environ["BASE_URL"] = old_base_url


def verify_intake_portal_and_packager() -> None:
    """Spin up intake_app HTTP server on an ephemeral port and verify Stage A Gate + Stage B E-Signature flow."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), IntakePortalHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    old_admin_token = os.environ.get("ADMIN_API_TOKEN")

    try:
        # 1. Verify GET / renders the single-page intake form with scrollable UETA/ESIGN agreement & 2 CFR 200.459 clause
        home_resp = requests.get(
            f"{base_url}/?name=PAWS+Sanctuary&email=info%40pawsweb.org&county=Calaveras&acres=2300",
            timeout=5,
        )
        assert home_resp.status_code == 200
        assert "no-cache" in home_resp.headers.get("Cache-Control", "")
        assert "no-store" in home_resp.headers.get("Cache-Control", "")
        assert home_resp.headers.get("Pragma") == "no-cache"
        assert home_resp.headers.get("Expires") == "0"
        for expected_token in (
            "input-name",
            "input-email",
            "input-county",
            "input-apn",
            "input-acreage",
            "input-condition",
            "input-goals",
            "input-signer-name",
            "input-signer-title",
            "checkbox-terms",
            "scrollable-agreement-box",
            "Conservation Advisory &amp; Origination Agreement",
            "California Uniform Electronic Transactions Act",
            "2 CFR § 200.459",
            "5 business days",
            "1.5%",
            "Sacramento County, California",
            "I agree to the ZYS GoldFields PRO LLC Advisory Agreement.",
            "URLSearchParams",
            "PORTAL_BASE_URL",
        ):
            assert expected_token in home_resp.text, f"Missing expected token '{expected_token}' in portal HTML"

        # 1a. Verify /healthz (GET and HEAD) for external cron keep-alive
        healthz_head = requests.head(f"{base_url}/healthz", timeout=5)
        assert healthz_head.status_code == 200, f"Expected 200 on HEAD /healthz, got {healthz_head.status_code}"
        healthz_get = requests.get(f"{base_url}/healthz", timeout=5)
        assert healthz_get.status_code == 200, f"Expected 200 on GET /healthz, got {healthz_get.status_code}"
        assert healthz_get.text == "ok\n", f"Unexpected /healthz body: {healthz_get.text}"

        # 1b. Verify 1-click pre-filled magic link /claim?prospect_id=CA-T1-001 (and /claim/ + /?prospect_id=)
        for claim_path in ("/claim?prospect_id=CA-T1-001", "/claim/?prospect_id=CA-T1-001", "/?prospect_id=CA-T1-001"):
            claim_resp = requests.get(f"{base_url}{claim_path}", timeout=5)
            assert claim_resp.status_code == 200, f"Expected 200 on {claim_path}, got {claim_resp.status_code}"
            assert "no-cache" in claim_resp.headers.get("Cache-Control", "")
            assert "no-store" in claim_resp.headers.get("Cache-Control", "")
            for token in ("Paicines Ranch", "Pre-Screened Grant Allocation", "CA-T1-001", "83-3255248", "San Benito"):
                assert token in claim_resp.text, f"Missing token '{token}' in {claim_path} HTML"

        # 1c. Verify /intake_portal.html direct routing and no-cache headers
        portal_resp = requests.get(f"{base_url}/intake_portal.html", timeout=5)
        assert portal_resp.status_code == 200, f"Expected 200 on /intake_portal.html, got {portal_resp.status_code}"
        assert "no-cache" in portal_resp.headers.get("Cache-Control", "")
        assert "input-signer-name" in portal_resp.text
        assert "input-signer-title" in portal_resp.text

        # 1d. Verify HEAD requests on /, /claim, /claim/, and /intake_portal.html emit no-cache headers
        for path in ("/", "/claim", "/claim/", "/intake_portal.html"):
            h_resp = requests.head(f"{base_url}{path}", timeout=5)
            assert h_resp.status_code == 200, f"Expected 200 on HEAD {path}, got {h_resp.status_code}"
            assert "no-cache" in h_resp.headers.get("Cache-Control", ""), f"Missing no-cache header on HEAD {path}"

        # 1e. Verify root intake_portal.html file exists on disk and contains aligned fields
        static_html_file = Path(__file__).resolve().parent / "intake_portal.html"
        assert static_html_file.exists(), f"Static file {static_html_file} does not exist"
        static_html_text = static_html_file.read_text(encoding="utf-8")
        assert "input-signer-name" in static_html_text
        assert "input-signer-title" in static_html_text

        # 1c. Verify GET /api/health and GET /api/prospects
        os.environ.pop("ADMIN_API_TOKEN", None)
        health_resp = requests.get(f"{base_url}/api/health", timeout=5)
        assert health_resp.status_code == 200
        health_json = health_resp.json()
        assert health_json["status"] == "healthy"
        assert "notifications" in health_json
        assert health_json["database"]["backend"] in ("sqlite", "postgresql")
        assert health_json["base_url"].startswith("https://")
        assert health_json["daily_send_limit"] >= 1

        prospects_resp = requests.get(f"{base_url}/api/prospects", timeout=5)
        assert prospects_resp.status_code == 200
        prospects_json = prospects_resp.json()
        assert 25 <= prospects_json["count"] < 70, (
            f"Expected strict filtering to retain Tier-1 subset (<70, >=25), got {prospects_json['count']}"
        )
        api_scores = [p["probability_score"] for p in prospects_json["prospects"]]
        assert all(s >= 75 for s in api_scores), f"Found sub-75 prospect in /api/prospects: {min(api_scores)}"
        assert api_scores == sorted(api_scores, reverse=True), "/api/prospects is not ranked highest-to-lowest"

        # 1d. Verify /api/test-email diagnostic endpoint (GET, HEAD, POST)
        email_head = requests.head(f"{base_url}/api/test-email", timeout=5)
        assert email_head.status_code == 200, f"Expected 200 on HEAD /api/test-email, got {email_head.status_code}"
        email_test_get = requests.get(f"{base_url}/api/test-email", timeout=5)
        assert email_test_get.status_code == 200, f"Expected 200 on GET /api/test-email, got {email_test_get.status_code}"
        diag_data = email_test_get.json()
        assert diag_data["admin_email"] == "Ksandzhar@gmail.com"
        assert "status" in diag_data
        if diag_data["status"] == "missing_credentials":
            assert "RESEND_API_KEY" in diag_data["error"]
            assert len(diag_data["diagnostics"]["resolution_options"]) >= 2

        # 2. Verify unchecked Advisory Agreement checkbox is rejected with HTTP 400 when attempting Stage B unlock
        bad_payload = {
            "name": "Elena & Marcus Vance",
            "email": "vance.family@sierraridgeoak.com",
            "county": "El Dorado",
            "state": "CA",
            "apn": "089-140-027-000",
            "acreage": 45.0,
            "land_condition": "Degraded oak woodland with post-drought mortality and brush fuels.",
            "project_goals": "RPF management plan and native oak reforestation.",
            "agreed_to_terms": False,
            "preview_only": False,
        }
        bad_resp = requests.post(f"{base_url}/api/intake", json=bad_payload, timeout=5)
        assert bad_resp.status_code == 400, f"Expected 400 when terms unchecked, got {bad_resp.status_code}"

        # 2a. Verify rejection of random/nonsense entries (Issue 2)
        nonsense_payloads = [
            # Junk name
            {
                "name": "test",
                "email": "test@example.com",
                "county": "El Dorado",
                "apn": "089-140-027-000",
                "acreage": 45.0,
                "land_condition": "This is a random test text with no genuine meaning at all.",
                "project_goals": "Random test project goals for testing random input.",
                "agreed_to_terms": True,
            },
            # Acreage < 5.0 acres without prospect_id
            {
                "name": "Small Lot Owner",
                "email": "owner@lot.com",
                "county": "El Dorado",
                "apn": "089-140-027-000",
                "acreage": 2.5,
                "land_condition": "Oak woodland with forest fuels and tree mortality.",
                "project_goals": "Fuels thinning and pine tree reforestation.",
                "agreed_to_terms": True,
            },
            # Non-conservation description
            {
                "name": "Commercial Developer",
                "email": "dev@urbanllc.com",
                "county": "Sacramento",
                "apn": "089-140-027-000",
                "acreage": 50.0,
                "land_condition": "Empty parking lot with cracked asphalt and concrete buildings.",
                "project_goals": "Build a shopping strip mall and multi-family residential apartments.",
                "agreed_to_terms": True,
            },
        ]
        for idx, nonsense in enumerate(nonsense_payloads):
            resp = requests.post(f"{base_url}/api/intake", json=nonsense, timeout=5)
            assert resp.status_code == 400, f"Expected 400 for nonsense payload #{idx}, got {resp.status_code}: {resp.text}"
            assert "error" in resp.json()

        # 3. Verify Stage A ("Sign-to-Unlock" Gate when agreement_signed == False)
        stage_a_payload = {
            "name": "Elena & Marcus Vance",
            "email": "vance.family@sierraridgeoak.com",
            "county": "El Dorado",
            "state": "CA",
            "apn": "089-140-027-000",
            "acreage": 45.0,
            "land_condition": (
                "45-acre degraded blue oak, black oak, and ponderosa pine woodland in El Dorado County "
                "experiencing post-drought canopy mortality, heavy manzanita/buckbrush fuel buildup, "
                "and seasonal gully soil erosion."
            ),
            "project_goals": (
                "Complete a Registered Professional Forester (RPF) Forest Management Plan, mechanical "
                "understory fuels thinning, native oak and ponderosa pine reforestation, gully erosion "
                "stabilization, and deer/songbird wildlife habitat restoration."
            ),
            "agreed_to_terms": False,
            "preview_only": True,
        }
        preview_resp = requests.post(f"{base_url}/api/intake", json=stage_a_payload, timeout=10)
        assert preview_resp.status_code == 200, f"Expected 200 on Stage A preview, got {preview_resp.status_code}"
        preview_body = preview_resp.json()
        assert preview_body["stage"] == "preview_locked"
        assert preview_body["agreement_signed"] is False
        assert "California Forest Improvement Program" in preview_body["matched_grant_title"]
        assert "$" in preview_body["engineered_grant_request"] and "total project budget" in preview_body["engineered_grant_request"]
        assert int(preview_body["probability_score"].split("/")[0]) >= 75
        assert preview_body["matched_grant_id"] == "[LOCKED — E-SIGNATURE REQUIRED]"
        assert "grant_allocations" in preview_body
        assert len(preview_body["grant_allocations"]) >= 1
        alloc_sum = sum(a["award_amount"] for a in preview_body["grant_allocations"])
        assert f"${alloc_sum:,}" == preview_body["total_estimated_funding"]
        for alloc in preview_body["grant_allocations"]:
            assert "grant_name" in alloc and len(alloc["grant_name"]) > 5
            assert "agency" in alloc and len(alloc["agency"]) > 2
            assert alloc["award_amount"] > 0

        preview_md = preview_body["packet_markdown"]
        assert "Stage A: Client Grant Eligibility & Budget Preview Summary (LOCKED DOSSIER)" in preview_md
        assert "CCI-california-forest-improvement-program" not in preview_md
        assert "Part B: Ready-to-Send County RCD & USDA NRCS Handoff Package" not in preview_md
        assert "MASKED — COMPLETE E-SIGNATURE TO UNLOCK" in preview_md

        preview_intake_id = preview_body["intake_id"]
        forbidden_pkt = requests.get(f"{base_url}/api/packet/{preview_intake_id}", timeout=5)
        assert forbidden_pkt.status_code == 403, f"Expected 403 on unsigned intake packet, got {forbidden_pkt.status_code}"

        # 4. Verify Stage B: Submit Elena & Marcus Vance E-Signature to unlock full RCD Filing Dossier
        custom_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 ZYS-Vance-Verification/2.0",
            "X-Forwarded-For": "198.51.100.45",
        }
        vance_signed_payload = {
            "name": "Elena & Marcus Vance",
            "email": "vance.family@sierraridgeoak.com",
            "county": "El Dorado",
            "state": "CA",
            "apn": "089-140-027-000",
            "acreage": 45.0,
            "prospect_id": "CA-T1-002",
            "ein": "94-8271039",
            "land_condition": (
                "45-acre degraded blue oak, black oak, and ponderosa pine woodland in El Dorado County "
                "experiencing post-drought canopy mortality, heavy manzanita/buckbrush fuel buildup, "
                "and seasonal gully soil erosion."
            ),
            "project_goals": (
                "Complete a Registered Professional Forester (RPF) Forest Management Plan, mechanical "
                "understory fuels thinning, native oak and ponderosa pine reforestation, gully erosion "
                "stabilization, and deer/songbird wildlife habitat restoration."
            ),
            "signer_full_name": "Elena Vance",
            "signer_title": "Managing Co-Owner & Steward, Sierra Ridge Oak Woodland",
            "agreed_to_terms": True,
            "preview_only": False,
        }
        ok_resp = requests.post(
            f"{base_url}/api/intake",
            json=vance_signed_payload,
            headers=custom_headers,
            timeout=10,
        )
        assert ok_resp.status_code == 200, f"Expected 200 on signed intake, got {ok_resp.status_code}: {ok_resp.text}"
        body = ok_resp.json()
        assert body["status"] == "ok"
        assert body["stage"] == "unlocked_filing_dossier"
        assert body["agreement_signed"] is True
        assert body["matched_grant_id"] == "CCI-california-forest-improvement-program"
        assert "California Forest Improvement Program" in body["matched_grant_title"]
        assert int(body["probability_score"].split("/")[0]) >= 75
        assert body["eligibility_tier"] == "Tier-1 Statutory Eligibility Match (4/4 Core Criteria Met)"
        assert body["prospect_id"] == "CA-T1-002"
        assert body["ein"] == "94-8271039"
        assert body["signer_full_name"] == "Elena Vance"
        assert body["signer_title"] == "Managing Co-Owner & Steward, Sierra Ridge Oak Woodland"
        assert body["signer_ip_address"] == "198.51.100.45"
        assert "ZYS-Vance-Verification/2.0" in body["signer_user_agent"]
        assert len(body["contract_sha256_hash"]) == 64
        assert "grant_allocations" in body
        assert len(body["grant_allocations"]) >= 1
        b_sum = sum(a["award_amount"] for a in body["grant_allocations"])
        assert f"${b_sum:,}" == body["total_estimated_funding"]
        intake_id = body["intake_id"]

        # Wait briefly for background thread to write backup journal
        time.sleep(0.4)
        backup_journal = DEFAULT_DB_PATH.parent / "backups" / "submissions.jsonl"
        assert backup_journal.exists(), f"Expected backup journal at {backup_journal}"
        journal_text = backup_journal.read_text(encoding="utf-8")
        assert "Elena Vance" in journal_text
        assert "CA-T1-002" in journal_text

        # 5. Verify GET /api/packet/latest, GET /api/packet/<id>, GET /api/agreement/latest, GET /api/agreement/<id>, GET /api/audits
        dl_resp = requests.get(f"{base_url}/api/packet/latest", timeout=5)
        assert dl_resp.status_code == 200
        assert "089-140-027-000" in dl_resp.text
        assert "Notice of Authorized Advisory Representation" in dl_resp.text
        assert body["contract_sha256_hash"] in dl_resp.text

        arch_resp = requests.get(f"{base_url}/api/packet/{intake_id}", timeout=5)
        assert arch_resp.status_code == 200
        assert "089-140-027-000" in arch_resp.text

        # Verify ephemeral archive reconstruction (if container disk wiped while DB row persists)
        cached_pkt = PACKETS_ARCHIVE_DIR / f"client_submission_packet_{intake_id}.md"
        if cached_pkt.exists():
            cached_pkt.unlink()
        rebuilt_pkt = requests.get(f"{base_url}/api/packet/{intake_id}", timeout=5)
        assert rebuilt_pkt.status_code == 200, f"Expected on-the-fly reconstruction HTTP 200, got {rebuilt_pkt.status_code}"
        assert "089-140-027-000" in rebuilt_pkt.text

        agr_latest = requests.get(f"{base_url}/api/agreement/latest", timeout=5)
        assert agr_latest.status_code == 200
        assert "2 CFR § 200.459" in agr_latest.text
        assert "Sacramento County, California" in agr_latest.text
        assert body["contract_sha256_hash"] in agr_latest.text

        agr_id = requests.get(f"{base_url}/api/agreement/{intake_id}", timeout=5)
        assert agr_id.status_code == 200
        assert "Elena Vance" in agr_id.text

        audits_resp = requests.get(f"{base_url}/api/audits", timeout=5)
        assert audits_resp.status_code == 200
        audits_data = audits_resp.json()
        assert audits_data["count"] >= 1
        latest_audit = audits_data["audits"][0]
        assert latest_audit["signer_full_name"] == "Elena Vance"
        assert latest_audit["contract_sha256_hash"] == body["contract_sha256_hash"]

        intakes_resp = requests.get(f"{base_url}/api/intakes", timeout=5)
        assert intakes_resp.status_code == 200
        assert intakes_resp.json()["count"] >= 2

        # 6. Verify strict ADMIN_API_TOKEN protection on /api/cron/run-outreach & /api/outreach-status
        # Even without PUBLIC_MODE=1, cron endpoints MUST fail closed when ADMIN_API_TOKEN is unset!
        unauth_cron = requests.post(f"{base_url}/api/cron/run-outreach", json={"dry_run": True}, timeout=5)
        assert unauth_cron.status_code == 403, f"Expected 403 when ADMIN_API_TOKEN unset, got {unauth_cron.status_code}"

        os.environ["ADMIN_API_TOKEN"] = "zys_test_cron_token_987"
        bad_cron = requests.post(
            f"{base_url}/api/cron/run-outreach",
            headers={"Authorization": "Bearer wrong-token"},
            json={"dry_run": True},
            timeout=5,
        )
        assert bad_cron.status_code == 403, f"Expected 403 for wrong token, got {bad_cron.status_code}"

        dry_cron = requests.post(
            f"{base_url}/api/cron/run-outreach",
            headers={"Authorization": "Bearer zys_test_cron_token_987"},
            json={"dry_run": True, "daily_limit": 5},
            timeout=10,
        )
        assert dry_cron.status_code == 200, f"Expected 200 for authorized dry_run cron, got {dry_cron.status_code}: {dry_cron.text}"
        dry_json = dry_cron.json()
        assert dry_json["dry_run"] is True
        assert dry_json["status"] == "dry_run"
        assert dry_json["daily_send_limit"] == 5
        assert len(dry_json["planned"]) == 5
        # Verify claimed CA-T1-002 (signed above) is suppressed in the dry-run queue!
        planned_ids = {item["prospect_id"] for item in dry_json["planned"]}
        assert "CA-T1-002" not in planned_ids, "Expected signed prospect CA-T1-002 to be suppressed from outreach queue"

        status_resp = requests.get(
            f"{base_url}/api/outreach-status",
            headers={"Authorization": "Bearer zys_test_cron_token_987"},
            timeout=5,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["total_prospects"] >= 50
    finally:
        if old_admin_token is None:
            os.environ.pop("ADMIN_API_TOKEN", None)
        else:
            os.environ["ADMIN_API_TOKEN"] = old_admin_token
        server.shutdown()
        server.server_close()


def verify_outputs(
    db_path: Path = DEFAULT_DB_PATH,
    pitches_path: Path = DEFAULT_OUTPUT_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    packet_path: Path = DEFAULT_PACKET_PATH,
    preview_path: Path = DEFAULT_PREVIEW_PATH,
    signed_agreement_path: Path = DEFAULT_SIGNED_AGREEMENT_PATH,
    prospects_csv_path: Path = DEFAULT_CSV_PATH,
) -> None:
    """Run automated integrity assertions across SQLite, config.json, pitches, prospects CSV, signed agreement, and packet."""
    assert config_path.exists(), f"Missing config.json: {config_path}"
    cfg = load_matcher_config(config_path)
    assert cfg.min_probability_score == 75, f"Expected min_probability_score=75, got {cfg.min_probability_score}"

    assert db_path.exists(), f"Missing SQLite database: {db_path}"
    assert pitches_path.exists(), f"Missing outreach pitches file: {pitches_path}"
    assert packet_path.exists(), f"Missing client_submission_packet.md: {packet_path}"
    assert preview_path.exists(), f"Missing client_preview_summary.md: {preview_path}"
    assert signed_agreement_path.exists(), f"Missing signed_advisory_agreement.md: {signed_agreement_path}"
    assert prospects_csv_path.exists(), f"Missing california_prospects.csv: {prospects_csv_path}"

    root_dir = config_path.parent
    assert (root_dir / "render.yaml").exists(), "Missing Render deployment blueprint render.yaml"
    assert (root_dir / "start_public_tunnel.sh").exists(), "Missing Cloudflare tunnel script start_public_tunnel.sh"
    assert (root_dir / "legal_agreement.py").exists(), "Missing legal_agreement.py"
    assert (root_dir / "notifier.py").exists(), "Missing notifier.py"
    assert (root_dir / "outreach_scheduler.py").exists(), "Missing outreach_scheduler.py"
    assert (root_dir / ".env.example").exists(), "Missing .env.example"
    assert (root_dir / ".github" / "workflows" / "outreach_cron.yml").exists(), "Missing outreach_cron.yml"
    assert (root_dir / "followup_templates.md").exists(), "Missing followup_templates.md"

    with sqlite3.connect(str(db_path)) as conn:
        grant_cols = [r[1] for r in conn.execute("PRAGMA table_info(grants)").fetchall()]
        profile_cols = [r[1] for r in conn.execute("PRAGMA table_info(landowner_profiles)").fetchall()]
        audit_cols = [r[1] for r in conn.execute("PRAGMA table_info(esignature_audit_log)").fetchall()]
        outreach_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('prospect_outreach_status', 'outreach_send_log')"
            ).fetchall()
        }
        assert outreach_tables == {"prospect_outreach_status", "outreach_send_log"}, outreach_tables

        assert grant_cols == ["id", "title", "agency", "funding_amount", "deadline", "eligibility_text", "url"]
        assert profile_cols == ["id", "name", "state", "county", "acreage", "land_type", "features"]
        for req_col in (
            "signature_timestamp_utc",
            "signer_ip_address",
            "signer_user_agent",
            "contract_sha256_hash",
            "signer_full_name",
            "signer_title",
        ):
            assert req_col in audit_cols, f"Missing column {req_col} in esignature_audit_log"

        grant_count = conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
        profile_count = conn.execute("SELECT COUNT(*) FROM landowner_profiles").fetchone()[0]
        intake_count = conn.execute("SELECT COUNT(*) FROM client_intakes").fetchone()[0]
        signed_intake_count = conn.execute(
            "SELECT COUNT(*) FROM client_intakes WHERE agreement_signed = 1"
        ).fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) FROM esignature_audit_log").fetchone()[0]
        prospect_db_count = conn.execute("SELECT COUNT(*) FROM california_prospects").fetchone()[0]
        min_db_prob = conn.execute("SELECT MIN(probability_score) FROM california_prospects").fetchone()[0]

        assert grant_count >= 10, f"Expected >= 10 active grants in grants.db, found {grant_count}"
        assert profile_count == 3, f"Expected 3 seeded landowner profiles, found {profile_count}"
        assert intake_count >= 1, f"Expected >= 1 persisted client intake, found {intake_count}"
        assert signed_intake_count >= 1, f"Expected >= 1 signed client intake, found {signed_intake_count}"
        assert audit_count >= 1, f"Expected >= 1 e-signature audit record in grants.db, found {audit_count}"
        assert 25 <= prospect_db_count < 70, f"Expected Tier-1 filtered prospect count in [25, 69], found {prospect_db_count}"
        assert min_db_prob >= 75, f"Expected all DB prospects >= 75/100, found min={min_db_prob}"

        typo_grants = conn.execute("SELECT id, title FROM grants WHERE id LIKE '%-progra'").fetchall()
        assert len(typo_grants) == 0, f"Found grants with truncated '-progra' slug: {typo_grants}"
        car_grant = conn.execute("SELECT id FROM grants WHERE title LIKE '%Climate Adaptation and Resiliency%'").fetchone()
        assert car_grant is not None, "Climate Adaptation and Resiliency Program grant not found in grants.db"
        assert car_grant[0] == "CCI-climate-adaptation-and-resiliency-program", f"Unexpected grant ID: {car_grant[0]}"

    with prospects_csv_path.open("r", encoding="utf-8", newline="") as fh:
        csv_rows = list(csv.DictReader(fh))
    assert len(csv_rows) == prospect_db_count, "CSV prospect count mismatch with SQLite california_prospects table"
    categories_seen = {r["category"] for r in csv_rows}
    for required_cat in ("Wildlife Sanctuary", "Conservation Ranch", "Land Trust"):
        assert required_cat in categories_seen, f"Missing prospect category {required_cat} in CSV"

    ca_county_set = set(CA_COUNTIES)
    csv_scores: list[int] = []
    for row in csv_rows:
        assert "@" in row["contact_email"] and "." in row["contact_email"].split("@")[-1], f"Invalid email in {row}"
        assert row["county"] in ca_county_set, f"Non-California county {row['county']} in {row}"
        assert row["state"] == "CA"
        assert row["personalized_intake_url"].startswith("https://")
        assert "/claim?prospect_id=" in row["personalized_intake_url"]
        assert row["outreach_segment"] in ("Direct_Client", "Channel_Partner")
        if row["category"] == "Land Trust":
            assert row["outreach_segment"] == "Channel_Partner", f"Land Trust {row['organization_name']} not Channel_Partner"
        else:
            assert row["outreach_segment"] == "Direct_Client", f"{row['organization_name']} not Direct_Client"
        sc = int(row["probability_score"])
        assert sc >= 75, f"Candidate {row['organization_name']} has score {sc} < 75!"
        assert len(row["score_rationale"]) > 25, f"Missing 1-sentence score_rationale on {row['organization_name']}"
        csv_scores.append(sc)

    assert csv_scores == sorted(csv_scores, reverse=True), "california_prospects.csv is not ranked highest-to-lowest!"

    md_text = pitches_path.read_text(encoding="utf-8")
    assert NO_CALL_NEXT_STEP in md_text, "Missing zero-call digital intake CTA in outreach_pitches.md"
    assert cfg.firm_name in md_text, "Missing ZYS GoldFields PRO LLC signature in outreach_pitches.md"
    assert cfg.firm_subtitle in md_text, "Missing Conservation & Agricultural Grant Advisory subtitle"
    assert "Tier-1 Statutory Eligibility Match (4/4 Core Criteria Met)" in md_text
    assert "/claim?prospect_id=" in md_text
    assert "Deferred Administrative Origination & Packaging Fee (100% Waived if Grant is Not Awarded)" in md_text
    assert "This is a commercial message (advertisement)" in md_text
    assert "8698 Elk Grove Blvd Ste 1-279 Elk Grove CA 95624" in md_text

    followup_md = (root_dir / "followup_templates.md").read_text(encoding="utf-8")
    assert "Day 4 Follow-Up Email" in followup_md
    assert "Day 9 Follow-Up Email" in followup_md
    assert "/claim?prospect_id=" in followup_md
    assert "Deferred Administrative Origination & Packaging Fee (100% Waived if Grant is Not Awarded)" in followup_md
    assert "This is a commercial message (advertisement)" in followup_md
    assert "8698 Elk Grove Blvd Ste 1-279 Elk Grove CA 95624" in followup_md

    signed_md = signed_agreement_path.read_text(encoding="utf-8")
    assert "California Uniform Electronic Transactions Act" in signed_md
    assert "2 CFR § 200.459" in signed_md
    assert "Deferred Administrative Origination & Packaging Fee" in signed_md
    assert "100% Waived if Grant is Not Awarded" in signed_md
    assert "Registered Professional Forester" in signed_md
    assert "5 business days" in signed_md
    assert "1.5%" in signed_md
    assert "Sacramento County, California" in signed_md
    assert "Elena Vance" in signed_md

    packet_md = packet_path.read_text(encoding="utf-8")
    assert "Elena & Marcus Vance" in packet_md
    assert "089-140-027-000" in packet_md
    assert "CCI-california-forest-improvement-program" in packet_md
    assert "Part A: Pre-Filled Project Narrative & Budget Engineering" in packet_md
    assert "Part B: Ready-to-Send County RCD & USDA NRCS Handoff Package" in packet_md
    assert "Part C: Notice of Authorized Advisory Representation & E-Signature Certificate" in packet_md
    assert "El Dorado County & Georgetown Divide Resource Conservation Districts" in packet_md
    assert "USDA NRCS Placerville Service Center" in packet_md
    assert "2 CFR § 200.459" in packet_md
    assert "ZYS GoldFields PRO LLC" in packet_md

    print("\n=== California UETA / ESIGN, PostgreSQL/Domain/Scheduler & Sign-to-Unlock Verification Passed ===")
    print(f"  Config File:         {config_path} ({json.dumps(cfg.model_dump())})")
    print(
        f"  Database Layer:      {db_path} "
        f"({grant_count} grants, {profile_count} profiles, {prospect_db_count} Tier-1 CA prospects, "
        f"{intake_count} intakes, {audit_count} cryptographic e-signature audit records)"
    )
    print(
        f"  Tier-1 CA CSV:       {prospects_csv_path} "
        f"({len(csv_rows)} Tier-1 orgs, ranked {csv_scores[0]}/100 down to {csv_scores[-1]}/100)"
    )
    print(f"  Signed Agreement:    {signed_agreement_path} ({len(signed_md):,} bytes — Elena Vance SHA-256 verified)")
    print(f"  Stage A Preview:     {preview_path} ({len(preview_path.read_text(encoding='utf-8')):,} bytes — Masked gate summary)")
    print(f"  Unlocked Dossier:    {packet_path} ({len(packet_md):,} bytes — Stage B unlocked with Notice of Representation)")
    print(f"  Follow-up Templates: {root_dir / 'followup_templates.md'} ({len(followup_md):,} bytes — Day 4 & Day 9 sequences)")


def main() -> None:
    ingest_all_grants()
    run_seed()
    build_california_prospects_dataset()
    run_matcher()
    # Reset client_intakes, esignature_audit_log, outreach tracking, and stale packet archives before clean verification run
    with sqlite3.connect(str(DEFAULT_DB_PATH)) as conn:
        conn.execute("DELETE FROM client_intakes")
        conn.execute("DELETE FROM esignature_audit_log")
        conn.execute("DELETE FROM prospect_outreach_status")
        conn.execute("DELETE FROM outreach_send_log")
        conn.commit()
    packets_dir = DEFAULT_DB_PATH.parent / "packets"
    if packets_dir.exists():
        for old_pkt in packets_dir.glob("*.md"):
            old_pkt.unlink()
    backup_file = DEFAULT_DB_PATH.parent / "backups" / "submissions.jsonl"
    if backup_file.exists():
        backup_file.unlink()
    verify_persistence_domain_and_outreach_scheduler()
    verify_intake_portal_and_packager()
    verify_outputs()


if __name__ == "__main__":
    main()
