from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

from arch_indexer.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "arch-indexer" / "SKILL.md"
_PLACEHOLDER_RE = re.compile(r"\{\{[a-zA-Z0-9_-]+\}\}|<[a-zA-Z0-9_-]+>")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _substitute_placeholders(command: str) -> str:
    return _PLACEHOLDER_RE.sub("placeholder", command)


def _maybe_add_fenced_command(line: str, invocations: list[list[str]]) -> None:
    candidate = line[2:].lstrip() if line.startswith("- ") else line
    if candidate == "arch-indexer" or candidate.startswith("arch-indexer "):
        tokens = shlex.split(_substitute_placeholders(candidate))
        invocations.append(tokens[1:])


def _extract_cli_invocations(content: str) -> list[list[str]]:
    """Extract `arch-indexer` CLI argument lists from fenced and inline code examples."""
    invocations: list[list[str]] = []
    in_fence = False
    fence_lang = ""
    pending_line = ""

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_lang = stripped[3:].strip().lower()
                pending_line = ""
            else:
                if pending_line:
                    _maybe_add_fenced_command(pending_line, invocations)
                    pending_line = ""
                in_fence = False
                fence_lang = ""
            continue

        if in_fence:
            if fence_lang not in ("", "bash", "sh", "text"):
                continue
            combined = f"{pending_line} {stripped}".strip() if pending_line else stripped
            if combined.endswith("\\"):
                pending_line = combined[:-1].rstrip()
                continue
            pending_line = ""
            _maybe_add_fenced_command(combined, invocations)
            continue

        for span in _INLINE_CODE_RE.findall(raw_line):
            candidate = span.strip()
            if not candidate.startswith("arch-indexer "):
                continue
            tokens = shlex.split(_substitute_placeholders(candidate))
            if len(tokens) > 2:
                invocations.append(tokens[1:])

    return invocations


def test_skill_file_exists() -> None:
    assert SKILL_PATH.is_file(), f"Skill file not found at {SKILL_PATH}"


def test_skill_front_matter_parses() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert content.startswith("---"), "SKILL.md must start with YAML front matter"

    parts = content.split("---", 2)
    assert len(parts) >= 3, "SKILL.md front matter must be enclosed between '---' markers"

    front_matter_raw = parts[1]
    data = yaml.safe_load(front_matter_raw)
    assert isinstance(data, dict), f"Front matter must parse to dict, got {type(data)}"

    # Required fields per spec
    assert data.get("name") == "arch-indexer", "Front matter 'name' must be 'arch-indexer'"
    assert "description" in data, "Front matter must contain 'description'"
    description = data["description"]
    assert isinstance(description, str) and description.strip(), (
        "Front matter 'description' must be a non-empty string"
    )


def test_skill_commands_exist_in_cli_parser() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    parser = build_parser()

    invocations = _extract_cli_invocations(content)
    assert invocations, "No 'arch-indexer' CLI invocations found in SKILL.md"

    parsed_commands: set[str] = set()
    parsed_toolchain_actions: set[str] = set()
    parsed_query_actions: set[str] = set()

    for argv in invocations:
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            pytest.fail(f"Documented CLI invocation failed to parse: {argv!r} (exit={exc.code})")
        parsed_commands.add(args.command)
        if args.command == "toolchain":
            parsed_toolchain_actions.add(args.toolchain_action)
        elif args.command == "query":
            parsed_query_actions.add(args.query_action)

    expected_commands = {
        "toolchain",
        "index",
        "plan",
        "query",
        "assemble",
        "check-citations",
        "check-mermaid",
    }
    assert expected_commands.issubset(parsed_commands), (
        f"Missing expected workflow commands in SKILL.md: {expected_commands - parsed_commands}"
    )
    assert {"ensure", "status"}.issubset(parsed_toolchain_actions)
    assert {
        "def",
        "refs",
        "file-symbols",
        "entrypoints",
        "module-summary",
        "flow-trace",
    }.issubset(parsed_query_actions)

    # Negative controls for the documented templates: the same extraction pipeline must
    # reject a misspelled option and a missing required option.
    with pytest.raises(SystemExit):
        parser.parse_args(["assemble", "placeholder", "--draft-dir", "docs/.drafts"])
    with pytest.raises(SystemExit):
        parser.parse_args(["toolchain", "ensure"])
