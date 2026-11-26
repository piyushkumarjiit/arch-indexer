"""Spring application-event listeners become ``event`` entries; app boots link triggers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arch_indexer.cli import main
from arch_indexer.graph import load_graph

APP = """package demo.app;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class DemoApplication {
    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }
}
"""

LISTENERS = """package demo.app.work;

import org.springframework.context.event.EventListener;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.transaction.event.TransactionalEventListener;
import demo.app.support.Helper;

public class Workers {
    @Scheduled(fixedRate = 5000)
    public void tick() { Helper.assist(); }

    @EventListener
    public void onCorpus(CorpusChanged event) { Helper.assist(); }

    @TransactionalEventListener(classes = CorpusDeleted.class)
    public void onDelete() { Helper.assist(); }
}
"""

HELPER = """package demo.app.support;

public class Helper {
    public static void assist() {}
}
"""

OUTSIDE = """package demo.other;

import org.springframework.scheduling.annotation.Scheduled;

public class Elsewhere {
    @Scheduled(cron = "0 0 * * * *")
    public void nightly() {}
}
"""


def _index(tmp_path: Path) -> tuple[dict, Path]:
    repo = tmp_path / "repo"
    root = repo / "app/src/main/java"
    for rel, text in {
        "demo/app/DemoApplication.java": APP,
        "demo/app/work/Workers.java": LISTENERS,
        "demo/app/support/Helper.java": HELPER,
        "demo/other/Elsewhere.java": OUTSIDE,
    }.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (repo / "app/pom.xml").write_text("<project><artifactId>app</artifactId></project>")
    cache = tmp_path / "cache"
    main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
    return load_graph(repo / "docs" / "graph.json"), cache


def test_event_and_transactional_listeners_become_event_entries(tmp_path: Path) -> None:
    _, cache = _index(tmp_path)
    db = next((cache / "repos").glob("*/index.db"))
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT name, route_or_trigger, symbol_fqname FROM entrypoints "
            "WHERE kind = 'event' ORDER BY name"
        ).fetchall()
    assert rows == [
        ("onCorpus", "CorpusChanged", "demo.app.work.Workers.onCorpus"),
        ("onDelete", "CorpusDeleted", "demo.app.work.Workers.onDelete"),
    ]


def test_event_entries_have_outgoing_edges(tmp_path: Path) -> None:
    graph, _ = _index(tmp_path)
    events = [n for n in graph["nodes"] if n["metadata"].get("kind") == "event"]
    assert len(events) == 2
    for node in events:
        assert any(e["source"] == node["id"] for e in graph["edges"]), node["id"]


def test_spring_application_run_boots_scheduled_entries_under_its_package(
    tmp_path: Path,
) -> None:
    graph, _ = _index(tmp_path)
    main_node = next(
        n
        for n in graph["nodes"]
        if n["metadata"].get("kind") == "process" and n["symbol"] == "main"
    )
    booted = {
        e["target"]
        for e in graph["edges"]
        if e["source"] == main_node["id"] and e["kind"] == "boots"
    }
    by_symbol = {n["id"]: n["symbol"] for n in graph["nodes"]}
    booted_symbols = {by_symbol[t] for t in booted}
    assert "tick" in booted_symbols
    assert {"onCorpus", "onDelete"} <= booted_symbols
    assert "nightly" not in booted_symbols
    assert all(e["confidence"] == "heuristic" for e in graph["edges"] if e["kind"] == "boots")


WORKER_APP = """package demo.worker;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = {"demo.worker", "demo.cache"})
public final class WorkerApplication {
    public static void main(final String[] arguments) {
        final SpringApplication application = new SpringApplication(WorkerApplication.class);
        application.run(arguments);
    }
}
"""

CACHE_WORKER = """package demo.cache;

import org.springframework.scheduling.annotation.Scheduled;

public class CacheWorker {
    @Scheduled(fixedDelay = 10)
    public void refresh() {}
}
"""


def test_variable_receiver_run_and_scan_base_packages_boot_sibling_package(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    root = repo / "app/src/main/java"
    for rel, text in {
        "demo/worker/WorkerApplication.java": WORKER_APP,
        "demo/cache/CacheWorker.java": CACHE_WORKER,
    }.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (repo / "app/pom.xml").write_text("<project><artifactId>app</artifactId></project>")
    main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(tmp_path / "c")])
    graph = load_graph(repo / "docs" / "graph.json")
    boots = [e for e in graph["edges"] if e["kind"] == "boots"]
    assert [e["target"] for e in boots] == ["ep:scheduled:refresh"]


REACTOR_APP = """package demo.app;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class ReactorApp {
    public static void main(String[] args) {
        SpringApplication.run(ReactorApp.class, args);
    }
}
"""

REACTOR_JOBS = """package demo.app.jobs;

import org.springframework.scheduling.annotation.Scheduled;

public class Jobs {
    @Scheduled(fixedRate = 1000)
    public void refreshJob() {}
}
"""

REACTOR_APPLE = """package demo.apple;

import org.springframework.scheduling.annotation.Scheduled;

public class Billing {
    @Scheduled(fixedRate = 2000)
    public void nightly() {}
}
"""


def test_maven_reactor_fallback_boots_only_segment_delimited_subpackages(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    for module, rel, text in (
        ("app", "demo/app/ReactorApp.java", REACTOR_APP),
        ("billing", "demo/app/jobs/Jobs.java", REACTOR_JOBS),
        ("billing", "demo/apple/Billing.java", REACTOR_APPLE),
    ):
        target = repo / module / "src/main/java" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for module in ("app", "billing"):
        (repo / module / "pom.xml").write_text(
            f"<project><artifactId>{module}</artifactId></project>"
        )
    main(
        [
            "index",
            str(repo),
            "--offline",
            "--force-full",
            "--cache-dir",
            str(tmp_path / "c"),
        ]
    )
    graph = load_graph(repo / "docs" / "graph.json")
    scheduled = {
        n.get("symbol") for n in graph["nodes"] if n["metadata"].get("kind") == "scheduled"
    }
    assert scheduled == {"refreshJob", "nightly"}
    main_node = next(
        n
        for n in graph["nodes"]
        if n["metadata"].get("kind") == "process" and n["symbol"] == "main"
    )
    by_symbol = {n["id"]: n["symbol"] for n in graph["nodes"]}
    booted = {
        by_symbol[e["target"]]
        for e in graph["edges"]
        if e["source"] == main_node["id"] and e["kind"] == "boots"
    }
    assert booted == {"refreshJob"}
