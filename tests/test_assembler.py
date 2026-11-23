from __future__ import annotations

import io
import json

import pytest

from arch_indexer.assembler import (
    DraftFormatError,
    assemble,
    inject_diagrams,
    parse_draft_file,
    patch_section_in_doc,
    renumber_footnotes_in_doc,
)
from arch_indexer.cli import main
from arch_indexer.skeletons import get_skeleton_path, load_skeleton


def test_skeleton_loading_and_content():
    """Verify canonical document skeletons exist, load, and have expected section markers."""
    design_skel = load_skeleton("DESIGN.md")
    assert "# Technical Design Document" in design_skel
    assert "<!-- SECTION:design-sec1:START:exempt -->" in design_skel
    assert "<!-- SECTION:design-sec2:START -->" in design_skel
    assert "<!-- SECTION:design-sec3:START -->" in design_skel
    assert "<!-- SECTION:design-sec4:START -->" in design_skel
    assert "<!-- SECTION:design-sec5:START -->" in design_skel
    assert "<!-- SECTION:design-sec6:START -->" in design_skel
    assert "<!-- SECTION:design-sec7:START -->" in design_skel
    assert "<!-- SECTION:design-sec8:START -->" in design_skel
    assert "<!-- SECTION:design-references:START:exempt -->" in design_skel
    assert "<!-- AUTO-DIAGRAM:module-overview:START -->" in design_skel
    assert "<!-- AUTO-DIAGRAM:flow-overview:START -->" in design_skel

    guide_skel = load_skeleton("USER_GUIDE.md")
    assert "# User Guide" in guide_skel
    assert "<!-- SECTION:guide-sec1:START:exempt -->" in guide_skel
    assert "<!-- SECTION:guide-sec2:START -->" in guide_skel
    assert "<!-- SECTION:guide-sec3:START -->" in guide_skel
    assert "<!-- SECTION:guide-references:START:exempt -->" in guide_skel
    assert "### How to Task Template" in guide_skel
    assert "[HUMAN INTENT NEEDED]" in guide_skel

    assert get_skeleton_path("DESIGN.md").is_file()
    assert get_skeleton_path("USER_GUIDE.md").is_file()


def test_parse_draft_file(tmp_path):
    """Test parsing draft frontmatter and extracting sections and definitions."""
    draft_path = tmp_path / "packet-1.md"
    draft_path.write_text(
        "---\n"
        "packet_id: packet-1\n"
        "target_doc: DESIGN.md\n"
        "section_ids:\n"
        "  - design-sec2\n"
        "---\n"
        "## 2. Components & Modules\n\n"
        "The service starts properly[^1].\n\n"
        "[^1]: `src/app.py:10` — `start`\n",
        encoding="utf-8",
    )

    packet = parse_draft_file(draft_path)
    assert packet.packet_id == "packet-1"
    assert packet.target_doc == "DESIGN.md"
    assert packet.section_ids == ["design-sec2"]
    assert "design-sec2" in packet.sections
    assert "[^packet-1__1]" in packet.sections["design-sec2"]
    assert "packet-1__1" in packet.definitions
    assert packet.definitions["packet-1__1"] == "`src/app.py:10` — `start`"


def test_parse_draft_multi_section(tmp_path):
    """Test parsing draft with multiple sections."""
    draft_path = tmp_path / "packet-multi.md"
    draft_path.write_text(
        "---\n"
        "packet_id: multi-packet\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2, design-sec3]\n"
        "---\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components\n"
        "Component text[^1].\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "<!-- SECTION:design-sec3:START -->\n"
        "## 3. Flows\n"
        "Flow text[^2].\n"
        "<!-- SECTION:design-sec3:END -->\n\n"
        "[^1]: `src/c.py:5` — `comp`\n"
        "[^2]: `src/f.py:8` — `flow`\n",
        encoding="utf-8",
    )

    packet = parse_draft_file(draft_path)
    assert packet.packet_id == "multi-packet"
    assert packet.section_ids == ["design-sec2", "design-sec3"]
    assert "design-sec2" in packet.sections
    assert "design-sec3" in packet.sections
    assert "[^multi-packet__1]" in packet.sections["design-sec2"]
    assert "[^multi-packet__2]" in packet.sections["design-sec3"]


def test_patch_section_in_doc():
    doc = (
        "<!-- SECTION:sec1:START -->\n"
        "Old Sec 1\n"
        "<!-- SECTION:sec1:END -->\n\n"
        "<!-- SECTION:sec2:START -->\n"
        "Old Sec 2\n"
        "<!-- SECTION:sec2:END -->\n"
    )
    patched = patch_section_in_doc(doc, "sec1", [("pkt-a", "New Sec 1 Content")])
    assert "New Sec 1 Content" in patched
    assert "Old Sec 2" in patched
    assert "Old Sec 1" not in patched
    assert "<!-- SLICE:pkt-a:START -->" in patched


