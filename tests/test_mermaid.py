from __future__ import annotations

import io
import json

from arch_indexer.checks.mermaid import (
    CODE_AUTO_DIAGRAM_ERROR,
    CODE_AUTO_DIAGRAM_MISMATCH,
    CODE_EMPTY_BODY,
    CODE_INVALID_ARROW,
    CODE_INVALID_HEADER,
    CODE_INVALID_NODE_ID,
    CODE_UNBALANCED_DELIMITERS,
    CODE_UNBALANCED_QUOTES,
    CODE_UNBALANCED_SUBGRAPH,
    check_mermaid,
    run_check_mermaid,
    validate_mermaid_block,
)


def _validate_snippet(snippet: str) -> list:
    return validate_mermaid_block(snippet.strip().splitlines(), "docs/DESIGN.md", 1)


# --- Tests for each declared diagram type (valid and invalid) ---


def test_flowchart_valid_and_invalid():
    # Valid flowchart with various arrows and shapes
    valid_snippet = """
    flowchart TD
      subgraph sg1 [Service Layer]
        nodeA["Service A"] --> nodeB[Service B]
        nodeB -.-> nodeC[(Database)]
        nodeC ==> nodeD{Is Valid?}
        nodeD -- Yes --> nodeE([Finish])
      end
    """
    assert _validate_snippet(valid_snippet) == []

    # Invalid flowchart: single dash arrow ->
    invalid_arrow = """
    flowchart TD
      nodeA -> nodeB
    """
    errs = _validate_snippet(invalid_arrow)
    assert any(e.code == CODE_INVALID_ARROW for e in errs)

    # Invalid flowchart: node ID with invalid characters
    invalid_node_id = """
    flowchart TD
      node@1 --> nodeB
    """
    errs = _validate_snippet(invalid_node_id)
    assert any(e.code == CODE_INVALID_NODE_ID for e in errs)

    # Invalid flowchart: unbalanced subgraph
    unbalanced_sg = """
    flowchart TD
      subgraph sg1
        A --> B
    """
    errs = _validate_snippet(unbalanced_sg)
    assert any(e.code == CODE_UNBALANCED_SUBGRAPH for e in errs)


def test_graph_valid_and_invalid():
    valid_graph = """
    graph LR
      A[Start] --> B[Process]
      B --> C[End]
    """
    assert _validate_snippet(valid_graph) == []

    # Invalid graph: unclosed bracket
    invalid_graph = """
    graph LR
      A[Start --> B
    """
    errs = _validate_snippet(invalid_graph)
    assert any(e.code == CODE_UNBALANCED_DELIMITERS for e in errs)


def test_sequence_diagram_valid_and_invalid():
    valid_seq = """
    sequenceDiagram
      actor User
      participant Web
      participant API
      User->>Web: Click button
      Web->>API: HTTP POST /orders
      API-->>Web: 201 Created
      Web-->>User: Success
    """
    assert _validate_snippet(valid_seq) == []

    # Invalid sequence: using flowchart thick arrow ==>
    invalid_seq = """
    sequenceDiagram
      Alice ==> Bob: Invalid thick arrow
    """
    errs = _validate_snippet(invalid_seq)
    assert any(e.code == CODE_INVALID_ARROW for e in errs)


def test_class_diagram_valid_and_invalid():
    valid_class = """
    classDiagram
      class Animal {
        +String name
        +makeSound()
      }
      class Duck {
        +swim()
      }
      Animal <|-- Duck
    """
    assert _validate_snippet(valid_class) == []

    # Invalid class: unclosed curly bracket
    invalid_class_bracket = """
    classDiagram
      class Animal {
        +String name
    """
    errs = _validate_snippet(invalid_class_bracket)
    assert any(e.code == CODE_UNBALANCED_DELIMITERS for e in errs)

    # Invalid class: using flowchart dotted arrow -.->
    invalid_class_arrow = """
    classDiagram
      Animal -.-> Duck
    """
    errs = _validate_snippet(invalid_class_arrow)
    assert any(e.code == CODE_INVALID_ARROW for e in errs)


