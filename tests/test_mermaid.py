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
    CODE_UNBALANCED_BLOCK,
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
    assert _validate_snippet("sequenceDiagram\n  loop Every 5s (retry: 3)\n  end\n") == []
    assert _validate_snippet('sequenceDiagram\n  participant A as "svc: api"\n') == []
    assert _validate_snippet("sequenceDiagram\n  Note over A: retry (max: 3)\n") == []

    # Structural errors outside label text are still detected.
    assert any(e.code == CODE_INVALID_ARROW for e in _validate_snippet("flowchart TD\n  A -> B\n"))
    assert any(
        e.code == CODE_UNBALANCED_DELIMITERS
        for e in _validate_snippet("flowchart TD\n  A[unclosed --> B\n")
    )
    assert any(
        e.code == CODE_INVALID_ARROW
        for e in _validate_snippet("sequenceDiagram\n  Alice ==> Bob: text\n")
    )


# --- Tokenizer corpus: label text is opaque, structure is checked ---
# Oracle: Mermaid flowchart, sequence, class, ER and state grammars. Each
# VALID entry is a diagram Mermaid renders; each INVALID entry is a diagram
# Mermaid rejects, paired with the error code the gate must report.

_VALID_CORPUS: dict[str, list[str]] = {
    "flowchart TD": [
        # Shape labels: comparisons, generics, arrow-like and bracketed text
        "A[count > 0] --> B",
        "A[Map<K, V>] --> B",
        "A[a -> b] --> B",
        "A[x --> y] --> B",
        "A(step -> next) --> B",
        'A["quoted (parens) [x]"] --> B',
        # Every node shape
        "A((circle)) --> B(((double)))",
        "A([stadium]) --> B[[subroutine]]",
        "A[(database)] --> B{{hexagon}}",
        "A>flag] --> B[/parallelogram/]",
        "A[\\alt\\] --> B[/trapezoid\\]",
        "A[\\inv/] --> B{decision}",
        'A@{ shape: rect, label: "x" } --> B',
        # Pipe edge labels
        "A -->|Map<K,V>| B",
        "A -->|count>0| B",
        "A -->|a -> b| B",
        'A -->|"pipe | in string"| B',
        "A-->|x|B",
        "A ---|text| B",
        # Inline edge labels
        "A -- x>0 --> B",
        "A -- a -> b --> B",
        "A -- re-try --> B",
        "A -- text --- B",
        "A == text ==> B",
        "A == text === B",
        "A -. text .-> B",
        "A -. text .- B",
        "A-- text -->B",
        # The space after the inline-label opener is optional for every opener
        "A--text-->B",
        "A==text==>B",
        "A-.text.->B",
        "A-.text.-B",
        "A o--text-->B",
        "A<--text-->B",
        # Unlabeled links do not open a label
        "A --- B --> C",
        "A --- B[x --> y] --> C",
        # Link types and length variants
        "A --> B",
        "A --- B",
        "A ---- B",
        "A ----> B",
        "A === B",
        "A ==> B",
        "A ===> B",
        "A -.- B",
        "A -.-> B",
        "A -..-> B",
        "A --x B",
        "A --o B",
        "A <--> B",
        "A <-.-> B",
        "A <==> B",
        "A o--o B",
        "A x--x B",
        "A ~~~ B",
        "A-->B",
        # ':::class', '&', ';'
        "A:::hot --> B:::cold & C",
        "A & B --> C & D",
        "A --> B; C --> D;",
        "re-try --> B",
        # Keywords
        "style A fill:#f9f,stroke:#333",
        "classDef hot fill:#f00;",
        "class A,B hot",
        'click A callback "tooltip"',
        "linkStyle 0 stroke:#f00",
        'subgraph sg1 ["Title (v2)"]\n  direction LR\n  A --> B\nend',
        'subgraph "Service: Layer"\n  A --> B\nend',
    ],
    "graph LR": [
        "A[Start] --> B[Process]",
        "A -->|yes| B{ok?}",
    ],
    "sequenceDiagram": [
        "A->>B: smile :)",
        "A->>B: x ==> y",
        "A->>B: Map<K,V> [x] {y}",
        "A-)B: event",
        "A--)B: done",
        "A-xB: lost",
        "A--xB: lost",
        "A->B: x",
        "A-->B: y",
        "A-->>B: z",
        "A->>+B: activate",
        "B-->>-A: deactivate",
        # Hyphens are ordinary characters inside actor names
        "Client->>auth-service: login",
        "auth-service-->>Client: token",
        "api-gw->>auth-service: forward",
        'participant auth-service as "Auth Service"\n  auth-service->>Client: ok',
        "participant DB as Postgres (db:5432)",
        'participant A as "svc: api"',
        "actor User",
        "loop Every 5s (retry: 3)\n  A->>B: poll\nend",
        "alt ok (200)\n  A->>B: x\nelse error [5xx]\n  A->>B: y\nend",
        "opt maybe\n  A->>B: x\nend",
        "par A to B\n  A->>B: x\nand A to C\n  A->>C: y\nend",
        "rect rgb(0, 0, 255)\n  A->>B: x\nend",
        "critical connect\n  A->>B: x\noption timeout\n  A->>B: y\nend",
        "break when (x > 0\n  A->>B: x\nend",
        "Note over A: retry (max: 3)",
        "Note right of A: unbalanced ( text",
        "Note over A,B: shared",
        "autonumber",
        "activate A",
    ],
    "classDiagram": [
        "class Animal {\n  +String name\n  +makeSound() Map~K,V~\n}",
        "class Shape~T~",
        'class A["Label (x)"]',
        "class A { +x }",
        "class Animal:::someclass",
        "<<interface>> Shape",
        "Animal <|-- Duck",
        "A *-- B",
        "A o-- B",
        "A --> B",
        "A -- B",
        "A ..> B",
        "A ..|> B",
        "A .. B",
        "A --() B",
        'A "1" --> "*" B : has',
        "A --> B : uses (Map<K,V>)",
        "Animal : +int age",
        "Animal : +isMammal() bool",
        'note for A "text (x)"',
        "namespace Shapes {\n  class Square\n}",
    ],
    "erDiagram": [
        "CUSTOMER ||--o{ ORDER : places",
        "ORDER ||--|{ LINE-ITEM : contains",
        "CUSTOMER }|..|{ DELIVERY-ADDRESS : uses",
        'A |o..o| B : "may (have)"',
        'CUSTOMER {\n  string name PK "the (name)"\n}',
        "CUSTOMER",
        "p[Person] {\n  string x\n}",
    ],
    "stateDiagram-v2": [
        "[*] --> Still",
        "Still --> [*]",
        "S1 --> S2 : go (fast) -> now",
        'state "Long desc" as S1',
        "state S1 {\n  [*] --> A\n  --\n  [*] --> B\n}",
        "state f <<fork>>",
        "state c <<choice>>",
        "S1 : a description (x)",
        "note right of S1 : hi (x",
        "note left of S1\n  multi (line\nend note",
        "S1:::cls --> S2",
        "S1",
        "direction LR",
    ],
}