def test_renumber_footnotes_and_deduplication():
    doc = (
        "# Design\n\n"
        "<!-- SECTION:sec1:START -->\n"
        "Part A calls func_a[^ref1] and again func_a[^ref2].\n"
        "<!-- SECTION:sec1:END -->\n\n"
        "<!-- SECTION:sec2:START -->\n"
        "Part B calls func_b[^ref3].\n"
        "<!-- SECTION:sec2:END -->\n\n"
        "<!-- SECTION:references:START:exempt -->\n"
        "## References\n\n"
        "<!-- SECTION:references:END -->\n"
    )
    definitions = {
        "ref1": "`src/a.py:10` — `func_a`",
        "ref2": "`src/a.py:10` — `func_a`",  # identical to ref1
        "ref3": "`src/b.py:20` — `func_b`",
    }
    renumbered = renumber_footnotes_in_doc(doc, definitions)

    assert "Part A calls func_a[^1] and again func_a[^1]." in renumbered
    assert "Part B calls func_b[^2]." in renumbered
    assert "[^1]: `src/a.py:10` — `func_a`" in renumbered
    assert "[^2]: `src/b.py:20` — `func_b`" in renumbered
    assert "[^3]" not in renumbered


def test_inject_diagrams():
    doc = (
        "<!-- AUTO-DIAGRAM:module-overview:START -->\n"
        "```mermaid\n"
        "flowchart LR\n"
        "  Old --> Diagram\n"
        "```\n"
        "<!-- AUTO-DIAGRAM:module-overview:END -->\n\n"
        "<!-- AUTO-DIAGRAM:flow-overview:START -->\n"
        "```mermaid\n"
        "flowchart TD\n"
        "  Untouched\n"
        "```\n"
        "<!-- AUTO-DIAGRAM:flow-overview:END -->\n"
    )
    mapping = {
        "module-overview": "flowchart LR\n  ModA --> ModB",
    }
    injected = inject_diagrams(doc, mapping)
    assert "ModA --> ModB" in injected
    assert "Untouched" in injected

    # If mapping is None, doc is unchanged
    assert inject_diagrams(doc, None) == doc