def test_er_diagram_valid_and_invalid():
    valid_er = """
    erDiagram
      CUSTOMER ||--o{ ORDER : places
      ORDER ||--|{ LINE_ITEM : contains
      CUSTOMER {
        string name
        string email
      }
    """
    assert _validate_snippet(valid_er) == []

    # Invalid ER: using flowchart arrow -->
    invalid_er = """
    erDiagram
      CUSTOMER --> ORDER : places
    """
    errs = _validate_snippet(invalid_er)
    assert any(e.code == CODE_INVALID_ARROW for e in errs)


def test_state_diagram_valid_and_invalid():
    valid_state = """
    stateDiagram-v2
      [*] --> Still
      Still --> [*]
      Still --> Moving
      Moving --> Still
      Moving --> Crash
      Crash --> [*]
    """
    assert _validate_snippet(valid_state) == []

    # Invalid state: using single dash arrow ->
    invalid_state = """
    stateDiagram-v2
      [*] -> Still
    """
    errs = _validate_snippet(invalid_state)
    assert any(e.code == CODE_INVALID_ARROW for e in errs)


# --- Tests for each error code rule ---


def test_error_invalid_header():
    snippet = """
    unknownDiagramType
      A --> B
    """
    errs = _validate_snippet(snippet)
    assert len(errs) == 1
    assert errs[0].code == CODE_INVALID_HEADER


def test_error_empty_body():
    snippet = """
    flowchart TD
      %% only a comment
    """
    errs = _validate_snippet(snippet)
    assert len(errs) == 1
    assert errs[0].code == CODE_EMPTY_BODY


def test_error_unbalanced_quotes():
    snippet = """
    flowchart TD
      nodeA["Unterminated string] --> nodeB
    """
    errs = _validate_snippet(snippet)
    assert any(e.code == CODE_UNBALANCED_QUOTES for e in errs)


def test_error_unbalanced_delimiters():
    snippet = """
    flowchart TD
      nodeA(Mismatched bracket] --> nodeB
    """
    errs = _validate_snippet(snippet)
    assert any(e.code == CODE_UNBALANCED_DELIMITERS for e in errs)


# --- Full repository & CLI integration tests ---


def test_check_mermaid_repo_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Architecture\n\n"
        "```mermaid\n"
        "flowchart LR\n"
        "  Client --> Server\n"
        "  Server --> DB[(PostgreSQL)]\n"
        "```\n",
        encoding="utf-8",
    )

    errors, notes = check_mermaid(repo)
    assert errors == []
    assert any("USER_GUIDE.md not found, skipping." in n for n in notes)

    out = io.StringIO()
    code = run_check_mermaid(repo, out=out)
    assert code == 0


