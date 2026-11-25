import json
from pathlib import Path

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_ERROR, EXIT_OK, main
from arch_indexer.graph import load_graph
from arch_indexer.mermaid_gen import diagram_ids
from arch_indexer.planner import extract_delta_slice
from arch_indexer.source_scope import is_test_path

TEST_REPO_FILES = {
    "server/app.ts": (
        "const app = express();\n"
        "\n"
        "function listOrders(req: any, res: any) {\n"
        "  res.json([]);\n"
        "}\n"
        "\n"
        "app.get('/orders', listOrders);\n"
    ),
    "tests/orders.test.ts": (
        "test('lists orders', async () => {\n  await fetch('/test-only/orders');\n});\n"
    ),
    "tests/helpers.ts": (
        "export function callHelper() {\n  fetch('/test-only/helper');\n  return null;\n}\n"
    ),
}


def _write_repo(path: Path) -> Path:
    for rel_path, content in TEST_REPO_FILES.items():
        target = path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return path


def _index(repo: Path, tmp_path: Path, *extra: str):
    return main(
        [
            "index",
            "--offline",
            str(repo),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--docs-dir",
            "docs",
            *extra,
        ]
    )


def _plan(repo: Path, tmp_path: Path) -> dict:
    code = main(["plan", str(repo), "--cache-dir", str(tmp_path / "cache")])
    assert code == EXIT_OK
    work_plan = next((tmp_path / "cache" / "repos").glob("*/work-plan.json"))
    return json.loads(work_plan.read_text(encoding="utf-8"))


def _query(repo: Path, tmp_path: Path, *args: str, capsys) -> int:
    capsys.readouterr()
    return main(["query", str(repo), *args, "--cache-dir", str(tmp_path / "cache")])


def test_test_path_rules():
    assert is_test_path("tests/test_app.py")
    assert is_test_path("test/test_app.py")
    assert is_test_path("tests/helpers.py")
    assert is_test_path("src/package/__tests__/button.tsx")
    assert is_test_path("src/app.test.ts")
    assert is_test_path("src/app.spec.tsx")
    assert is_test_path("server/orders_test.go")
    assert is_test_path("src/test/java/com/acme/OrdersIT.java")
    assert is_test_path("conftest.py")
    assert is_test_path("src/pkg/test_utils.py")

    assert not is_test_path("src/app.py")
    assert not is_test_path("src/latest.py")
    assert not is_test_path("src/contest.py")
    assert not is_test_path("server/app.ts")
    assert not is_test_path("src/testing_support.ts")


def test_tests_are_not_architecture_by_default(tmp_path: Path, capsys):
    repo = _write_repo(tmp_path / "repo")
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED

    graph = load_graph(repo / "docs" / "graph.json")
    assert graph["include_tests"] is False
    assert {module["name"] for module in graph["modules"]} == {"server"}
    assert {node["file"] for node in graph["nodes"]} == {"server/app.ts"}
    assert not [node for node in graph["nodes"] if node["kind"] == "frontend_caller"]
    diagrams = diagram_ids(graph)
    assert "flow-orders" in diagrams
    assert "flow-test-only" not in diagrams

    master_index = json.loads(
        next((tmp_path / "cache" / "repos").glob("*/master-index.json")).read_text(encoding="utf-8")
    )
    assert [module["name"] for module in master_index["modules"]] == ["server"]
    assert master_index["include_tests"] is False
    assert master_index["summary"]["total_files"] == 1
    assert master_index["summary"]["total_entrypoints"] == 1

    plan = _plan(repo, tmp_path)
    assert plan["packets"]
    assert all("tests" not in packet["assigned_modules"] for packet in plan["packets"]), plan[
        "packets"
    ]
    assert not [packet for packet in plan["packets"] if packet["packet_id"].endswith("-mod-tests")]

    assert _query(repo, tmp_path, "entrypoints", "--json", capsys=capsys) == EXIT_OK
    entrypoints = json.loads(capsys.readouterr().out)
    assert entrypoints["count"] == 1
    assert [row["file_path"] for row in entrypoints["entrypoints"]] == ["server/app.ts"]

    assert _query(repo, tmp_path, "module-summary", "tests", capsys=capsys) == EXIT_ERROR
    assert "module not found" in capsys.readouterr().err