def test_assemble_first_run_creates_docs(tmp_path):
    """First run creates docs from skeletons, patches sections, and deletes drafts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text(
        "def start_service():\n    return True\n", encoding="utf-8"
    )

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    draft1 = drafts / "packet-1.md"
    draft1.write_text(
        "---\n"
        "packet_id: packet-1\n"
        "target_doc: DESIGN.md\n"
        "section_ids:\n"
        "  - design-sec2\n"
        "---\n"
        "## 2. Components & Modules\n\n"
        "The core service is initialized here[^1].\n\n"
        "[^1]: `src/service.py:1` — `start_service`\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=out, err=err)
    assert code == 0

    design_file = repo / "docs" / "DESIGN.md"
    assert design_file.is_file()
    content = design_file.read_text(encoding="utf-8")
    assert "The core service is initialized here[^1]." in content
    assert "[^1]: `src/service.py:1` — `start_service`" in content
    assert "## 1. Architecture Overview" in content
    assert "## 3. Control & Data Flows" in content

    # Drafts directory must be deleted on success
    assert not drafts.exists()


def test_assemble_incremental_run_patches_only_named_sections(tmp_path):
    """Incremental run patches only named sections, preserving untouched sections and citations."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text(
        "def start_service():\n    return True\n\ndef helper_flow():\n    return False\n",
        encoding="utf-8",
    )

    # First run: create docs with sec2 and sec3
    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "packet-init.md").write_text(
        "---\n"
        "packet_id: init\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2, design-sec3]\n"
        "---\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "Initial components text[^1].\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "<!-- SECTION:design-sec3:START -->\n"
        "## 3. Control & Data Flows\n\n"
        "Initial flow text[^2].\n"
        "<!-- SECTION:design-sec3:END -->\n\n"
        "[^1]: `src/service.py:1` — `start_service`\n"
        "[^2]: `src/service.py:4` — `helper_flow`\n",
        encoding="utf-8",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts")
    assert code == 0
    assert not drafts.exists()

    # Second run: re-draft ONLY design-sec2's own packet
    drafts.mkdir(parents=True)
    (drafts / "packet-patch-sec2.md").write_text(
        "---\n"
        "packet_id: init\n"
        "target_doc: DESIGN.md\n"
        "section_ids:\n"
        "  - design-sec2\n"
        "---\n"
        "## 2. Components & Modules\n\n"
        "Updated components text with new citation[^1].\n\n"
        "[^1]: `src/service.py:1` — `start_service`\n",
        encoding="utf-8",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts")
    assert code == 0
    assert not drafts.exists()

    design_content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    # Section 2 was updated
    assert "Updated components text with new citation[^1]." in design_content
    assert "Initial components text" not in design_content

    # Section 3 was preserved untouched!
    assert "Initial flow text[^2]." in design_content

    # Both citations are present in References
    assert "[^1]: `src/service.py:1` — `start_service`" in design_content
    assert "[^2]: `src/service.py:4` — `helper_flow`" in design_content


def test_assemble_footnote_deduplication_across_drafts(tmp_path):
    """Identical citations across multiple drafts are deduplicated into a single footnote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def common():\n    pass\n\ndef specific():\n    pass\n", encoding="utf-8"
    )

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)

    # Draft 1
    (drafts / "d1.md").write_text(
        "---\n"
        "packet_id: d1\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "Sec 2 calls common[^1].\n\n"
        "[^1]: `src/app.py:1` — `common`\n",
        encoding="utf-8",
    )

    # Draft 2: also calls common as local [^1], plus specific as local [^2]
    (drafts / "d2.md").write_text(
        "---\n"
        "packet_id: d2\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec3]\n"
        "---\n"
        "Sec 3 calls common[^1] and specific[^2].\n\n"
        "[^1]: `src/app.py:1` — `common`\n"
        "[^2]: `src/app.py:4` — `specific`\n",
        encoding="utf-8",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts")
    assert code == 0

    design_content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "Sec 2 calls common[^1]." in design_content
    assert "Sec 3 calls common[^1] and specific[^2]." in design_content
    assert "[^1]: `src/app.py:1` — `common`" in design_content
    assert "[^2]: `src/app.py:4` — `specific`" in design_content
    assert "[^3]" not in design_content


def test_assemble_diagram_injection(tmp_path):
    """Mermaid blocks are injected into AUTO-DIAGRAM slots from --diagrams JSON mapping."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "d.md").write_text(
        "---\n"
        "packet_id: d\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "Sec 2 text[^1].\n\n"
        "[^1]: `src/app.py:1` — `run`\n",
        encoding="utf-8",
    )

    diagrams_file = tmp_path / "diagrams.json"
    diagrams_file.write_text(
        json.dumps(
            {
                "module-overview": "flowchart LR\n  App --> DB",
            }
        ),
        encoding="utf-8",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts", diagrams=diagrams_file)
    assert code == 0

    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "App --> DB" in content
    # flow-overview was not in diagrams mapping, remains default placeholder
    assert "Flow" in content


def test_assemble_gate_failure_keeps_drafts_and_exits_3_citation_error(tmp_path):
    """If check-citations fails, assemble exits 3 and keeps drafts intact."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    draft_file = drafts / "broken.md"
    draft_file.write_text(
        "---\n"
        "packet_id: broken\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "Sec 2 text[^1].\n\n"
        "[^1]: `src/app.py:99` — `missing_symbol`\n",
        encoding="utf-8",
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", err=err)
    assert code == 3
    # Drafts must still exist on failure!
    assert drafts.exists()
    assert draft_file.is_file()


def test_assemble_gate_failure_keeps_drafts_uncited_section(tmp_path):
    """If draft adds technical prose to DESIGN.md without citations, exit 3 is returned."""
    repo = tmp_path / "repo"
    repo.mkdir()

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    draft_file = drafts / "uncited.md"
    draft_file.write_text(
        "---\n"
        "packet_id: uncited\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "## 2. Components & Modules\n\n"
        "This is technical content with zero citations.\n",
        encoding="utf-8",
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", err=err)
    assert code == 3
    assert drafts.exists()
    assert draft_file.is_file()


def test_assemble_gate_failure_invalid_mermaid(tmp_path):
    """If draft or injected diagram has invalid Mermaid syntax, check-mermaid triggers exit 3."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    draft_file = drafts / "d.md"
    draft_file.write_text(
        "---\n"
        "packet_id: d\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "Sec 2 text[^1].\n\n"
        "[^1]: `src/app.py:1` — `run`\n",
        encoding="utf-8",
    )

    bad_diagrams = tmp_path / "bad_diagrams.json"
    bad_diagrams.write_text(
        json.dumps({"module-overview": "invalid_header\n  A --> B"}),
        encoding="utf-8",
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", diagrams=bad_diagrams, err=err)
    assert code == 3
    assert drafts.exists()


def test_assemble_user_guide(tmp_path):
    """Test assembling drafts targeting USER_GUIDE.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "cli.py").write_text("def execute():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "guide-draft.md").write_text(
        "---\n"
        "packet_id: guide-draft\n"
        "target_doc: USER_GUIDE.md\n"
        "section_ids: [guide-sec3]\n"
        "---\n"
        "### How to Run Pipeline\n"
        "- **When to use**: Starting the pipeline\n"
        "- **Prerequisites**: Installed tool\n"
        "- **Steps**:\n"
        "  1. Run `arch-indexer index`[^1]\n"
        "- **Expected Result**: Success\n"
        "- **Common Errors**:\n"
        "  - *Error*: Missing toolchain\n\n"
        "[^1]: `src/cli.py:1` — `execute`\n",
        encoding="utf-8",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts")
    assert code == 0
    assert not drafts.exists()

    guide_content = (repo / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    assert "### How to Run Pipeline" in guide_content
    assert "[^1]: `src/cli.py:1` — `execute`" in guide_content


def test_assemble_cli_invocation(tmp_path):
    """Test CLI command `arch-indexer assemble <repo> --drafts-dir docs/.drafts`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def main_app():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "packet-cli.md").write_text(
        "---\n"
        "packet_id: packet-cli\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "CLI assembled section[^1].\n\n"
        "[^1]: `src/app.py:1` — `main_app`\n",
        encoding="utf-8",
    )

    code = main(["assemble", str(repo), "--drafts-dir", "docs/.drafts"])
    assert code == 0
    assert not drafts.exists()
    assert (repo / "docs" / "DESIGN.md").is_file()


def test_assemble_refuses_drafts_dir_that_is_repo_root_or_docs_dir(tmp_path):
    """The drafts directory must never be the repo root or the docs output directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Project\n\nOverview prose.\n", encoding="utf-8")

    code = assemble(repo=repo, drafts_dir=".", out=io.StringIO(), err=io.StringIO())
    assert code == 1
    assert (repo / "README.md").is_file()

    docs = repo / "docs"
    docs.mkdir()
    existing = docs / "DESIGN.md"
    existing.write_text("# Technical Design Document\n\nExisting.\n", encoding="utf-8")

    code = assemble(repo=repo, drafts_dir="docs", out=io.StringIO(), err=io.StringIO())
    assert code == 1
    assert existing.is_file()


def test_assemble_missing_declared_section_exits_3_and_keeps_drafts(tmp_path):
    """A draft that declares sections it does not supply is rejected, and drafts are kept."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "partial.md"
    draft.write_text(
        "---\n"
        "packet_id: partial\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2, design-sec3]\n"
        "---\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "Only section two is present[^1].\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "[^1]: `src/app.py:1` — `run`\n",
        encoding="utf-8",
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=err)
    assert code == 3
    assert "design-sec3" in err.getvalue()
    assert draft.is_file()
    assert not (repo / "docs" / "DESIGN.md").exists()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "Intro prose before any marker[^1].\n\n"
            "<!-- SECTION:design-sec2:START -->\n"
            "## 2. Components & Modules\n\n"
            "Component prose[^1].\n"
            "<!-- SECTION:design-sec2:END -->\n\n"
            "[^1]: `src/app.py:1` — `alpha`\n",
            id="prose-before-first-marker",
        ),
        pytest.param(
            "<!-- SECTION:design-sec2:START -->\n"
            "## 2. Components & Modules\n\n"
            "Component prose[^1].\n"
            "<!-- SECTION:design-sec2:END -->\n\n"
            "Trailing prose owned by no marker[^1].\n\n"
            "[^1]: `src/app.py:1` — `alpha`\n",
            id="prose-after-last-marker",
        ),
        pytest.param(
            "<!-- SECTION:design-sec2:START -->\n"
            "## 2. Components & Modules\n\nComponent prose[^1].\n"
            "<!-- SECTION:design-sec2:END -->\n\n"
            "<!-- SECTION:design-sec3:START -->\n"
            "## 3. Control & Data Flows\n\nUndeclared flow prose[^1].\n"
            "<!-- SECTION:design-sec3:END -->\n\n"
            "[^1]: `src/app.py:1` — `alpha`\n",
            id="marker-id-not-in-section-ids",
        ),
        pytest.param(
            "<!-- SECTION:design-sec2:START -->\n"
            "## 2. Components & Modules\n\nFirst copy[^1].\n"
            "<!-- SECTION:design-sec2:END -->\n\n"
            "<!-- SECTION:design-sec2:START -->\n"
            "Second copy[^1].\n"
            "<!-- SECTION:design-sec2:END -->\n\n"
            "[^1]: `src/app.py:1` — `alpha`\n",
            id="repeated-marker-id",
        ),
        pytest.param(
            "<!-- SLICE:other-packet:START -->\n"
            "## 2. Components & Modules\n\nCopied slice prose[^1].\n"
            "<!-- SLICE:other-packet:END -->\n\n"
            "[^1]: `src/app.py:1` — `alpha`\n",
            id="draft-authors-slice-markers",
        ),
    ],
)
def test_assemble_rejects_drafted_prose_no_section_owns(tmp_path, body):
    """Prose outside declared section markers exits 3 with drafts kept and no docs written."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(drafts, "unowned.md", "unowned", ["design-sec2"], body)

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=err)
    assert code == 3
    assert (drafts / "unowned.md").is_file()
    assert not (repo / "docs" / "DESIGN.md").exists()


def test_assemble_incremental_slice_redraft_preserves_sibling_slices(tmp_path):
    """Re-drafting one module slice keeps the sibling slices and their citations (SPEC 9.2/9.3)."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "billing.md",
        "design-sec2-mod-billing",
        ["design-sec2"],
        "## 2. Components & Modules\n\nBilling module debits accounts[^1].\n\n"
        "[^1]: `src/app.py:1` — `alpha`\n",
    )
    _write_design_draft(
        drafts,
        "orders.md",
        "design-sec2-mod-orders",
        ["design-sec2"],
        "## 2. Components & Modules\n\nOrders module tracks shipments[^1].\n\n"
        "[^1]: `src/app.py:4` — `beta`\n",
    )

    assert assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO()) == 0
    first = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert first.index("Billing module debits accounts") < first.index(
        "Orders module tracks shipments"
    )

    drafts.mkdir(parents=True)
    _write_design_draft(
        drafts,
        "billing-2.md",
        "design-sec2-mod-billing",
        ["design-sec2"],
        "## 2. Components & Modules\n\nBilling module now refunds accounts[^1].\n\n"
        "[^1]: `src/app.py:1` — `alpha`\n",
    )

    assert assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO()) == 0
    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "Billing module now refunds accounts[^1]." in content
    assert "Billing module debits accounts" not in content
    assert "Orders module tracks shipments[^2]." in content
    assert "[^2]: `src/app.py:4` — `beta`" in content
    assert "<!-- SLICE:design-sec2-mod-orders:START -->" in content
    assert content.count("<!-- SLICE:design-sec2-mod-orders:START -->") == 1


