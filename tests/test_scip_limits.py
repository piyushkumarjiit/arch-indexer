"""Regression tests for the SCIP Tier 1 / Tier 2 matching limitations that were
accepted when the SCIP support PR was closed and are fixed here.

Each test pins one limitation recorded when the SCIP support PR was closed:

1. fallback definition spans (no SCIP enclosing range) leaked sibling
   declarations or trivia into the body hash,
2. the Java/TypeScript heuristic and the Python AST paths kept one span per
   physical line, so a second import on the same line was invisible,
3. locally aliased external imports never reached ``unresolved_refs``,
4. residual alias / statement-module binding across same-line statements and
   same-line Tier 2 metadata,
5. SCIP column offsets were used as Python string indices without honouring the
   document position encoding,
6. the Python import fallback on a syntax-error file marked docstring prose as
   imports,
7. the AST import text was keyed by start line and dropped the second of two
   statements on one line,
8. the ref-kind colon heuristic mislabelled a class used as a value in a
   multi-line dict as a type annotation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from arch_indexer import scip_pb2
from arch_indexer.plugins.base import IndexingTier, IndexResult, IndexUnit
from arch_indexer.scip import _import_info, ingest_scip

DEFINITION = scip_pb2.SymbolRole.Definition
READ = scip_pb2.SymbolRole.ReadAccess


def _occ(
    symbol: str,
    line: int,
    col: int,
    end_col: int,
    roles: int = READ,
    **kwargs: object,
) -> scip_pb2.Occurrence:
    params: dict[str, object] = {
        "symbol": symbol,
        "symbol_roles": roles,
        "range": [line, col, end_col],
    }
    params.update(kwargs)
    return scip_pb2.Occurrence(**params)


def _unit(repo: Path, files: list[Path], language: str = "python") -> IndexUnit:
    return IndexUnit(
        unit_id=f"{language}:root",
        name="root",
        language=language,
        root_dir=repo,
        files=files,
    )


def _ingest(
    repo: Path,
    docs: list[scip_pb2.Document],
    unit: IndexUnit,
    *,
    metadata: scip_pb2.Metadata | None = None,
    **kwargs: object,
) -> IndexResult:
    index = scip_pb2.Index(metadata=metadata or scip_pb2.Metadata(), documents=docs)
    scip_path = repo / "synthetic.scip"
    scip_path.write_bytes(index.SerializeToString())
    return ingest_scip(scip_path, unit, **kwargs)


def _body_hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# 1. Fallback definition span -------------------------------------------------


def _two_function_repo(tmp_path: Path, note: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text(f"def one():\n    return 1\n\n\n{note}\ndef two():\n    return 2\n")
    return repo, app


def _two_function_doc() -> scip_pb2.Document:
    return scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ("scip-python python pkg 0.1.0 `app`/one().", 0, 4, 7, DEFINITION),
            _occ("scip-python python pkg 0.1.0 `app`/two().", 5, 4, 7, DEFINITION),
        ],
    )


def test_fallback_definition_span_excludes_trailing_trivia(tmp_path: Path):
    """A fallback span stops at the last body line, not at the next sibling's trivia."""
    repo, app = _two_function_repo(tmp_path, "# note for two")
    res = _ingest(repo, [_two_function_doc()], _unit(repo, [app]))
    symbols = {s["name"]: s for s in res.symbols}

    assert symbols["one"]["start_line"] == 1
    assert symbols["one"]["end_line"] == 2
    assert symbols["one"]["body_sha256"] == _body_hash(["def one():", "    return 1"])

    # Editing the trivia between the two declarations must not change either hash.
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_repo, other_app = _two_function_repo(other_root, "# unrelated trivia")
    other_res = _ingest(other_repo, [_two_function_doc()], _unit(other_repo, [other_app]))
    other_symbols = {s["name"]: s for s in other_res.symbols}

    assert other_symbols["one"]["body_sha256"] == symbols["one"]["body_sha256"]
    assert other_symbols["two"]["body_sha256"] == symbols["two"]["body_sha256"]


