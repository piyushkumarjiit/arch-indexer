import json
from pathlib import Path

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_ERROR, EXIT_OK, main

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

    # A leading "./" and an absolute path resolve to the same indexed relative path.
    code, dotted, _ = _query(
        capsys, str(repo), "file-symbols", "./app/service.py", "--cache-dir", str(cache)
    )
    assert code == EXIT_OK
    assert dotted == out

    code, out, err = _query(
        capsys,
        str(repo),
        "file-symbols",
        str(repo / "app" / "service.py"),
        "--cache-dir",
        str(cache),
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

    code, out, _ = _query(
        capsys, str(repo), "flow-trace", "ep:1", "--json", "--cache-dir", str(cache)
    )
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
