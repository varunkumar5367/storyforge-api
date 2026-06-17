"""
database.py — Dynamic connection pool and schema initialisation.
Supports both SQLite (local development) and PostgreSQL (production).
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import aiosqlite
from dotenv import load_dotenv
import hashlib
import binascii

load_dotenv()
logger = logging.getLogger("storyforge.database")

DATABASE_URL: str = os.getenv("DATABASE_URL", "./storyforge.db")

import asyncio
try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:
    psycopg = None
    dict_row = None
    AsyncConnectionPool = None

_pool: AsyncConnectionPool | None = None
_pool_lock = asyncio.Lock()

async def get_pg_pool() -> AsyncConnectionPool | None:
    global _pool
    if psycopg is None or AsyncConnectionPool is None:
        return None
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = AsyncConnectionPool(
                    conninfo=DATABASE_URL,
                    open=False,
                    min_size=1,
                    max_size=10,
                    kwargs={"row_factory": dict_row, "prepare_threshold": None}
                )
                await _pool.open()
                logger.info("PostgreSQL connection pool initialized with prepared statements disabled.")
    return _pool

async def close_db() -> None:
    """Close the database connection pool (PostgreSQL only)."""
    global _pool
    if _pool is not None:
        logger.info("Closing PostgreSQL connection pool...")
        await _pool.close()
        _pool = None


def hash_password(password: str) -> str:
    salt = "default_salt_storyforge_2026"
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    )
    return f"{salt}${binascii.hexlify(pwd_hash).decode('utf-8')}"

def verify_password(password: str, hashed: str) -> bool:
    try:
        salt, val = hashed.split("$", 1)
        pwd_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
        )
        return binascii.hexlify(pwd_hash).decode("utf-8") == val
    except Exception:
        return False

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'user',
    created_at     TEXT NOT NULL,
    full_name      TEXT NOT NULL DEFAULT '',
    display_name   TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL DEFAULT '',
    phone          TEXT NOT NULL DEFAULT '',
    dob            TEXT NOT NULL DEFAULT '',
    avatar_data    TEXT NOT NULL DEFAULT '',
    pollen_balance REAL NOT NULL DEFAULT 20.0,
    last_seen      TEXT NOT NULL DEFAULT '',
    is_active      INTEGER NOT NULL DEFAULT 1
);
"""

