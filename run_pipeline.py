"""End-to-end execution and verification runner for ZYS GoldFields PRO LLC Grant Hunter."""

from __future__ import annotations

import csv
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
from pathlib import Path

import requests

from admin_packager import DEFAULT_PACKET_PATH
from db import DEFAULT_DB_PATH
from intake_app import IntakePortalHandler
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
    """Spin up intake_app HTTP server on an ephemeral port and verify full Vance + public API flow."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), IntakePortalHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. Verify GET / renders the single-page intake form with all required fields & URLSearchParams prefill
        home_resp = requests.get(
            f"{base_url}/?name=PAWS+Sanctuary&email=info%40pawsweb.org&county=Calaveras&acres=2300",
            timeout=5,
        )
        assert home_resp.status_code == 200
        for field_id in (
            "input-name",
            "input-email",
            "input-county",
            "input-apn",
            "input-acreage",
            "input-condition",
            "input-goals",
            "checkbox-terms",
            "URLSearchParams",
        ):
            assert field_id in home_resp.text, f"Missing expected token {field_id} in portal HTML"

        # 1b. Verify GET /api/health and GET /api/prospects
        health_resp = requests.get(f"{base_url}/api/health", timeout=5)
        assert health_resp.status_code == 200
        assert health_resp.json()["status"] == "healthy"

        prospects_resp = requests.get(f"{base_url}/api/prospects", timeout=5)
        assert prospects_resp.status_code == 200
        prospects_json = prospects_resp.json()
        assert 25 <= prospects_json["count"] < 70, (
            f"Expected strict filtering to retain Tier-1 subset (<70, >=25), got {prospects_json['count']}"
        )
        api_scores = [p["probability_score"] for p in prospects_json["prospects"]]
        assert all(s >= 75 for s in api_scores), f"Found sub-75 prospect in /api/prospects: {min(api_scores)}"
        assert api_scores == sorted(api_scores, reverse=True), "/api/prospects is not ranked highest-to-lowest"

        # 2. Verify unchecked 7% terms checkbox is rejected with HTTP 400
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
        }
        bad_resp = requests.post(f"{base_url}/api/intake", json=bad_payload, timeout=5)
        assert bad_resp.status_code == 400, f"Expected 400 when terms unchecked, got {bad_resp.status_code}"

        # 3. Submit the real Elena & Marcus Vance (El Dorado County) intake with 7% terms checked
        vance_payload = {
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
            "agreed_to_terms": True,
        }
        ok_resp = requests.post(f"{base_url}/api/intake", json=vance_payload, timeout=10)
        assert ok_resp.status_code == 200, f"Expected 200 on valid intake, got {ok_resp.status_code}: {ok_resp.text}"
        body = ok_resp.json()
        assert body["status"] == "ok"
        assert "California Forest Improvement Program" in body["matched_grant_title"]
        assert int(body["probability_score"].split("/")[0]) >= 75
        intake_id = body["intake_id"]

        # 4. Verify GET /api/packet/latest, GET /api/packet/<id>, and GET /api/intakes
        dl_resp = requests.get(f"{base_url}/api/packet/latest", timeout=5)
        assert dl_resp.status_code == 200
        assert "089-140-027-000" in dl_resp.text

        arch_resp = requests.get(f"{base_url}/api/packet/{intake_id}", timeout=5)
        assert arch_resp.status_code == 200
        assert "089-140-027-000" in arch_resp.text

        intakes_resp = requests.get(f"{base_url}/api/intakes", timeout=5)
        assert intakes_resp.status_code == 200
        assert intakes_resp.json()["count"] >= 1
    finally:
        server.shutdown()
        server.server_close()


def verify_outputs(
    db_path: Path = DEFAULT_DB_PATH,
    pitches_path: Path = DEFAULT_OUTPUT_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    packet_path: Path = DEFAULT_PACKET_PATH,
    prospects_csv_path: Path = DEFAULT_CSV_PATH,
) -> None:
    """Run automated integrity assertions across SQLite, config.json, pitches, prospects CSV, and packet."""
    assert config_path.exists(), f"Missing config.json: {config_path}"
    cfg = load_matcher_config(config_path)
    assert cfg.min_probability_score == 75, f"Expected min_probability_score=75, got {cfg.min_probability_score}"

    assert db_path.exists(), f"Missing SQLite database: {db_path}"
    assert pitches_path.exists(), f"Missing outreach pitches file: {pitches_path}"
    assert packet_path.exists(), f"Missing client_submission_packet.md: {packet_path}"
    assert prospects_csv_path.exists(), f"Missing california_prospects.csv: {prospects_csv_path}"

    root_dir = config_path.parent
    assert (root_dir / "render.yaml").exists(), "Missing Render deployment blueprint render.yaml"
    assert (root_dir / "start_public_tunnel.sh").exists(), "Missing Cloudflare tunnel script start_public_tunnel.sh"

    with sqlite3.connect(str(db_path)) as conn:
        grant_cols = [r[1] for r in conn.execute("PRAGMA table_info(grants)").fetchall()]
        profile_cols = [r[1] for r in conn.execute("PRAGMA table_info(landowner_profiles)").fetchall()]

        assert grant_cols == ["id", "title", "agency", "funding_amount", "deadline", "eligibility_text", "url"]
        assert profile_cols == ["id", "name", "state", "county", "acreage", "land_type", "features"]

        grant_count = conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
        profile_count = conn.execute("SELECT COUNT(*) FROM landowner_profiles").fetchone()[0]
        intake_count = conn.execute("SELECT COUNT(*) FROM client_intakes").fetchone()[0]
        prospect_db_count = conn.execute("SELECT COUNT(*) FROM california_prospects").fetchone()[0]
        min_db_prob = conn.execute("SELECT MIN(probability_score) FROM california_prospects").fetchone()[0]

        assert grant_count >= 10, f"Expected >= 10 active grants in grants.db, found {grant_count}"
        assert profile_count == 3, f"Expected 3 seeded landowner profiles, found {profile_count}"
        assert intake_count >= 1, f"Expected >= 1 persisted client intake, found {intake_count}"
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
        sc = int(row["probability_score"])
        assert sc >= 75, f"Candidate {row['organization_name']} has score {sc} < 75!"
        assert len(row["score_rationale"]) > 25, f"Missing 1-sentence score_rationale on {row['organization_name']}"
        csv_scores.append(sc)

    assert csv_scores == sorted(csv_scores, reverse=True), "california_prospects.csv is not ranked highest-to-lowest!"

    md_text = pitches_path.read_text(encoding="utf-8")
    assert NO_CALL_NEXT_STEP in md_text, "Missing zero-call digital intake CTA in outreach_pitches.md"
    assert cfg.firm_name in md_text, "Missing ZYS GoldFields PRO LLC signature in outreach_pitches.md"
    assert cfg.firm_subtitle in md_text, "Missing Conservation & Agricultural Grant Advisory subtitle"
    assert "Grant Win Probability Score" in md_text, "Missing Probability Score label in outreach_pitches.md"
    assert "1-Sentence Score Rationale" in md_text, "Missing 1-Sentence Score Rationale in outreach_pitches.md"
    assert "walk-through call" not in md_text.lower(), "Legacy phone call CTA still present!"
    assert "Northern California Conservation Grant Advisory Desk" not in md_text, "Legacy desk signature present!"
    assert "Automated Conservation Grant & Subsidy Hunter" not in md_text, "Legacy subtitle present!"

    packet_md = packet_path.read_text(encoding="utf-8")
    assert "Elena & Marcus Vance" in packet_md
    assert "089-140-027-000" in packet_md
    assert "Part A: Pre-Filled Project Narrative & Budget Engineering" in packet_md
    assert "Part B: Ready-to-Send County RCD & USDA NRCS Handoff Package" in packet_md
    assert "El Dorado County & Georgetown Divide Resource Conservation Districts" in packet_md
    assert "USDA NRCS Placerville Service Center" in packet_md
    assert "ZYS GoldFields PRO LLC" in packet_md

    print("\n=== Grant Probability Engine (>=75 Tier-1) & Pipeline Verification Passed ===")
    print(f"  Config File:         {config_path} ({json.dumps(cfg.model_dump())})")
    print(
        f"  SQLite DB:           {db_path} "
        f"({grant_count} grants, {profile_count} profiles, {prospect_db_count} Tier-1 CA prospects, {intake_count} client intakes)"
    )
    print(
        f"  Tier-1 CA CSV:       {prospects_csv_path} "
        f"({len(csv_rows)} Tier-1 orgs, ranked {csv_scores[0]}/100 down to {csv_scores[-1]}/100)"
    )
    print(f"  Outreach Briefs:     {pitches_path} ({len(md_text):,} bytes — Tier-1 >=75 only, ranked highest-to-lowest)")
    print(f"  Submission Packet:   {packet_path} ({len(packet_md):,} bytes — Elena & Marcus Vance ready-to-file dossier)")


def main() -> None:
    ingest_all_grants()
    run_seed()
    build_california_prospects_dataset()
    run_matcher()
    # Reset client_intakes and remove stale packet archives before clean verification intake submission
    with sqlite3.connect(str(DEFAULT_DB_PATH)) as conn:
        conn.execute("DELETE FROM client_intakes")
        conn.commit()
    packets_dir = DEFAULT_DB_PATH.parent / "packets"
    if packets_dir.exists():
        for old_pkt in packets_dir.glob("client_submission_packet_*.md"):
            old_pkt.unlink()
    verify_intake_portal_and_packager()
    verify_outputs()


if __name__ == "__main__":
    main()
