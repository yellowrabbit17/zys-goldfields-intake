"""SQLite database layer for grants.db."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Iterable

from models import ClientIntake, GrantRecord, LandownerProfile, ProspectRecord

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "grants.db"


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Return a SQLite connection with row factory enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create the grants, landowner_profiles, and client_intakes tables if they do not exist."""
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
                submitted_at TEXT NOT NULL
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
                personalized_intake_url TEXT NOT NULL,
                registry_source TEXT NOT NULL
            )
            """
        )
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
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO client_intakes (
                name, email, county, state, apn, acreage,
                land_condition, project_goals, agreed_to_terms, submitted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ts,
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
    return intake.model_copy(update={"id": new_id, "submitted_at": ts})


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
                   land_condition, project_goals, agreed_to_terms, submitted_at
            FROM client_intakes
            ORDER BY id DESC
            """
        )
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            d["agreed_to_terms"] = bool(d["agreed_to_terms"])
            rows.append(ClientIntake(**d))
        return rows


def upsert_prospects(
    prospects: Iterable[ProspectRecord],
    db_path: Path | str = DEFAULT_DB_PATH,
    reset_existing: bool = True,
) -> int:
    """Persist verified California prospects into california_prospects table."""
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
                top_matched_grant, matched_grant_deadline, personalized_intake_url, registry_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    p.personalized_intake_url,
                    p.registry_source,
                )
                for p in rows
            ],
        )
        conn.commit()
    return len(rows)


def fetch_all_prospects(db_path: Path | str = DEFAULT_DB_PATH) -> list[ProspectRecord]:
    """Load all verified California prospects from grants.db."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT prospect_id, organization_name, category, county, service_counties,
                   city, state, contact_email, phone, website, ein,
                   estimated_acreage, land_type, conservation_focus,
                   top_matched_grant, matched_grant_deadline, personalized_intake_url, registry_source
            FROM california_prospects
            ORDER BY category, county, organization_name
            """
        )
        return [ProspectRecord(**dict(row)) for row in cur.fetchall()]

