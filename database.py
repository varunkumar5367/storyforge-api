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

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

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
        self.row_factory = None

    async def __aenter__(self):
        if self.is_postgres:
            if psycopg is None:
                raise ImportError("PostgreSQL connection requested, but 'psycopg' package is not installed.")
            self.conn = await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row)
        else:
            self.conn = await aiosqlite.connect(self.database_url)
            self.conn.row_factory = aiosqlite.Row
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            await self.conn.close()

    def execute(self, query: str, parameters=None):
        return CursorContextManager(self, query, parameters, self.is_postgres)

    async def commit(self):
        if self.conn:
            await self.conn.commit()

    async def close(self):
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
