"""Dual-backend database layer supporting Lakebase Postgres on Neon (pooled/direct + TCP/HTTP) with SQLite fallback."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse, urlunparse
import requests
from requests.adapters import HTTPAdapter

from models import (
    ClientIntake,
    ESignatureAuditRecord,
    GrantRecord,
    LandownerProfile,
    ProspectRecord,
    load_env_files,
)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - optional only if Postgres unused
    psycopg2 = None  # type: ignore[assignment]

logger = logging.getLogger("db")

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "grants.db"
_INIT_LOCK = threading.Lock()
_INITIALIZED_TARGETS: set[str] = set()
_TCP_BLOCKED_ENDPOINTS: set[str] = set()
_DRIVER_LOCK = threading.Lock()
_LAST_ACTIVE_DRIVER: str = ""
_HTTP_SQL_SESSION = requests.Session()
_HTTP_SQL_SESSION.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=32))


def _normalize_postgres_dsn(dsn: str) -> str:
    cleaned = dsn.strip()
    if cleaned.startswith("postgres://"):
        cleaned = "postgresql://" + cleaned[len("postgres://") :]
    return cleaned


def _strip_pooler_host(dsn: str) -> str:
    """Convert a '-pooler' Neon hostname into the corresponding direct compute hostname."""
    parsed = urlparse(dsn)
    host = parsed.hostname or ""
    if "-pooler." not in host:
        return dsn
    direct_host = host.replace("-pooler.", ".", 1)
    netloc = parsed.netloc.replace(host, direct_host, 1)
    return urlunparse(parsed._replace(netloc=netloc))


def _is_neon_host(hostname: str | None) -> bool:
    return bool(hostname and hostname.lower().endswith(".neon.tech"))


def get_database_url(db_path: Path | str = DEFAULT_DB_PATH, *, direct: bool = False) -> str | None:
    """Return normalized PostgreSQL DSN when configured (supporting pooled app vs direct migration URLs).

    - Application queries (`direct=False`): prefers `DATABASE_URL_POOLED`, then `DATABASE_URL`.
    - Schema migrations (`direct=True`): prefers `DATABASE_URL_UNPOOLED`, then unpooled `DATABASE_URL`.
    - Explicitly passed custom SQLite paths bypass Postgres cleanly so isolated tests keep using SQLite.
    """
    load_env_files(Path(__file__).resolve().parent)
    if isinstance(db_path, str):
        raw_str = db_path.strip()
        if raw_str.startswith("postgres://") or raw_str.startswith("postgresql://"):
            norm = _normalize_postgres_dsn(raw_str)
            return _strip_pooler_host(norm) if direct else norm
    try:
        is_default_target = Path(db_path).resolve() == DEFAULT_DB_PATH.resolve()
    except OSError:
        is_default_target = str(db_path) == str(DEFAULT_DB_PATH)
    if not is_default_target:
        return None

    env_pooled = os.environ.get("DATABASE_URL_POOLED", "").strip()
    env_unpooled = os.environ.get("DATABASE_URL_UNPOOLED", "").strip()
    env_url = os.environ.get("DATABASE_URL", "").strip()

    if direct:
        for candidate in (env_unpooled, env_url, env_pooled):
            if candidate:
                return _strip_pooler_host(_normalize_postgres_dsn(candidate))
        return None

    for candidate in (env_pooled, env_url, env_unpooled):
        if candidate:
            return _normalize_postgres_dsn(candidate)
    return None


def database_backend_info(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Return non-sensitive database backend metadata safe for /api/health and diagnostics."""
    dsn = get_database_url(db_path, direct=False)
    if dsn:
        direct_dsn = get_database_url(db_path, direct=True) or dsn
        parsed = urlparse(dsn)
        direct_parsed = urlparse(direct_dsn)
        db_name = parsed.path.lstrip("/") or "postgres"
        driver = _LAST_ACTIVE_DRIVER
        if not driver:
            mode = os.environ.get("NEON_SQL_TRANSPORT", "auto").strip().lower()
            if mode == "http" or (parsed.hostname or "") in _TCP_BLOCKED_ENDPOINTS:
                driver = "neon-serverless-http"
            else:
                driver = "psycopg2-tcp"
        return {
            "backend": "postgresql",
            "persistent": True,
            "driver": driver,
            "pooled": "-pooler" in (parsed.hostname or ""),
            "direct_migrations": "-pooler" not in (direct_parsed.hostname or ""),
            "host": parsed.hostname or "managed-postgres",
            "migration_host": direct_parsed.hostname or parsed.hostname or "managed-postgres",
            "database": db_name,
        }
    return {
        "backend": "sqlite",
        "persistent": False,
        "driver": "sqlite3",
        "pooled": False,
        "direct_migrations": True,
        "host": "local-filesystem",
        "migration_host": "local-filesystem",
        "database": Path(db_path).name,
    }


