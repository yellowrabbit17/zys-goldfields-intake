"""SQLite database layer for grants.db."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Iterable

from models import ClientIntake, ESignatureAuditRecord, GrantRecord, LandownerProfile, ProspectRecord

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "grants.db"


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Return a SQLite connection with row factory enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create the grants, landowner_profiles, client_intakes, california_prospects, and esignature_audit_log tables."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                agency TEXT NOT NULL,
                funding_amount TEXT NOT NULL,
                deadline TEXT NOT NULL,
                eligibility_text TEXT NOT NULL,
                url TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS landowner_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                state TEXT NOT NULL,
                county TEXT NOT NULL,
                acreage REAL NOT NULL,
                land_type TEXT NOT NULL,
                features TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_intakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                county TEXT NOT NULL,
                state TEXT NOT NULL,
                apn TEXT NOT NULL,
                acreage REAL NOT NULL,
                land_condition TEXT NOT NULL,
                project_goals TEXT NOT NULL,
                agreed_to_terms INTEGER NOT NULL,
                agreement_signed INTEGER NOT NULL DEFAULT 0,
                signer_full_name TEXT NOT NULL DEFAULT '',
                signer_title TEXT NOT NULL DEFAULT '',
                signature_timestamp_utc TEXT NOT NULL DEFAULT '',
                signer_ip_address TEXT NOT NULL DEFAULT '',
                signer_user_agent TEXT NOT NULL DEFAULT '',
                contract_sha256_hash TEXT NOT NULL DEFAULT '',
                signed_agreement_path TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL
            )
            """
        )
        intake_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(client_intakes)").fetchall()
        }
        new_intake_cols = [
            ("agreement_signed", "INTEGER NOT NULL DEFAULT 0"),
            ("signer_full_name", "TEXT NOT NULL DEFAULT ''"),
            ("signer_title", "TEXT NOT NULL DEFAULT ''"),
            ("signature_timestamp_utc", "TEXT NOT NULL DEFAULT ''"),
            ("signer_ip_address", "TEXT NOT NULL DEFAULT ''"),
            ("signer_user_agent", "TEXT NOT NULL DEFAULT ''"),
            ("contract_sha256_hash", "TEXT NOT NULL DEFAULT ''"),
            ("signed_agreement_path", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_ddl in new_intake_cols:
            if col_name not in intake_cols:
                conn.execute(f"ALTER TABLE client_intakes ADD COLUMN {col_name} {col_ddl}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS esignature_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intake_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                client_email TEXT NOT NULL,
                county TEXT NOT NULL,
                apn TEXT NOT NULL,
                signer_full_name TEXT NOT NULL,
                signer_title TEXT NOT NULL,
                agreement_version TEXT NOT NULL,
                agreement_signed INTEGER NOT NULL DEFAULT 1,
                signature_timestamp_utc TEXT NOT NULL,
                signer_ip_address TEXT NOT NULL,
                signer_user_agent TEXT NOT NULL,
                contract_sha256_hash TEXT NOT NULL,
                matched_grant_id TEXT NOT NULL DEFAULT '',
                matched_grant_title TEXT NOT NULL DEFAULT '',
                engineered_grant_request INTEGER NOT NULL DEFAULT 0,
                contingent_fee_dollars INTEGER NOT NULL DEFAULT 0,
                signed_agreement_path TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS california_prospects (
                prospect_id TEXT PRIMARY KEY,
                organization_name TEXT NOT NULL,
                category TEXT NOT NULL,
                county TEXT NOT NULL,
                service_counties TEXT NOT NULL,
                city TEXT NOT NULL,
                state TEXT NOT NULL,
                contact_email TEXT NOT NULL,
                phone TEXT NOT NULL,
                website TEXT NOT NULL,
                ein TEXT NOT NULL,
                estimated_acreage REAL NOT NULL,
                land_type TEXT NOT NULL,
                conservation_focus TEXT NOT NULL,
                top_matched_grant TEXT NOT NULL,
                matched_grant_deadline TEXT NOT NULL,
                probability_score INTEGER NOT NULL DEFAULT 0,
                acreage_score INTEGER NOT NULL DEFAULT 0,
                entity_score INTEGER NOT NULL DEFAULT 0,
                ecological_score INTEGER NOT NULL DEFAULT 0,
                comatch_score INTEGER NOT NULL DEFAULT 0,
                score_breakdown TEXT NOT NULL DEFAULT '',
                score_rationale TEXT NOT NULL DEFAULT '',
                personalized_intake_url TEXT NOT NULL,
                registry_source TEXT NOT NULL
            )
            """
        )
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(california_prospects)").fetchall()
        }
        new_cols = [
            ("probability_score", "INTEGER NOT NULL DEFAULT 0"),
            ("acreage_score", "INTEGER NOT NULL DEFAULT 0"),
            ("entity_score", "INTEGER NOT NULL DEFAULT 0"),
            ("ecological_score", "INTEGER NOT NULL DEFAULT 0"),
            ("comatch_score", "INTEGER NOT NULL DEFAULT 0"),
            ("score_breakdown", "TEXT NOT NULL DEFAULT ''"),
            ("score_rationale", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_ddl in new_cols:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE california_prospects ADD COLUMN {col_name} {col_ddl}")
        conn.commit()


def upsert_grants(grants: Iterable[GrantRecord], db_path: Path | str = DEFAULT_DB_PATH) -> int:
    """Insert or update validated GrantRecord entries into the grants table."""
    init_db(db_path)
    records = list(grants)
    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO grants (id, title, agency, funding_amount, deadline, eligibility_text, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                agency = excluded.agency,
                funding_amount = excluded.funding_amount,
                deadline = excluded.deadline,
                eligibility_text = excluded.eligibility_text,
                url = excluded.url
            """,
            [
                (
                    g.id,
                    g.title,
                    g.agency,
                    g.funding_amount,
                    g.deadline,
                    g.eligibility_text,
                    g.url,
                )
                for g in records
            ],
        )
        conn.commit()
    return len(records)


def seed_landowner_profiles(
    profiles: Iterable[LandownerProfile],
    db_path: Path | str = DEFAULT_DB_PATH,
    reset_existing: bool = True,
) -> int:
    """Populate landowner_profiles with validated LandownerProfile rows."""
    init_db(db_path)
    rows = list(profiles)
    with get_connection(db_path) as conn:
        if reset_existing:
            conn.execute("DELETE FROM landowner_profiles")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'landowner_profiles'")
        conn.executemany(
            """
            INSERT INTO landowner_profiles (name, state, county, acreage, land_type, features)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p.name,
                    p.state,
                    p.county,
                    p.acreage,
                    p.land_type,
                    p.features,
                )
                for p in rows
            ],
        )
        conn.commit()
    return len(rows)


def insert_client_intake(
    intake: ClientIntake,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> ClientIntake:
    """Store a validated ClientIntake submission into grants.db and return it with its ID."""
    init_db(db_path)
    ts = intake.submitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig_ts = intake.signature_timestamp_utc or (ts if intake.agreement_signed else "")
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO client_intakes (
                name, email, county, state, apn, acreage,
                land_condition, project_goals, agreed_to_terms,
                agreement_signed, signer_full_name, signer_title,
                signature_timestamp_utc, signer_ip_address, signer_user_agent,
                contract_sha256_hash, signed_agreement_path, submitted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intake.name,
                intake.email,
                intake.county,
                intake.state,
                intake.apn,
                intake.acreage,
                intake.land_condition,
                intake.project_goals,
                1 if intake.agreed_to_terms else 0,
                1 if intake.agreement_signed else 0,
                intake.signer_full_name,
                intake.signer_title,
                sig_ts,
                intake.signer_ip_address,
                intake.signer_user_agent,
                intake.contract_sha256_hash,
                intake.signed_agreement_path,
                ts,
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
    return intake.model_copy(
        update={
            "id": new_id,
            "submitted_at": ts,
            "signature_timestamp_utc": sig_ts,
        }
    )


def update_client_intake_signature(
    intake_id: int,
    signer_full_name: str,
    signer_title: str,
    signature_timestamp_utc: str,
    signer_ip_address: str,
    signer_user_agent: str,
    contract_sha256_hash: str,
    signed_agreement_path: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> ClientIntake:
    """Record a completed e-signature on an existing client_intakes row and return the updated model."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE client_intakes
            SET agreed_to_terms = 1,
                agreement_signed = 1,
                signer_full_name = ?,
                signer_title = ?,
                signature_timestamp_utc = ?,
                signer_ip_address = ?,
                signer_user_agent = ?,
                contract_sha256_hash = ?,
                signed_agreement_path = ?
            WHERE id = ?
            """,
            (
                signer_full_name,
                signer_title,
                signature_timestamp_utc,
                signer_ip_address,
                signer_user_agent,
                contract_sha256_hash,
                signed_agreement_path,
                intake_id,
            ),
        )
        conn.commit()
    updated = fetch_client_intake_by_id(intake_id, db_path=db_path)
    if updated is None:
        raise ValueError(f"ClientIntake #{intake_id} not found in grants.db")
    return updated


def fetch_client_intake_by_id(
    intake_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> ClientIntake | None:
    """Fetch a single ClientIntake record by ID from grants.db."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, name, email, county, state, apn, acreage,
                   land_condition, project_goals, agreed_to_terms,
                   agreement_signed, signer_full_name, signer_title,
                   signature_timestamp_utc, signer_ip_address, signer_user_agent,
                   contract_sha256_hash, signed_agreement_path, submitted_at
            FROM client_intakes
            WHERE id = ?
            """,
            (intake_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["agreed_to_terms"] = bool(d["agreed_to_terms"])
        d["agreement_signed"] = bool(d["agreement_signed"])
        return ClientIntake(**d)


def is_intake_agreement_signed(
    intake_id: int | None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> bool:
    """Check directly in grants.db whether agreement_signed == True for the given intake_id."""
    if intake_id is None:
        return False
    record = fetch_client_intake_by_id(intake_id, db_path=db_path)
    return bool(record and record.agreement_signed)


def insert_esignature_audit_record(
    record: ESignatureAuditRecord,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> ESignatureAuditRecord:
    """Insert a cryptographic legal audit record into esignature_audit_log in grants.db."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO esignature_audit_log (
                intake_id, client_name, client_email, county, apn,
                signer_full_name, signer_title, agreement_version, agreement_signed,
                signature_timestamp_utc, signer_ip_address, signer_user_agent,
                contract_sha256_hash, matched_grant_id, matched_grant_title,
                engineered_grant_request, contingent_fee_dollars, signed_agreement_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.intake_id or 0,
                record.client_name,
                record.client_email,
                record.county,
                record.apn,
                record.signer_full_name,
                record.signer_title,
                record.agreement_version,
                1 if record.agreement_signed else 0,
                record.signature_timestamp_utc,
                record.signer_ip_address,
                record.signer_user_agent,
                record.contract_sha256_hash,
                record.matched_grant_id,
                record.matched_grant_title,
                record.engineered_grant_request,
                record.contingent_fee_dollars,
                record.signed_agreement_path,
            ),
        )
        conn.commit()
        audit_id = cur.lastrowid
    return record.model_copy(update={"id": audit_id})


def fetch_all_esignature_audits(
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[ESignatureAuditRecord]:
    """Load all cryptographic e-signature audit records from grants.db."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, intake_id, client_name, client_email, county, apn,
                   signer_full_name, signer_title, agreement_version, agreement_signed,
                   signature_timestamp_utc, signer_ip_address, signer_user_agent,
                   contract_sha256_hash, matched_grant_id, matched_grant_title,
                   engineered_grant_request, contingent_fee_dollars, signed_agreement_path
            FROM esignature_audit_log
            ORDER BY id DESC
            """
        )
        rows: list[ESignatureAuditRecord] = []
        for row in cur.fetchall():
            d = dict(row)
            d["agreement_signed"] = bool(d["agreement_signed"])
            rows.append(ESignatureAuditRecord(**d))
        return rows


def fetch_all_grants(db_path: Path | str = DEFAULT_DB_PATH) -> list[GrantRecord]:
    """Load all stored grants from grants.db as GrantRecord models."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT id, title, agency, funding_amount, deadline, eligibility_text, url FROM grants ORDER BY id"
        )
        return [GrantRecord(**dict(row)) for row in cur.fetchall()]


def fetch_all_profiles(db_path: Path | str = DEFAULT_DB_PATH) -> list[LandownerProfile]:
    """Load all landowner profiles from grants.db as LandownerProfile models."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT id, name, state, county, acreage, land_type, features FROM landowner_profiles ORDER BY id"
        )
        return [LandownerProfile(**dict(row)) for row in cur.fetchall()]


