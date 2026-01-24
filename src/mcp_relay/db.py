"""Database layer for MCP Relay - supports SQLite and PostgreSQL."""

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import aiosqlite

logger = logging.getLogger(__name__)

# Schema for MCP servers table
SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'sse',
    description TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT DEFAULT 'unknown',
    tools_count INTEGER DEFAULT 0,
    last_seen TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mcp_servers_name ON mcp_servers(name);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled);
"""

# PostgreSQL schema (slightly different syntax)
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'sse',
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT DEFAULT 'unknown',
    tools_count INTEGER DEFAULT 0,
    last_seen TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mcp_servers_name ON mcp_servers(name);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled);
"""


class Database:
    """Simple async database wrapper supporting SQLite and PostgreSQL."""

    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv(
            "DATABASE_URL", "sqlite:///data/mcp_relay.db"
        )
        self._conn = None
        self._pool = None
        self._is_postgres = self.database_url.startswith("postgres")

    @property
    def is_postgres(self) -> bool:
        return self._is_postgres

    def _convert_placeholders(self, sql: str) -> str:
        """Convert ? placeholders to $1, $2, etc. for PostgreSQL."""
        if not self._is_postgres:
            return sql
        result = []
        param_index = 0
        for char in sql:
            if char == "?":
                param_index += 1
                result.append(f"${param_index}")
            else:
                result.append(char)
        return "".join(result)

    async def connect(self) -> None:
        """Establish database connection."""
        if self._is_postgres:
            try:
                import asyncpg

                parsed = urlparse(self.database_url)
                self._pool = await asyncpg.create_pool(
                    self.database_url,
                    min_size=2,
                    max_size=10,
                )
                logger.info(f"Connected to PostgreSQL: {parsed.hostname}")
            except ImportError:
                raise ImportError("asyncpg required for PostgreSQL support")
        else:
            # SQLite
            db_path = self.database_url.replace("sqlite:///", "")
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            self._conn = await aiosqlite.connect(db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            logger.info(f"Connected to SQLite: {db_path}")

    async def close(self) -> None:
        """Close database connection."""
        if self._is_postgres and self._pool:
            await self._pool.close()
        elif self._conn:
            await self._conn.close()

    async def init_schema(self) -> None:
        """Initialize database schema."""
        if self._is_postgres:
            async with self._pool.acquire() as conn:
                await conn.execute(SCHEMA_PG)
        else:
            await self._conn.executescript(SCHEMA)
            await self._conn.commit()
        logger.info("Database schema initialized")

    async def execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a SQL statement."""
        sql = self._convert_placeholders(sql)
        if self._is_postgres:
            async with self._pool.acquire() as conn:
                await conn.execute(sql, *params)
        else:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Fetch a single row."""
        sql = self._convert_placeholders(sql)
        if self._is_postgres:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
                return dict(row) if row else None
        else:
            cursor = await self._conn.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Fetch all rows."""
        sql = self._convert_placeholders(sql)
        if self._is_postgres:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
                return [dict(row) for row in rows]
        else:
            cursor = await self._conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetchval(self, sql: str, params: tuple = ()) -> any:
        """Fetch a single value."""
        sql = self._convert_placeholders(sql)
        if self._is_postgres:
            async with self._pool.acquire() as conn:
                return await conn.fetchval(sql, *params)
        else:
            cursor = await self._conn.execute(sql, params)
            row = await cursor.fetchone()
            return row[0] if row else None

    # Server CRUD operations

    async def list_servers(self, enabled_only: bool = False) -> list[dict]:
        """List all registered servers."""
        sql = "SELECT * FROM mcp_servers"
        if enabled_only:
            sql += " WHERE enabled = ?"
            return await self.fetchall(sql, (1 if not self._is_postgres else True,))
        return await self.fetchall(sql)

    async def get_server(self, name: str) -> Optional[dict]:
        """Get a server by name."""
        return await self.fetchone("SELECT * FROM mcp_servers WHERE name = ?", (name,))

    async def create_server(
        self,
        name: str,
        url: str,
        transport: str = "sse",
        description: Optional[str] = None,
        enabled: bool = True,
    ) -> dict:
        """Create a new server registration."""
        now = datetime.now(timezone.utc).isoformat()
        enabled_val = 1 if not self._is_postgres else enabled

        await self.execute(
            """
            INSERT INTO mcp_servers (name, url, transport, description, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, url, transport, description, enabled_val, now),
        )
        return await self.get_server(name)

    async def update_server(
        self,
        name: str,
        url: Optional[str] = None,
        transport: Optional[str] = None,
        description: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[dict]:
        """Update a server registration."""
        server = await self.get_server(name)
        if not server:
            return None

        updates = []
        params = []
        if url is not None:
            updates.append("url = ?")
            params.append(url)
        if transport is not None:
            updates.append("transport = ?")
            params.append(transport)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if (not self._is_postgres and enabled) else enabled)

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now(timezone.utc).isoformat())
            params.append(name)
            await self.execute(
                f"UPDATE mcp_servers SET {', '.join(updates)} WHERE name = ?",
                tuple(params),
            )
        return await self.get_server(name)

    async def delete_server(self, name: str) -> bool:
        """Delete a server registration."""
        server = await self.get_server(name)
        if not server:
            return False
        await self.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        return True

    async def update_server_status(
        self,
        name: str,
        status: str,
        tools_count: int = 0,
    ) -> None:
        """Update server health status."""
        now = datetime.now(timezone.utc).isoformat()
        await self.execute(
            """
            UPDATE mcp_servers
            SET status = ?, tools_count = ?, last_seen = ?, updated_at = ?
            WHERE name = ?
            """,
            (status, tools_count, now, now, name),
        )