_INVALID_CORPUS: list[tuple[str, str]] = [
    # Unclosed shape
    ("flowchart TD\n  A[unclosed --> B", CODE_UNBALANCED_DELIMITERS),
    ("flowchart TD\n  A --- B[unclosed --> C", CODE_UNBALANCED_DELIMITERS),
    ("flowchart TD\n  A --- B[label (oops] --> C", CODE_UNBALANCED_DELIMITERS),
    ("flowchart TD\n  A === B{bad] --> C", CODE_UNBALANCED_DELIMITERS),
    ("flowchart TD\n  A(x] --> B", CODE_UNBALANCED_DELIMITERS),
    ("flowchart TD\n  A -->|oops B", CODE_UNBALANCED_DELIMITERS),
    ("classDiagram\n  class A {\n    +x", CODE_UNBALANCED_DELIMITERS),
    ("classDiagram\n  }", CODE_UNBALANCED_DELIMITERS),
    ("erDiagram\n  A {\n    string x", CODE_UNBALANCED_DELIMITERS),
    ("stateDiagram-v2\n  state S {\n    A", CODE_UNBALANCED_DELIMITERS),
    # Unclosed string
    ('flowchart TD\n  A["oops] --> B', CODE_UNBALANCED_QUOTES),
    ('flowchart TD\n  A -->|"oops| B', CODE_UNBALANCED_QUOTES),
    ('sequenceDiagram\n  participant A as "oops', CODE_UNBALANCED_QUOTES),
    ('erDiagram\n  A {\n    string x "oops\n  }', CODE_UNBALANCED_QUOTES),
    ('stateDiagram-v2\n  state "oops as S1', CODE_UNBALANCED_QUOTES),
    # Unbalanced subgraph
    ("flowchart TD\n  subgraph X\n    A --> B", CODE_UNBALANCED_SUBGRAPH),
    ("flowchart TD\n  A --> B\n  end", CODE_UNBALANCED_SUBGRAPH),
    # Bad header and empty body
    ("Flowchart TD\n  A --> B", CODE_INVALID_HEADER),
    ("stateDiagram\n  [*] --> S", CODE_INVALID_HEADER),
    ("sequenceDiagram\n  %% nothing", CODE_EMPTY_BODY),
    # Bad arrow
    ("flowchart TD\n  A -> B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A => B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A -.> B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A ->> B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A -->> B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A --- B -> C --> D", CODE_INVALID_ARROW),
    ("flowchart TD\n  A -- unterminated label B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A--text--B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A==text==B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A-.text.B", CODE_INVALID_ARROW),
    ("flowchart TD\n  A -->", CODE_INVALID_ARROW),
    ("flowchart TD\n  --> B", CODE_INVALID_ARROW),
    ("sequenceDiagram\n  Alice ==> Bob: text", CODE_INVALID_ARROW),
    ("sequenceDiagram\n  A -.-> B: text", CODE_INVALID_ARROW),
    ("classDiagram\n  A -.-> B", CODE_INVALID_ARROW),
    ("classDiagram\n  A ==> B", CODE_INVALID_ARROW),
    ("erDiagram\n  A --> B : x", CODE_INVALID_ARROW),
    ("stateDiagram-v2\n  [*] -> S", CODE_INVALID_ARROW),
    ("stateDiagram-v2\n  S ==> T", CODE_INVALID_ARROW),
    # Bad node id
    ("flowchart TD\n  node@1 --> B", CODE_INVALID_NODE_ID),
    ("flowchart TD\n  A --- B c --> C", CODE_INVALID_NODE_ID),
    ("flowchart TD\n  subgraph sg@1 [Title]\n  end", CODE_INVALID_NODE_ID),
    ("classDiagram\n  An@imal <|-- Duck", CODE_INVALID_NODE_ID),
    ("erDiagram\n  A@ ||--o{ B : x", CODE_INVALID_NODE_ID),
    ("stateDiagram-v2\n  S@1 --> T", CODE_INVALID_NODE_ID),
]