def test_include_tests_flag_restores_test_architecture(tmp_path: Path, capsys):
    repo = _write_repo(tmp_path / "repo")
    assert _index(repo, tmp_path, "--include-tests") == EXIT_DELTA_DETECTED

    graph = load_graph(repo / "docs" / "graph.json")
    assert graph["include_tests"] is True
    assert {module["name"] for module in graph["modules"]} == {"server", "tests"}
    assert {node["file"] for node in graph["nodes"]} == {
        "server/app.ts",
        "tests/helpers.ts",
        "tests/orders.test.ts",
    }
    diagrams = diagram_ids(graph)
    assert "flow-orders" in diagrams
    assert "flow-test-only" in diagrams

    master_index = json.loads(
        next((tmp_path / "cache" / "repos").glob("*/master-index.json")).read_text(encoding="utf-8")
    )
    assert {module["name"] for module in master_index["modules"]} == {"server", "tests"}
    assert master_index["include_tests"] is True
    assert master_index["summary"]["total_files"] == 3

    plan = _plan(repo, tmp_path)
    assert [packet for packet in plan["packets"] if "tests" in packet["assigned_modules"]]

    assert _query(repo, tmp_path, "entrypoints", "--include-tests", "--json", capsys=capsys) == (
        EXIT_OK
    )
    entrypoints = json.loads(capsys.readouterr().out)
    assert {row["file_path"] for row in entrypoints["entrypoints"]} == {
        "server/app.ts",
        "tests/helpers.ts",
        "tests/orders.test.ts",
    }
    assert _query(repo, tmp_path, "module-summary", "tests", "--include-tests", capsys=capsys) == (
        EXIT_OK
    )


def _write_canonical_docs(repo: Path) -> None:
    docs = repo / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "DESIGN.md").write_text(
        "# Design\n\n"
        "## 2. Components & Modules\n\n"
        "`listOrders` handles `/orders`[^1].\n\n"
        "## References\n\n"
        "[^1]: `server/app.ts:3` \u2014 `listOrders`\n",
        encoding="utf-8",
    )
    (docs / "USER_GUIDE.md").write_text(
        "# User Guide\n\n"
        "## 1. System Overview\n\n"
        "Orders are listed via `listOrders`[^1].\n\n"
        "## References\n\n"
        "[^1]: `server/app.ts:3` \u2014 `listOrders`\n",
        encoding="utf-8",
    )


def test_test_only_delta_exits_ok_by_default_and_purges_deleted_test(tmp_path: Path, capsys):
    repo = _write_repo(tmp_path / "repo")
    _write_canonical_docs(repo)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED

    (repo / "tests" / "orders.test.ts").write_text(
        "test('lists orders updated', async () => {\n  await fetch('/test-only/orders');\n});\n",
        encoding="utf-8",
    )
    (repo / "tests" / "added.test.ts").write_text(
        "test('added', () => {\n  return 1;\n});\n",
        encoding="utf-8",
    )
    (repo / "tests" / "helpers.ts").unlink()

    capsys.readouterr()
    assert _index(repo, tmp_path) == EXIT_OK
    stdout = capsys.readouterr().out
    assert "No changes: documentation is up to date" in stdout

    delta_payload = json.loads(
        next((tmp_path / "cache" / "repos").glob("*/index-delta.json")).read_text(encoding="utf-8")
    )
    assert all(count == 0 for count in delta_payload["counts"].values())

    assert _query(repo, tmp_path, "file-symbols", "tests/helpers.ts", capsys=capsys) == EXIT_ERROR


def test_test_only_delta_exits_delta_detected_when_tests_included(tmp_path: Path):
    repo = _write_repo(tmp_path / "repo")
    _write_canonical_docs(repo)
    assert _index(repo, tmp_path, "--include-tests") == EXIT_DELTA_DETECTED

    (repo / "tests" / "orders.test.ts").write_text(
        "test('lists orders updated', async () => {\n  await fetch('/test-only/orders');\n});\n",
        encoding="utf-8",
    )
    assert _index(repo, tmp_path, "--include-tests") == EXIT_DELTA_DETECTED


def test_move_only_product_shift_with_test_edit_auto_relocates_citations(tmp_path: Path):
    repo = _write_repo(tmp_path / "repo")
    _write_canonical_docs(repo)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED

    (repo / "server" / "app.ts").write_text(
        "\n\n" + TEST_REPO_FILES["server/app.ts"],
        encoding="utf-8",
    )
    (repo / "tests" / "orders.test.ts").write_text(
        "test('lists orders modified', async () => {\n  await fetch('/test-only/v2');\n});\n",
        encoding="utf-8",
    )

    assert _index(repo, tmp_path, "--auto-relocate-citations") == EXIT_OK
    design_text = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "server/app.ts:5" in design_text


def test_extract_delta_slice_excludes_deleted_colocated_test_by_default():
    delta = {
        "is_first_run": False,
        "added_files": [],
        "modified_files": ["server/app.ts"],
        "deleted_files": ["server/app.test.ts", "server/old.ts"],
        "added_symbols": [],
        "modified_symbols": [],
        "moved_symbols": [],
        "deleted_symbols": [
            {"file_path": "server/app.test.ts", "start_line": 1, "fqname": "testListOrders"},
            {"file_path": "server/old.ts", "start_line": 1, "fqname": "oldHandler"},
        ],
    }
    module_files = {"server": ["server/app.ts"]}
    module_paths = {"server": "server"}

    excluded = extract_delta_slice(
        delta,
        ["server"],
        module_files,
        [],
        module_paths=module_paths,
        include_tests=False,
    )
    assert excluded["deleted_files"] == ["server/old.ts"]
    assert [s["file_path"] for s in excluded["deleted_symbols"]] == ["server/old.ts"]

    included = extract_delta_slice(
        delta,
        ["server"],
        module_files,
        [],
        module_paths=module_paths,
        include_tests=True,
    )
    assert included["deleted_files"] == ["server/app.test.ts", "server/old.ts"]
    assert [s["file_path"] for s in included["deleted_symbols"]] == [
        "server/app.test.ts",
        "server/old.ts",
    ]


