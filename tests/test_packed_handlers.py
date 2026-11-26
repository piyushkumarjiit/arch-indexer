"""One-line handlers: (line, col) spans attribute each call to its own method."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.cli import main
from arch_indexer.graph import load_graph

SERVICE = """package demo.svc;

public class Service {
    public static String a() { return "a"; }
    public static String b() { return "b"; }
    public static String c() { return "c"; }
}
"""

# All three handlers, and their calls, share the same physical line.
CONTROLLER = (
    "package demo.web;\n"
    "\n"
    "import demo.svc.Service;\n"
    "import org.springframework.web.bind.annotation.*;\n"
    "\n"
    "@RestController\n"
    "public class PackedController {\n"
    '    @GetMapping("/a") String a() { return Service.a(); } '
    '@PostMapping("/b") String b() { return Service.b(); } '
    '@DeleteMapping("/c") String c() { return Service.c(); }\n'
    "}\n"
)


def _graph(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    (repo / "src/main/java/demo/svc").mkdir(parents=True)
    (repo / "src/main/java/demo/web").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>")
    (repo / "src/main/java/demo/svc/Service.java").write_text(SERVICE)
    (repo / "src/main/java/demo/web/PackedController.java").write_text(CONTROLLER)
    main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(tmp_path / "cache")])
    return load_graph(repo / "docs" / "graph.json")


def test_packed_one_liner_handlers_each_get_exactly_their_own_calls_edge(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    entries = {n["metadata"]["method"]: n for n in graph["nodes"] if n["kind"] == "entrypoint"}
    assert set(entries) == {"GET", "POST", "DELETE"}
    expected = {"GET": "a", "POST": "b", "DELETE": "c"}
    for verb, method in expected.items():
        targets = [
            e["target"]
            for e in graph["edges"]
            if e["source"] == entries[verb]["id"] and e["kind"] == "calls"
        ]
        assert targets == [f"sym:demo.svc.Service.{method}"]


def test_packed_handler_entries_carry_distinct_columns(tmp_path: Path) -> None:
    repo = tmp_path
    _graph(tmp_path)
    import sqlite3

    db = next((repo / "cache" / "repos").glob("*/index.db"))
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT method, line, col FROM entrypoints WHERE kind = 'http' ORDER BY col"
        ).fetchall()
    assert [r[0] for r in rows] == ["GET", "POST", "DELETE"]
    assert len({r[2] for r in rows}) == 3
    assert len({r[1] for r in rows}) == 1