CREATE_POLLEN_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS pollen_requests (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    amount       REAL NOT NULL,
    message      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    reviewed_at  TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
"""

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL DEFAULT 'pending',
    progress_percent  INTEGER NOT NULL DEFAULT 0,
    current_step      TEXT,
    story_text        TEXT,
    story_filename    TEXT,
    character_memory  TEXT,   -- JSON blob
    scenes            TEXT,   -- JSON blob
    created_at        TEXT NOT NULL,
    completed_at      TEXT,
    error_message     TEXT,
    download_urls     TEXT,   -- JSON blob set on pipeline completion
    voice             TEXT NOT NULL DEFAULT 'en-US-JennyNeural',
    logs              TEXT    -- JSON list of log strings
);
"""

CREATE_ANALYTICS_RENDERS_TABLE = """
CREATE TABLE IF NOT EXISTS analytics_renders (
    id                TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    user_id           TEXT,
    username          TEXT,
    total_duration    REAL NOT NULL,
    step_durations    TEXT, -- JSON
    peak_memory_mb    REAL NOT NULL,
    status            TEXT NOT NULL,
    error_message     TEXT,
    ffmpeg_cmd        TEXT,
    ffmpeg_stderr     TEXT,
    credit_consumed   REAL NOT NULL DEFAULT 0.0,
    created_at        TEXT NOT NULL
);
"""

CREATE_ANALYTICS_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS analytics_events (
    id           TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    user_id      TEXT,
    username     TEXT,
    metadata     TEXT, -- JSON
    created_at   TEXT NOT NULL
);
"""

CREATE_SERVER_STATUS_TABLE = """
CREATE TABLE IF NOT EXISTS server_status (
    id                   TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'offline',
    tunnel_url           TEXT,
    last_ping            TEXT NOT NULL,
    max_concurrent_tasks INTEGER NOT NULL DEFAULT 1,
    max_concurrent_users INTEGER NOT NULL DEFAULT 5,
    active_tasks         INTEGER NOT NULL DEFAULT 0,
    active_users         INTEGER NOT NULL DEFAULT 0,
    cpu_usage            REAL NOT NULL DEFAULT 0.0,
    ram_usage            REAL NOT NULL DEFAULT 0.0
);
"""

CREATE_WAKE_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS wake_requests (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    message     TEXT,
    created_at  TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Compatibility Layer
# ---------------------------------------------------------------------------
def translate_query(query: str, is_postgres: bool) -> str:
    """Translates query placeholder and SQLite PRAGMAs to Postgres counterparts."""
    if not is_postgres:
        return query
    
    # Intercept SQLite schema checks
    if "PRAGMA table_info" in query:
        m = re.search(r"table_info\((\w+)\)", query)
        if m:
            table_name = m.group(1)
            return f"SELECT column_name AS name FROM information_schema.columns WHERE table_name = '{table_name}'"
            
    # Swap parameter placeholders
    return query.replace("?", "%s")

class DatabaseCursor:
    def __init__(self, cursor, is_postgres: bool):
        self.cursor = cursor
        self.is_postgres = is_postgres

    async def fetchall(self):
        return await self.cursor.fetchall()

    async def fetchone(self):
        row = await self.cursor.fetchone()
        if self.is_postgres and row is not None:
            # psycopg dict_row yields plain dicts. Let's make sure we return it.
            return row
        return row

    @property
    def rowcount(self):
        return self.cursor.rowcount

    async def close(self):
        if hasattr(self.cursor, "close"):
            import inspect
            if inspect.iscoroutinefunction(self.cursor.close):
                await self.cursor.close()
            else:
                self.cursor.close()

class CursorContextManager:
    def __init__(self, conn, query: str, parameters, is_postgres: bool):
        self.conn = conn
        self.query = query
        self.parameters = parameters
        self.is_postgres = is_postgres
        self.cursor = None

    def __await__(self):
        async def _execute():
            translated = translate_query(self.query, self.is_postgres)
            if self.is_postgres:
                cur = await self.conn.conn.execute(translated, self.parameters)
                self.cursor = DatabaseCursor(cur, self.is_postgres)
                return self.cursor
            else:
                cur = await self.conn.conn.execute(self.query, self.parameters or ())
                self.cursor = DatabaseCursor(cur, self.is_postgres)
                return self.cursor
        return _execute().__await__()

    async def __aenter__(self):
        return await self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.cursor:
            await self.cursor.close()

class DatabaseConnection:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.is_postgres = database_url.startswith("postgresql://") or database_url.startswith("postgres://")
        self.conn = None
        self.conn_ctx = None
        self.row_factory = None

    async def __aenter__(self):
        if self.is_postgres:
            if psycopg is None:
                raise ImportError("PostgreSQL connection requested, but 'psycopg' package is not installed.")
            pool = await get_pg_pool()
            if pool is None:
                raise ImportError("PostgreSQL connection requested, but connection pool could not be initialized.")
            self.conn_ctx = pool.connection()
            self.conn = await self.conn_ctx.__aenter__()
        else:
            self.conn = await aiosqlite.connect(self.database_url)
            self.conn.row_factory = aiosqlite.Row
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.is_postgres:
            if self.conn_ctx:
                await self.conn_ctx.__aexit__(exc_type, exc_val, exc_tb)
        else:
            if self.conn:
                await self.conn.close()

    def execute(self, query: str, parameters=None):
        return CursorContextManager(self, query, parameters, self.is_postgres)

    async def commit(self):
        if self.conn:
            await self.conn.commit()

    async def close(self):
        if self.is_postgres:
            if self.conn_ctx:
                await self.conn_ctx.__aexit__(None, None, None)
        else:
            if self.conn:
                await self.conn.close()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def get_db() -> DatabaseConnection:
    """Open and return a new DatabaseConnection context manager."""
    return DatabaseConnection(DATABASE_URL)


async def init_db() -> None:
    """Create tables if they do not already exist and run schema migrations."""
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(CREATE_JOBS_TABLE)
        await db.execute(CREATE_USERS_TABLE)
        await db.execute(CREATE_POLLEN_REQUESTS_TABLE)
        await db.execute(CREATE_ANALYTICS_RENDERS_TABLE)
        await db.execute(CREATE_ANALYTICS_EVENTS_TABLE)
        await db.execute(CREATE_SERVER_STATUS_TABLE)
        await db.execute(CREATE_WAKE_REQUESTS_TABLE)
        await db.commit()

        # Seed default server_status row if not exists
        async with db.execute("SELECT * FROM server_status WHERE id = ?", ("current",)) as cur:
            row = await cur.fetchone()
            if not row:
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    """
                    INSERT INTO server_status (id, status, last_ping)
                    VALUES (?, ?, ?)
                    """,
                    ("current", "offline", now),
                )
                await db.commit()

        # Schema migrations: check columns for users table
        db.row_factory = aiosqlite.Row
        async with db.execute("PRAGMA table_info(users)") as cur:
            user_columns = [row["name"] for row in await cur.fetchall()]

        users_migrations = [
            ("full_name", "ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''"),
            ("display_name", "ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"),
            ("email", "ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''"),
            ("phone", "ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''"),
            ("dob", "ALTER TABLE users ADD COLUMN dob TEXT NOT NULL DEFAULT ''"),
            ("avatar_data", "ALTER TABLE users ADD COLUMN avatar_data TEXT NOT NULL DEFAULT ''"),
            ("pollen_balance", "ALTER TABLE users ADD COLUMN pollen_balance REAL NOT NULL DEFAULT 20.0"),
            ("last_seen", "ALTER TABLE users ADD COLUMN last_seen TEXT NOT NULL DEFAULT ''"),
            ("is_active", "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        ]

        for col, query in users_migrations:
            if col not in user_columns:
                logger.info("Migrating database: adding '%s' column to 'users' table.", col)
                await db.execute(query)
                await db.commit()

        # Schema migrations: check columns
        async with db.execute("PRAGMA table_info(jobs)") as cur:
            columns = [row["name"] for row in await cur.fetchall()]

        if "download_urls" not in columns:
            logger.info("Migrating database: adding 'download_urls' column to 'jobs' table.")
            await db.execute("ALTER TABLE jobs ADD COLUMN download_urls TEXT")
            await db.commit()

        if "voice" not in columns:
            logger.info("Migrating database: adding 'voice' column to 'jobs' table.")
            await db.execute("ALTER TABLE jobs ADD COLUMN voice TEXT NOT NULL DEFAULT 'en-US-JennyNeural'")
            await db.commit()

        if "logs" not in columns:
            logger.info("Migrating database: adding 'logs' column to 'jobs' table.")
            await db.execute("ALTER TABLE jobs ADD COLUMN logs TEXT")
            await db.commit()

        if "user_id" not in columns:
            logger.info("Migrating database: adding 'user_id' column to 'jobs' table.")
            await db.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT")
            await db.commit()

        # Seed admin user if not exists
        async with db.execute("SELECT * FROM users WHERE username = ?", ("varun5367",)) as cur:
            row = await cur.fetchone()
            if not row:
                import uuid
                user_id = str(uuid.uuid4())
                pwd_hash = hash_password("Varun@5367")
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    """
                    INSERT INTO users (id, username, password_hash, role, created_at)
                    VALUES (?, ?, ?, 'admin', ?)
                    """,
                    (user_id, "varun5367", pwd_hash, now),
                )
                await db.commit()
                logger.info("Admin user 'varun5367' seeded successfully.")

    logger.info("Database schema initialised at '%s'.", DATABASE_URL)


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------
async def create_job(job_id: str, story_text: str, story_filename: str, voice: str = "en-US-JennyNeural", user_id: str | None = None) -> dict:
    """Insert a new job row and return it as a dict."""
    now = datetime.now(timezone.utc).isoformat()
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO jobs
                (id, status, progress_percent, current_step,
                 story_text, story_filename, created_at, voice, user_id)
            VALUES (?, 'pending', 0, 'queued', ?, ?, ?, ?, ?)
            """,
            (job_id, story_text, story_filename, now, voice, user_id),
        )
        await db.commit()
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)


