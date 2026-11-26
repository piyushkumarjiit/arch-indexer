from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arch_indexer import __version__
from arch_indexer.config import RepoCachePaths
from arch_indexer.module_identity import REVERSE_DOMAIN_TLDS
from arch_indexer.source_scope import in_scope

UNRESOLVED_PREFIX_SPLIT_RE = re.compile(r"[./#@]")

SCHEMA_MAJOR: int = 2
SCHEMA_VERSION: str = "2.0"


def _repo_namespaces(modules: Iterable[str], fqnames: Iterable[str]) -> set[str]:
    """Every package/module prefix that belongs to the indexed repository."""
    namespaces: set[str] = set()
    for module in modules:
        parts = [part for part in UNRESOLVED_PREFIX_SPLIT_RE.split(str(module)) if part]
        start = 2 if len(parts) > 1 and parts[0].lower() in REVERSE_DOMAIN_TLDS else 1
        for size in range(start, len(parts) + 1):
            namespaces.add(".".join(parts[:size]))
            namespaces.add("/".join(parts[:size]))
    for fqname in fqnames:
        if fqname:
            namespaces.add(str(fqname))
    return namespaces


def is_repo_internal_symbol(
    raw_symbol: str,
    namespaces: set[str],
) -> bool:
    """Whether an unresolved reference names a package or symbol owned by the repository.

    A reference is external when its root package belongs to a third-party library
    (``java.util``, ``org.springframework``); it is in-repo when the referenced name,
    its parent symbol, or one of its package prefixes matches an indexed module or
    symbol fqname. Relative imports (``./x``, ``../x``, ``.``, ``..``) are always
    in-repo. Only the in-repo class signals a defect in the index.
    """
    symbol = str(raw_symbol).strip()
    if not symbol:
        return False
    if symbol in (".", "..") or symbol.startswith(("./", "../")):
        return True
    if symbol in namespaces:
        return True
    parent = symbol.rsplit(".", 1)[0]
    if parent != symbol and parent in namespaces:
        return True
    parts = [part for part in UNRESOLVED_PREFIX_SPLIT_RE.split(symbol) if part]
    for size in range(len(parts) - 1, 0, -1):
        if ".".join(parts[:size]) in namespaces or "/".join(parts[:size]) in namespaces:
            return True
    return False


def split_unresolved_refs(
    raw_symbols: Iterable[str],
    modules: Iterable[str],
    fqnames: Iterable[str],
) -> tuple[int, int]:
    """Count unresolved references as ``(external, in_repo)``."""
    namespaces = _repo_namespaces(modules, fqnames)
    external = 0
    in_repo = 0
    for raw_symbol in raw_symbols:
        if is_repo_internal_symbol(raw_symbol, namespaces):
            in_repo += 1
        else:
            external += 1
    return external, in_repo


@dataclass(frozen=True)
class TableSpec:
    """Registry entry defining how an index table is purged and deterministically upserted."""

    name: str
    file_column: str | None = None
    unit_column: str | None = None
    columns: tuple[str, ...] = ()
    required_columns: frozenset[str] = frozenset()
    upsert_mode: str = "insert"
    sort_key: Callable[[dict[str, Any]], Any] | None = None