def test_fallback_definition_span_excludes_same_line_sibling(tmp_path: Path):
    """Two declarations on one line never hash each other's source."""

    def ingest(value: str) -> dict[str, dict]:
        repo = tmp_path / f"repo-{value}"
        repo.mkdir()
        app = repo / "app.py"
        app.write_text(f"x = 1; y = {value}\n")
        doc = scip_pb2.Document(
            relative_path="app.py",
            language="Python",
            occurrences=[
                _occ("scip-python python pkg 0.1.0 `app`/x.", 0, 0, 1, DEFINITION),
                _occ("scip-python python pkg 0.1.0 `app`/y.", 0, 7, 8, DEFINITION),
            ],
        )
        res = _ingest(repo, [doc], _unit(repo, [app]))
        return {s["name"]: s for s in res.symbols}

    first = ingest("2")
    second = ingest("3")

    assert first["x"]["end_line"] == 1
    assert first["y"]["end_line"] == 1
    assert first["x"]["body_sha256"] != first["y"]["body_sha256"]
    # Changing the sibling's value only changes the sibling's hash.
    assert first["x"]["body_sha256"] == second["x"]["body_sha256"]
    assert first["y"]["body_sha256"] != second["y"]["body_sha256"]


# 2. One span per import statement -------------------------------------------


def test_java_heuristic_imports_cover_each_statement_on_one_line(tmp_path: Path):
    """Both `import` statements on one physical line are classified as imports."""
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    app = java_dir / "App.java"
    first_line = "import java.util.List; import java.util.Map;"
    app.write_text(f"{first_line}\n\nclass App {{}}\n")

    doc = scip_pb2.Document(
        relative_path="src/main/java/com/example/App.java",
        language="Java",
        occurrences=[
            _occ(
                "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#",
                2,
                6,
                9,
                DEFINITION,
            ),
            _occ(
                "semanticdb maven maven/com.example/demo 1.0.0 java/util/List#",
                0,
                first_line.index("List"),
                first_line.index("List") + 4,
            ),
            _occ(
                "semanticdb maven maven/com.example/demo 1.0.0 java/util/Map#",
                0,
                first_line.index("Map"),
                first_line.index("Map") + 3,
            ),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app], "java"))

    ref_kinds = {r["symbol_fqname"]: r["ref_kind"] for r in res.references}
    assert ref_kinds["java.util.List"] == "import"
    assert ref_kinds["java.util.Map"] == "import"


def test_typescript_heuristic_imports_cover_each_statement_on_one_line(tmp_path: Path):
    """A second `import` after a `;` on the same line is still an import."""
    repo = tmp_path / "repo"
    src = repo / "src"
    src.mkdir(parents=True)
    app = src / "app.ts"
    first_line = 'import { a } from "mod-a"; import { b } from "mod-b";'
    app.write_text(f"{first_line}\n")

    doc = scip_pb2.Document(
        relative_path="src/app.ts",
        language="TypeScript",
        occurrences=[
            _occ(
                "scip-typescript npm ts-fixture 1.0.0 src/`app.ts`/a.",
                0,
                first_line.index("a }"),
                first_line.index("a }") + 1,
            ),
            _occ(
                "scip-typescript npm ts-fixture 1.0.0 src/`app.ts`/b.",
                0,
                first_line.index("b }"),
                first_line.index("b }") + 1,
            ),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app], "typescript"))

    ref_kinds = {r["symbol_fqname"]: r["ref_kind"] for r in res.references}
    assert ref_kinds["src.app.a"] == "import"
    assert ref_kinds["src.app.b"] == "import"