def test_check_mermaid_repo_failure_exits_3(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Architecture\n\n"
        "```mermaid\n"
        "flowchart LR\n"
        "  Client -> Server\n"  # Invalid single dash
        "```\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    code = run_check_mermaid(repo, out=out)
    assert code == 3
    output = out.getvalue()
    assert "INVALID_ARROW: docs/DESIGN.md:5:" in output


def test_auto_diagram_markers_handled_cleanly(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Architecture Overview\n\n"
        "<!-- AUTO-DIAGRAM:module-overview:START -->\n"
        "```mermaid\n"
        "flowchart LR\n"
        "  ModuleA --> ModuleB\n"
        "```\n"
        "<!-- AUTO-DIAGRAM:module-overview:END -->\n",
        encoding="utf-8",
    )

    errors, _ = check_mermaid(repo)
    assert errors == []

    out = io.StringIO()
    code = run_check_mermaid(repo, out=out)
    assert code == 0


def test_check_mermaid_json_output(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "```mermaid\nflowchart TD\n  A -> B\n```\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    code = run_check_mermaid(repo, output_format="json", out=out)
    assert code == 3

    data = json.loads(out.getvalue())
    assert data["valid"] is False
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == CODE_INVALID_ARROW


def test_invalid_repo_dir_returns_1(tmp_path):
    out = io.StringIO()
    err = io.StringIO()
    code = run_check_mermaid(tmp_path / "nonexistent", out=out, err=err)
    assert code == 1
    assert "does not exist" in err.getvalue()


def test_mermaid_valid_syntaxes_from_review():
    # 1. Chaining operator &
    assert _validate_snippet("flowchart TD\n  A --> B & C\n") == []

    # 2. Class shorthand :::class
    assert _validate_snippet("flowchart TD\n  A:::hot --> B\n") == []
    assert _validate_snippet("flowchart TD\n  A[Label]:::hot --> B:::cold\n") == []

    # 3. Trailing semicolon / statement separator
    assert _validate_snippet("flowchart TD\n  A --> B;\n") == []
    assert _validate_snippet("flowchart TD\n  A --> B; C --> D;\n") == []

    # 4. Header with semicolon
    assert _validate_snippet("graph TD;\n  A --> B\n") == []
    assert _validate_snippet("flowchart LR;\n  A --> B\n") == []

    # 5. linkStyle keyword skipped
    assert _validate_snippet("flowchart TD\n  A --> B\n  linkStyle 0 stroke:#f00\n") == []

    # 6. Hyphen in label
    assert _validate_snippet("flowchart TD\n  A -- re-try --> B\n") == []

    # 7. Subgraph titles: free text and quoted
    assert _validate_snippet("flowchart TD\n  subgraph Service Layer\n    A --> B\n  end\n") == []
    assert _validate_snippet('flowchart TD\n  subgraph "Service Layer"\n    A --> B\n  end\n') == []

    # 8. Asymmetric shape opener >
    assert _validate_snippet("flowchart TD\n  A>flag] --> B\n") == []

    # 9. Cross arrow --x
    assert _validate_snippet("flowchart TD\n  A --x B\n") == []


def test_mermaid_exact_case_headers(tmp_path):
    # Valid exact headers
    assert _validate_snippet("flowchart LR\n  A --> B\n") == []
    assert _validate_snippet("graph TD\n  A --> B\n") == []
    assert _validate_snippet("sequenceDiagram\n  Alice->>Bob: Hello\n") == []
    assert _validate_snippet("classDiagram\n  class Animal\n") == []
    assert _validate_snippet("erDiagram\n  CUSTOMER ||--o{ ORDER : places\n") == []
    assert _validate_snippet("stateDiagram-v2\n  [*] --> State1\n") == []

    # Case-insensitive headers should fail INVALID_HEADER
    errs = _validate_snippet("Flowchart TD\n  A --> B\n")
    assert any(e.code == CODE_INVALID_HEADER for e in errs)

    errs = _validate_snippet("Graph LR\n  A --> B\n")
    assert any(e.code == CODE_INVALID_HEADER for e in errs)

    # stateDiagram without -v2 should fail INVALID_HEADER
    errs = _validate_snippet("stateDiagram\n  [*] --> S1\n")
    assert any(e.code == CODE_INVALID_HEADER for e in errs)


def test_auto_diagram_hook_failure_reported(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Architecture Overview\n\n"
        "<!-- AUTO-DIAGRAM:module-overview:START -->\n"
        "```mermaid\n"
        "flowchart LR\n"
        "  ModuleA --> ModuleB\n"
        "```\n"
        "<!-- AUTO-DIAGRAM:module-overview:END -->\n",
        encoding="utf-8",
    )

    import arch_indexer.checks.mermaid as mermaid_mod

    monkeypatch.setattr(
        mermaid_mod,
        "verify_auto_diagram",
        lambda diagram_id, mermaid_code, repo_path, graph_path=None: (
            False,
            "Diagram does not match graph.json hash",
        ),
    )

    errors, _ = check_mermaid(repo)
    assert len(errors) == 1
    assert errors[0].code == CODE_AUTO_DIAGRAM_MISMATCH
    assert "Diagram does not match graph.json hash" in errors[0].message

    out = io.StringIO()
    code = run_check_mermaid(repo, out=out)
    assert code == 3
    assert "AUTO_DIAGRAM_MISMATCH" in out.getvalue()


def test_auto_diagram_markers_validation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    # Case 1: END without START
    design1 = docs / "DESIGN.md"
    design1.write_text(
        "<!-- AUTO-DIAGRAM:mod:END -->\n",
        encoding="utf-8",
    )
    errors, _ = check_mermaid(repo)
    assert any(e.code == CODE_AUTO_DIAGRAM_ERROR for e in errors)
    assert any("without matching START" in e.message for e in errors)

    # Case 2: START with no mermaid block before END
    design1.write_text(
        "<!-- AUTO-DIAGRAM:mod:START -->\n<!-- AUTO-DIAGRAM:mod:END -->\n",
        encoding="utf-8",
    )
    errors, _ = check_mermaid(repo)
    assert any(e.code == CODE_AUTO_DIAGRAM_ERROR for e in errors)
    assert any("contains no mermaid block" in e.message for e in errors)

    # Case 3: Unclosed START (missing END)
    design1.write_text(
        "<!-- AUTO-DIAGRAM:mod:START -->\n```mermaid\nflowchart TD\n  A --> B\n```\n",
        encoding="utf-8",
    )
    errors, _ = check_mermaid(repo)
    assert any(e.code == CODE_AUTO_DIAGRAM_ERROR for e in errors)
    assert any("missing matching END" in e.message for e in errors)

    # Case 4: Multiple mermaid blocks inside one AUTO-DIAGRAM
    design1.write_text(
        "<!-- AUTO-DIAGRAM:mod:START -->\n"
        "```mermaid\n"
        "flowchart TD\n"
        "  A --> B\n"
        "```\n"
        "```mermaid\n"
        "flowchart TD\n"
        "  C --> D\n"
        "```\n"
        "<!-- AUTO-DIAGRAM:mod:END -->\n",
        encoding="utf-8",
    )
    errors, _ = check_mermaid(repo)
    assert any(e.code == CODE_AUTO_DIAGRAM_ERROR for e in errors)
    assert any("must wrap exactly one mermaid block" in e.message for e in errors)


def test_form_feed_in_mermaid_document(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    # Form feed character before the mermaid block
    design.write_text(
        "# Heading\x0cSection\n\n```mermaid\nflowchart LR\n  A --> B\n```\n",
        encoding="utf-8",
    )

    errors, _ = check_mermaid(repo)
    assert errors == []


def test_label_text_is_not_parsed_as_diagram_structure():
    # Mermaid shape labels and sequence messages are free text: comparison
    # operators, generics and arrow-like text inside them are not diagram
    # structure. Oracle: Mermaid shape/label and sequence-message grammar.
    assert _validate_snippet("flowchart TD\n  A[count > 0] --> B\n") == []
    assert _validate_snippet("flowchart TD\n  A[Map<K, V>] --> B\n") == []
    assert _validate_snippet("flowchart TD\n  A[a -> b] --> B\n") == []
    assert _validate_snippet("flowchart TD\n  A[x --> y] --> B\n") == []
    assert _validate_snippet("flowchart TD\n  A(step -> next) --> B\n") == []
    assert _validate_snippet("flowchart TD\n  A -->|Map<K,V>| B\n") == []
    assert _validate_snippet("flowchart TD\n  A -->|count>0| B\n") == []
    assert _validate_snippet("flowchart TD\n  A -->|a -> b| B\n") == []
    assert _validate_snippet("flowchart TD\n  A -- x>0 --> B\n") == []
    assert _validate_snippet("flowchart TD\n  A -- a -> b --> B\n") == []
    assert _validate_snippet("sequenceDiagram\n  A->>B: smile :)\n") == []
    assert _validate_snippet("sequenceDiagram\n  A->>B: x ==> y\n") == []

    # Non-message sequence lines keep their structural delimiters and quotes.
    assert _validate_snippet("sequenceDiagram\n  participant DB as Postgres (db:5432)\n") == []
    assert _validate_snippet("sequenceDiagram\n  loop Every 5s (retry: 3)\n") == []
    assert _validate_snippet('sequenceDiagram\n  participant A as "svc: api"\n') == []
    assert _validate_snippet("sequenceDiagram\n  Note over A: retry (max: 3)\n") == []

    # Structural errors outside label text are still detected.
    assert any(
        e.code == CODE_INVALID_ARROW for e in _validate_snippet("flowchart TD\n  A -> B\n")
    )
    assert any(
        e.code == CODE_UNBALANCED_DELIMITERS
        for e in _validate_snippet("flowchart TD\n  A[unclosed --> B\n")
    )
    assert any(
        e.code == CODE_INVALID_ARROW
        for e in _validate_snippet("sequenceDiagram\n  Alice ==> Bob: text\n")
    )