TABLE_REGISTRY: dict[str, TableSpec] = {
    "files": TableSpec(
        name="files",
        file_column="path",
        unit_column="unit_id",
        columns=(
            "path",
            "language",
            "sha256",
            "line_count",
            "module",
            "tier",
            "fallback_reason",
            "unit_id",
        ),
        required_columns=frozenset({"path", "language", "sha256", "line_count", "module", "tier"}),
        upsert_mode="replace",
        sort_key=lambda f: f["path"],
    ),
    "symbols": TableSpec(
        name="symbols",
        file_column="file_path",
        columns=(
            "fqname",
            "name",
            "kind",
            "file_path",
            "start_line",
            "end_line",
            "body_sha256",
            "visibility",
            "decorators_json",
        ),
        required_columns=frozenset(
            {"fqname", "name", "kind", "file_path", "start_line", "end_line", "body_sha256"}
        ),
        upsert_mode="insert",
        sort_key=lambda s: (s["file_path"], s["start_line"], s["end_line"], s["fqname"]),
    ),
    "references": TableSpec(
        name="references",
        file_column="file_path",
        columns=("symbol_fqname", "file_path", "line", "col", "ref_kind"),
        required_columns=frozenset({"symbol_fqname", "file_path", "line", "col", "ref_kind"}),
        upsert_mode="insert",
        sort_key=lambda r: (
            r["file_path"],
            r["line"],
            r["col"],
            r["symbol_fqname"],
            r["ref_kind"],
        ),
    ),
    "entrypoints": TableSpec(
        name="entrypoints",
        file_column="file_path",
        columns=(
            "kind",
            "name",
            "method",
            "route_or_trigger",
            "file_path",
            "line",
            "symbol_fqname",
            "auth_symbols_json",
        ),
        required_columns=frozenset({"kind", "name", "route_or_trigger", "file_path", "line"}),
        upsert_mode="insert",
        sort_key=lambda e: (e["file_path"], e["line"], e["kind"], e["name"]),
    ),
    "infra_artifacts": TableSpec(
        name="infra_artifacts",
        file_column="file_path",
        columns=("kind", "file_path", "line", "summary_json"),
        required_columns=frozenset({"kind", "file_path", "line", "summary_json"}),
        upsert_mode="insert",
        sort_key=lambda a: (a["file_path"], a["line"], a["kind"]),
    ),
    "unresolved_refs": TableSpec(
        name="unresolved_refs",
        file_column="file_path",
        columns=("file_path", "line", "raw_symbol", "language"),
        required_columns=frozenset({"file_path", "line", "raw_symbol", "language"}),
        upsert_mode="insert",
        sort_key=lambda u: (u["file_path"], u["line"], u["raw_symbol"]),
    ),
    "units": TableSpec(
        name="units",
        unit_column="unit_id",
        columns=(
            "unit_id",
            "language",
            "tier",
            "files_count",
            "fallback_reason",
            "message",
            "manifest_sha256",
            "files_sha256",
            "indexed_at_commit",
        ),
        required_columns=frozenset({"unit_id", "language", "tier", "files_count"}),
        upsert_mode="replace",
        sort_key=lambda u: u["unit_id"],
    ),
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    module TEXT NOT NULL,
    tier TEXT NOT NULL,
    fallback_reason TEXT,
    unit_id TEXT
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

CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    tier TEXT NOT NULL,
    files_count INTEGER NOT NULL,
    fallback_reason TEXT,
    message TEXT,
    manifest_sha256 TEXT,
    files_sha256 TEXT,
    indexed_at_commit TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_unit_id ON files(unit_id);
CREATE INDEX IF NOT EXISTS idx_symbols_fqname ON symbols(fqname);
CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_refs_symbol_fqname ON "references"(symbol_fqname);
CREATE INDEX IF NOT EXISTS idx_refs_file_path ON "references"(file_path);
CREATE INDEX IF NOT EXISTS idx_entrypoints_file_path ON entrypoints(file_path);
CREATE INDEX IF NOT EXISTS idx_infra_artifacts_file_path ON infra_artifacts(file_path);
CREATE INDEX IF NOT EXISTS idx_unresolved_refs_file_path ON unresolved_refs(file_path);
"""


def _parse_schema_major(version: str | None) -> int | None:
    if not version:
        return None
    major_part = version.strip().split(".", 1)[0]
    try:
        return int(major_part)
    except ValueError:
        return None


class IndexStore:
    """SQLite backing store for repository architecture index (index.db)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._closed = False
        self._extra_tables: dict[str, TableSpec] = {}
        self.needs_full_rebuild = False
        self.init_schema()

    @classmethod
    def from_repo(
        cls, repo_path: str | Path, cache_dir_override: str | Path | None = None
    ) -> IndexStore:
        """Create or open index.db at <cache>/repos/<repo-id>/index.db."""
        cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir_override)
        cache_paths.ensure_dirs()
        return cls(cache_paths.index_db)

    def register_table(self, spec: TableSpec) -> None:
        """Register an additional index table for table-driven purge and upsert."""
        self._extra_tables[spec.name] = spec

    def _registered_tables(self) -> dict[str, TableSpec]:
        if not self._extra_tables:
            return dict(TABLE_REGISTRY)
        return {**TABLE_REGISTRY, **self._extra_tables}

    def _existing_tables(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row[0] for row in rows}

    def init_schema(self) -> None:
        """Initialize table schema and indexes, flagging lower-MAJOR DBs for rebuild."""
        existing = self._existing_tables()
        if existing:
            if "meta" not in existing:
                self.needs_full_rebuild = True
                return
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            major = _parse_schema_major(row[0] if row is not None else None)
            if major != SCHEMA_MAJOR:
                self.needs_full_rebuild = True
                return

        self.needs_full_rebuild = False
        with self.conn:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_MAJOR}")
            self.conn.executemany(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("arch_indexer_version", __version__),
                    ("created_at_commit", ""),
                ],
            )

    def get_meta(self, key: str) -> str | None:
        """Read a metadata value from the ``meta`` table."""
        if "meta" not in self._existing_tables():
            return None
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a metadata value in the ``meta`` table."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def clear_all(self) -> None:
        """Drop all tables and recreate schema v2 for a clean full index."""
        existing = self._existing_tables()
        with self.conn:
            for table in sorted(existing):
                self.conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        self.init_schema()

    def close(self) -> None:
        """Checkpoint WAL and close database connection."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        self.conn.close()

    def __enter__(self) -> IndexStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def upsert_rows(self, table_name: str, rows: Iterable[dict[str, Any]]) -> None:
        """Insert or replace rows in deterministic order for any registered table."""
        with self.conn:
            self._upsert_rows(table_name, rows)

    def _upsert_rows(self, table_name: str, rows: Iterable[dict[str, Any]]) -> None:
        spec = self._registered_tables().get(table_name)
        if spec is None:
            raise KeyError(f"Unknown index table: {table_name!r}")
        if not spec.columns:
            raise ValueError(f"Table {table_name!r} has no registered columns for upsert")

        row_list = list(rows)
        if not row_list:
            return
        if spec.sort_key is not None:
            row_list = sorted(row_list, key=spec.sort_key)

        records = [
            tuple(
                row[col] if col in spec.required_columns else row.get(col) for col in spec.columns
            )
            for row in row_list
        ]
        verb = "INSERT OR REPLACE" if spec.upsert_mode == "replace" else "INSERT"
        col_sql = ", ".join(f'"{col}"' for col in spec.columns)
        placeholders = ", ".join("?" for _ in spec.columns)
        self.conn.executemany(
            f'{verb} INTO "{spec.name}" ({col_sql}) VALUES ({placeholders})',
            records,
        )

    def insert_files(self, files: Iterable[dict[str, Any]]) -> None:
        """Insert file records in deterministic row order by path."""
        self.upsert_rows("files", files)

    def _insert_files(self, files: Iterable[dict[str, Any]]) -> None:
        self._upsert_rows("files", files)

    def insert_symbols(self, symbols: Iterable[dict[str, Any]]) -> None:
        """Insert symbol records in deterministic row order."""
        self.upsert_rows("symbols", symbols)

    def _insert_symbols(self, symbols: Iterable[dict[str, Any]]) -> None:
        self._upsert_rows("symbols", symbols)

    def insert_references(self, refs: Iterable[dict[str, Any]]) -> None:
        """Insert reference records in deterministic row order into quoted "references" table."""
        self.upsert_rows("references", refs)

    def _insert_references(self, refs: Iterable[dict[str, Any]]) -> None:
        self._upsert_rows("references", refs)

    def insert_entrypoints(self, entrypoints: Iterable[dict[str, Any]]) -> None:
        """Insert entrypoints in deterministic row order."""
        self.upsert_rows("entrypoints", entrypoints)

    def _insert_entrypoints(self, entrypoints: Iterable[dict[str, Any]]) -> None:
        self._upsert_rows("entrypoints", entrypoints)

    def insert_infra_artifacts(self, infra: Iterable[dict[str, Any]]) -> None:
        """Insert infra artifacts in deterministic row order."""
        self.upsert_rows("infra_artifacts", infra)

    def _insert_infra_artifacts(self, infra: Iterable[dict[str, Any]]) -> None:
        self._upsert_rows("infra_artifacts", infra)

    def insert_unresolved_refs(self, unresolved: Iterable[dict[str, Any]]) -> None:
        """Insert unresolved refs in deterministic row order."""
        self.upsert_rows("unresolved_refs", unresolved)

    def _insert_unresolved_refs(self, unresolved: Iterable[dict[str, Any]]) -> None:
        self._upsert_rows("unresolved_refs", unresolved)

    def insert_unit_data(
        self,
        *,
        unit_id: str | None = None,
        files: Iterable[dict[str, Any]] = (),
        symbols: Iterable[dict[str, Any]] = (),
        references: Iterable[dict[str, Any]] = (),
        entrypoints: Iterable[dict[str, Any]] = (),
        infra_artifacts: Iterable[dict[str, Any]] = (),
        unresolved_refs: Iterable[dict[str, Any]] = (),
        **extra_tables: Iterable[dict[str, Any]],
    ) -> None:
        """Stream insertion of all artifacts extracted from a unit within a single transaction."""
        batches: dict[str, Iterable[dict[str, Any]]] = {
            "files": files,
            "symbols": symbols,
            "references": references,
            "entrypoints": entrypoints,
            "infra_artifacts": infra_artifacts,
            "unresolved_refs": unresolved_refs,
            **extra_tables,
        }
        if unit_id is not None:
            batches["files"] = [
                {**f, "unit_id": f.get("unit_id") or unit_id} for f in batches["files"]
            ]
        with self.conn:
            for table_name in self._registered_tables():
                rows = batches.get(table_name)
                if rows:
                    self._upsert_rows(table_name, rows)

    def insert_unit_record(
        self,
        *,
        unit_id: str,
        language: str,
        tier: str,
        files_count: int,
        fallback_reason: str | None = None,
        message: str = "",
        manifest_sha256: str | None = None,
        files_sha256: str | None = None,
        indexed_at_commit: str | None = None,
    ) -> None:
        """Record how one indexing unit resolved (tier, hashes, fallback reason, message)."""
        self.upsert_rows(
            "units",
            [
                {
                    "unit_id": unit_id,
                    "language": language,
                    "tier": tier,
                    "files_count": files_count,
                    "fallback_reason": fallback_reason,
                    "message": message,
                    "manifest_sha256": manifest_sha256,
                    "files_sha256": files_sha256,
                    "indexed_at_commit": indexed_at_commit,
                }
            ],
        )

    def get_unit_records(self) -> list[dict[str, Any]]:
        """Return every unit's tier and fallback detail in deterministic unit-id order."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT unit_id, language, tier, files_count, fallback_reason, message
            FROM units
            ORDER BY unit_id
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_unit_states(self) -> list[dict[str, Any]]:
        """Return every unit's full incremental state record in deterministic unit-id order."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT unit_id, language, tier, files_count, fallback_reason, message,
                   manifest_sha256, files_sha256, indexed_at_commit
            FROM units
            ORDER BY unit_id
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_file_records(self, *, include_unit_id: bool = False) -> list[dict[str, Any]]:
        """Return every indexed file record in deterministic path order."""
        cols = "path, language, sha256, line_count, module, tier, fallback_reason"
        if include_unit_id:
            cols += ", unit_id"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT {cols} FROM files ORDER BY path")
        return [dict(row) for row in cursor.fetchall()]

    def get_symbol_records(self) -> list[dict[str, Any]]:
        """Return every indexed symbol record in deterministic order."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT fqname, name, kind, file_path, start_line, end_line, body_sha256,
                   visibility, decorators_json
            FROM symbols
            ORDER BY file_path, start_line, end_line, fqname, kind
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def _file_keyed_tables(self) -> list[TableSpec]:
        specs = [spec for spec in self._registered_tables().values() if spec.file_column]
        specs.sort(key=lambda s: 1 if s.name == "files" else 0)
        return specs

    def _unit_keyed_tables(self) -> list[TableSpec]:
        return [spec for spec in self._registered_tables().values() if spec.unit_column]

    def _purge_files_in_tx(self, purged: list[str]) -> None:
        if not purged:
            return
        params = [(path,) for path in purged]
        for spec in self._file_keyed_tables():
            self.conn.executemany(
                f'DELETE FROM "{spec.name}" WHERE "{spec.file_column}" = ?',
                params,
            )

    def purge_files(self, paths: Iterable[str]) -> list[str]:
        """Delete every row referencing the given file paths from all registered tables.

        Returns the sorted list of purged paths. Safe to call with an empty list and
        idempotent, so a deleted file can never remain indexed in any table.
        """
        purged = sorted({str(path) for path in paths if path})
        if not purged:
            return []
        with self.conn:
            self._purge_files_in_tx(purged)
        return purged

    def purge_unit(self, unit_id: str, *, remove_unit: bool = True) -> list[str]:
        """Delete all rows in every registered table for files belonging to ``unit_id``.

        Also deletes rows keyed directly by ``unit_id`` (including ``units`` when
        ``remove_unit`` is true). Returns the sorted list of file paths purged.
        """
        rows = self.conn.execute(
            "SELECT path FROM files WHERE unit_id = ? ORDER BY path",
            (unit_id,),
        ).fetchall()
        purged = [row[0] for row in rows]
        with self.conn:
            self._purge_files_in_tx(purged)
            for spec in self._unit_keyed_tables():
                if spec.name == "units" and not remove_unit:
                    continue
                self.conn.execute(
                    f'DELETE FROM "{spec.name}" WHERE "{spec.unit_column}" = ?',
                    (unit_id,),
                )
        return purged

    def get_summary(self, *, include_tests: bool | None = None) -> dict[str, Any]:
        """Collect index summary metrics.

        ``include_tests=None`` reports the raw index tables. When a boolean is passed, the
        counts are scoped exactly like ``master-index.json``/``graph.json``: test files
        (and their symbols, references, entrypoints, infra artifacts and unresolved refs)
        are excluded unless ``include_tests`` is true. The index command uses that scoped
        form so its printed summary cannot disagree with the artifacts.
        """
        scoped = include_tests is not None
        include_test_rows = bool(include_tests)

        def keep(path: str) -> bool:
            return not scoped or in_scope(path, include_test_rows)

        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT path, language, tier
            FROM files
            ORDER BY path
            """
        )
        all_file_rows = cursor.fetchall()
        file_rows = [row for row in all_file_rows if keep(row["path"])]
        breakdown_counts: dict[tuple[str, str], int] = {}
        for row in file_rows:
            key = (row["language"], row["tier"])
            breakdown_counts[key] = breakdown_counts.get(key, 0) + 1
        files_breakdown = [
            (language, tier, count) for (language, tier), count in sorted(breakdown_counts.items())
        ]
        total_files = len(file_rows)

        def scoped_count(table: str, column: str = "file_path") -> int:
            cursor.execute(f'SELECT {column}, COUNT(*) FROM "{table}" GROUP BY {column}')
            return sum(count for path, count in cursor.fetchall() if keep(path))

        total_symbols = scoped_count("symbols")
        total_references = scoped_count("references")
        total_entrypoints = scoped_count("entrypoints")
        total_infra = scoped_count("infra_artifacts")

        # Split unresolved references into external-library symbols (expected) and
        # repository-owned symbols (a real index defect). Totals stay unchanged. The
        # namespace sets stay unscoped so the split matches master-index.json.
        cursor.execute("SELECT DISTINCT module FROM files")
        repo_modules = [row[0] for row in cursor.fetchall() if row[0]]
        cursor.execute("SELECT fqname FROM symbols")
        repo_fqnames = [row[0] for row in cursor.fetchall() if row[0]]
        cursor.execute("SELECT file_path, raw_symbol FROM unresolved_refs")
        unresolved_symbols = [raw for path, raw in cursor.fetchall() if keep(path)]
        total_unresolved = len(unresolved_symbols)
        unresolved_external, unresolved_in_repo = split_unresolved_refs(
            iter(unresolved_symbols), repo_modules, repo_fqnames
        )

        return {
            "total_files": total_files,
            "excluded_test_files": (len(all_file_rows) - total_files) if scoped else 0,
            "files_breakdown": files_breakdown,
            "total_symbols": total_symbols,
            "total_references": total_references,
            "total_entrypoints": total_entrypoints,
            "total_infra_artifacts": total_infra,
            "total_unresolved_refs": total_unresolved,
            "unresolved_external_refs": unresolved_external,
            "unresolved_in_repo_refs": unresolved_in_repo,
            "units": self.get_unit_records(),
        }

    def format_summary(self, *, include_tests: bool | None = None) -> str:
        """Format the index summary line plus each unit's tier and fallback detail."""
        summary = self.get_summary(include_tests=include_tests)
        breakdown_parts = [
            f"{lang}: {count} [{tier}]" for lang, tier, count in summary["files_breakdown"]
        ]
        breakdown_str = f" ({', '.join(breakdown_parts)})" if breakdown_parts else ""
        excluded = summary["excluded_test_files"]
        excluded_str = f" [{excluded} test file(s) excluded]" if excluded else ""
        lines = [
            f"Indexed {summary['total_files']} files{breakdown_str}: "
            f"{summary['total_symbols']} symbols, "
            f"{summary['total_references']} references, "
            f"{summary['total_entrypoints']} entrypoints, "
            f"{summary['total_infra_artifacts']} infra artifacts, "
            f"{summary['total_unresolved_refs']} unresolved refs "
            f"({summary['unresolved_external_refs']} external, "
            f"{summary['unresolved_in_repo_refs']} in-repo){excluded_str}"
        ]
        if summary["units"]:
            lines.append("Unit tiers:")
            lines.extend(self._format_unit_row(unit) for unit in summary["units"])
        return "\n".join(lines)

    @staticmethod
    def _format_unit_row(unit: dict[str, Any]) -> str:
        detail = f"  {unit['unit_id']}: {unit['files_count']} files [{unit['tier']}"
        if unit["fallback_reason"]:
            detail += f": {unit['fallback_reason']}"
        detail += "]"
        if unit["message"]:
            detail += f" — {unit['message']}"
        return detail
