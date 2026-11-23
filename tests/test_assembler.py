from __future__ import annotations

import io
import json

from arch_indexer.assembler import (
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
    patched = patch_section_in_doc(doc, "sec1", "New Sec 1 Content")
    assert "New Sec 1 Content" in patched
    assert "Old Sec 2" in patched
    assert "Old Sec 1" not in patched


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

    # Second run: patch ONLY design-sec2
    drafts.mkdir(parents=True)
    (drafts / "packet-patch-sec2.md").write_text(
        "---\n"
        "packet_id: patch-sec2\n"
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