def test_python_import_fallback_keeps_each_statement_on_one_line(tmp_path: Path):
    """The syntax-error fallback also finds the second statement on a line."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("import os; import json\n\ndef broken(:\n    pass\n")

    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ("scip-python python pkg 0.1.0 os/__init__:", 0, 7, 9),
            _occ("scip-python python pkg 0.1.0 json/__init__:", 0, 18, 22),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app]))

    ref_kinds = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_kinds[(1, "os")] == "import"
    assert ref_kinds[(1, "json")] == "import"


# 3. Locally aliased external imports ----------------------------------------


def test_locally_aliased_external_import_reaches_unresolved_refs(tmp_path: Path):
    """A local alias bound to an external module records its external target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    line = "import requests as r"
    app.write_text(f"{line}\n\ndef go():\n    return r.get('/x')\n")

    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ("local 0", 0, line.index("r"), line.index("r") + 1, DEFINITION),
            _occ("local 0", 3, 11, 12),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app]))

    refs = {(r["line"], r["symbol_fqname"], r["ref_kind"]) for r in res.references}
    assert (4, "requests.get", "read") in refs
    unresolved = {(u["line"], u["raw_symbol"]) for u in res.unresolved_refs}
    assert (4, "requests.get") in unresolved


def test_locally_aliased_internal_module_is_not_unresolved(tmp_path: Path):
    """An alias bound to a repo module stays resolved once the module is indexed."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    config = repo / "app" / "config.py"
    config.write_text("def load():\n    return {}\n")
    app = repo / "app" / "app.py"
    line = "import app.config as cfg"
    app.write_text(f"{line}\n\ndef go():\n    return cfg.load()\n")

    doc = scip_pb2.Document(
        relative_path="app/app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app.app`/__init__:", 0, 0, 0, DEFINITION),
            _occ("local 0", 0, line.index("cfg"), line.index("cfg") + 3, DEFINITION),
            _occ("local 0", 3, 11, 14),
        ],
    )
    symbol_context = {
        "fqnames": {"python": {"app.config.load"}},
        "modules": {"python": {"app", "app.config", "app.app"}},
    }
    res = _ingest(repo, [doc], _unit(repo, [app, config]), symbol_context=symbol_context)

    refs = {(r["line"], r["symbol_fqname"]) for r in res.references}
    assert (4, "app.config.load") in refs
    assert all(u["raw_symbol"] != "app.config.load" for u in res.unresolved_refs)


# 4. Residual alias / statement-module binding and same-line Tier 2 metadata --


def test_same_line_from_import_binds_its_own_statement(tmp_path: Path):
    """A `from ... import ... as ...` after `;` binds its imported name, not the module."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    first_line = "import os; from pkg import helper as h"
    app.write_text(f"{first_line}\n\ndef go():\n    return h()\n")

    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ("scip-python python pkg 0.1.0 os/__init__:", 0, 7, 9),
            _occ("local 0", 0, 7, 9, DEFINITION),
            _occ(
                "scip-python python pkg 0.1.0 pkg/__init__:",
                0,
                first_line.index("pkg"),
                first_line.index("pkg") + 3,
            ),
            _occ(
                "local 1",
                0,
                first_line.index("h", first_line.index("as")),
                first_line.index("h", first_line.index("as")) + 1,
                DEFINITION,
            ),
            _occ("local 1", 3, 11, 12),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app]))

    refs = {(r["line"], r["symbol_fqname"], r["ref_kind"]) for r in res.references}
    assert (4, "pkg.helper", "call") in refs
    assert (4, "pkg", "call") not in refs


