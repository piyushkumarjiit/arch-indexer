"""Non-Spring-MVC Java entrypoint families (process, scheduled, queue, JAX-RS, gRPC)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.plugins.base import IndexUnit
from arch_indexer.plugins.java_plugin import JavaPlugin


def _entrypoints(tmp_path: Path, source: str, name: str = "Sample.java") -> list[dict]:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    unit = IndexUnit(
        unit_id="java:root", name="root", language="java", root_dir=tmp_path, files=[path]
    )
    return JavaPlugin().index_unit_tier2(unit).entrypoints


def _by_kind(entrypoints: list[dict], kind: str) -> list[dict]:
    return [e for e in entrypoints if e["kind"] == kind]


def test_spring_boot_main_is_a_process_entrypoint(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.app;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class DemoApplication {
    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }
}
""",
    )
    (main,) = _by_kind(eps, "process")
    assert main["name"] == "main"
    assert main["symbol_fqname"] == "demo.app.DemoApplication.main"
    assert "SpringBootApplication" in main["route_or_trigger"]


def test_plain_main_without_spring_is_a_process_entrypoint(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.tool;

public class Tool {
    public static void main(String... args) {}
    public void main(int notAnEntrypoint) {}
}
""",
    )
    (main,) = _by_kind(eps, "process")
    assert main["route_or_trigger"] == "main(String[])"


def test_scheduled_methods_report_their_trigger(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.jobs;

import org.springframework.scheduling.annotation.Scheduled;

public class Jobs {
    @Scheduled(cron = "0 0 * * * *")
    public void hourly() {}

    @Scheduled(fixedRate = 5000)
    public void often() {}
}
""",
    )
    triggers = {e["name"]: e["route_or_trigger"] for e in _by_kind(eps, "scheduled")}
    assert triggers == {"hourly": "cron=0 0 * * * *", "often": "fixedRate=5000"}


def test_kafka_rabbit_and_jms_listeners_are_queue_entrypoints(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.msg;

public class Listeners {
    @KafkaListener(topics = {"orders", "refunds"}, groupId = "g")
    public void onKafka(String body) {}

    @RabbitListener(queues = "invoices")
    public void onRabbit(String body) {}

    @JmsListener(destination = "alerts")
    public void onJms(String body) {}
}
""",
    )
    triggers = {e["name"]: e["route_or_trigger"] for e in _by_kind(eps, "queue")}
    assert triggers == {
        "onKafka": "orders,refunds",
        "onRabbit": "invoices",
        "onJms": "alerts",
    }


def test_class_level_rabbit_listener_handlers_are_queue_entrypoints(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.msg;

@RabbitListener(queues = "payments")
public class PaymentHandler {
    @RabbitHandler
    public void handle(String body) {}

    public void helper() {}
}
""",
    )
    (queue,) = _by_kind(eps, "queue")
    assert (queue["name"], queue["route_or_trigger"]) == ("handle", "payments")


def test_runner_implementations_are_process_entrypoints(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.boot;

public class Seeder implements CommandLineRunner {
    @Override
    public void run(String... args) {}
}
""",
    )
    (runner,) = _by_kind(eps, "process")
    assert runner["name"] == "run"
    assert runner["route_or_trigger"] == "CommandLineRunner"

    eps = _entrypoints(
        tmp_path,
        """
package demo.boot;

public class Warmup implements Serializable, ApplicationRunner {
    public void run(ApplicationArguments args) {}
}
""",
        name="Warmup.java",
    )
    assert [e["route_or_trigger"] for e in _by_kind(eps, "process")] == ["ApplicationRunner"]


def test_jaxrs_path_methods_are_http_entrypoints(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.rs;

import javax.ws.rs.*;

@Path("/widgets")
public class WidgetResource {
    @GET
    public String list() { return ""; }

    @POST
    @Path("/{id}/archive")
    public void archive() {}

    public void notExposed() {}
}
""",
    )
    http = {e["name"]: (e["method"], e["route_or_trigger"]) for e in _by_kind(eps, "http")}
    assert http == {
        "list": ("GET", "/widgets"),
        "archive": ("POST", "/widgets/{id}/archive"),
    }


def test_grpc_service_implementations_are_entrypoints(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.rpc;

public class QuoteService extends QuoteGrpc.QuoteImplBase {
    @Override
    public void getQuote(Request req, StreamObserver<Reply> out) {}

    private void helper() {}
}
""",
    )
    (grpc,) = _by_kind(eps, "grpc")
    assert (grpc["name"], grpc["route_or_trigger"]) == ("getQuote", "Quote/getQuote")

    eps = _entrypoints(
        tmp_path,
        """
package demo.rpc;

@GrpcService
public class PriceService {
    @Override
    public void price(Request req, StreamObserver<Reply> out) {}
}
""",
        name="PriceService.java",
    )
    assert [e["route_or_trigger"] for e in _by_kind(eps, "grpc")] == ["PriceService/price"]


def test_grpc_lifecycle_overrides_are_not_entrypoints(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.rpc;

public class QuoteService extends QuoteGrpc.QuoteImplBase {
    @Override
    public void getQuote(Request req, StreamObserver<Reply> out) {}

    @Override
    public String toString() { return "quote"; }

    @Override
    public boolean equals(Object other) { return false; }

    @Override
    public ServerServiceDefinition bindService() { return null; }

    @Override
    public void onStart() {}
}
""",
    )
    assert [e["route_or_trigger"] for e in _by_kind(eps, "grpc")] == ["Quote/getQuote"]


def test_ordinary_classes_have_no_entrypoints(tmp_path: Path):
    eps = _entrypoints(
        tmp_path,
        """
package demo.plain;

public class Plain {
    public void run() {}
    public void handle() {}
}
""",
    )
    assert eps == []