def fetch_all_intakes(db_path: Path | str = DEFAULT_DB_PATH) -> list[ClientIntake]:
    """Load all client intake submissions from grants.db."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, name, email, county, state, apn, acreage,
                   land_condition, project_goals, agreed_to_terms,
                   agreement_signed, signer_full_name, signer_title,
                   signature_timestamp_utc, signer_ip_address, signer_user_agent,
                   contract_sha256_hash, signed_agreement_path, submitted_at
            FROM client_intakes
            ORDER BY id DESC
            """
        )
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            d["agreed_to_terms"] = bool(d["agreed_to_terms"])
            d["agreement_signed"] = bool(d["agreement_signed"])
            rows.append(ClientIntake(**d))
        return rows


def upsert_prospects(
    prospects: Iterable[ProspectRecord],
    db_path: Path | str = DEFAULT_DB_PATH,
    reset_existing: bool = True,
) -> int:
    """Persist verified Tier-1 California prospects into california_prospects table."""
    init_db(db_path)
    rows = list(prospects)
    with get_connection(db_path) as conn:
        if reset_existing:
            conn.execute("DELETE FROM california_prospects")
        conn.executemany(
            """
            INSERT INTO california_prospects (
                prospect_id, organization_name, category, county, service_counties,
                city, state, contact_email, phone, website, ein,
                estimated_acreage, land_type, conservation_focus,
                top_matched_grant, matched_grant_deadline,
                probability_score, acreage_score, entity_score, ecological_score,
                comatch_score, score_breakdown, score_rationale,
                personalized_intake_url, registry_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(prospect_id) DO UPDATE SET
                organization_name = excluded.organization_name,
                category = excluded.category,
                county = excluded.county,
                service_counties = excluded.service_counties,
                city = excluded.city,
                state = excluded.state,
                contact_email = excluded.contact_email,
                phone = excluded.phone,
                website = excluded.website,
                ein = excluded.ein,
                estimated_acreage = excluded.estimated_acreage,
                land_type = excluded.land_type,
                conservation_focus = excluded.conservation_focus,
                top_matched_grant = excluded.top_matched_grant,
                matched_grant_deadline = excluded.matched_grant_deadline,
                probability_score = excluded.probability_score,
                acreage_score = excluded.acreage_score,
                entity_score = excluded.entity_score,
                ecological_score = excluded.ecological_score,
                comatch_score = excluded.comatch_score,
                score_breakdown = excluded.score_breakdown,
                score_rationale = excluded.score_rationale,
                personalized_intake_url = excluded.personalized_intake_url,
                registry_source = excluded.registry_source
            """,
            [
                (
                    p.prospect_id,
                    p.organization_name,
                    p.category,
                    p.county,
                    p.service_counties,
                    p.city,
                    p.state,
                    p.contact_email,
                    p.phone,
                    p.website,
                    p.ein,
                    p.estimated_acreage,
                    p.land_type,
                    p.conservation_focus,
                    p.top_matched_grant,
                    p.matched_grant_deadline,
                    p.probability_score,
                    p.acreage_score,
                    p.entity_score,
                    p.ecological_score,
                    p.comatch_score,
                    p.score_breakdown,
                    p.score_rationale,
                    p.personalized_intake_url,
                    p.registry_source,
                )
                for p in rows
            ],
        )
        conn.commit()
    return len(rows)


def fetch_all_prospects(db_path: Path | str = DEFAULT_DB_PATH) -> list[ProspectRecord]:
    """Load all verified California prospects from grants.db ranked highest probability score to lowest."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT prospect_id, organization_name, category, county, service_counties,
                   city, state, contact_email, phone, website, ein,
                   estimated_acreage, land_type, conservation_focus,
                   top_matched_grant, matched_grant_deadline,
                   probability_score, acreage_score, entity_score, ecological_score,
                   comatch_score, score_breakdown, score_rationale,
                   personalized_intake_url, registry_source
            FROM california_prospects
            ORDER BY probability_score DESC, estimated_acreage DESC, organization_name ASC
            """
        )
        return [ProspectRecord(**dict(row)) for row in cur.fetchall()]