def test_same_line_tier2_metadata_matches_each_sibling(tmp_path: Path):
    """Two declarations on one line each keep their own Tier 2 metadata."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("x = 1; y = 2\n")

    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/x.", 0, 0, 1, DEFINITION),
            _occ("scip-python python pkg 0.1.0 `app`/y.", 0, 7, 8, DEFINITION),
        ],
    )
    tier2 = IndexResult(
        unit_id="python:root",
        tier=IndexingTier.TIER2,
        success=True,
        files_indexed=[app],
        files=[
            {
                "path": "app.py",
                "language": "python",
                "sha256": "",
                "line_count": 1,
                "module": "app",
                "tier": IndexingTier.TIER2.value,
                "fallback_reason": None,
            }
        ],
        symbols=[
            {
                "fqname": "app.x",
                "name": "x",
                "kind": "variable",
                "file_path": "app.py",
                "start_line": 1,
                "end_line": 1,
                "body_sha256": "a" * 64,
                "visibility": "public",
                "decorators_json": "[]",
            },
            {
                "fqname": "app.y",
                "name": "y",
                "kind": "variable",
                "file_path": "app.py",
                "start_line": 1,
                "end_line": 1,
                "body_sha256": "b" * 64,
                "visibility": "private",
                "decorators_json": "[]",
            },
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app]), tier2_result=tier2)

    symbols = {s["name"]: s for s in res.symbols}
    assert symbols["x"]["body_sha256"] == "a" * 64
    assert symbols["x"]["visibility"] == "public"
    assert symbols["y"]["body_sha256"] == "b" * 64
    assert symbols["y"]["visibility"] == "private"


# 5. SCIP position encoding ---------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "encoding", "field"),
    [
        ('label = "héllo"; import ', scip_pb2.UTF8, "metadata"),
        ('label = "🙂"; import ', scip_pb2.UTF16, "metadata"),
        ('label = "héllo"; import ', scip_pb2.UTF8CodeUnitOffsetFromLineStart, "document"),
        ('label = "🙂"; import ', scip_pb2.UTF16CodeUnitOffsetFromLineStart, "document"),
    ],
)
def test_scip_columns_honour_document_position_encoding(
    tmp_path: Path, prefix: str, encoding: int, field: str
):
    """Non-ASCII prefixes must not shift the columns used as str indices."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    line = f"{prefix}requests as r"
    app.write_text(f"{line}\n\ndef go():\n    return r.get('/x')\n", encoding="utf-8")

    if encoding == scip_pb2.UTF8:
        alias_col = len(prefix.encode("utf-8"))
    elif encoding == scip_pb2.UTF16:
        alias_col = len(prefix.encode("utf-16-le")) // 2
    else:
        alias_col = len(prefix)
    codepoint_col = len(prefix)

    metadata = scip_pb2.Metadata()
    doc_kwargs: dict[str, object] = {}
    if field == "metadata":
        metadata.text_document_encoding = encoding
    else:
        doc_kwargs["position_encoding"] = encoding
    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ("local 0", 0, alias_col, alias_col + 15, DEFINITION),
            _occ("local 0", 3, 11, 12),
        ],
        **doc_kwargs,
    )
    res = _ingest(repo, [doc], _unit(repo, [app]), metadata=metadata)

    refs = {(r["line"], r["col"], r["symbol_fqname"]): r for r in res.references}
    assert (1, codepoint_col, "requests") in refs
    assert (4, 11, "requests.get") in refs
    assert refs[(1, codepoint_col, "requests")]["ref_kind"] == "import"


# 6. Python import fallback on a syntax-error file ---------------------------


def test_python_import_fallback_ignores_docstring_prose(tmp_path: Path):
    """Docstring prose that looks like an import is not recorded as one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    text = (
        '"""Module docs.\n'
        "\n"
        "import os\n"
        "from pathlib import Path\n"
        '"""\n'
        "\n"
        "import requests as r\n"
        "\n"
        "\n"
        "def go():\n"
        "    return r.get('/x')\n"
        "\n"
        "\n"
        "def broken(:\n"
        "    pass\n"
    )
    app.write_text(text)

    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ("local 0", 6, 19, 20, DEFINITION),
            _occ("local 0", 10, 11, 12),
        ],
    )
    info = _import_info(text.splitlines(), "python")
    covered_lines = {line for start, _, end, _ in info.spans for line in range(start, end + 1)}
    assert covered_lines == {7}

    res = _ingest(repo, [doc], _unit(repo, [app]))

    refs = {(r["line"], r["symbol_fqname"], r["ref_kind"]) for r in res.references}
    assert (7, "requests", "import") in refs
    assert (11, "requests.get", "read") in refs
    assert not [r for r in res.references if r["line"] in (3, 4)]