def test_assemble_inserts_new_slice_in_packet_id_order(tmp_path):
    """A slice added by a later run is inserted among the existing slices by packet_id."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "orders.md",
        "design-sec2-mod-orders",
        ["design-sec2"],
        "Orders module tracks shipments[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )
    assert assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO()) == 0

    drafts.mkdir(parents=True)
    _write_design_draft(
        drafts,
        "accounts.md",
        "design-sec2-mod-accounts",
        ["design-sec2"],
        "Accounts module owns balances[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n",
    )
    _write_design_draft(
        drafts,
        "billing.md",
        "design-sec2-mod-billing",
        ["design-sec2"],
        "Billing module debits accounts[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n",
    )
    assert assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO()) == 0

    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert (
        content.index("Accounts module owns balances")
        < content.index("Billing module debits accounts")
        < content.index("Orders module tracks shipments")
    )


def test_assemble_remove_slice_retires_stale_slice(tmp_path):
    """Retiring a deleted packet's slice drops its prose and citations, keeping siblings."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "billing.md",
        "design-sec2-mod-billing",
        ["design-sec2"],
        "Billing module debits accounts[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n",
    )
    _write_design_draft(
        drafts,
        "orders.md",
        "design-sec2-mod-orders",
        ["design-sec2"],
        "Orders module tracks shipments[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )
    assert assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO()) == 0

    drafts.mkdir(parents=True)
    _write_design_draft(
        drafts,
        "orders.md",
        "design-sec2-mod-orders",
        ["design-sec2"],
        "Orders module tracks shipments v2[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )
    code = assemble(
        repo=repo,
        drafts_dir="docs/.drafts",
        remove_slices=["design-sec2-mod-billing"],
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert code == 0
    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "Billing module debits accounts" not in content
    assert "<!-- SLICE:design-sec2-mod-billing:START -->" not in content
    assert "src/app.py:1" not in content
    assert "Orders module tracks shipments v2[^1]." in content
    assert "[^1]: `src/app.py:4` — `beta`" in content
    assert content.count("## 2. Components & Modules") == 1


def test_assemble_remove_slice_without_drafts(tmp_path):
    """--remove-slice alone retires slices even when the drafts directory is gone."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "billing.md",
        "design-sec2-mod-billing",
        ["design-sec2"],
        "Billing module debits accounts[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n",
    )
    _write_design_draft(
        drafts,
        "orders.md",
        "design-sec2-mod-orders",
        ["design-sec2"],
        "Orders module tracks shipments[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )
    assert assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO()) == 0
    assert not drafts.exists()

    code = assemble(
        repo=repo,
        drafts_dir="docs/.drafts",
        remove_slices=["design-sec2-mod-billing", "design-sec2-mod-orders"],
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert code == 0
    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "<!-- SLICE:" not in content
    assert "Billing module debits accounts" not in content
    assert "Orders module tracks shipments" not in content
    assert "src/app.py:1" not in content
    assert "src/app.py:4" not in content
    assert "## 2. Components & Modules" in content


@pytest.mark.parametrize(
    "second_sections",
    [["design-sec2"], ["design-sec4"]],
    ids=["same-section", "different-section"],
)
def test_assemble_rejects_repeated_packet_id_across_drafts(tmp_path, second_sections):
    """A packet_id declared by two drafts exits 1 and keeps both, whatever sections they patch."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "first.md",
        "design-sec2-mod-billing",
        ["design-sec2"],
        "First billing prose[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n",
    )
    _write_design_draft(
        drafts,
        "second.md",
        "design-sec2-mod-billing",
        second_sections,
        "Second billing prose[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=err)
    assert code == 1
    assert "design-sec2-mod-billing" in err.getvalue()
    assert (drafts / "first.md").is_file()
    assert (drafts / "second.md").is_file()
    assert not (repo / "docs" / "DESIGN.md").exists()


def test_assemble_rejects_packet_id_reused_by_two_documents(tmp_path):
    """A packet_id reused by a DESIGN and a USER_GUIDE draft exits 1 before merging citations."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "design.md",
        "pkt-shared",
        ["design-sec2"],
        "Design prose[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n",
    )
    (drafts / "guide.md").write_text(
        "---\n"
        "packet_id: pkt-shared\n"
        "target_doc: USER_GUIDE.md\n"
        "section_ids: [guide-sec2]\n"
        "---\n"
        "Guide prose[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
        encoding="utf-8",
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=err)
    assert code == 1
    assert "pkt-shared" in err.getvalue()
    assert (drafts / "design.md").is_file()
    assert (drafts / "guide.md").is_file()
    assert not (repo / "docs" / "DESIGN.md").exists()
    assert not (repo / "docs" / "USER_GUIDE.md").exists()


def test_assemble_rejects_undeclared_marker_naming_the_section(tmp_path):
    """The undeclared marker diagnostic names the offending section id."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "stray.md",
        "stray",
        ["design-sec2"],
        "<!-- SECTION:design-sec2:START -->\n"
        "Section two prose[^1].\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "<!-- SECTION:design-sec5:START -->\n"
        "Never patched prose[^1].\n"
        "<!-- SECTION:design-sec5:END -->\n\n"
        "[^1]: `src/app.py:1` — `alpha`\n",
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=err)
    assert code == 3
    assert "design-sec5" in err.getvalue()
    assert (drafts / "stray.md").is_file()


def test_assemble_accepts_single_section_draft_wrapped_in_its_markers(tmp_path):
    """A single-section draft may wrap its body in that section's markers."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts,
        "wrapped.md",
        "wrapped",
        ["design-sec2"],
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "Wrapped prose[^1].\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "[^1]: `src/app.py:1` — `alpha`\n",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO())
    assert code == 0
    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "Wrapped prose[^1]." in content
    assert content.count("<!-- SECTION:design-sec2:START -->") == 1
    assert "[^1]: `src/app.py:1` — `alpha`" in content


@pytest.mark.parametrize(
    "draft_body",
    [
        "# Just prose\n\nNo front-matter here.\n",
        "---\npacket_id: p1\ntarget_doc: DESIGN.md\n---\nSection ids are absent.\n",
    ],
)
def test_assemble_rejects_drafts_without_usable_section_metadata(tmp_path, draft_body):
    """Drafts lacking front-matter or section_ids exit 3, keep drafts, and write no docs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "metadata-less.md"
    draft.write_text(draft_body, encoding="utf-8")

    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO())
    assert code == 3
    assert draft.is_file()
    assert not (repo / "docs" / "DESIGN.md").exists()


