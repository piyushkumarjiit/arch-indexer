"""Tier 2 fact extractor: call, decorator and register facts for every language (slice 2)."""

from __future__ import annotations

import json

import pytest
import tree_sitter_java as tsjava
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser

from arch_indexer.facts import extract_facts

PARSERS = {
    "python": Language(tspython.language()),
    "typescript": Language(tstypescript.language_typescript()),
    "java": Language(tsjava.language()),
}


def _facts(language: str, source: str, module: str = "m", **kwargs):
    data = source.encode("utf-8")
    tree = Parser(PARSERS[language]).parse(data)
    ext = {"python": "py", "typescript": "ts", "java": "java"}[language]
    return extract_facts(language, f"m.{ext}", data, tree, module, **kwargs)


def _names(facts, kind: str) -> list[str]:
    return [f["name_text"] for f in facts if f["kind"] == kind]


COMMENTED = {
    "python": '# observer.schedule(h, "p")\ns = "observer.schedule(h, p)"\nreal(1)\n',
    "typescript": (
        '// observer.schedule(h, p)\nconst s = "observer.schedule(h, p)";\n'
        "const t = `x.y(z)`;\nreal(1);\n"
    ),
    "java": (
        "class A {\n  void f() {\n    // obs.schedule(h, p);\n"
        '    String s = "obs.schedule(h, p)";\n    /* obs.run(x); */\n    real(1);\n  }\n}\n'
    ),
}


@pytest.mark.parametrize("language", ["python", "typescript", "java"])
def test_commented_out_and_string_calls_yield_no_fact(language: str) -> None:
    facts = _facts(language, COMMENTED[language])
    assert _names(facts, "call") == ["real"]


def test_python_register_via_local_alias_resolves_class() -> None:
    source = (
        "class H:\n"
        "    def on_any_event(self, e): ...\n"
        "def main():\n"
        "    handler = H()\n"
        "    observer.schedule(handler, 'path')\n"
    )
    facts = _facts("python", source, known_fqnames={"m.H", "m.H.on_any_event", "m.main"})
    registers = [f for f in facts if f["kind"] == "register"]
    assert len(registers) == 1
    fact = registers[0]
    assert fact["name_text"] == "observer.schedule"
    assert fact["receiver_text"] == "observer"
    assert fact["enclosing_fqname"] == "m.main"
    args = json.loads(fact["args_json"])
    assert args[0]["kind"] == "ref"
    assert args[0]["ref_fqname"] == "m.H"
    assert args[1]["kind"] == "literal"


def test_python_constructed_argument_registers_class() -> None:
    source = "class H: ...\nobserver.schedule(H(), 'p')\n"
    facts = _facts("python", source, known_fqnames={"m.H"})
    register = next(f for f in facts if f["kind"] == "register")
    arg = json.loads(register["args_json"])[0]
    assert arg["ref_fqname"] == "m.H"


def test_plain_call_with_literal_args_is_not_a_register() -> None:
    facts = _facts("python", "print('hi', 1)\n")
    assert _names(facts, "call") == ["print"]
    assert _names(facts, "register") == []


def test_typescript_inline_callable_argument() -> None:
    facts = _facts("typescript", "app.get('/x', (req, res) => res.send(1));\n")
    register = next(f for f in facts if f["kind"] == "register")
    args = json.loads(register["args_json"])
    assert [a["kind"] for a in args] == ["literal", "inline_callable"]
    assert register["receiver_text"] == "app"


def test_typescript_new_object_alias_in_register() -> None:
    source = "class H { handle() {} }\nfunction go() { const h = new H(); bus.on('e', h); }\n"
    facts = _facts("typescript", source, known_fqnames={"m.H", "m.H.handle", "m.go"})
    register = next(f for f in facts if f["kind"] == "register")
    assert json.loads(register["args_json"])[1]["ref_fqname"] == "m.H"


def test_java_method_reference_and_lambda_arguments() -> None:
    source = "class A {\n  void tick() {}\n  void go() { exec.submit(this::tick, () -> 1); }\n}\n"
    facts = _facts("java", source, "p", known_fqnames={"p.A", "p.A.tick", "p.A.go"})
    register = next(f for f in facts if f["kind"] == "register")
    args = json.loads(register["args_json"])
    assert args[0]["kind"] == "ref"
    assert args[0]["ref_fqname"] == "p.A.tick"
    assert args[1]["kind"] == "inline_callable"


def test_decorator_fact_has_name_args_and_target_line() -> None:
    source = "@app.get('/x')\ndef handler():\n    pass\n"
    facts = _facts("python", source)
    decorator = next(f for f in facts if f["kind"] == "decorator")
    assert decorator["name_text"] == "app.get"
    assert json.loads(decorator["args_json"])[0]["text"] == "'/x'"
    assert json.loads(decorator["extra_json"])["target_line"] == 2
    assert json.loads(decorator["extra_json"])["target_name"] == "handler"


def test_java_annotation_decorator_fact() -> None:
    source = "class A {\n  @Scheduled(fixedRate = 5)\n  void tick() {}\n}\n"
    facts = _facts("java", source, "p")
    decorator = next(f for f in facts if f["kind"] == "decorator")
    assert decorator["name_text"] == "Scheduled"
    assert decorator["enclosing_fqname"] == "p.A"
    # A Java method node starts at its annotations, so this is the symbol's ``start_line``.
    assert json.loads(decorator["extra_json"])["target_line"] == 2
    assert json.loads(decorator["extra_json"])["target_name"] == "tick"


def test_columns_are_code_point_indexes() -> None:
    facts = _facts("python", "x = 'é€' ; call_it(1)\n")
    call = next(f for f in facts if f["kind"] == "call")
    # 'é' is 2 UTF-8 bytes and '€' is 3, but the stored column counts code points.
    assert call["col"] == len("x = 'é€' ; ")


def test_facts_are_sorted_and_deterministic() -> None:
    source = "b(1)\na(2)\n"
    first = _facts("python", source)
    assert first == _facts("python", source)
    assert [f["line"] for f in first] == sorted(f["line"] for f in first)