async def get_job(job_id: str) -> dict | None:
    """Fetch a single job by ID. Returns None if not found."""
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_job(job_id: str, **fields) -> None:
    """
    Update arbitrary columns on a job row.
    Pass keyword arguments matching column names.
    JSON-serialisable objects (dict/list) are automatically serialised.
    """
    if not fields:
        return

    serialised = {}
    for k, v in fields.items():
        serialised[k] = json.dumps(v) if isinstance(v, (dict, list)) else v

    set_clause = ", ".join(f"{k} = ?" for k in serialised)
    values = list(serialised.values()) + [job_id]

    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            f"UPDATE jobs SET {set_clause} WHERE id = ?", values  # noqa: S608
        )
        await db.commit()


async def list_jobs(limit: int = 50, user_id: str | None = None) -> list[dict]:
    """Return the most-recent N jobs ordered by creation time, optionally filtered by user_id."""
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            query = """
                SELECT j.*, u.username 
                FROM jobs j 
                LEFT JOIN users u ON j.user_id = u.id 
                WHERE j.user_id = ? 
                ORDER BY j.created_at DESC LIMIT ?
            """
            params = (user_id, limit)
        else:
            query = """
                SELECT j.*, u.username 
                FROM jobs j 
                LEFT JOIN users u ON j.user_id = u.id 
                ORDER BY j.created_at DESC LIMIT ?
            """
            params = (limit,)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_job(job_id: str) -> bool:
    """Delete a job by ID from the database. Returns True if row was deleted."""
    async with DatabaseConnection(DATABASE_URL) as db:
        async with db.execute("DELETE FROM jobs WHERE id = ?", (job_id,)) as cur:
            rowcount = cur.rowcount
            await db.commit()
            return rowcount > 0


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
async def get_user_by_username(username: str) -> dict | None:
    """Fetch user by username. Returns None if not found."""
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE username = ?", (username,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_user(user_id: str, username: str, password_hash: str, role: str = "user") -> dict:
    """Insert a new user row and return it as a dict."""
    now = datetime.now(timezone.utc).isoformat()
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO users (id, username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, username, password_hash, role, now),
        )
        await db.commit()
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)