def test_parse_draft_file_strips_inline_frontmatter_comments(tmp_path):
    """YAML inline comments are stripped from unquoted front-matter values."""
    draft_path = tmp_path / "packet.md"
    draft_path.write_text(
        "---\n"
        "packet_id: packet-7  # worker packet\n"
        "target_doc: DESIGN.md  # or USER_GUIDE.md\n"
        "section_ids:\n"
        "  - design-sec2  # components\n"
        "---\n"
        "Section body[^1].\n\n"
        "[^1]: `src/app.py:1` — `run`\n",
        encoding="utf-8",
    )

    packet = parse_draft_file(draft_path)
    assert packet.packet_id == "packet-7"
    assert packet.target_doc == "DESIGN.md"
    assert packet.section_ids == ["design-sec2"]


def test_parse_draft_file_rejects_unknown_target_doc(tmp_path):
    """Only the two canonical document names are accepted as target_doc."""
    draft_path = tmp_path / "packet.md"
    draft_path.write_text(
        "---\npacket_id: p1\ntarget_doc: NOTES.md\nsection_ids: [design-sec2]\n---\nBody\n",
        encoding="utf-8",
    )

    with pytest.raises(DraftFormatError):
        parse_draft_file(draft_path)


def test_patch_section_in_doc_preserves_canonical_heading():
    """Patching keeps one canonical heading however the draft spells its own heading."""
    doc = (
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "<!-- SECTION:design-sec2:END -->\n"
    )

    prose_only = patch_section_in_doc(doc, "design-sec2", [("pkt", "Component prose[^1].")])
    assert (
        "## 2. Components & Modules\n\n<!-- SLICE:pkt:START -->\nComponent prose[^1]." in prose_only
    )

    deeper_heading = patch_section_in_doc(
        doc, "design-sec2", [("pkt", "### How to Run It\n\nSteps[^1].")]
    )
    assert (
        "## 2. Components & Modules\n\n<!-- SLICE:pkt:START -->\n### How to Run It"
        in deeper_heading
    )

    same_level = patch_section_in_doc(
        doc, "design-sec2", [("pkt", "## 2. Components\n\nRenamed prose[^1].")]
    )
    assert same_level == (
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "<!-- SLICE:pkt:START -->\n"
        "Renamed prose[^1].\n"
        "<!-- SLICE:pkt:END -->\n"
        "<!-- SECTION:design-sec2:END -->\n"
    )


