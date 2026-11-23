from __future__ import annotations

import re
from pathlib import Path

import yaml

from arch_indexer.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "arch-indexer" / "SKILL.md"


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

    # Description must state when firstmate should load it
    desc_lower = description.lower()
    assert "load" in desc_lower or "firstmate" in desc_lower, (
        "Front matter 'description' should state when firstmate should load it"
    )


def test_skill_commands_exist_in_cli_parser() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    parser = build_parser()

    # Discover top-level parser commands
    subparsers_action = next(a for a in parser._actions if a.dest == "command")
    valid_commands = set(subparsers_action.choices.keys())

    # Find every occurrence of 'arch-indexer <command>'
    command_invocations = re.findall(r"arch-indexer[ \t]+([a-zA-Z0-9_-]+)", content)
    assert command_invocations, "No 'arch-indexer <command>' invocations found in SKILL.md"

    # Verify that every command extracted from SKILL.md exists in the CLI parser
    for cmd in command_invocations:
        assert cmd in valid_commands, (
            f"Command '{cmd}' mentioned as 'arch-indexer {cmd}' in SKILL.md "
            f"does not exist in CLI parser. Valid top-level commands: {valid_commands}"
        )

    # Verify that all core workflow commands are mentioned
    expected_commands = {
        "toolchain",
        "index",
        "plan",
        "query",
        "assemble",
        "check-citations",
        "check-mermaid",
    }
    mentioned = set(command_invocations)
    assert expected_commands.issubset(mentioned), (
        f"Missing expected workflow commands in SKILL.md: {expected_commands - mentioned}"
    )

    # Verify toolchain subactions mentioned in SKILL.md
    toolchain_parser = subparsers_action.choices["toolchain"]
    tc_sub_action = next(a for a in toolchain_parser._actions if a.dest == "toolchain_action")
    valid_tc_actions = set(tc_sub_action.choices.keys())
    tc_subcommands = re.findall(r"arch-indexer[ \t]+toolchain[ \t]+([a-zA-Z0-9_-]+)", content)
    for subcmd in tc_subcommands:
        assert subcmd in valid_tc_actions, (
            f"Subcommand '{subcmd}' mentioned in 'arch-indexer toolchain {subcmd}' "
            f"does not exist in toolchain subparser choices: {valid_tc_actions}"
        )

    # Verify query subactions mentioned in SKILL.md
    query_parser = subparsers_action.choices["query"]
    q_sub_action = next(a for a in query_parser._actions if a.dest == "query_action")
    valid_q_actions = set(q_sub_action.choices.keys())
    q_subcommands = re.findall(r"arch-indexer[ \t]+query[ \t]+\S+[ \t]+([a-zA-Z0-9_-]+)", content)
    for q_action in q_subcommands:
        assert q_action in valid_q_actions, (
            f"Query action '{q_action}' mentioned in 'arch-indexer query <repo> {q_action}' "
            f"does not exist in query subparser choices: {valid_q_actions}"
        )