def test_valid_corpus_passes_for_every_diagram_type():
    failures = []
    for header, statements in _VALID_CORPUS.items():
        for statement in statements:
            errs = _validate_snippet(f"{header}\n{statement}\n")
            if errs:
                failures.append((header, statement, [e.format_line() for e in errs]))
    assert failures == []


def test_invalid_corpus_fails_with_expected_code():
    misses = []
    for snippet, code in _INVALID_CORPUS:
        errs = _validate_snippet(snippet)
        if not any(e.code == code for e in errs):
            misses.append((snippet, code, [e.format_line() for e in errs]))
    assert misses == []


def test_unlabeled_link_does_not_swallow_following_node():
    # A '---'/'===' link must not be read as an inline-label opener that hides
    # errors in the next node expression.
    errs = _validate_snippet("flowchart TD\n  A --- B[unclosed --> C\n")
    assert [e.code for e in errs] == [CODE_UNBALANCED_DELIMITERS]
    assert _validate_snippet("flowchart TD\n  A --- B[x --> y] --> C\n") == []


def test_invalid_corpus_reports_correct_line_number():
    block = ["flowchart TD", "  A --> B", "  C -> D"]
    errs = validate_mermaid_block(block, "docs/DESIGN.md", 10)
    assert [(e.code, e.line) for e in errs] == [(CODE_INVALID_ARROW, 13)]


# --- sequenceDiagram block balance ---
# Oracle: every sequence block opens with a header statement and closes with
# 'end'; 'else', 'and' and 'option' only continue an 'alt', 'par' and
# 'critical' block respectively.

_SEQUENCE_BALANCED_CORPUS: list[str] = [
    "loop Every 5s (retry: 3)\n  A->>B: poll\nend",
    "alt ok (200)\n  A->>B: x\nelse error [5xx]\n  A->>B: y\nelse timeout\n  A->>B: z\nend",
    "opt maybe\n  A->>B: x\nend",
    "par A to B\n  A->>B: x\nand A to C\n  A->>C: y\nend",
    "rect rgb(0, 0, 255)\n  A->>B: x\nend",
    "critical connect\n  A->>B: x\noption timeout\n  A->>B: y\nend",
    "break when (x > 0)\n  A->>B: abandoned\nend",
    "box LightBlue Service\n  participant A\n  A->>B: x\nend",
    # Nesting: each 'end' closes the innermost open block.
    "alt outer\n  loop inner\n    A->>B: x\n  end\n"
    "else other\n  opt deep\n    A->>B: y\n  end\nend",
    (
        "par a\n  critical c\n    A->>B: x\n  option o\n    A->>B: y\n  end\n"
        "and b\n  rect rgb(0,0,0)\n    A->>B: z\n  end\nend"
    ),
    "loop a\n  alt b\n    A->>B: x\n  else c\n    A->>B: y\n  end\nend",
]

