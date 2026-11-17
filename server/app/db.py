"""SQLite storage layer (aiosqlite, WAL, plain SQL).

Schema is created idempotently at startup with ``CREATE TABLE IF NOT EXISTS``.
One connection is shared for the whole process; aiosqlite serialises access on a
worker thread, which is plenty for this workload and keeps write ordering simple.

Multi-user isolation is enforced in the schema where it matters: ``sessions`` and
``client_files`` use a composite ``(user_id, id)`` primary key, so a user can
never overwrite or read another user's row even with a guessed id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

logger = logging.getLogger("dgxchat.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    username_key  TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked      INTEGER NOT NULL DEFAULT 0,
    label        TEXT,
    expires_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

CREATE TABLE IF NOT EXISTS sessions (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id         TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_updated ON sessions(user_id, updated_at DESC);

-- denormalised listing table kept in sync with sessions (§2.5)
CREATE TABLE IF NOT EXISTS session_summaries (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id         TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_summaries_user_updated
    ON session_summaries(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS files (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL DEFAULT '',
    mime_type  TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    purpose    TEXT,
    sha256     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id);

-- client-local attachment ids mirrored to the cloud (§2.3 /api/files/{local_id})
CREATE TABLE IF NOT EXISTS client_files (
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    local_id     TEXT NOT NULL,
    mime_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, local_id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id    TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data       TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL
);

-- invite-only registration
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    max_uses   INTEGER NOT NULL DEFAULT 0,
    used_count INTEGER NOT NULL DEFAULT 0,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_invites_created ON invites(created_at DESC);

-- per-user daily counters (TTS characters, ASR calls, ...)
CREATE TABLE IF NOT EXISTS daily_counters (
    user_id TEXT NOT NULL,
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    amount  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, kind)
);

-- deletion tombstones so other devices can drop sessions deleted elsewhere
CREATE TABLE IF NOT EXISTS deleted_sessions (
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    deleted_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_deleted_sessions_time
    ON deleted_sessions(user_id, deleted_at);

-- admin audit trail
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   TEXT,
    actor_name TEXT,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    ip         TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    endpoint          TEXT NOT NULL,
    model             TEXT,
    upstream_model    TEXT,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'ok',
    detail            TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, created_at DESC);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


#: Columns added after the first release; applied idempotently at startup so an
#: existing database picks them up without a migration framework.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("tokens", "expires_at", "ALTER TABLE tokens ADD COLUMN expires_at INTEGER"),
)


async def _apply_migrations(conn: aiosqlite.Connection) -> None:
    for table, column, statement in _COLUMN_MIGRATIONS:
        async with conn.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if column not in columns:
            await conn.execute(statement)


def utc_iso(ms: int | None = None) -> str:
    """RFC 3339 UTC timestamp with a ``Z`` suffix (Anthropic style)."""
    stamp = time.time() if ms is None else ms / 1000
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None
        #: Serialises explicit transactions and every write statement inside this
        #: process.  Cross-process safety comes from BEGIN IMMEDIATE itself.
        self._write_lock = asyncio.Lock()
        #: Task that currently owns the open transaction (None when closed).  A
        #: task-identity check (rather than a ContextVar) is deliberate: child
        #: tasks inherit context but must still wait for the lock.
        self._tx_owner: asyncio.Task[Any] | None = None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None = SQLite autocommit mode.  Transactions are then
        # only opened explicitly via Database.transaction(); this prevents
        # sqlite3's implicit BEGIN from colliding with it (and with concurrent
        # requests sharing this connection).
        conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        await _apply_migrations(conn)
        await conn.commit()
        self.conn = conn

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    def _c(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("database is not connected")
        return self.conn

    # -- helpers -----------------------------------------------------------
    def _in_tx(self) -> bool:
        """True only for the task that actually owns the open transaction."""
        return self._tx_owner is not None and self._tx_owner is asyncio.current_task()

    async def execute(self, sql: str, params: Sequence[Any] = (), *, commit: bool = True) -> None:
        """Run one statement.

        Writes are serialised with :meth:`transaction` using the same
        ``asyncio.Lock``.  Inside an open transaction this method never commits
        (regardless of ``commit=``), so a stray write from another coroutine can
        neither commit nor be committed separately.
        """
        conn = self._c()
        if self._in_tx():
            await conn.execute(sql, params)
            return
        async with self._write_lock:
            await conn.execute(sql, params)
            if commit:
                await conn.commit()

    async def executemany(
        self, sql: str, params: Iterable[Sequence[Any]], *, commit: bool = True
    ) -> None:
        conn = self._c()
        rows = list(params)
        if self._in_tx():
            await conn.executemany(sql, rows)
            return
        async with self._write_lock:
            await conn.executemany(sql, rows)
            if commit:
                await conn.commit()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._c().execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._c().execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        return row[0]

    async def commit(self) -> None:
        """Commit outside a transaction.  Inside one, the context manager owns it."""
        if self._in_tx():
            return
        async with self._write_lock:
            await self._c().commit()

    async def rollback(self) -> None:
        if self._in_tx():
            return
        async with self._write_lock:
            await self._c().rollback()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["Database"]:
        """A real, isolated write transaction: ``BEGIN IMMEDIATE`` … ``COMMIT``.

        Guarantees:
          * the whole body is atomic — a failure rolls back every statement,
            including side effects such as invite-code consumption;
          * no other coroutine's write can interleave: every write path takes the
            same in-process ``asyncio.Lock`` for its whole duration, and writes
            inside the transaction are marked via a ContextVar so they never
            commit early;
          * ``BEGIN IMMEDIATE`` takes SQLite's write lock immediately, so other
            processes block (``busy_timeout``) instead of observing a partial
            state.  Multi-worker deployments therefore rely on that lock; never
            replace this with SELECT-then-INSERT.
        """
        conn = self._c()
        async with self._write_lock:
            owner = asyncio.current_task()
            self._tx_owner = owner
            try:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self
                    await conn.commit()
                except BaseException:
                    try:
                        await conn.rollback()
                    except Exception:  # noqa: BLE001 - rollback must not mask the error
                        logger.warning("rollback failed", exc_info=True)
                    raise
            finally:
                self._tx_owner = None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