def test_index_stdout_summary_matches_master_index_scope(tmp_path: Path, capsys):
    """Printed summary counts must equal master-index.json counts (FQD run N2)."""
    repo = _write_repo(tmp_path / "repo")
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    out = capsys.readouterr().out
    master_index = json.loads(
        next((tmp_path / "cache" / "repos").glob("*/master-index.json")).read_text(encoding="utf-8")
    )
    summary = master_index["summary"]

    assert summary["total_files"] == 1
    assert summary["excluded_test_files"] == 2
    assert f"Indexed {summary['total_files']} files" in out
    assert f"{summary['total_entrypoints']} entrypoints," in out
    assert (
        f"{summary['total_unresolved_refs']} unresolved refs "
        f"({summary['unresolved_external_refs']} external, "
        f"{summary['unresolved_in_repo_refs']} in-repo)"
    ) in out
    assert f"[{summary['excluded_test_files']} test file(s) excluded]" in out

    included_repo = _write_repo(tmp_path / "repo_included")
    included_cache = tmp_path / "cache_included"
    assert (
        main(
            [
                "index",
                "--offline",
                str(included_repo),
                "--cache-dir",
                str(included_cache),
                "--include-tests",
            ]
        )
        == EXIT_DELTA_DETECTED
    )
    included_out = capsys.readouterr().out
    included_master = json.loads(
        next((included_cache / "repos").glob("*/master-index.json")).read_text(encoding="utf-8")
    )
    included_summary = included_master["summary"]
    assert included_summary["total_files"] == 3
    assert included_summary["excluded_test_files"] == 0
    assert f"Indexed {included_summary['total_files']} files" in included_out
    assert "test file(s) excluded" not in included_out


def _write_reference_fixture(path: Path) -> Path:
    files = {
        "server/app.ts": (
            "export function listOrders(req: any, res: any) {\n"
            "  res.json([]);\n"
            "}\n"
            "\n"
            "export function handleOrders(req: any, res: any) {\n"
            "  listOrders(req, res);\n"
            "  fetch('/orders');\n"
            "}\n"
        ),
        "tests/orders.test.ts": (
            "import { listOrders } from '../server/app';\n"
            "\n"
            "export function runOrdersTest() {\n"
            "  listOrders({}, {});\n"
            "  fetch('/test-only');\n"
            "}\n"
        ),
    }
    for rel_path, content in files.items():
        target = path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return path


def test_refs_and_flow_trace_hide_test_files_unless_included(tmp_path: Path, capsys):
    """query refs/flow-trace must respect the test scope (FQD run N3)."""
    repo = _write_reference_fixture(tmp_path / "repo")
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    assert _query(repo, tmp_path, "refs", "listOrders", "--json", capsys=capsys) == EXIT_OK
    scoped_refs = json.loads(capsys.readouterr().out)
    assert scoped_refs["references"]
    assert all(is_test_path(row["file_path"]) is False for row in scoped_refs["references"])
    assert any(row["file_path"] == "server/app.ts" for row in scoped_refs["references"])

    assert (
        _query(
            repo,
            tmp_path,
            "refs",
            "listOrders",
            "--include-tests",
            "--json",
            capsys=capsys,
        )
        == EXIT_OK
    )
    all_refs = json.loads(capsys.readouterr().out)
    assert any(row["file_path"] == "tests/orders.test.ts" for row in all_refs["references"])
    assert all_refs["count"] > scoped_refs["count"]

    assert (
        _query(repo, tmp_path, "entrypoints", "--include-tests", "--json", capsys=capsys) == EXIT_OK
    )
    entrypoints = json.loads(capsys.readouterr().out)["entrypoints"]
    test_entrypoint = next(row for row in entrypoints if is_test_path(row["file_path"]))

    code = _query(repo, tmp_path, "flow-trace", str(test_entrypoint["id"]), capsys=capsys)
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "entrypoint not found" in captured.err
    assert captured.out == ""

    assert (
        _query(
            repo,
            tmp_path,
            "flow-trace",
            str(test_entrypoint["id"]),
            "--include-tests",
            capsys=capsys,
        )
        == EXIT_OK
    )
    included_trace = capsys.readouterr().out
    assert "tests/orders.test.ts" in included_trace