def test_assemble_keeps_skeleton_heading_when_draft_omits_it(tmp_path):
    """A prose-only draft must not delete the canonical heading from the generated doc."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "prose.md").write_text(
        "---\n"
        "packet_id: prose\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "Components prose without its own heading[^1].\n\n"
        "[^1]: `src/app.py:1` — `run`\n",
        encoding="utf-8",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO())
    assert code == 0

    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert (
        "## 2. Components & Modules\n\n<!-- SLICE:prose:START -->\n"
        "Components prose without its own heading[^1].\n<!-- SLICE:prose:END -->"
    ) in content


def test_assemble_undefined_draft_reference_cannot_bind_existing_citation(tmp_path):
    """An undefined draft footnote must fail the dangling gate, not reuse an old citation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def common():\n    pass\n\ndef specific():\n    pass\n", encoding="utf-8"
    )

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "first.md").write_text(
        "---\n"
        "packet_id: first\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "Section two calls common[^1].\n\n"
        "[^1]: `src/app.py:1` — `common`\n",
        encoding="utf-8",
    )
    assert assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO()) == 0

    drafts.mkdir(parents=True)
    orphan = drafts / "second.md"
    orphan.write_text(
        "---\n"
        "packet_id: second\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec3]\n"
        "---\n"
        "Section three calls something undefined[^1].\n",
        encoding="utf-8",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO())
    assert code == 3
    assert orphan.is_file()


