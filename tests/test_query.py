import json
from pathlib import Path

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_ERROR, EXIT_OK, main
from arch_indexer.store import IndexStore

FIXTURE_FILES = {
    "app/__init__.py": "",
    "app/main.py": """\
from fastapi import FastAPI

from app.service import create_order

app = FastAPI()


@app.post("/orders")
def handle_order():
    return create_order()
""",
    "app/service.py": """\
from app.audit import log_event
from app.store import save_order


def create_order():
    save_order()
    log_event()
    return {}
""",
    "app/store.py": """\
def save_order():
    db.session.execute("INSERT INTO orders VALUES (1)")
""",
    "app/audit.py": """\
import requests


def log_event():
    requests.post("https://audit.example/events")
""",
}


def _write_fixture(repo: Path) -> None:
    for rel_path, content in FIXTURE_FILES.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def _index(repo: Path, cache: Path, capsys) -> None:
    assert main(["index", str(repo), "--cache-dir", str(cache)]) == EXIT_DELTA_DETECTED
    capsys.readouterr()  # discard the indexer summary so query tests see only query output


def _query(capsys, *args: str) -> tuple[int, str, str]:
    code = main(["query", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _json(out: str) -> dict:
    return json.loads(out)


def test_query_missing_index_exits_error_without_creating_cache(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    code, out, err = _query(capsys, str(repo), "def", "anything", "--cache-dir", str(cache))

    assert code == EXIT_ERROR
    assert out == ""
    assert "no index found" in err
    assert "arch-indexer index" in err
    assert not (cache / "repos").exists()


def test_def_resolves_name_and_fqname_and_rejects_unknown(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, err = _query(capsys, str(repo), "def", "create_order", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert out.strip() == "app/service.py:5 function app.service.create_order"
    assert err == ""

    code, out, _ = _query(
        capsys, str(repo), "def", "app.service.create_order", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    assert out.strip() == "app/service.py:5 function app.service.create_order"

    code, out, err = _query(capsys, str(repo), "def", "missing_symbol", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "symbol not found: missing_symbol" in err


def test_def_json(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, _ = _query(
        capsys, str(repo), "def", "create_order", "--json", "--cache-dir", str(cache)
    )

    assert code == EXIT_OK
    payload = _json(out)
    assert payload["count"] == 1
    definition = payload["definitions"][0]
    assert definition["fqname"] == "app.service.create_order"
    assert (definition["file_path"], definition["start_line"]) == ("app/service.py", 5)


def test_refs_limit_and_json(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, err = _query(capsys, str(repo), "refs", "create_order", "--cache-dir", str(cache))
    assert code == EXIT_OK
    rows = out.strip().splitlines()
    assert rows == [
        "app/main.py:3 import app.service.create_order",
        "app/main.py:10 call app.service.create_order",
    ]
    assert err == ""

    code, out, err = _query(
        capsys,
        str(repo),
        "refs",
        "create_order",
        "--limit",
        "1",
        "--json",
        "--cache-dir",
        str(cache),
    )
    assert code == EXIT_OK
    payload = _json(out)
    assert payload["count"] == 2
    assert payload["returned"] == 1
    assert payload["truncated"] is True
    assert len(payload["references"]) == 1
    assert "showing 1 of 2 references" in err

    code, out, err = _query(capsys, str(repo), "refs", "missing_symbol", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "symbol not found: missing_symbol" in err


def test_file_symbols_and_unknown_file(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, _ = _query(
        capsys, str(repo), "file-symbols", "app/service.py", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    assert out.strip() == "app/service.py:5 function app.service.create_order"

    code, out, err = _query(
        capsys, str(repo), "file-symbols", "app/missing.py", "--cache-dir", str(cache)
    )
    assert code == EXIT_ERROR
    assert out == ""
    assert "file not indexed: app/missing.py" in err


def test_entrypoints_filter_and_json(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, _ = _query(
        capsys, str(repo), "entrypoints", "--module", "app", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    assert out.strip() == "app/main.py:9 http POST /orders app.main.handle_order id=1"

    code, out, _ = _query(capsys, str(repo), "entrypoints", "--json", "--cache-dir", str(cache))
    assert code == EXIT_OK
    payload = _json(out)
    assert payload["count"] == 1
    entrypoint = payload["entrypoints"][0]
    assert entrypoint["route_or_trigger"] == "/orders"
    assert entrypoint["symbol_fqname"] == "app.main.handle_order"
    assert entrypoint["line"] == 9
    assert entrypoint["symbol_line"] == entrypoint["line"]
    assert entrypoint["module"] == "app.main"

    code, out, err = _query(
        capsys, str(repo), "entrypoints", "--module", "nope", "--cache-dir", str(cache)
    )
    assert code == EXIT_ERROR
    assert out == ""
    assert "module not found: nope" in err


def test_module_summary_text_and_json(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, _ = _query(capsys, str(repo), "module-summary", "app", "--cache-dir", str(cache))
    assert code == EXIT_OK
    lines = out.strip().splitlines()
    assert lines[0] == "# module app files=5 symbols=5 entrypoints=1"
    assert "app/store.py:1 python tree-sitter" in lines
    assert "app/store.py:1 function app.store.save_order" in lines
    assert "app/main.py:9 http POST /orders app.main.handle_order id=1" in lines

    code, out, _ = _query(
        capsys, str(repo), "module-summary", "app.service", "--json", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    payload = _json(out)
    assert payload["module"] == "app.service"
    assert payload["counts"] == {"files": 1, "symbols": 1, "entrypoints": 0}
    assert payload["files"][0]["path"] == "app/service.py"

    code, out, err = _query(capsys, str(repo), "module-summary", "nope", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "module not found: nope" in err


def test_module_summary_accepts_graph_module_ids_and_bare_keys(tmp_path: Path, capsys):
    """Work packets name modules mod:<key> (graph/master-index); module-summary must accept it."""
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    graph = json.loads((repo / "docs" / "graph.json").read_text(encoding="utf-8"))
    master = json.loads(
        next((cache / "repos").glob("*/master-index.json")).read_text(encoding="utf-8")
    )
    master_by_id = {module["id"]: module for module in master["modules"]}
    assert {module["id"] for module in graph["modules"]} == set(master_by_id)

    for record in graph["modules"]:
        code, out, _ = _query(
            capsys,
            str(repo),
            "module-summary",
            record["id"],
            "--json",
            "--cache-dir",
            str(cache),
        )
        assert code == EXIT_OK
        payload = _json(out)
        assert [file["path"] for file in payload["files"]] == master_by_id[record["id"]]["files"]

        # The bare key is the other spelling a packet or master-index reader may use.
        code, out, _ = _query(
            capsys,
            str(repo),
            "module-summary",
            record["name"],
            "--json",
            "--cache-dir",
            str(cache),
        )
        assert code == EXIT_OK
        assert set(master_by_id[record["id"]]["files"]) <= {
            file["path"] for file in _json(out)["files"]
        }

    # The dotted package namespace keeps selecting a package slice.
    code, out, _ = _query(
        capsys, str(repo), "module-summary", "app.service", "--json", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    payload = _json(out)
    assert [file["path"] for file in payload["files"]] == ["app/service.py"]

    # An explicit mod: id does not fall back to dotted package prefixes.
    code, _, err = _query(
        capsys, str(repo), "module-summary", "mod:app.service", "--json", "--cache-dir", str(cache)
    )
    assert code == EXIT_ERROR
    assert "module not found: mod:app.service" in err

    entry_module = next(module for module in master["modules"] if "app/main.py" in module["files"])
    code, out, _ = _query(
        capsys,
        str(repo),
        "entrypoints",
        "--module",
        entry_module["id"],
        "--cache-dir",
        str(cache),
    )
    assert code == EXIT_OK
    assert "app.main.handle_order" in out


def test_entrypoint_symbol_line_is_the_citation_definition_line(tmp_path: Path, capsys):
    """entrypoints exposes the definition line that check-citations accepts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source_lines = ["x = 1"] * 8 + ["def handler():", "    return 1"]
    source_path = repo / "src" / "app.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"

    with IndexStore.from_repo(repo, cache_dir_override=cache) as store:
        store.insert_files(
            [
                {
                    "path": "src/app.py",
                    "language": "python",
                    "sha256": "a" * 64,
                    "line_count": len(source_lines),
                    "module": "src.app",
                    "tier": "tree-sitter",
                }
            ]
        )
        store.insert_symbols(
            [
                {
                    "fqname": "src.app.handler",
                    "name": "handler",
                    "kind": "function",
                    "file_path": "src/app.py",
                    "start_line": 9,
                    "end_line": 10,
                    "body_sha256": "b" * 64,
                }
            ]
        )
        store.insert_entrypoints(
            [
                {
                    "kind": "http",
                    "name": "handler",
                    "method": "GET",
                    "route_or_trigger": "/items",
                    "file_path": "src/app.py",
                    "line": 3,
                    "symbol_fqname": "src.app.handler",
                    "auth_symbols_json": "[]",
                }
            ]
        )

    code, out, _ = _query(capsys, str(repo), "entrypoints", "--json", "--cache-dir", str(cache))
    assert code == EXIT_OK
    entrypoint = _json(out)["entrypoints"][0]
    assert (entrypoint["line"], entrypoint["symbol_line"]) == (3, 9)

    code, out, _ = _query(capsys, str(repo), "entrypoints", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert out.strip().endswith("symbol_line=9")

    draft = repo / "docs" / ".drafts" / "packet.md"
    draft.parent.mkdir(parents=True)

    # The registration/annotation line is not a definition: the citation check rejects it.
    draft.write_text(
        "Items are served[^1].\n\n[^1]: `src/app.py:3` \u2014 `handler`\n",
        encoding="utf-8",
    )
    code = main(["check-citations", str(repo), "--file", "docs/.drafts/packet.md"])
    assert code == 3

    # Citing symbol_line follows the one convention check-citations enforces.
    draft.write_text(
        "Items are served[^1].\n\n[^1]: `src/app.py:9` \u2014 `handler`\n",
        encoding="utf-8",
    )
    code = main(["check-citations", str(repo), "--file", "docs/.drafts/packet.md"])
    assert code == 0


def test_entrypoint_symbol_line_falls_back_for_cross_file_handler(tmp_path: Path, capsys):
    """Cross-file handlers keep symbol_line in the entrypoint's file so citations stay valid."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "routes.ts").write_text(
        'import { getItems } from "./handlers";\napp.get("/items", getItems);\n',
        encoding="utf-8",
    )
    (repo / "src" / "handlers.ts").write_text(
        "\n" * 19 + "export function getItems() {\n  return [];\n}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"

    with IndexStore.from_repo(repo, cache_dir_override=cache) as store:
        store.insert_files(
            [
                {
                    "path": "src/routes.ts",
                    "language": "typescript",
                    "sha256": "a" * 64,
                    "line_count": 2,
                    "module": "src.routes",
                    "tier": "tree-sitter",
                },
                {
                    "path": "src/handlers.ts",
                    "language": "typescript",
                    "sha256": "b" * 64,
                    "line_count": 22,
                    "module": "src.handlers",
                    "tier": "tree-sitter",
                },
            ]
        )
        store.insert_symbols(
            [
                {
                    "fqname": "src.handlers.getItems",
                    "name": "getItems",
                    "kind": "function",
                    "file_path": "src/handlers.ts",
                    "start_line": 20,
                    "end_line": 22,
                    "body_sha256": "c" * 64,
                }
            ]
        )
        store.insert_entrypoints(
            [
                {
                    "kind": "http",
                    "name": "getItems",
                    "method": "GET",
                    "route_or_trigger": "/items",
                    "file_path": "src/routes.ts",
                    "line": 2,
                    "symbol_fqname": "src.handlers.getItems",
                    "auth_symbols_json": "[]",
                }
            ]
        )

    code, out, _ = _query(capsys, str(repo), "entrypoints", "--json", "--cache-dir", str(cache))
    assert code == EXIT_OK
    entrypoint = _json(out)["entrypoints"][0]
    assert entrypoint["file_path"] == "src/routes.ts"
    assert (entrypoint["line"], entrypoint["symbol_line"]) == (2, 2)

    code, out, _ = _query(capsys, str(repo), "entrypoints", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert "symbol_line=" not in out

    draft = repo / "docs" / ".drafts" / "packet.md"
    draft.parent.mkdir(parents=True)
    citation = f"{entrypoint['file_path']}:{entrypoint['symbol_line']}"
    draft.write_text(
        f"Items are served[^1].\n\n[^1]: `{citation}` \u2014 `getItems`\n",
        encoding="utf-8",
    )
    code = main(
        [
            "check-citations",
            str(repo),
            "--file",
            "docs/.drafts/packet.md",
            "--cache-dir",
            str(cache),
        ]
    )
    assert code == 0


def test_flow_trace_walks_calls_and_marks_sinks(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, _ = _query(capsys, str(repo), "flow-trace", "1", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert out.strip().splitlines() == [
        "0 app/main.py:9 app.main.handle_order",
        "1 app/main.py:10 app.main.handle_order -> app.service.create_order",
        "2 app/service.py:6 app.service.create_order -> app.store.save_order",
        "2 app/service.py:7 app.service.create_order -> app.audit.log_event",
        "3 app/store.py:2 [datastore] app.store.save_order -> db.session.execute",
        "3 app/audit.py:5 [external-sink] app.audit.log_event -> requests.post",
    ]

    code, out, _ = _query(capsys, str(repo), "flow-trace", "1", "--json", "--cache-dir", str(cache))
    assert code == EXIT_OK
    payload = _json(out)
    assert payload["entrypoint"]["id"] == 1
    assert payload["root_symbol"]["fqname"] == "app.main.handle_order"
    sinks = {step["callee"]: step["sink"] for step in payload["steps"] if step["sink"]}
    assert sinks == {
        "db.session.execute": "datastore",
        "requests.post": "external-sink",
    }


def test_flow_trace_depth_bound_and_unknown_entrypoint(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, _ = _query(
        capsys, str(repo), "flow-trace", "1", "--depth", "2", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    lines = out.strip().splitlines()
    assert len(lines) == 4
    assert not any("datastore" in line or "external-sink" in line for line in lines)

    code, out, err = _query(capsys, str(repo), "flow-trace", "999", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "entrypoint not found: 999" in err

    code, out, err = _query(capsys, str(repo), "flow-trace", "not-an-id", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "invalid entrypoint id: not-an-id" in err

    code, out, err = _query(capsys, str(repo), "flow-trace", "ep:1", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "invalid entrypoint id: ep:1" in err


def test_query_output_is_deterministic(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    commands = [
        ("def", "create_order"),
        ("refs", "create_order"),
        ("file-symbols", "app/service.py"),
        ("entrypoints",),
        ("module-summary", "app"),
        ("flow-trace", "1"),
    ]
    for command in commands:
        first_code, first_out, _ = _query(capsys, str(repo), *command, "--cache-dir", str(cache))
        second_code, second_out, _ = _query(capsys, str(repo), *command, "--cache-dir", str(cache))
        assert (first_code, first_out) == (second_code, second_out)


def _entrypoint_id(capsys, repo: Path, cache: Path, kind: str) -> int:
    code, out, err = _query(capsys, str(repo), "entrypoints", "--json", "--cache-dir", str(cache))
    assert code == EXIT_OK, err
    matches = [e for e in _json(out)["entrypoints"] if e["kind"] == kind]
    assert len(matches) == 1, matches
    return matches[0]["id"]


def test_flow_trace_expands_project_symbols_named_like_sinks(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text(
        """\
from fastapi import FastAPI

from app.client import fetch

app = FastAPI()


@app.get("/data")
def handler():
    return fetch("/data")
"""
    )
    (repo / "app" / "client.py").write_text(
        """\
import requests


def fetch(url):
    return _load(url)


def _load(url):
    return requests.get(url)
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    entrypoint_id = _entrypoint_id(capsys, repo, cache, "http")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(entrypoint_id), "--cache-dir", str(cache)
    )

    assert code == EXIT_OK, err
    assert out.strip().splitlines() == [
        "0 app/main.py:9 app.main.handler",
        "1 app/main.py:10 app.main.handler -> app.client.fetch",
        "2 app/client.py:5 app.client.fetch -> app.client._load",
        "3 app/client.py:9 [external-sink] app.client._load -> requests.get",
    ]


def test_flow_trace_does_not_label_root_symbol_as_sink(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text(
        """\
import requests
from fastapi import FastAPI

app = FastAPI()


@app.get("/ping")
def fetch():
    return requests.get("https://example.test")
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    entrypoint_id = _entrypoint_id(capsys, repo, cache, "http")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(entrypoint_id), "--cache-dir", str(cache)
    )

    assert code == EXIT_OK, err
    assert out.strip().splitlines() == [
        "0 app/main.py:8 app.main.fetch",
        "1 app/main.py:9 [external-sink] app.main.fetch -> requests.get",
    ]


def test_flow_trace_drops_bare_open_fetch_got_and_matches_exact_axios(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text(
        """\
import tarfile

from fastapi import FastAPI

app = FastAPI()


@app.get("/archive")
def unpack():
    tarfile.open("a.tar")
    open("b.txt")
    fetch("https://example.test")
    got("https://example.test")
    client.axios("https://example.test")
    return axios("https://example.test")
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    entrypoint_id = _entrypoint_id(capsys, repo, cache, "http")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(entrypoint_id), "--cache-dir", str(cache)
    )

    assert code == EXIT_OK, err
    assert out.strip().splitlines() == [
        "0 app/main.py:9 app.main.unpack",
        "1 app/main.py:10 app.main.unpack -> tarfile.open",
        "1 app/main.py:11 app.main.unpack -> open",
        "1 app/main.py:12 app.main.unpack -> fetch",
        "1 app/main.py:13 app.main.unpack -> got",
        "1 app/main.py:14 app.main.unpack -> client.axios",
        "1 app/main.py:15 [external-sink] app.main.unpack -> axios",
    ]


def test_flow_trace_does_not_label_ambiguous_session_calls_as_datastore(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text(
        """\
import requests
from fastapi import FastAPI

app = FastAPI()


@app.get("/session")
def call():
    session = requests.Session()
    session.get("https://example.test")
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    entrypoint_id = _entrypoint_id(capsys, repo, cache, "http")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(entrypoint_id), "--cache-dir", str(cache)
    )

    assert code == EXIT_OK, err
    assert out.strip().splitlines() == [
        "0 app/main.py:8 app.main.call",
        "1 app/main.py:9 app.main.call -> requests.Session",
        "1 app/main.py:10 app.main.call -> session.get",
    ]


def test_flow_trace_resolves_module_scope_entrypoint_via_synthetic_symbols(tmp_path: Path, capsys):
    """Slice 3 (§4.3): flow-trace resolves main/cli via synthetic_symbols."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "cli.py").write_text(
        """\
def build_parser():
    from argparse import ArgumentParser

    return ArgumentParser()


def run():
    import requests

    requests.get("https://example.test")


if __name__ == "__main__":
    build_parser().parse_args()
    run()
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    cli_id = _entrypoint_id(capsys, repo, cache, "cli")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(cli_id), "--json", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK, err
    assert json.loads(out)["root_symbol"]["fqname"].startswith("app.cli.")

    main_id = _entrypoint_id(capsys, repo, cache, "main")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(main_id), "--json", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["root_symbol"]["fqname"] == "app.cli.__main__"

    # Synthetic symbols remain hidden from query def / query file-symbols.
    fs_code, fs_out, _ = _query(
        capsys, str(repo), "file-symbols", "app/cli.py", "--cache-dir", str(cache)
    )
    assert fs_code == EXIT_OK
    assert "__main__" not in fs_out
    assert "<module>" not in fs_out


def test_flow_trace_roots_at_synthetic_block_instead_of_a_later_symbol(tmp_path: Path, capsys):
    """Slice 3 (§4.3): main entrypoint resolves its __main__ block, never a later function."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "script.py").write_text(
        """\
import requests


def run():
    requests.get("https://example.test")


if __name__ == "__main__":
    run()


def later():
    return None
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    main_id = _entrypoint_id(capsys, repo, cache, "main")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(main_id), "--json", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["root_symbol"]["fqname"] == "app.script.__main__"
    assert "later" not in json.dumps(payload)


def test_flow_trace_errors_for_module_level_typescript_api_call(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.ts").write_text(
        """\
const payload = {};

fetch("https://api.example.test/data", payload);

export function unrelated(): number {
  return 1;
}
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    api_call_id = _entrypoint_id(capsys, repo, cache, "api_call")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(api_call_id), "--cache-dir", str(cache)
    )
    assert code == EXIT_ERROR
    assert out == ""
    assert "no traceable symbol" in err


def test_flow_trace_labels_library_sinks_by_qualified_suffix(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text(
        """\
import sqlalchemy
from kafka import KafkaProducer

from fastapi import FastAPI

app = FastAPI()


@app.get("/jobs")
def publish():
    sqlalchemy.create_engine("sqlite://")
    KafkaProducer(bootstrap_servers="localhost")
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    entrypoint_id = _entrypoint_id(capsys, repo, cache, "http")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(entrypoint_id), "--cache-dir", str(cache)
    )

    assert code == EXIT_OK, err
    assert out.strip().splitlines() == [
        "0 app/main.py:10 app.main.publish",
        "1 app/main.py:11 [datastore] app.main.publish -> sqlalchemy.create_engine",
        "1 app/main.py:12 [datastore] app.main.publish -> KafkaProducer",
    ]


def test_flow_trace_does_not_label_qualified_library_open_as_datastore(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text(
        """\
from PIL import Image

from fastapi import FastAPI

app = FastAPI()


@app.get("/preview")
def preview():
    return Image.open("photo.png")
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    entrypoint_id = _entrypoint_id(capsys, repo, cache, "http")
    code, out, err = _query(
        capsys, str(repo), "flow-trace", str(entrypoint_id), "--cache-dir", str(cache)
    )

    assert code == EXIT_OK, err
    assert out.strip().splitlines() == [
        "0 app/main.py:9 app.main.preview",
        "1 app/main.py:10 app.main.preview -> Image.open",
    ]


def test_refs_on_defined_unreferenced_symbol_returns_empty(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    _write_fixture(repo)
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, err = _query(capsys, str(repo), "refs", "handle_order", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert out == ""
    assert err == ""

    code, out, err = _query(
        capsys, str(repo), "refs", "handle_order", "--json", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    assert err == ""
    payload = _json(out)
    assert payload == {
        "query": "handle_order",
        "count": 0,
        "returned": 0,
        "limit": 100,
        "truncated": False,
        "references": [],
    }


def test_query_matching_is_case_sensitive_for_symbols_and_modules(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "models.py").write_text(
        """\
class User:
    pass
"""
    )
    (repo / "app" / "routes.py").write_text(
        """\
from fastapi import FastAPI

from app.models import User

app = FastAPI()


@app.get("/users")
def user():
    return User()


def call_user():
    return user()
"""
    )
    cache = tmp_path / "cache"
    _index(repo, cache, capsys)

    code, out, _ = _query(capsys, str(repo), "refs", "User", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert out.strip().splitlines() == [
        "app/routes.py:3 import app.models.User",
        "app/routes.py:10 call app.models.User",
    ]

    code, out, err = _query(capsys, str(repo), "def", "routes.User", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "symbol not found: routes.User" in err

    code, out, _ = _query(capsys, str(repo), "def", "routes.user", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert out.strip() == "app/routes.py:9 function app.routes.user"

    code, out, err = _query(capsys, str(repo), "refs", "routes.User", "--cache-dir", str(cache))
    assert code == EXIT_ERROR
    assert out == ""
    assert "symbol not found: routes.User" in err

    for subcommand in ("entrypoints", "module-summary"):
        args = (
            ("entrypoints", "--module", "App")
            if subcommand == "entrypoints"
            else ("module-summary", "App")
        )
        code, out, err = _query(capsys, str(repo), *args, "--cache-dir", str(cache))
        assert code == EXIT_ERROR
        assert out == ""
        assert "module not found: App" in err

    code, out, _ = _query(
        capsys, str(repo), "entrypoints", "--module", "app", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    assert "app.routes.user" in out

    code, out, _ = _query(
        capsys, str(repo), "module-summary", "app.routes", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    assert out.startswith("# module app.routes ")


def test_units_query_reports_tier_reason_and_message(tmp_path: Path, capsys):
    from arch_indexer.config import RepoCachePaths
    from arch_indexer.store import IndexStore

    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"
    paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    paths.ensure_dirs()
    with IndexStore(paths.index_db) as store:
        store.insert_unit_record(
            unit_id="java:module-a",
            language="java",
            tier="tree-sitter",
            files_count=12,
            fallback_reason="build_failure",
            message="[ERROR] RequireMavenVersion 3.8.7 not in [3.9.0,4.0.0)",
        )
        store.insert_unit_record(
            unit_id="java:module-b",
            language="java",
            tier="scip",
            files_count=3,
        )

    code, out, err = _query(capsys, str(repo), "units", "--cache-dir", str(cache))
    assert code == EXIT_OK
    assert err == ""
    assert out.splitlines() == [
        "java:module-a 12 files [tree-sitter: build_failure] — "
        "[ERROR] RequireMavenVersion 3.8.7 not in [3.9.0,4.0.0)",
        "java:module-b 3 files [scip]",
    ]

    code, out, _ = _query(capsys, str(repo), "units", "--json", "--cache-dir", str(cache))
    assert code == EXIT_OK
    payload = _json(out)
    assert payload["count"] == 2
    assert payload["units"][0] == {
        "unit_id": "java:module-a",
        "language": "java",
        "tier": "tree-sitter",
        "files_count": 12,
        "fallback_reason": "build_failure",
        "message": "[ERROR] RequireMavenVersion 3.8.7 not in [3.9.0,4.0.0)",
    }
    assert payload["units"][1]["fallback_reason"] is None
