"""End-to-end execution and verification runner for ZYS GoldFields PRO LLC Grant Hunter."""

from __future__ import annotations

import csv
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
import time
from pathlib import Path

import requests

from admin_packager import DEFAULT_PACKET_PATH, DEFAULT_PREVIEW_PATH
from db import DEFAULT_DB_PATH
from intake_app import IntakePortalHandler
from legal_agreement import DEFAULT_SIGNED_AGREEMENT_PATH
from matcher import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_PATH,
    NO_CALL_NEXT_STEP,
    load_matcher_config,
    run_matcher,
)
from prospect_hunter import (
    CA_COUNTIES,
    DEFAULT_CSV_PATH,
    build_california_prospects_dataset,
)
from scraper import ingest_all_grants
from seed_profiles import run_seed


def verify_intake_portal_and_packager() -> None:
    """Spin up intake_app HTTP server on an ephemeral port and verify Stage A Gate + Stage B E-Signature flow."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), IntakePortalHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. Verify GET / renders the single-page intake form with scrollable UETA/ESIGN agreement & 2 CFR 200.459 clause
        home_resp = requests.get(
            f"{base_url}/?name=PAWS+Sanctuary&email=info%40pawsweb.org&county=Calaveras&acres=2300",
            timeout=5,
        )
        assert home_resp.status_code == 200
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
        ):
            assert expected_token in home_resp.text, f"Missing expected token '{expected_token}' in portal HTML"

        # 1a. Verify /healthz (GET and HEAD) for external cron keep-alive
        healthz_head = requests.head(f"{base_url}/healthz", timeout=5)
        assert healthz_head.status_code == 200, f"Expected 200 on HEAD /healthz, got {healthz_head.status_code}"
        healthz_get = requests.get(f"{base_url}/healthz", timeout=5)
        assert healthz_get.status_code == 200, f"Expected 200 on GET /healthz, got {healthz_get.status_code}"
        assert healthz_get.text == "ok\n", f"Unexpected /healthz body: {healthz_get.text}"

        # 1b. Verify 1-click pre-filled magic link /claim?prospect_id=CA-T1-001
        claim_resp = requests.get(f"{base_url}/claim?prospect_id=CA-T1-001", timeout=5)
        assert claim_resp.status_code == 200, f"Expected 200 on /claim, got {claim_resp.status_code}"
        for token in ("Paicines Ranch", "Pre-Screened Grant Allocation", "CA-T1-001", "83-3255248", "San Benito"):
            assert token in claim_resp.text, f"Missing token '{token}' in /claim?prospect_id=CA-T1-001 HTML"

        # 1c. Verify GET /api/health and GET /api/prospects
        health_resp = requests.get(f"{base_url}/api/health", timeout=5)
        assert health_resp.status_code == 200
        health_json = health_resp.json()
        assert health_json["status"] == "healthy"
        assert "notifications" in health_json

        prospects_resp = requests.get(f"{base_url}/api/prospects", timeout=5)
        assert prospects_resp.status_code == 200
        prospects_json = prospects_resp.json()
        assert 25 <= prospects_json["count"] < 70, (
            f"Expected strict filtering to retain Tier-1 subset (<70, >=25), got {prospects_json['count']}"
        )
        api_scores = [p["probability_score"] for p in prospects_json["prospects"]]
        assert all(s >= 75 for s in api_scores), f"Found sub-75 prospect in /api/prospects: {min(api_scores)}"
        assert api_scores == sorted(api_scores, reverse=True), "/api/prospects is not ranked highest-to-lowest"

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

        # 3. Verify Stage A ("Sign-to-Unlock" Gate when agreement_signed == False):
        #    Shows matched grant, engineered budget total, and probability score, but masks
        #    RCD Co-Sponsor Memorandum, grant solicitation ID, and copy-paste narrative.
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
        preview_md = preview_body["packet_markdown"]
        assert "Stage A: Client Grant Eligibility & Budget Preview Summary (LOCKED DOSSIER)" in preview_md
        # Ensure solicitation ID, copy-paste narrative, and RCD co-sponsor memo are masked in Stage A
        assert "CCI-california-forest-improvement-program" not in preview_md
        assert "Part B: Ready-to-Send County RCD & USDA NRCS Handoff Package" not in preview_md
        assert "MASKED — COMPLETE E-SIGNATURE TO UNLOCK" in preview_md

        preview_intake_id = preview_body["intake_id"]
        # Attempting to download unlocked packet for unsigned intake must return HTTP 403 Forbidden
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
    finally:
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
    assert (root_dir / "followup_templates.md").exists(), "Missing followup_templates.md"

    with sqlite3.connect(str(db_path)) as conn:
        grant_cols = [r[1] for r in conn.execute("PRAGMA table_info(grants)").fetchall()]
        profile_cols = [r[1] for r in conn.execute("PRAGMA table_info(landowner_profiles)").fetchall()]
        audit_cols = [r[1] for r in conn.execute("PRAGMA table_info(esignature_audit_log)").fetchall()]

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

    print("\n=== California UETA / ESIGN, 2 CFR § 200.459 & Sign-to-Unlock Verification Passed ===")
    print(f"  Config File:         {config_path} ({json.dumps(cfg.model_dump())})")
    print(
        f"  SQLite DB:           {db_path} "
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
    # Reset client_intakes, esignature_audit_log, and stale packet archives before clean verification run
    with sqlite3.connect(str(DEFAULT_DB_PATH)) as conn:
        conn.execute("DELETE FROM client_intakes")
        conn.execute("DELETE FROM esignature_audit_log")
        conn.commit()
    packets_dir = DEFAULT_DB_PATH.parent / "packets"
    if packets_dir.exists():
        for old_pkt in packets_dir.glob("*.md"):
            old_pkt.unlink()
    backup_file = DEFAULT_DB_PATH.parent / "backups" / "submissions.jsonl"
    if backup_file.exists():
        backup_file.unlink()
    verify_intake_portal_and_packager()
    verify_outputs()


if __name__ == "__main__":
    main()