def test_assemble_deletes_only_consumed_draft_files(tmp_path):
    """Assembly removes the draft files it parsed and leaves unrelated files in place."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "packet.md"
    draft.write_text(
        "---\n"
        "packet_id: packet\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec2]\n"
        "---\n"
        "Components text[^1].\n\n"
        "[^1]: `src/app.py:1` — `run`\n",
        encoding="utf-8",
    )
    notes = drafts / "worker-notes.txt"
    notes.write_text("scratch notes\n", encoding="utf-8")

    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO())
    assert code == 0
    assert not draft.exists()
    assert notes.is_file()
    assert drafts.is_dir()


@pytest.mark.parametrize(
    ("section_id", "body", "reason"),
    [
        pytest.param(
            "design-sec1",
            "Prose beside a slot[^1].\n\n"
            "<!-- AUTO-DIAGRAM:module-overview:START -->\n"
            "<!-- AUTO-DIAGRAM:module-overview:END -->\n\n"
            "[^1]: `src/app.py:1` — `alpha`\n",
            "AUTO-DIAGRAM",
            id="module-overview-slot",
        ),
        pytest.param(
            "design-sec3",
            "Prose beside a slot[^1].\n\n"
            "<!-- AUTO-DIAGRAM:flow-overview:START -->\n"
            "<!-- AUTO-DIAGRAM:flow-overview:END -->\n\n"
            "[^1]: `src/app.py:4` — `beta`\n",
            "AUTO-DIAGRAM",
            id="flow-overview-slot",
        ),
        pytest.param(
            "design-sec3",
            "Prose with a hand-written diagram[^1].\n\n"
            "```mermaid\nflowchart TD\n  A --> B\n```\n\n"
            "[^1]: `src/app.py:4` — `beta`\n",
            "Mermaid",
            id="mermaid-block",
        ),
    ],
)
def test_assemble_rejects_drafts_that_author_diagrams(tmp_path, section_id, body, reason):
    """Diagram slots and Mermaid blocks belong to the document, never to a draft."""
    repo, drafts = _repo_with_app(tmp_path)
    draft = drafts / "diagram.md"
    _write_design_draft(drafts, draft.name, "diagram", [section_id], body)

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=err)
    assert code == 3
    assert str(draft) in err.getvalue()
    assert reason in err.getvalue()
    assert draft.is_file()
    assert not (repo / "docs" / "DESIGN.md").exists()


def test_assemble_preserves_diagram_slots_across_redraft_and_slice_removal(tmp_path):
    """Document-owned slots survive a slice re-draft and a later --remove-slice."""
    repo, drafts = _repo_with_app(tmp_path)
    diagrams_file = tmp_path / "diagrams.json"
    diagrams_file.write_text(
        json.dumps({"flow-overview": "flowchart TD\n  A --> B"}), encoding="utf-8"
    )
    _write_design_draft(
        drafts,
        "flow.md",
        "p3",
        ["design-sec3"],
        "Flow prose v1[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )
    assert (
        assemble(
            repo=repo,
            drafts_dir="docs/.drafts",
            diagrams=diagrams_file,
            out=io.StringIO(),
            err=io.StringIO(),
        )
        == 0
    )
    first = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert first.count("<!-- AUTO-DIAGRAM:flow-overview:START -->") == 1
    assert "A --> B" in first

    drafts.mkdir(parents=True)
    _write_design_draft(
        drafts,
        "flow.md",
        "p3",
        ["design-sec3"],
        "Flow prose v2[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )
    assert (
        assemble(
            repo=repo,
            drafts_dir="docs/.drafts",
            diagrams=diagrams_file,
            out=io.StringIO(),
            err=io.StringIO(),
        )
        == 0
    )
    second = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert second.count("<!-- AUTO-DIAGRAM:flow-overview:START -->") == 1
    assert "A --> B" in second
    assert "Flow prose v2" in second
    assert "Flow prose v1" not in second

    assert (
        assemble(
            repo=repo,
            drafts_dir="docs/.drafts",
            diagrams=diagrams_file,
            remove_slices=["p3"],
            out=io.StringIO(),
            err=io.StringIO(),
        )
        == 0
    )
    third = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert third.count("<!-- AUTO-DIAGRAM:flow-overview:START -->") == 1
    assert "A --> B" in third
    assert "Flow prose v2" not in third


def test_assemble_preserves_auto_diagram_slots_in_patched_sections(tmp_path):
    """Prose drafts must not delete the AUTO-DIAGRAM slots owned by the document."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def overview():\n    pass\n\ndef flow():\n    pass\n", encoding="utf-8"
    )

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "flow.md").write_text(
        "---\n"
        "packet_id: flow\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec1, design-sec3]\n"
        "---\n"
        "<!-- SECTION:design-sec1:START -->\n"
        "The system is one module[^1].\n"
        "<!-- SECTION:design-sec1:END -->\n\n"
        "<!-- SECTION:design-sec3:START -->\n"
        "Control flows from the entrypoint to the worker[^2].\n"
        "<!-- SECTION:design-sec3:END -->\n\n"
        "[^1]: `src/app.py:1` — `overview`\n"
        "[^2]: `src/app.py:4` — `flow`\n",
        encoding="utf-8",
    )

    diagrams_file = tmp_path / "diagrams.json"
    diagrams_file.write_text(
        json.dumps({"flow-overview": "flowchart TD\n  Entry --> Worker"}), encoding="utf-8"
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts", diagrams=diagrams_file)
    assert code == 0

    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "The system is one module[^1]." in content
    assert "Control flows from the entrypoint to the worker[^2]." in content
    assert "Entry --> Worker" in content
    assert "Module Overview" in content


def _write_design_draft(drafts, name, packet_id, sec_ids, body):
    sec_list = ", ".join(sec_ids)
    (drafts / name).write_text(
        f"---\npacket_id: {packet_id}\ntarget_doc: DESIGN.md\nsection_ids: [{sec_list}]\n---\n"
        + body,
        encoding="utf-8",
    )


def _repo_with_app(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(
        "def alpha():\n    pass\n\ndef beta():\n    pass\n", encoding="utf-8"
    )
    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    return repo, drafts


def test_inject_diagrams_matches_slot_ids_exactly():
    """Only exact slot ids match; alias spellings such as 'diagram:<id>' are ignored."""
    doc = (
        "<!-- AUTO-DIAGRAM:module-overview:START -->\nold\n"
        "<!-- AUTO-DIAGRAM:module-overview:END -->\n"
    )
    assert inject_diagrams(doc, {"diagram:module-overview": "flowchart LR\n  A --> B"}) == doc
    injected = inject_diagrams(doc, {"module-overview": "flowchart LR\n  A --> B"})
    assert "A --> B" in injected


def test_assemble_relative_paths_resolve_against_repo_not_cwd(tmp_path, monkeypatch):
    """Relative --drafts-dir and --diagrams resolve against <repo>, never the CWD."""
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts, "d.md", "d", ["design-sec2"], "Sec 2[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n"
    )
    (repo / "diagrams.json").write_text(
        json.dumps({"module-overview": "flowchart LR\n  InRepo --> Diagram"}), encoding="utf-8"
    )

    # A decoy CWD with same-named paths must never be used
    cwd = tmp_path / "elsewhere"
    (cwd / "docs" / ".drafts").mkdir(parents=True)
    (cwd / "diagrams.json").write_text(
        json.dumps({"module-overview": "flowchart LR\n  Cwd --> Decoy"}), encoding="utf-8"
    )
    monkeypatch.chdir(cwd)

    code = assemble(
        repo=repo,
        drafts_dir="docs/.drafts",
        diagrams="diagrams.json",
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert code == 0
    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "InRepo --> Diagram" in content
    assert "Cwd --> Decoy" not in content
    assert not drafts.exists()
    assert (cwd / "docs" / ".drafts").is_dir()


def test_assemble_relative_drafts_dir_missing_in_repo_does_not_fall_back_to_cwd(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    cwd = tmp_path / "elsewhere"
    cwd_drafts = cwd / "drafts"
    cwd_drafts.mkdir(parents=True)
    _write_design_draft(cwd_drafts, "d.md", "d", ["design-sec2"], "Prose.\n")
    monkeypatch.chdir(cwd)

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="drafts", out=io.StringIO(), err=err)
    assert code == 1
    assert (cwd_drafts / "d.md").is_file()
    assert not (repo / "docs").exists()


def test_assemble_relative_diagrams_missing_in_repo_does_not_fall_back_to_cwd(
    tmp_path, monkeypatch
):
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts, "d.md", "d", ["design-sec2"], "Sec 2[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n"
    )
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "diagrams.json").write_text(json.dumps({"module-overview": "x"}), encoding="utf-8")
    monkeypatch.chdir(cwd)

    code = assemble(
        repo=repo,
        drafts_dir="docs/.drafts",
        diagrams="diagrams.json",
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert code == 1
    assert (drafts / "d.md").is_file()


@pytest.mark.parametrize("outside", ["absolute", "dotdot"])
def test_assemble_refuses_drafts_dir_outside_repo(tmp_path, outside):
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external-drafts"
    external.mkdir()
    _write_design_draft(external, "d.md", "d", ["design-sec2"], "Prose.\n")

    drafts_arg = str(external) if outside == "absolute" else "../external-drafts"
    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir=drafts_arg, out=io.StringIO(), err=err)
    assert code == 1
    assert "outside the target repository" in err.getvalue()
    assert (external / "d.md").is_file()
    assert not (repo / "docs").exists()