# 7. AST import text keyed per statement -------------------------------------


def test_python_ast_import_text_keeps_second_statement_on_one_line(tmp_path: Path):
    """The second of two AST import statements on one line keeps its own text."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    line = "from os import sep; from json import dumps as d"
    app.write_text(f"{line}\n\ndef go():\n    return d({{}})\n")

    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ(
                "scip-python python pkg 0.1.0 os/__init__:",
                0,
                line.index("os"),
                line.index("os") + 2,
            ),
            _occ(
                "scip-python python pkg 0.1.0 json/__init__:",
                0,
                line.index("json"),
                line.index("json") + 4,
            ),
            _occ(
                "local 0",
                0,
                line.index("s", line.index("sep")),
                line.index("s", line.index("sep")) + 1,
                DEFINITION,
            ),
            _occ(
                "local 0",
                0,
                line.index("d", line.index("dumps")),
                line.index("d", line.index("dumps")) + 1,
                DEFINITION,
            ),
            _occ("local 0", 3, 11, 12),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app]))

    refs = {(r["line"], r["symbol_fqname"], r["ref_kind"]) for r in res.references}
    assert (4, "json.dumps", "call") in refs
    assert (4, "os.sep", "call") not in refs


# 8. Ref-kind colon heuristic ------------------------------------------------


def test_ref_kind_colon_heuristic_ignores_multiline_dict_value(tmp_path: Path):
    """A class used as a dict value is a value reference, not a type annotation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    text = (
        "CONFIG = {\n"
        '    "handler": Handler,\n'
        "}\n"
        "\n"
        "\n"
        "def build(handler: Handler):\n"
        "    return handler\n"
    )
    app.write_text(text)

    handler_sym = "scip-python python pkg 0.1.0 `app`/Handler#"
    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ(handler_sym, 1, 15, 22),
            _occ(
                "scip-python python pkg 0.1.0 `app`/build().",
                5,
                4,
                9,
                DEFINITION,
            ),
            _occ(handler_sym, 5, 19, 26),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app]))

    ref_kinds = {(r["line"], r["ref_kind"]) for r in res.references}
    assert (2, "read") in ref_kinds
    assert (6, "type") in ref_kinds
    assert (2, "type") not in ref_kinds


def test_ref_kind_colon_heuristic_ignores_braces_in_strings(tmp_path: Path):
    """A brace in a string does not make a same-line annotation look like a dict value."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text('x = "{"; y: Handler\n')

    handler_sym = "scip-python python pkg 0.1.0 `app`/Handler#"
    doc = scip_pb2.Document(
        relative_path="app.py",
        language="Python",
        occurrences=[
            _occ("scip-python python pkg 0.1.0 `app`/__init__:", 0, 0, 0, DEFINITION),
            _occ(handler_sym, 0, 12, 19),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app]))

    ref_kinds = {(r["line"], r["ref_kind"]) for r in res.references}
    assert (1, "type") in ref_kinds
    assert (1, "read") not in ref_kinds


def test_ref_kind_colon_heuristic_keeps_typescript_member_annotation(tmp_path: Path):
    """A TypeScript member annotation inside a brace body stays a type reference."""
    repo = tmp_path / "repo"
    src = repo / "src"
    src.mkdir(parents=True)
    app = src / "app.ts"
    text = "interface Config {\n  handler: Handler;\n}\n"
    app.write_text(text)

    handler_sym = "scip-typescript npm ts-fixture 1.0.0 src/`app.ts`/Handler#"
    doc = scip_pb2.Document(
        relative_path="src/app.ts",
        language="TypeScript",
        occurrences=[
            _occ(handler_sym, 1, 11, 18),
        ],
    )
    res = _ingest(repo, [doc], _unit(repo, [app], "typescript"))

    ref_kinds = {(r["line"], r["ref_kind"]) for r in res.references}
    assert (2, "type") in ref_kinds
    assert (2, "read") not in ref_kinds
