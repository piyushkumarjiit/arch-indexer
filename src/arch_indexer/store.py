from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from arch_indexer.config import RepoCachePaths

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    module TEXT NOT NULL,
    tier TEXT NOT NULL,
    fallback_reason TEXT
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fqname TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    visibility TEXT,
    decorators_json TEXT
);

CREATE TABLE IF NOT EXISTS "references" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_fqname TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    col INTEGER NOT NULL,
    ref_kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entrypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    method TEXT,
    route_or_trigger TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    symbol_fqname TEXT,
    auth_symbols_json TEXT
);

CREATE TABLE IF NOT EXISTS infra_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unresolved_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    raw_symbol TEXT NOT NULL,
    language TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_symbols_fqname ON symbols(fqname);
CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_refs_symbol_fqname ON "references"(symbol_fqname);
CREATE INDEX IF NOT EXISTS idx_refs_file_path ON "references"(file_path);
CREATE INDEX IF NOT EXISTS idx_entrypoints_file_path ON entrypoints(file_path);
CREATE INDEX IF NOT EXISTS idx_infra_artifacts_file_path ON infra_artifacts(file_path);
CREATE INDEX IF NOT EXISTS idx_unresolved_refs_file_path ON unresolved_refs(file_path);
"""


class IndexStore:
    """SQLite backing store for repository architecture index (index.db)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    @classmethod
    def from_repo(
        cls, repo_path: str | Path, cache_dir_override: str | Path | None = None
    ) -> IndexStore:
        """Create or open index.db at <cache>/repos/<repo-id>/index.db."""
        cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir_override)
        cache_paths.ensure_dirs()
        return cls(cache_paths.index_db)

    def init_schema(self) -> None:
        """Initialize table schema and indexes."""
        with self.conn:
            self.conn.executescript(SCHEMA_SQL)

    def clear_all(self) -> None:
        """Drop and recreate all tables for a clean full index."""
        with self.conn:
            self.conn.execute('DROP TABLE IF EXISTS "references"')
            self.conn.execute("DROP TABLE IF EXISTS symbols")
            self.conn.execute("DROP TABLE IF EXISTS entrypoints")
            self.conn.execute("DROP TABLE IF EXISTS infra_artifacts")
            self.conn.execute("DROP TABLE IF EXISTS unresolved_refs")
            self.conn.execute("DROP TABLE IF EXISTS files")
        self.init_schema()

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()

    def __enter__(self) -> IndexStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def insert_files(self, files: Iterable[dict[str, Any]]) -> None:
        """Insert file records in deterministic row order by path."""
        with self.conn:
            self._insert_files(files)

    def _insert_files(self, files: Iterable[dict[str, Any]]) -> None:
        sorted_files = sorted(files, key=lambda f: f["path"])
        records = [
            (
                f["path"],
                f["language"],
                f["sha256"],
                f["line_count"],
                f["module"],
                f["tier"],
                f.get("fallback_reason"),
            )
            for f in sorted_files
        ]
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO files
            (path, language, sha256, line_count, module, tier, fallback_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )

    def insert_symbols(self, symbols: Iterable[dict[str, Any]]) -> None:
        """Insert symbol records in deterministic row order."""
        with self.conn:
            self._insert_symbols(symbols)

    def _insert_symbols(self, symbols: Iterable[dict[str, Any]]) -> None:
        sorted_symbols = sorted(
            symbols,
            key=lambda s: (s["file_path"], s["start_line"], s["end_line"], s["fqname"]),
        )
        records = [
            (
                s["fqname"],
                s["name"],
                s["kind"],
                s["file_path"],
                s["start_line"],
                s["end_line"],
                s["body_sha256"],
                s.get("visibility"),
                s.get("decorators_json"),
            )
            for s in sorted_symbols
        ]
        self.conn.executemany(
            """
            INSERT INTO symbols
            (fqname, name, kind, file_path, start_line, end_line,
             body_sha256, visibility, decorators_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )

    def insert_references(self, refs: Iterable[dict[str, Any]]) -> None:
        """Insert reference records in deterministic row order into quoted "references" table."""
        with self.conn:
            self._insert_references(refs)

    def _insert_references(self, refs: Iterable[dict[str, Any]]) -> None:
        sorted_refs = sorted(
            refs,
            key=lambda r: (r["file_path"], r["line"], r["col"], r["symbol_fqname"], r["ref_kind"]),
        )
        records = [
            (r["symbol_fqname"], r["file_path"], r["line"], r["col"], r["ref_kind"])
            for r in sorted_refs
        ]
        self.conn.executemany(
            """
            INSERT INTO "references"
            (symbol_fqname, file_path, line, col, ref_kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            records,
        )

    def insert_entrypoints(self, entrypoints: Iterable[dict[str, Any]]) -> None:
        """Insert entrypoints in deterministic row order."""
        with self.conn:
            self._insert_entrypoints(entrypoints)

    def _insert_entrypoints(self, entrypoints: Iterable[dict[str, Any]]) -> None:
        sorted_entries = sorted(
            entrypoints,
            key=lambda e: (e["file_path"], e["line"], e["kind"], e["name"]),
        )
        records = [
            (
                e["kind"],
                e["name"],
                e.get("method"),
                e["route_or_trigger"],
                e["file_path"],
                e["line"],
                e.get("symbol_fqname"),
                e.get("auth_symbols_json"),
            )
            for e in sorted_entries
        ]
        self.conn.executemany(
            """
            INSERT INTO entrypoints
            (kind, name, method, route_or_trigger, file_path, line,
             symbol_fqname, auth_symbols_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )

    def insert_infra_artifacts(self, infra: Iterable[dict[str, Any]]) -> None:
        """Insert infra artifacts in deterministic row order."""
        with self.conn:
            self._insert_infra_artifacts(infra)

    def _insert_infra_artifacts(self, infra: Iterable[dict[str, Any]]) -> None:
        sorted_infra = sorted(
            infra,
            key=lambda a: (a["file_path"], a["line"], a["kind"]),
        )
        records = [(a["kind"], a["file_path"], a["line"], a["summary_json"]) for a in sorted_infra]
        self.conn.executemany(
            """
            INSERT INTO infra_artifacts
            (kind, file_path, line, summary_json)
            VALUES (?, ?, ?, ?)
            """,
            records,
        )

    def insert_unresolved_refs(self, unresolved: Iterable[dict[str, Any]]) -> None:
        """Insert unresolved refs in deterministic row order."""
        with self.conn:
            self._insert_unresolved_refs(unresolved)

    def _insert_unresolved_refs(self, unresolved: Iterable[dict[str, Any]]) -> None:
        sorted_unresolved = sorted(
            unresolved,
            key=lambda u: (u["file_path"], u["line"], u["raw_symbol"]),
        )
        records = [
            (u["file_path"], u["line"], u["raw_symbol"], u["language"]) for u in sorted_unresolved
        ]
        self.conn.executemany(
            """
            INSERT INTO unresolved_refs
            (file_path, line, raw_symbol, language)
            VALUES (?, ?, ?, ?)
            """,
            records,
        )

    def insert_unit_data(
        self,
        *,
        files: Iterable[dict[str, Any]] = (),
        symbols: Iterable[dict[str, Any]] = (),
        references: Iterable[dict[str, Any]] = (),
        entrypoints: Iterable[dict[str, Any]] = (),
        infra_artifacts: Iterable[dict[str, Any]] = (),
        unresolved_refs: Iterable[dict[str, Any]] = (),
    ) -> None:
        """Stream insertion of all artifacts extracted from a unit within a single transaction."""
        with self.conn:
            if files:
                self._insert_files(files)
            if symbols:
                self._insert_symbols(symbols)
            if references:
                self._insert_references(references)
            if entrypoints:
                self._insert_entrypoints(entrypoints)
            if infra_artifacts:
                self._insert_infra_artifacts(infra_artifacts)
            if unresolved_refs:
                self._insert_unresolved_refs(unresolved_refs)

    def get_summary(self) -> dict[str, Any]:
        """Collect index summary metrics."""
        cursor = self.conn.cursor()

        # Files breakdown
        cursor.execute(
            """
            SELECT language, tier, COUNT(*) as count
            FROM files
            GROUP BY language, tier
            ORDER BY language, tier
            """
        )
        files_breakdown = [
            (row["language"], row["tier"], row["count"]) for row in cursor.fetchall()
        ]

        cursor.execute("SELECT COUNT(*) FROM files")
        total_files = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM symbols")
        total_symbols = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM "references"')
        total_references = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM entrypoints")
        total_entrypoints = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM infra_artifacts")
        total_infra = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM unresolved_refs")
        total_unresolved = cursor.fetchone()[0]

        return {
            "total_files": total_files,
            "files_breakdown": files_breakdown,
            "total_symbols": total_symbols,
            "total_references": total_references,
            "total_entrypoints": total_entrypoints,
            "total_infra_artifacts": total_infra,
            "total_unresolved_refs": total_unresolved,
        }

    def format_summary(self) -> str:
        """Format one-line index summary matching spec."""
        summary = self.get_summary()
        breakdown_parts = [
            f"{lang}: {count} [{tier}]" for lang, tier, count in summary["files_breakdown"]
        ]
        breakdown_str = f" ({', '.join(breakdown_parts)})" if breakdown_parts else ""
        return (
            f"Indexed {summary['total_files']} files{breakdown_str}: "
            f"{summary['total_symbols']} symbols, "
            f"{summary['total_references']} references, "
            f"{summary['total_entrypoints']} entrypoints, "
            f"{summary['total_infra_artifacts']} infra artifacts, "
            f"{summary['total_unresolved_refs']} unresolved refs"
        )