def test_assemble_concatenates_drafts_targeting_same_section(tmp_path):
    """Drafts sharing a section are combined in packet_id order with footnotes renumbered."""
    repo, drafts = _repo_with_app(tmp_path)
    # File names sort opposite to packet ids to prove ordering is by packet_id
    _write_design_draft(
        drafts,
        "a-file.md",
        "pkt-b",
        ["design-sec2"],
        "## 2. Components & Modules\n\nBeta module text[^1].\n\n[^1]: `src/app.py:4` — `beta`\n",
    )
    _write_design_draft(
        drafts,
        "z-file.md",
        "pkt-a",
        ["design-sec2"],
        "## 2. Components & Modules\n\nAlpha module text[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n",
    )

    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=io.StringIO())
    assert code == 0
    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "Alpha module text[^1]." in content
    assert "Beta module text[^2]." in content
    assert content.index("Alpha module text") < content.index("Beta module text")
    assert content.count("## 2. Components & Modules") == 1
    assert "[^1]: `src/app.py:1` — `alpha`" in content
    assert "[^2]: `src/app.py:4` — `beta`" in content


def test_assemble_unknown_section_exits_3_keeps_drafts_and_writes_nothing(tmp_path):
    repo, drafts = _repo_with_app(tmp_path)
    _write_design_draft(
        drafts, "good.md", "good", ["design-sec2"], "Sec 2[^1].\n\n[^1]: `src/app.py:1` — `alpha`\n"
    )
    _write_design_draft(
        drafts, "bad.md", "bad", ["design-sec99"], "Nowhere[^1].\n\n[^1]: `src/app.py:4` — `beta`\n"
    )

    err = io.StringIO()
    code = assemble(repo=repo, drafts_dir="docs/.drafts", out=io.StringIO(), err=err)
    assert code == 3
    assert "design-sec99" in err.getvalue()
    assert (drafts / "good.md").is_file()
    assert (drafts / "bad.md").is_file()
    assert not (repo / "docs" / "DESIGN.md").exists()