def _adapt_sql(sql: str, is_postgres: bool, *, positional: bool = False) -> str:
    """Convert SQLite '?' placeholders outside single-quoted string literals to '%s' or '$1..$N' for Postgres."""
    if not is_postgres:
        return sql
    out: list[str] = []
    in_single_quote = False
    i = 0
    param_idx = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_single_quote and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single_quote = not in_single_quote
        elif ch == "?" and not in_single_quote:
            param_idx += 1
            out.append(f"${param_idx}" if positional else "%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _coerce_neon_field_value(value: Any, data_type_id: int) -> Any:
    if value is None:
        return None
    if data_type_id in (20, 21, 23):  # int8, int2, int4
        return int(value) if not isinstance(value, bool) else int(value)
    if data_type_id in (700, 701, 1700):  # float4, float8, numeric
        return float(value)
    if data_type_id == 16:  # bool
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("t", "true", "1")
    return value


class _NeonHttpCursor:
    """Cursor-like wrapper over Neon Serverless HTTP /sql JSON results."""

    def __init__(self, rows: list[dict[str, Any]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount
        raw_id = rows[0].get("id") if rows else None
        try:
            self.lastrowid = int(raw_id) if raw_id is not None and not isinstance(raw_id, bool) else 0
        except (ValueError, TypeError):
            self.lastrowid = 0

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _NeonHttpSession:
    """HTTPS /sql session matching _DBSession interface for proxied/serverless environments."""

    is_postgres = True

    def __init__(self, dsn: str, timeout: float = 20.0) -> None:
        parsed = urlparse(dsn)
        if not parsed.hostname:
            raise ValueError("Invalid Postgres DSN: missing hostname")
        self.dsn = dsn
        self.endpoint = f"https://{parsed.hostname}/sql"
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Neon-Connection-String": self.dsn,
            "Content-Type": "application/json",
            "User-Agent": "ZYS-GoldFields-NeonHTTP/1.0",
        }

    @staticmethod
    def _parse_result(payload: dict[str, Any]) -> _NeonHttpCursor:
        fields = payload.get("fields") or []
        oid_map = {str(f.get("name")): int(f.get("dataTypeID") or 25) for f in fields if f.get("name")}
        converted_rows: list[dict[str, Any]] = []
        for raw_row in payload.get("rows") or []:
            row_dict: dict[str, Any] = {}
            for col_name, raw_val in raw_row.items():
                row_dict[col_name] = _coerce_neon_field_value(raw_val, oid_map.get(col_name, 25))
            converted_rows.append(row_dict)
        rowcount = int(payload.get("rowCount") if payload.get("rowCount") is not None else len(converted_rows))
        return _NeonHttpCursor(converted_rows, rowcount)

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> _NeonHttpCursor:
        adapted = _adapt_sql(sql, True, positional=True)
        resp = _HTTP_SQL_SESSION.post(
            self.endpoint,
            headers=self._headers(),
            json={"query": adapted, "params": list(params)},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Neon HTTP /sql error {resp.status_code}: {resp.text[:400]}")
        return self._parse_result(resp.json())

    def executemany(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> None:
        if not seq_of_params:
            return
        adapted = _adapt_sql(sql, True, positional=True)
        batch_size = 50
        for idx in range(0, len(seq_of_params), batch_size):
            chunk = seq_of_params[idx : idx + batch_size]
            queries = [{"query": adapted, "params": list(p)} for p in chunk]
            resp = _HTTP_SQL_SESSION.post(
                self.endpoint,
                headers=self._headers(),
                json={"queries": queries},
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Neon HTTP /sql batch error {resp.status_code}: {resp.text[:400]}")

    def insert_returning_id(self, sql: str, params: tuple[Any, ...]) -> int:
        adapted = _adapt_sql(sql, True, positional=True).rstrip().rstrip(";") + " RETURNING id"
        cur = self.execute(adapted, params)
        row = cur.fetchone()
        return int(row["id"]) if row and row.get("id") is not None else 0


class _DBSession:
    """Unified cursor/transaction wrapper over psycopg2 and sqlite3."""

    def __init__(self, conn: Any, is_postgres: bool) -> None:
        self.conn = conn
        self.is_postgres = is_postgres

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        adapted = _adapt_sql(sql, self.is_postgres, positional=False)
        if self.is_postgres:
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(adapted, params)
            return cur
        return self.conn.execute(adapted, params)

    def executemany(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> None:
        if not seq_of_params:
            return
        adapted = _adapt_sql(sql, self.is_postgres, positional=False)
        if self.is_postgres:
            with self.conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, adapted, seq_of_params, page_size=100)
            return
        self.conn.executemany(adapted, seq_of_params)

    def insert_returning_id(self, sql: str, params: tuple[Any, ...]) -> int:
        if self.is_postgres:
            adapted = _adapt_sql(sql, True, positional=False).rstrip().rstrip(";") + " RETURNING id"
            with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(adapted, params)
                row = cur.fetchone()
                return int(row["id"])
        cur = self.conn.execute(_adapt_sql(sql, False, positional=False), params)
        return int(cur.lastrowid or 0)


@contextmanager
def _open_session(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    direct: bool = False,
) -> Iterator[_DBSession | _NeonHttpSession]:
    """Open a managed SQLite or PostgreSQL (TCP or Neon Serverless HTTP /sql) session."""
    global _LAST_ACTIVE_DRIVER
    dsn = get_database_url(db_path, direct=direct)
    if dsn:
        parsed = urlparse(dsn)
        hostname = parsed.hostname or ""
        transport_pref = os.environ.get("NEON_SQL_TRANSPORT", "auto").strip().lower()
        use_http_directly = (
            transport_pref == "http"
            or (transport_pref == "auto" and hostname in _TCP_BLOCKED_ENDPOINTS)
            or psycopg2 is None
        )
        if use_http_directly and _is_neon_host(hostname):
            with _DRIVER_LOCK:
                _LAST_ACTIVE_DRIVER = "neon-serverless-http"
            yield _NeonHttpSession(dsn)
            return

        if psycopg2 is None:
            raise RuntimeError(
                "DATABASE_URL is configured for PostgreSQL, but psycopg2-binary is not installed. "
                "Install it with: pip install psycopg2-binary"
            )
        try:
            conn = psycopg2.connect(dsn, connect_timeout=8)
        except psycopg2.OperationalError as exc:
            if transport_pref != "tcp" and _is_neon_host(hostname):
                with _DRIVER_LOCK:
                    first_switch = hostname not in _TCP_BLOCKED_ENDPOINTS
                    _TCP_BLOCKED_ENDPOINTS.add(hostname)
                    _LAST_ACTIVE_DRIVER = "neon-serverless-http"
                if first_switch:
                    logger.info(
                        "TCP PostgreSQL port 5432 unreachable for %s (%s); using Neon Serverless HTTPS /sql transport.",
                        hostname,
                        exc,
                    )
                yield _NeonHttpSession(dsn)
                return
            raise

        with _DRIVER_LOCK:
            _LAST_ACTIVE_DRIVER = "psycopg2-tcp"
        try:
            session = _DBSession(conn, is_postgres=True)
            yield session
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            session = _DBSession(conn, is_postgres=False)
            yield session
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Legacy helper returning a raw SQLite connection when operating against local SQLite."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH, force: bool = False) -> None:
    """Create and migrate all portal, audit, prospect, and outreach scheduler tables (uses direct unpooled DSN for Postgres DDL)."""
    dsn = get_database_url(db_path, direct=True)
    cache_key = dsn if dsn else f"sqlite:{Path(db_path).resolve()}"
    if not force:
        with _INIT_LOCK:
            if cache_key in _INITIALIZED_TARGETS:
                return

    is_pg = bool(dsn)
    pk_col = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real_type = "DOUBLE PRECISION" if is_pg else "REAL"

    with _open_session(db_path, direct=True) as s:
        s.execute(
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
        s.execute(
            f"""
            CREATE TABLE IF NOT EXISTS landowner_profiles (
                id {pk_col},
                name TEXT NOT NULL,
                state TEXT NOT NULL,
                county TEXT NOT NULL,
                acreage {real_type} NOT NULL,
                land_type TEXT NOT NULL,
                features TEXT NOT NULL
            )
            """
        )
        s.execute(
            f"""
            CREATE TABLE IF NOT EXISTS client_intakes (
                id {pk_col},
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                county TEXT NOT NULL,
                state TEXT NOT NULL,
                apn TEXT NOT NULL,
                acreage {real_type} NOT NULL,
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
        new_intake_cols = [
            ("agreement_signed", "INTEGER NOT NULL DEFAULT 0"),
            ("signer_full_name", "TEXT NOT NULL DEFAULT ''"),
            ("signer_title", "TEXT NOT NULL DEFAULT ''"),
            ("signature_timestamp_utc", "TEXT NOT NULL DEFAULT ''"),
            ("signer_ip_address", "TEXT NOT NULL DEFAULT ''"),
            ("signer_user_agent", "TEXT NOT NULL DEFAULT ''"),
            ("contract_sha256_hash", "TEXT NOT NULL DEFAULT ''"),
            ("signed_agreement_path", "TEXT NOT NULL DEFAULT ''"),
            ("prospect_id", "TEXT NOT NULL DEFAULT ''"),
            ("ein", "TEXT NOT NULL DEFAULT ''"),
        ]
        if is_pg:
            for col_name, col_ddl in new_intake_cols:
                s.execute(f"ALTER TABLE client_intakes ADD COLUMN IF NOT EXISTS {col_name} {col_ddl}")
        else:
            intake_cols = {
                row["name"] for row in s.execute("PRAGMA table_info(client_intakes)").fetchall()
            }
            for col_name, col_ddl in new_intake_cols:
                if col_name not in intake_cols:
                    s.execute(f"ALTER TABLE client_intakes ADD COLUMN {col_name} {col_ddl}")

        s.execute(
            f"""
            CREATE TABLE IF NOT EXISTS esignature_audit_log (
                id {pk_col},
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
        s.execute(
            f"""
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
                estimated_acreage {real_type} NOT NULL,
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
                registry_source TEXT NOT NULL,
                outreach_segment TEXT NOT NULL DEFAULT 'Direct_Client'
            )
            """
        )
        new_prospect_cols = [
            ("probability_score", "INTEGER NOT NULL DEFAULT 0"),
            ("acreage_score", "INTEGER NOT NULL DEFAULT 0"),
            ("entity_score", "INTEGER NOT NULL DEFAULT 0"),
            ("ecological_score", "INTEGER NOT NULL DEFAULT 0"),
            ("comatch_score", "INTEGER NOT NULL DEFAULT 0"),
            ("score_breakdown", "TEXT NOT NULL DEFAULT ''"),
            ("score_rationale", "TEXT NOT NULL DEFAULT ''"),
            ("outreach_segment", "TEXT NOT NULL DEFAULT 'Direct_Client'"),
        ]
        if is_pg:
            for col_name, col_ddl in new_prospect_cols:
                s.execute(f"ALTER TABLE california_prospects ADD COLUMN IF NOT EXISTS {col_name} {col_ddl}")
        else:
            existing_cols = {
                row["name"] for row in s.execute("PRAGMA table_info(california_prospects)").fetchall()
            }
            for col_name, col_ddl in new_prospect_cols:
                if col_name not in existing_cols:
                    s.execute(f"ALTER TABLE california_prospects ADD COLUMN {col_name} {col_ddl}")

        # Persistent prospect outreach cadence state (never wiped by catalog refreshes)
        s.execute(
            """
            CREATE TABLE IF NOT EXISTS prospect_outreach_status (
                prospect_id TEXT PRIMARY KEY,
                organization_name TEXT NOT NULL DEFAULT '',
                contact_email TEXT NOT NULL DEFAULT '',
                outreach_segment TEXT NOT NULL DEFAULT 'Direct_Client',
                status TEXT NOT NULL DEFAULT 'pending',
                initial_sent_at TEXT NOT NULL DEFAULT '',
                day4_sent_at TEXT NOT NULL DEFAULT '',
                day9_sent_at TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '',
                signed_at TEXT NOT NULL DEFAULT '',
                unsubscribed_at TEXT NOT NULL DEFAULT '',
                last_message_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        s.execute(
            f"""
            CREATE TABLE IF NOT EXISTS outreach_send_log (
                id {pk_col},
                prospect_id TEXT NOT NULL,
                sequence_step TEXT NOT NULL,
                recipient_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                provider_message_id TEXT NOT NULL DEFAULT '',
                sent_at_utc TEXT NOT NULL,
                sent_date_utc TEXT NOT NULL,
                magic_link TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sent',
                error_message TEXT NOT NULL DEFAULT '',
                UNIQUE(prospect_id, sequence_step)
            )
            """
        )
        s.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreach_send_log_date ON outreach_send_log (sent_date_utc)"
        )

    with _INIT_LOCK:
        _INITIALIZED_TARGETS.add(cache_key)


def _sync_intake_to_outreach_status(
    s: _DBSession,
    prospect_id: str,
    email: str,
    is_signed: bool,
    event_ts: str,
) -> None:
    """Update prospect_outreach_status when a landowner claims or signs on the portal."""
    pid = (prospect_id or "").strip()
    em = (email or "").strip().lower()
    if pid:
        if is_signed:
            s.execute(
                """
                UPDATE prospect_outreach_status
                SET status = 'signed',
                    signed_at = CASE WHEN signed_at = '' THEN ? ELSE signed_at END,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    updated_at = ?
                WHERE LOWER(prospect_id) = LOWER(?)
                """,
                (event_ts, event_ts, event_ts, pid),
            )
        else:
            s.execute(
                """
                UPDATE prospect_outreach_status
                SET status = CASE WHEN status = 'signed' THEN 'signed' ELSE 'claimed' END,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    updated_at = ?
                WHERE LOWER(prospect_id) = LOWER(?)
                """,
                (event_ts, event_ts, pid),
            )
    if em:
        if is_signed:
            s.execute(
                """
                UPDATE prospect_outreach_status
                SET status = 'signed',
                    signed_at = CASE WHEN signed_at = '' THEN ? ELSE signed_at END,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    updated_at = ?
                WHERE LOWER(contact_email) = ?
                """,
                (event_ts, event_ts, event_ts, em),
            )
        else:
            s.execute(
                """
                UPDATE prospect_outreach_status
                SET status = CASE WHEN status = 'signed' THEN 'signed' ELSE 'claimed' END,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    updated_at = ?
                WHERE LOWER(contact_email) = ?
                """,
                (event_ts, event_ts, em),
            )


def upsert_grants(grants: Iterable[GrantRecord], db_path: Path | str = DEFAULT_DB_PATH) -> int:
    """Insert or update validated GrantRecord entries into the grants table."""
    init_db(db_path)
    records = list(grants)
    with _open_session(db_path) as s:
        s.executemany(
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
    return len(records)


def seed_landowner_profiles(
    profiles: Iterable[LandownerProfile],
    db_path: Path | str = DEFAULT_DB_PATH,
    reset_existing: bool = True,
) -> int:
    """Populate landowner_profiles with validated LandownerProfile rows."""
    init_db(db_path)
    rows = list(profiles)
    with _open_session(db_path) as s:
        if reset_existing:
            if s.is_postgres:
                s.execute("TRUNCATE TABLE landowner_profiles RESTART IDENTITY")
            else:
                s.execute("DELETE FROM landowner_profiles")
                seq_exists = s.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
                ).fetchone()
                if seq_exists:
                    s.execute("DELETE FROM sqlite_sequence WHERE name = 'landowner_profiles'")
        s.executemany(
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
    return len(rows)


def insert_client_intake(
    intake: ClientIntake,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> ClientIntake:
    """Store a validated ClientIntake submission into the active database and return it with its ID."""
    init_db(db_path)
    ts = intake.submitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig_ts = intake.signature_timestamp_utc or (ts if intake.agreement_signed else "")
    with _open_session(db_path) as s:
        new_id = s.insert_returning_id(
            """
            INSERT INTO client_intakes (
                name, email, county, state, apn, acreage,
                land_condition, project_goals, agreed_to_terms,
                agreement_signed, signer_full_name, signer_title,
                signature_timestamp_utc, signer_ip_address, signer_user_agent,
                contract_sha256_hash, signed_agreement_path, submitted_at,
                prospect_id, ein
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                intake.prospect_id,
                intake.ein,
            ),
        )
        _sync_intake_to_outreach_status(
            s,
            prospect_id=intake.prospect_id,
            email=intake.email,
            is_signed=bool(intake.agreement_signed),
            event_ts=sig_ts or ts,
        )
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
    with _open_session(db_path) as s:
        s.execute(
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
        row = s.execute(
            """
            SELECT id, name, email, county, state, apn, acreage,
                   land_condition, project_goals, agreed_to_terms,
                   agreement_signed, signer_full_name, signer_title,
                   signature_timestamp_utc, signer_ip_address, signer_user_agent,
                   contract_sha256_hash, signed_agreement_path, submitted_at,
                   prospect_id, ein
            FROM client_intakes
            WHERE id = ?
            """,
            (intake_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"ClientIntake #{intake_id} not found in database")
        d = dict(row)
        _sync_intake_to_outreach_status(
            s,
            prospect_id=str(d.get("prospect_id") or ""),
            email=str(d.get("email") or ""),
            is_signed=True,
            event_ts=signature_timestamp_utc,
        )
    d["agreed_to_terms"] = bool(d["agreed_to_terms"])
    d["agreement_signed"] = bool(d["agreement_signed"])
    return ClientIntake(**d)


def fetch_client_intake_by_id(
    intake_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> ClientIntake | None:
    """Fetch a single ClientIntake record by ID from the database."""
    init_db(db_path)
    with _open_session(db_path) as s:
        row = s.execute(
            """
            SELECT id, name, email, county, state, apn, acreage,
                   land_condition, project_goals, agreed_to_terms,
                   agreement_signed, signer_full_name, signer_title,
                   signature_timestamp_utc, signer_ip_address, signer_user_agent,
                   contract_sha256_hash, signed_agreement_path, submitted_at,
                   prospect_id, ein
            FROM client_intakes
            WHERE id = ?
            """,
            (intake_id,),
        ).fetchone()
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
    """Check directly in database whether agreement_signed == True for the given intake_id."""
    if intake_id is None:
        return False
    record = fetch_client_intake_by_id(intake_id, db_path=db_path)
    return bool(record and record.agreement_signed)


def insert_esignature_audit_record(
    record: ESignatureAuditRecord,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> ESignatureAuditRecord:
    """Insert a cryptographic legal audit record into esignature_audit_log."""
    init_db(db_path)
    with _open_session(db_path) as s:
        audit_id = s.insert_returning_id(
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
    return record.model_copy(update={"id": audit_id})


def fetch_all_esignature_audits(
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[ESignatureAuditRecord]:
    """Load all cryptographic e-signature audit records ordered newest-first."""
    init_db(db_path)
    with _open_session(db_path) as s:
        raw_rows = s.execute(
            """
            SELECT id, intake_id, client_name, client_email, county, apn,
                   signer_full_name, signer_title, agreement_version, agreement_signed,
                   signature_timestamp_utc, signer_ip_address, signer_user_agent,
                   contract_sha256_hash, matched_grant_id, matched_grant_title,
                   engineered_grant_request, contingent_fee_dollars, signed_agreement_path
            FROM esignature_audit_log
            ORDER BY id DESC
            """
        ).fetchall()
        rows: list[ESignatureAuditRecord] = []
        for row in raw_rows:
            d = dict(row)
            d["agreement_signed"] = bool(d["agreement_signed"])
            rows.append(ESignatureAuditRecord(**d))
        return rows


def fetch_all_grants(db_path: Path | str = DEFAULT_DB_PATH) -> list[GrantRecord]:
    """Load all stored grants from database as GrantRecord models."""
    init_db(db_path)
    with _open_session(db_path) as s:
        raw_rows = s.execute(
            "SELECT id, title, agency, funding_amount, deadline, eligibility_text, url FROM grants ORDER BY id"
        ).fetchall()
        return [GrantRecord(**dict(row)) for row in raw_rows]


def fetch_all_profiles(db_path: Path | str = DEFAULT_DB_PATH) -> list[LandownerProfile]:
    """Load all landowner profiles from database as LandownerProfile models."""
    init_db(db_path)
    with _open_session(db_path) as s:
        raw_rows = s.execute(
            "SELECT id, name, state, county, acreage, land_type, features FROM landowner_profiles ORDER BY id"
        ).fetchall()
        return [LandownerProfile(**dict(row)) for row in raw_rows]


def fetch_all_intakes(db_path: Path | str = DEFAULT_DB_PATH) -> list[ClientIntake]:
    """Load all client intake submissions from database."""
    init_db(db_path)
    with _open_session(db_path) as s:
        raw_rows = s.execute(
            """
            SELECT id, name, email, county, state, apn, acreage,
                   land_condition, project_goals, agreed_to_terms,
                   agreement_signed, signer_full_name, signer_title,
                   signature_timestamp_utc, signer_ip_address, signer_user_agent,
                   contract_sha256_hash, signed_agreement_path, submitted_at,
                   prospect_id, ein
            FROM client_intakes
            ORDER BY id DESC
            """
        ).fetchall()
        rows: list[ClientIntake] = []
        for row in raw_rows:
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
    """Persist verified Tier-1 California prospects into california_prospects without touching outreach history."""
    init_db(db_path)
    rows = list(prospects)
    with _open_session(db_path) as s:
        if reset_existing:
            s.execute("DELETE FROM california_prospects")
        s.executemany(
            """
            INSERT INTO california_prospects (
                prospect_id, organization_name, category, county, service_counties,
                city, state, contact_email, phone, website, ein,
                estimated_acreage, land_type, conservation_focus,
                top_matched_grant, matched_grant_deadline,
                probability_score, acreage_score, entity_score, ecological_score,
                comatch_score, score_breakdown, score_rationale,
                personalized_intake_url, registry_source, outreach_segment
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                registry_source = excluded.registry_source,
                outreach_segment = excluded.outreach_segment
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
                    p.outreach_segment,
                )
                for p in rows
            ],
        )
    return len(rows)


def fetch_all_prospects(db_path: Path | str = DEFAULT_DB_PATH) -> list[ProspectRecord]:
    """Load all verified California prospects ranked highest probability score to lowest."""
    init_db(db_path)
    with _open_session(db_path) as s:
        raw_rows = s.execute(
            """
            SELECT prospect_id, organization_name, category, county, service_counties,
                   city, state, contact_email, phone, website, ein,
                   estimated_acreage, land_type, conservation_focus,
                   top_matched_grant, matched_grant_deadline,
                   probability_score, acreage_score, entity_score, ecological_score,
                   comatch_score, score_breakdown, score_rationale,
                   personalized_intake_url, registry_source, outreach_segment
            FROM california_prospects
            ORDER BY probability_score DESC, estimated_acreage DESC, prospect_id ASC
            """
        ).fetchall()
        return [ProspectRecord(**dict(row)) for row in raw_rows]


def fetch_prospect_by_id(prospect_id: str, db_path: Path | str = DEFAULT_DB_PATH) -> ProspectRecord | None:
    """Look up a single ProspectRecord by prospect_id (case-insensitive)."""
    pid = (prospect_id or "").strip()
    if not pid:
        return None
    init_db(db_path)
    with _open_session(db_path) as s:
        row = s.execute(
            """
            SELECT prospect_id, organization_name, category, county, service_counties,
                   city, state, contact_email, phone, website, ein,
                   estimated_acreage, land_type, conservation_focus,
                   top_matched_grant, matched_grant_deadline,
                   probability_score, acreage_score, entity_score, ecological_score,
                   comatch_score, score_breakdown, score_rationale,
                   personalized_intake_url, registry_source, outreach_segment
            FROM california_prospects
            WHERE LOWER(prospect_id) = LOWER(?)
            """,
            (pid,),
        ).fetchone()
        return ProspectRecord(**dict(row)) if row else None


# ---------------------------------------------------------------------------
# Outreach Scheduling, Claim/Sign Suppression & Daily Warm-Up Rate Limiting
# ---------------------------------------------------------------------------

def sync_outreach_status_from_prospects(
    prospects: Iterable[ProspectRecord],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Seed missing prospect_outreach_status rows without overwriting send timestamps or signed status."""
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = list(prospects)
    with _open_session(db_path) as s:
        s.executemany(
            """
            INSERT INTO prospect_outreach_status (
                prospect_id, organization_name, contact_email, outreach_segment, status, updated_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(prospect_id) DO UPDATE SET
                organization_name = excluded.organization_name,
                contact_email = excluded.contact_email,
                outreach_segment = excluded.outreach_segment
            """,
            [
                (
                    p.prospect_id,
                    p.organization_name,
                    p.contact_email.lower(),
                    p.outreach_segment,
                    now_iso,
                )
                for p in items
            ],
        )
        # Reconcile any historical client_intakes into prospect_outreach_status
        intake_rows = s.execute(
            """
            SELECT prospect_id, email, agreement_signed, submitted_at, signature_timestamp_utc
            FROM client_intakes
            """
        ).fetchall()
        for ir in intake_rows:
            d = dict(ir)
            is_signed = bool(d.get("agreement_signed"))
            ev_ts = str(d.get("signature_timestamp_utc") or d.get("submitted_at") or now_iso)
            _sync_intake_to_outreach_status(
                s,
                prospect_id=str(d.get("prospect_id") or ""),
                email=str(d.get("email") or ""),
                is_signed=is_signed,
                event_ts=ev_ts,
            )
    return len(items)


def fetch_outreach_statuses(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, dict[str, Any]]:
    """Return mapping {prospect_id: status_dict} from prospect_outreach_status."""
    init_db(db_path)
    with _open_session(db_path) as s:
        raw_rows = s.execute(
            """
            SELECT prospect_id, organization_name, contact_email, outreach_segment, status,
                   initial_sent_at, day4_sent_at, day9_sent_at, claimed_at, signed_at,
                   unsubscribed_at, last_message_id, last_error, updated_at
            FROM prospect_outreach_status
            ORDER BY prospect_id ASC
            """
        ).fetchall()
        return {str(row["prospect_id"]): dict(row) for row in raw_rows}


def fetch_claimed_and_signed_identities(db_path: Path | str = DEFAULT_DB_PATH) -> tuple[set[str], set[str]]:
    """Return (claimed_or_signed_prospect_ids_upper, claimed_or_signed_emails_lower)."""
    init_db(db_path)
    ids: set[str] = set()
    emails: set[str] = set()
    with _open_session(db_path) as s:
        for row in s.execute("SELECT prospect_id, email FROM client_intakes").fetchall():
            d = dict(row)
            if str(d.get("prospect_id") or "").strip():
                ids.add(str(d["prospect_id"]).strip().upper())
            if str(d.get("email") or "").strip():
                emails.add(str(d["email"]).strip().lower())
        for row in s.execute(
            """
            SELECT prospect_id, contact_email
            FROM prospect_outreach_status
            WHERE status IN ('claimed', 'signed', 'unsubscribed', 'bounced', 'completed')
               OR claimed_at != '' OR signed_at != '' OR unsubscribed_at != ''
            """
        ).fetchall():
            d = dict(row)
            if str(d.get("prospect_id") or "").strip():
                ids.add(str(d["prospect_id"]).strip().upper())
            if str(d.get("contact_email") or "").strip():
                emails.add(str(d["contact_email"]).strip().lower())
    return ids, emails


def get_daily_send_count_utc(date_utc: str | None = None, db_path: Path | str = DEFAULT_DB_PATH) -> int:
    """Count emails sent/reserved today in UTC for warm-up rate limiting."""
    target_date = date_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    init_db(db_path)
    with _open_session(db_path) as s:
        row = s.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM outreach_send_log
            WHERE sent_date_utc = ? AND status IN ('sent', 'sending')
            """,
            (target_date,),
        ).fetchone()
        return int(dict(row)["cnt"]) if row else 0


def fetch_sent_steps_map(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, set[str]]:
    """Return mapping {prospect_id: {'day0_initial', 'day4_followup', ...}} already sent or in flight."""
    init_db(db_path)
    result: dict[str, set[str]] = {}
    with _open_session(db_path) as s:
        raw_rows = s.execute(
            "SELECT prospect_id, sequence_step FROM outreach_send_log WHERE status IN ('sent', 'sending')"
        ).fetchall()
        for row in raw_rows:
            d = dict(row)
            result.setdefault(str(d["prospect_id"]), set()).add(str(d["sequence_step"]))
    return result


def reserve_outreach_send(
    prospect_id: str,
    sequence_step: str,
    recipient_email: str,
    subject: str,
    provider: str,
    sent_at_utc: str,
    sent_date_utc: str,
    magic_link: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int | None:
    """Atomically claim the unique (prospect_id, sequence_step) lock before sending an email.

    Returns log_id if reserved, or None if this prospect+step was already sent or reserved.
    """
    init_db(db_path)
    with _open_session(db_path) as s:
        if s.is_postgres:
            row = s.execute(
                """
                INSERT INTO outreach_send_log (
                    prospect_id, sequence_step, recipient_email, subject,
                    provider, provider_message_id, sent_at_utc, sent_date_utc,
                    magic_link, status, error_message
                )
                VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, 'sending', '')
                ON CONFLICT(prospect_id, sequence_step) DO NOTHING
                RETURNING id
                """,
                (
                    prospect_id,
                    sequence_step,
                    recipient_email.lower(),
                    subject,
                    provider,
                    sent_at_utc,
                    sent_date_utc,
                    magic_link,
                ),
            ).fetchone()
            return int(row["id"]) if row else None
        cur = s.execute(
            """
            INSERT INTO outreach_send_log (
                prospect_id, sequence_step, recipient_email, subject,
                provider, provider_message_id, sent_at_utc, sent_date_utc,
                magic_link, status, error_message
            )
            VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, 'sending', '')
            ON CONFLICT(prospect_id, sequence_step) DO NOTHING
            """,
            (
                prospect_id,
                sequence_step,
                recipient_email.lower(),
                subject,
                provider,
                sent_at_utc,
                sent_date_utc,
                magic_link,
            ),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid)


def finalize_outreach_send(
    log_id: int,
    prospect_id: str,
    sequence_step: str,
    provider: str,
    provider_message_id: str,
    sent_at_utc: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Mark an outreach send confirmed and advance prospect_outreach_status timestamps."""
    init_db(db_path)
    step_col_map = {
        "day0_initial": ("initial_sent_at", "day0_sent"),
        "day4_followup": ("day4_sent_at", "day4_sent"),
        "day9_followup": ("day9_sent_at", "completed"),
    }
    if sequence_step not in step_col_map:
        raise ValueError(f"Unsupported sequence_step: {sequence_step}")
    ts_col, next_status = step_col_map[sequence_step]

    with _open_session(db_path) as s:
        s.execute(
            """
            UPDATE outreach_send_log
            SET status = 'sent', provider = ?, provider_message_id = ?, sent_at_utc = ?
            WHERE id = ?
            """,
            (provider, provider_message_id, sent_at_utc, log_id),
        )
        s.execute(
            f"""
            UPDATE prospect_outreach_status
            SET {ts_col} = ?,
                status = CASE
                    WHEN status IN ('claimed', 'signed', 'unsubscribed', 'bounced') THEN status
                    ELSE ?
                END,
                last_message_id = ?,
                last_error = '',
                updated_at = ?
            WHERE prospect_id = ?
            """,
            (sent_at_utc, next_status, provider_message_id, sent_at_utc, prospect_id),
        )


def release_outreach_reservation(
    log_id: int,
    prospect_id: str,
    error_message: str,
    updated_at_utc: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Release an unconfirmed 'sending' reservation when provider API fails prior to send."""
    init_db(db_path)
    with _open_session(db_path) as s:
        s.execute(
            "DELETE FROM outreach_send_log WHERE id = ? AND status = 'sending'",
            (log_id,),
        )
        s.execute(
            """
            UPDATE prospect_outreach_status
            SET last_error = ?, updated_at = ?
            WHERE prospect_id = ?
            """,
            (error_message[:500], updated_at_utc, prospect_id),
        )


def mark_prospect_outreach_state(
    prospect_id: str,
    status: str,
    timestamp_utc: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> bool:
    """Manually mark a prospect status (e.g., unsubscribed, bounced, claimed, signed)."""
    allowed = {"pending", "claimed", "signed", "unsubscribed", "bounced", "completed"}
    norm_status = status.strip().lower()
    if norm_status not in allowed:
        raise ValueError(f"Invalid outreach status '{status}'. Must be one of: {sorted(allowed)}")
    ts = timestamp_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    init_db(db_path)
    with _open_session(db_path) as s:
        extra_col = ""
        if norm_status == "unsubscribed":
            extra_col = ", unsubscribed_at = ?"
        elif norm_status == "claimed":
            extra_col = ", claimed_at = ?"
        elif norm_status == "signed":
            extra_col = ", signed_at = ?"

        if extra_col:
            cur = s.execute(
                f"UPDATE prospect_outreach_status SET status = ?, updated_at = ? {extra_col} WHERE LOWER(prospect_id) = LOWER(?)",
                (norm_status, ts, ts, prospect_id),
            )
        else:
            cur = s.execute(
                "UPDATE prospect_outreach_status SET status = ?, updated_at = ? WHERE LOWER(prospect_id) = LOWER(?)",
                (norm_status, ts, prospect_id),
            )
        return cur.rowcount > 0


def fetch_outreach_send_logs(limit: int = 100, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return recent outreach send log records ordered newest-first."""
    init_db(db_path)
    with _open_session(db_path) as s:
        rows = s.execute(
            """
            SELECT id, prospect_id, sequence_step, recipient_email, subject,
                   provider, provider_message_id, sent_at_utc, sent_date_utc,
                   magic_link, status, error_message
            FROM outreach_send_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [dict(r) for r in rows]
