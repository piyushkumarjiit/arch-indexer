import shutil
import sqlite3
from pathlib import Path

from arch_indexer.cli import (
    EXIT_DELTA_DETECTED,
    EXIT_ERROR,
    EXIT_TOOLCHAIN_MISSING,
    main,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def copy_fixture(name: str, tmp_path: Path) -> Path:
    """Copy a fixture repo so `index` writes its docs artifacts outside the source tree."""
    target = tmp_path / "fixture" / name
    shutil.copytree(FIXTURES_DIR / name, target)
    return target


def test_index_command_e2e_python(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = copy_fixture("python", tmp_path)

    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    captured = capsys.readouterr()
    assert "Indexed 1 files (python: 1 [tree-sitter]):" in captured.out
    assert "1 symbols" in captured.out

    # Verify SQLite database was written at expected cache path
    db_files = list((cache / "repos").glob("*/index.db"))
    assert len(db_files) == 1
    db_path = db_files[0]
    assert db_path.is_file()

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT path, tier, fallback_reason FROM files")
    row = c.fetchone()
    assert row[0] == "src/app.py"
    assert row[1] == "tree-sitter"
    assert row[2] == "tool_unavailable"

    c.execute("SELECT name, kind, start_line, end_line FROM symbols")
    sym = c.fetchone()
    assert sym[0] == "hello"
    assert sym[1] == "function"
    assert sym[2] == 1
    assert sym[3] == 2
    conn.close()


def test_index_command_e2e_mixed(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = copy_fixture("mixed", tmp_path)

    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    captured = capsys.readouterr()
    assert "Indexed 4 files" in captured.out
    assert "infra: 1 [tree-sitter]" in captured.out
    assert "java: 1 [tree-sitter]" in captured.out
    assert "python: 1 [tree-sitter]" in captured.out
    assert "typescript: 1 [tree-sitter]" in captured.out


def test_index_strict_tier1_exits_toolchain_missing(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = copy_fixture("python", tmp_path)

    code = main(["index", str(repo), "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    code = main(["index", str(repo), "--offline", "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_TOOLCHAIN_MISSING

    captured = capsys.readouterr()
    assert "Tier 1 indexer unavailable" in captured.err


def test_index_unreadable_repo_exits_error(tmp_path: Path, capsys):
    missing = tmp_path / "does_not_exist"
    code = main(["index", str(missing)])
    assert code == EXIT_ERROR


def test_chunk_files_must_be_positive(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = copy_fixture("python", tmp_path)

    code = main(["index", str(repo), "--chunk-files", "-5", "--cache-dir", str(cache)])
    assert code == EXIT_ERROR
    assert "positive integer" in capsys.readouterr().err


def test_python_relative_and_src_layout_imports(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    pkg = repo / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .utils import helper\n")
    (pkg / "utils.py").write_text("def helper():\n    return 1\n")
    (pkg / "core.py").write_text("class Engine:\n    pass\n")
    (pkg / "cli.py").write_text(
        "from .core import Engine\nfrom . import core\nimport pkg.core\n"
        "\ndef build():\n    return Engine()\n"
    )

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute(
        'SELECT symbol_fqname FROM "references" '
        'WHERE file_path="src/pkg/cli.py" AND ref_kind="import"'
    )
    cli_imports = {row[0] for row in c.fetchall()}
    assert {"pkg.core.Engine", "pkg.core"} <= cli_imports

    c.execute(
        'SELECT symbol_fqname FROM "references" '
        'WHERE file_path="src/pkg/__init__.py" AND ref_kind="import"'
    )
    assert "pkg.utils.helper" in {row[0] for row in c.fetchall()}

    c.execute(
        'SELECT symbol_fqname FROM "references" '
        'WHERE file_path="src/pkg/cli.py" AND ref_kind="call"'
    )
    assert "pkg.core.Engine" in {row[0] for row in c.fetchall()}

    c.execute(
        "SELECT raw_symbol FROM unresolved_refs "
        'WHERE file_path IN ("src/pkg/cli.py", "src/pkg/__init__.py")'
    )
    assert not any(row[0].startswith("pkg") for row in c.fetchall())

    conn.close()


def test_typescript_index_import_and_alias(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    lib = repo / "lib" / "utils"
    lib.mkdir(parents=True)
    (lib / "index.ts").write_text(
        "export function helper() { return 1; }\nexport function original() { return 2; }\n"
    )
    (repo / "lib" / "app.ts").write_text(
        "import { helper } from './utils';\n"
        "import { original as aliased } from './utils';\n"
        "export function run() { helper(); aliased(); }\n"
    )

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute(
        'SELECT symbol_fqname FROM "references" WHERE file_path="lib/app.ts" AND ref_kind="import"'
    )
    assert {row[0] for row in c.fetchall()} == {"lib.utils.helper", "lib.utils.original"}

    c.execute(
        'SELECT symbol_fqname FROM "references" WHERE file_path="lib/app.ts" AND ref_kind="call"'
    )
    assert {row[0] for row in c.fetchall()} == {"lib.utils.helper", "lib.utils.original"}

    conn.close()


def test_bare_call_names_are_language_and_kind_scoped(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "storage.py").write_text("class Storage:\n    def open(self):\n        pass\n")
    (repo / "pyfetch.py").write_text("def fetch():\n    return None\n")
    (repo / "main.py").write_text('def run():\n    open("data")\n')
    (repo / "app.ts").write_text("export function callFetch() { fetch('http://x'); }\n")

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute(
        'SELECT symbol_fqname FROM "references" WHERE file_path="main.py" AND ref_kind="call"'
    )
    python_calls = {row[0] for row in c.fetchall()}
    assert "open" in python_calls
    assert "storage.Storage.open" not in python_calls

    c.execute('SELECT symbol_fqname FROM "references" WHERE file_path="app.ts" AND ref_kind="call"')
    ts_calls = {row[0] for row in c.fetchall()}
    assert "fetch" in ts_calls
    assert "pyfetch.fetch" not in ts_calls

    conn.close()


def test_python_project_root_imports(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    backend = repo / "backend"
    app = backend / "app"
    app.mkdir(parents=True)
    (backend / "pyproject.toml").write_text("[project]\nname = 'backend'\n")
    (app / "__init__.py").write_text("")
    (app / "core.py").write_text("class Engine:\n    pass\n")
    (app / "main.py").write_text(
        "from app.core import Engine\n\ndef build():\n    return Engine()\n"
    )

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute(
        'SELECT symbol_fqname FROM "references" '
        'WHERE file_path="backend/app/main.py" AND ref_kind="import"'
    )
    assert "backend.app.core.Engine" in {row[0] for row in c.fetchall()}

    c.execute(
        'SELECT symbol_fqname FROM "references" '
        'WHERE file_path="backend/app/main.py" AND ref_kind="call"'
    )
    assert "backend.app.core.Engine" in {row[0] for row in c.fetchall()}

    c.execute('SELECT fqname FROM symbols WHERE name="Engine"')
    assert [row[0] for row in c.fetchall()] == ["backend.app.core.Engine"]

    c.execute('SELECT raw_symbol FROM unresolved_refs WHERE file_path="backend/app/main.py"')
    assert not any(row[0].startswith("app") for row in c.fetchall())

    conn.close()


def test_imported_names_shadow_external_imports(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "util.py").write_text("def dumps(value):\n    return value\n")
    (repo / "a.py").write_text("from json import dumps\n\ndef run():\n    dumps({})\n")
    (repo / "util.ts").write_text("export function format(value: string) { return value; }\n")
    (repo / "app.ts").write_text("import { format } from 'date-fns';\nformat('x');\n")

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute('SELECT symbol_fqname FROM "references" WHERE file_path="a.py" AND ref_kind="call"')
    python_calls = {row[0] for row in c.fetchall()}
    assert python_calls == {"dumps"}

    c.execute('SELECT symbol_fqname FROM "references" WHERE file_path="app.ts" AND ref_kind="call"')
    assert {row[0] for row in c.fetchall()} == {"format"}

    conn.close()


def test_cross_language_module_tables(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    types_dir = repo / "types"
    types_dir.mkdir(parents=True)
    (types_dir / "index.ts").write_text("export const value = 1;\n")
    (repo / "use.py").write_text("import types\n")

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute(
        'SELECT symbol_fqname FROM "references" WHERE file_path="use.py" AND ref_kind="import"'
    )
    assert "types" not in {row[0] for row in c.fetchall()}

    c.execute('SELECT raw_symbol FROM unresolved_refs WHERE file_path="use.py"')
    assert "types" in {row[0] for row in c.fetchall()}

    conn.close()


def test_typescript_esm_js_specifier(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    lib = repo / "lib"
    lib.mkdir(parents=True)
    (lib / "utils.ts").write_text("export function x() { return 1; }\n")
    (lib / "app.ts").write_text(
        "import { x as runX } from './utils.js';\nexport function go() { runX(); }\n"
    )

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute(
        'SELECT symbol_fqname FROM "references" WHERE file_path="lib/app.ts" AND ref_kind="import"'
    )
    assert {row[0] for row in c.fetchall()} == {"lib.utils.x"}

    c.execute(
        'SELECT symbol_fqname FROM "references" WHERE file_path="lib/app.ts" AND ref_kind="call"'
    )
    assert {row[0] for row in c.fetchall()} == {"lib.utils.x"}

    c.execute('SELECT raw_symbol FROM unresolved_refs WHERE file_path="lib/app.ts"')
    assert c.fetchall() == []

    conn.close()


def test_cross_file_symbol_resolution(tmp_path: Path, capsys):
    repo = tmp_path / "multi_file_repo"
    repo.mkdir()
    (repo / "utils.py").write_text("def helper():\n    return 42\n")
    (repo / "main.py").write_text("from utils import helper\n\ndef run():\n    helper()\n")

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_files = list((cache / "repos").glob("*/index.db"))
    assert len(db_files) == 1
    conn = sqlite3.connect(db_files[0])
    c = conn.cursor()

    # Verify import reference resolved to utils.helper
    c.execute(
        'SELECT symbol_fqname, ref_kind FROM "references" '
        'WHERE file_path="main.py" AND ref_kind="import"'
    )
    import_row = c.fetchone()
    assert import_row is not None
    assert import_row[0] == "utils.helper"

    # Verify call reference resolved to utils.helper
    c.execute(
        'SELECT symbol_fqname, ref_kind FROM "references" '
        'WHERE file_path="main.py" AND ref_kind="call"'
    )
    call_row = c.fetchone()
    assert call_row is not None
    assert call_row[0] == "utils.helper"

    conn.close()


def test_index_offline_without_strict_tier1_falls_back(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    code = main(
        ["index", str(copy_fixture("python", tmp_path)), "--offline", "--cache-dir", str(cache)]
    )
    assert code == EXIT_DELTA_DETECTED


def test_python_sibling_projects_keep_unique_fqnames(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    for service in ("users", "orders"):
        app = repo / "services" / service / "app"
        app.mkdir(parents=True)
        (repo / "services" / service / "pyproject.toml").write_text("[project]\n")
        (app / "__init__.py").write_text("")
        (app / "db.py").write_text("def get_session():\n    return None\n")
        (app / "main.py").write_text(
            "from app.db import get_session\n\ndef run():\n    get_session()\n"
        )

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    conn = sqlite3.connect(next((cache / "repos").glob("*/index.db")))
    c = conn.cursor()
    c.execute('SELECT fqname FROM symbols WHERE name="get_session" ORDER BY fqname')
    assert [row[0] for row in c.fetchall()] == [
        "services.orders.app.db.get_session",
        "services.users.app.db.get_session",
    ]
    for service in ("users", "orders"):
        c.execute(
            'SELECT DISTINCT symbol_fqname FROM "references" WHERE file_path=?',
            (f"services/{service}/app/main.py",),
        )
        assert {row[0] for row in c.fetchall()} == {f"services.{service}.app.db.get_session"}
    c.execute("SELECT COUNT(*) FROM unresolved_refs")
    assert c.fetchone()[0] == 0
    conn.close()


def test_commonjs_require_shadows_repo_names(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "util.ts").write_text("export function format(v: string) { return v; }\n")
    (repo / "a.js").write_text("const { format } = require('date-fns');\nformat(d);\n")
    (repo / "b.js").write_text("const format = require('date-fns/format');\nformat(d);\n")

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    conn = sqlite3.connect(next((cache / "repos").glob("*/index.db")))
    c = conn.cursor()
    c.execute(
        'SELECT DISTINCT symbol_fqname FROM "references" '
        'WHERE file_path IN ("a.js", "b.js") AND ref_kind="call"'
    )
    assert {row[0] for row in c.fetchall()} == {"require", "format"}
    conn.close()


def test_unit_detection_ignores_excluded_names_above_repo(tmp_path: Path, capsys):
    repo = tmp_path / "build" / "app"
    for module in ("svc", "web"):
        src = repo / module / "src"
        src.mkdir(parents=True)
        (repo / module / "pom.xml").write_text(
            f"<project><artifactId>{module}</artifactId></project>"
        )
        (src / "A.java").write_text("package x;\nclass A {}\n")
        (repo / module / "tsconfig.json").write_text("{}")
        (src / "a.ts").write_text("export function a() {}\n")
    (repo / "pom.xml").write_text("<project><artifactId>root</artifactId></project>")

    from arch_indexer.plugins.java_plugin import JavaPlugin
    from arch_indexer.plugins.typescript_plugin import TypeScriptPlugin

    assert [u.unit_id for u in JavaPlugin().detect(repo)] == ["java:svc", "java:web"]
    assert [u.unit_id for u in TypeScriptPlugin().detect(repo)] == [
        "typescript:svc",
        "typescript:web",
    ]


def test_colliding_unit_ids_keep_each_units_definitions(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    nested = repo / "root"
    nested.mkdir(parents=True)
    (nested / "tsconfig.json").write_text("{}")
    (nested / "component.ts").write_text("export function component() { return 1; }\n")
    (repo / "index.ts").write_text("export function main() { return 2; }\n")

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT path FROM files ORDER BY path")
    assert [row[0] for row in c.fetchall()] == ["index.ts", "root/component.ts"]
    c.execute("SELECT name, COUNT(*) FROM symbols GROUP BY name ORDER BY name")
    assert c.fetchall() == [("component", 1), ("main", 1)]
    conn.close()


def test_offline_strict_tier1_infra_only_repo_succeeds(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    code = main(
        [
            "index",
            str(copy_fixture("infra", tmp_path)),
            "--offline",
            "--strict-tier1",
            "--cache-dir",
            str(cache),
        ]
    )
    assert code == EXIT_DELTA_DETECTED
    assert "infra: 5 [tree-sitter]" in capsys.readouterr().out


def test_offline_strict_tier1_failure_keeps_existing_index(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = copy_fixture("python", tmp_path)

    assert main(["index", str(repo), "--cache-dir", str(cache)]) == EXIT_DELTA_DETECTED
    db_path = next((cache / "repos").glob("*/index.db"))
    conn = sqlite3.connect(db_path)
    before = conn.execute("SELECT path, sha256 FROM files ORDER BY path").fetchall()
    conn.close()
    assert before

    code = main(["index", str(repo), "--offline", "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_TOOLCHAIN_MISSING
    capsys.readouterr()

    conn = sqlite3.connect(db_path)
    after = conn.execute("SELECT path, sha256 FROM files ORDER BY path").fetchall()
    conn.close()
    assert after == before