async def list_users() -> list[dict]:
    """Return all users in the system."""
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_user(user_id: str) -> bool:
    """Delete a user by ID from the database. Returns True if row was deleted."""
    async with DatabaseConnection(DATABASE_URL) as db:
        async with db.execute("DELETE FROM users WHERE id = ?", (user_id,)) as cur:
            rowcount = cur.rowcount
            await db.commit()
            return rowcount > 0


async def count_user_images_last_hour(user_id: str) -> int:
    """Count total scenes (images) generated by user in the last 60 minutes."""
    import json
    from datetime import datetime, timedelta, timezone
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    
    total_images = 0
    async with DatabaseConnection(DATABASE_URL) as db:
        async with db.execute(
            "SELECT scenes FROM jobs WHERE user_id = ? AND created_at > ? AND status != 'failed'",
            (user_id, one_hour_ago),
        ) as cur:
            rows = await cur.fetchall()
            for row in rows:
                scenes_str = row["scenes"]
                if scenes_str:
                    try:
                        scenes = json.loads(scenes_str)
                        total_images += len(scenes)
                    except Exception:
                        pass
    return total_images


async def get_user_by_id(user_id: str) -> dict | None:
    """Fetch user by id. Returns None if not found."""
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_user_profile(user_id: str, **fields) -> None:
    """Update profile information columns on a user row."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?", values  # noqa: S608
        )
        await db.commit()


async def update_user_password(user_id: str, password_hash: str) -> None:
    """Update password hash on a user row."""
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        await db.commit()


async def create_pollen_request(request_id: str, user_id: str, amount: float, message: str) -> dict:
    """Insert a new pollen credit request row and return it as a dict."""
    now = datetime.now(timezone.utc).isoformat()
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            """
            INSERT INTO pollen_requests (id, user_id, amount, message, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (request_id, user_id, amount, message, now),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pollen_requests WHERE id = ?", (request_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)


async def list_user_pollen_requests(user_id: str) -> list[dict]:
    """Retrieve all pollen requests submitted by a specific user (newest first)."""
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pollen_requests WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def list_all_pollen_requests() -> list[dict]:
    """Retrieve all pollen requests in the system with username join (newest first)."""
    async with DatabaseConnection(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT pr.*, u.username 
            FROM pollen_requests pr
            LEFT JOIN users u ON pr.user_id = u.id
            ORDER BY pr.created_at DESC
            """
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def review_pollen_request(request_id: str, status: str) -> dict | None:
    """Update a pollen request status (approved/denied)."""
    now = datetime.now(timezone.utc).isoformat()
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            "UPDATE pollen_requests SET status = ?, reviewed_at = ? WHERE id = ?",
            (status, now, request_id),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pollen_requests WHERE id = ?", (request_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_user_pollen_balance(user_id: str, amount: float) -> None:
    """Set the custom pollen balance limit for a user."""
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute("UPDATE users SET pollen_balance = ? WHERE id = ?", (amount, user_id))
        await db.commit()


async def save_render_analytics(
    job_id: str,
    user_id: str | None,
    total_duration: float,
    step_durations: dict,
    peak_memory_mb: float,
    status: str,
    error_message: str | None = None,
    ffmpeg_cmd: str | None = None,
    ffmpeg_stderr: str | None = None,
    credit_consumed: float = 0.0,
) -> None:
    """Insert a new render analytics entry into the database."""
    import uuid
    from database import get_user_by_id
    
    # Try to fetch username
    username = None
    if user_id:
        try:
            user = await get_user_by_id(user_id)
            if user:
                username = user.get("username")
        except Exception:
            pass
            
    now = datetime.now(timezone.utc).isoformat()
    row_id = str(uuid.uuid4())
    
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            """
            INSERT INTO analytics_renders (
                id, job_id, user_id, username, total_duration, step_durations,
                peak_memory_mb, status, error_message, ffmpeg_cmd, ffmpeg_stderr,
                credit_consumed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                job_id,
                user_id,
                username,
                total_duration,
                json.dumps(step_durations),
                peak_memory_mb,
                status,
                error_message,
                ffmpeg_cmd,
                ffmpeg_stderr,
                credit_consumed,
                now,
            ),
        )
        await db.commit()
    logger.info("Saved render analytics for job %s.", job_id)


async def save_analytics_event(
    event_type: str,
    user_id: str | None = None,
    username: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Insert a new user activity event into the database."""
    import uuid
    row_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            """
            INSERT INTO analytics_events (id, event_type, user_id, username, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                event_type,
                user_id,
                username,
                json.dumps(metadata or {}),
                now,
            ),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Server Status & Wake Requests Helpers
# ---------------------------------------------------------------------------
async def get_server_status() -> dict:
    """Retrieve the current server status and configuration settings."""
    async with DatabaseConnection(DATABASE_URL) as db:
        async with db.execute("SELECT * FROM server_status WHERE id = ?", ("current",)) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
            # Safe fallback if row does not exist
            return {
                "id": "current",
                "status": "offline",
                "tunnel_url": None,
                "last_ping": datetime.now(timezone.utc).isoformat(),
                "max_concurrent_tasks": 1,
                "max_concurrent_users": 5,
                "active_tasks": 0,
                "active_users": 0,
                "cpu_usage": 0.0,
                "ram_usage": 0.0
            }

async def update_server_status(**fields) -> None:
    """Update server status, health metrics, and settings fields."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + ["current"]
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            f"UPDATE server_status SET {set_clause} WHERE id = ?", values
        )
        await db.commit()

async def create_wake_request(message: str | None = None) -> dict:
    """Create a new wake request with status = 'pending'."""
    import uuid
    request_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    async with DatabaseConnection(DATABASE_URL) as db:
        await db.execute(
            """
            INSERT INTO wake_requests (id, status, message, created_at)
            VALUES (?, 'pending', ?, ?)
            """,
            (request_id, message, now)
        )
        await db.commit()
        async with db.execute("SELECT * FROM wake_requests WHERE id = ?", (request_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)

async def list_wake_requests(limit: int = 50) -> list[dict]:
    """Retrieve the most recent wake requests (newest first)."""
    async with DatabaseConnection(DATABASE_URL) as db:
        if not db.is_postgres:
            db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM wake_requests ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def review_wake_request(request_id: str, status: str) -> bool:
    """Approve/accept or ignore a wake request."""
    async with DatabaseConnection(DATABASE_URL) as db:
        async with db.execute(
            "UPDATE wake_requests SET status = ? WHERE id = ? AND status = 'pending'",
            (status, request_id)
        ) as cur:
            rowcount = cur.rowcount
            await db.commit()
            return rowcount > 0

async def get_average_scene_duration() -> float:
    """Calculate the average duration (in seconds) to render a single scene based on completed renders."""
    async with DatabaseConnection(DATABASE_URL) as db:
        async with db.execute(
            "SELECT SUM(total_duration) as total_dur, SUM(credit_consumed) as total_credits FROM analytics_renders WHERE status = 'completed' AND credit_consumed > 0"
        ) as cur:
            row = await cur.fetchone()
            if row and row["total_dur"] is not None and row["total_credits"] is not None and row["total_credits"] > 0:
                return float(row["total_dur"] / row["total_credits"])
            return 45.0  # Safe default: 45 seconds per scene