_SEQUENCE_UNBALANCED_CORPUS: list[tuple[str, str]] = [
    # Every opener must close with 'end'.
    ("sequenceDiagram\n  loop forever\n    A->>B: poll", "Unclosed 'loop' block"),
    ("sequenceDiagram\n  alt ok\n    A->>B: x", "Unclosed 'alt' block"),
    ("sequenceDiagram\n  opt maybe\n    A->>B: x", "Unclosed 'opt' block"),
    ("sequenceDiagram\n  par a\n    A->>B: x", "Unclosed 'par' block"),
    ("sequenceDiagram\n  rect rgb(0,0,0)\n    A->>B: x", "Unclosed 'rect' block"),
    ("sequenceDiagram\n  critical c\n    A->>B: x", "Unclosed 'critical' block"),
    ("sequenceDiagram\n  break x\n    A->>B: x", "Unclosed 'break' block"),
    ("sequenceDiagram\n  box Blue\n    A->>B: x", "Unclosed 'box' block"),
    # Only an inner block closed: the outer opener is still unbalanced.
    (
        "sequenceDiagram\n  loop a\n    A->>B: x\n    alt b\n      A->>C: y\n    end",
        "Unclosed 'loop' block",
    ),
    # An 'end' without an open block is an error, including one extra 'end'.
    ("sequenceDiagram\n  A->>B: x\n  end", "'end' without a matching block opener"),
    ("sequenceDiagram\n  loop a\n  end\n  end", "'end' without a matching block opener"),
    # Branch keywords are invalid outside their parent block.
    ("sequenceDiagram\n  else nope\n  end", "'else' is only valid inside an enclosing 'alt'"),
    ("sequenceDiagram\n  and nope\n  end", "'and' is only valid inside an enclosing 'par'"),
    (
        "sequenceDiagram\n  option nope\n  end",
        "'option' is only valid inside an enclosing 'critical'",
    ),
    # Branch keywords are invalid inside the wrong parent block.
    (
        "sequenceDiagram\n  loop a\n    else b\n  end",
        "'else' is only valid inside an enclosing 'alt'",
    ),
    (
        "sequenceDiagram\n  alt a\n    and b\n  end",
        "'and' is only valid inside an enclosing 'par'",
    ),
    (
        "sequenceDiagram\n  par a\n    else b\n  end",
        "'else' is only valid inside an enclosing 'alt'",
    ),
    (
        "sequenceDiagram\n  critical a\n    and b\n  end",
        "'and' is only valid inside an enclosing 'par'",
    ),
    # Branch keywords only see the innermost open block, not an outer one.
    (
        "sequenceDiagram\n  alt a\n    loop b\n      else c\n    end\n  end",
        "'else' is only valid inside an enclosing 'alt'",
    ),
]


def test_sequence_block_balance_valid_corpus():
    failures = []
    for statement in _SEQUENCE_BALANCED_CORPUS:
        errs = _validate_snippet(f"sequenceDiagram\n{statement}\n")
        if errs:
            failures.append((statement, [e.format_line() for e in errs]))
    assert failures == []


def test_sequence_block_balance_invalid_corpus():
    misses = []
    for snippet, expected in _SEQUENCE_UNBALANCED_CORPUS:
        errs = _validate_snippet(snippet)
        if not any(e.code == CODE_UNBALANCED_BLOCK and expected in e.message for e in errs):
            misses.append((snippet, expected, [e.format_line() for e in errs]))
    assert misses == []


def test_lone_loop_opener_fails_as_unbalanced_block():
    # A 'loop' header without a matching 'end' is not a valid diagram.
    errs = _validate_snippet("sequenceDiagram\n  loop Every 5s (retry: 3)\n")
    assert [(e.code, e.line) for e in errs] == [(CODE_UNBALANCED_BLOCK, 3)]


def test_unmatched_end_reports_line_and_fails_gate(tmp_path):
    block = ["sequenceDiagram", "  A->>B: x", "  end"]
    errs = validate_mermaid_block(block, "docs/DESIGN.md", 20)
    assert [(e.code, e.line) for e in errs] == [(CODE_UNBALANCED_BLOCK, 23)]

    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    (docs / "DESIGN.md").write_text(
        "```mermaid\nsequenceDiagram\n  A->>B: x\n  end\n```\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    code = run_check_mermaid(repo, out=out)
    assert code == 3
    assert "UNBALANCED_BLOCK: docs/DESIGN.md:4:" in out.getvalue()
