"""Single-file and v2 bundle static HTML viewer renderer for architecture graphs."""

from __future__ import annotations

import importlib.resources
import json
from typing import Any


def safe_json_embed(data: Any, *, compact: bool = False) -> str:
    """Serialize data to JSON, safely escaping HTML and JS line-terminator characters.

    Replaces '<', '>', '&', U+2028, and U+2029 with their JSON escapes so labels
    containing '</script>' or Unicode line separators cannot break inline script
    tags or single-line JSONP chunks while JSON.parse recovers the exact text.
    """
    kwargs: dict[str, Any] = {"sort_keys": True, "ensure_ascii": False}
    if compact:
        kwargs["separators"] = (",", ":")
    raw = json.dumps(data, **kwargs)
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_viewer(graph: dict[str, Any]) -> str:
    """Render a standalone, single-file HTML architecture flow viewer (v1)."""
    assets = importlib.resources.files("arch_indexer.viewer.assets")

    template = assets.joinpath("template.html").read_text(encoding="utf-8")
    css = assets.joinpath("viewer.css").read_text(encoding="utf-8")
    logic_js = assets.joinpath("viewer_logic.js").read_text(encoding="utf-8")
    viewer_js = assets.joinpath("viewer.js").read_text(encoding="utf-8")

    embedded_json = safe_json_embed(graph)

    html = template.replace("/* {{VIEWER_CSS}} */", css)
    html = html.replace("/* {{VIEWER_LOGIC_JS}} */", logic_js)
    html = html.replace("/* {{VIEWER_JS}} */", viewer_js)
    html = html.replace("{{GRAPH_DATA}}", embedded_json)

    return html


def has_viewer_v2_dist() -> bool:
    """Return True when the pre-built React Flow + ELKjs viewer dist assets exist."""
    try:
        dist = importlib.resources.files("arch_indexer.viewer.dist")
        return (
            dist.joinpath("index.html").is_file()
            and dist.joinpath("app.js").is_file()
            and dist.joinpath("app.css").is_file()
        )
    except (FileNotFoundError, ModuleNotFoundError):
        return False


def _license_banner() -> str:
    """Build an HTML comment banner embedding upstream license texts from dist/licenses/."""
    try:
        licenses_dir = importlib.resources.files("arch_indexer.viewer.dist").joinpath("licenses")
        entries = sorted(
            (p for p in licenses_dir.iterdir() if p.is_file() and p.name.endswith(".txt")),
            key=lambda p: p.name,
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return ""
    if not entries:
        return ""

    parts = ["<!--\nTHIRD-PARTY VIEWER LICENSES (unmodified bundled components):\n"]
    for entry in entries:
        text = entry.read_text(encoding="utf-8", errors="replace").replace("-->", "-- >")
        parts.append(f"=== {entry.name} ===\n{text.rstrip()}\n")
    parts.append("-->")
    return "\n".join(parts)


def render_viewer_v2(overview: dict[str, Any]) -> str:
    """Render the v2 React Flow + ELKjs overview HTML from pre-built ``viewer/dist/`` assets."""
    dist = importlib.resources.files("arch_indexer.viewer.dist")
    template = dist.joinpath("index.html").read_text(encoding="utf-8")
    app_js = dist.joinpath("app.js").read_text(encoding="utf-8")
    app_css = dist.joinpath("app.css").read_text(encoding="utf-8")

    embedded_overview = safe_json_embed(overview, compact=True)
    sidecar_scripts = (
        '<script type="text/plain" src="arch/vendor/elk-worker.min.js"></script>\n'
        '  <script src="arch/descriptions.js"></script>\n'
        '  <script src="arch/runtime-overlay.v1.js"></script>\n'
        '  <script src="arch/dataflow.v1.js"></script>'
    )

    html = template.replace(
        '<link rel="stylesheet" href="./app.css">',
        f"<style>\n{app_css}\n</style>",
    )
    html = html.replace(
        '<script id="arch-overview-data" type="application/json">{}</script>',
        f'<script id="arch-overview-data" type="application/json">{embedded_overview}</script>',
    )
    html = html.replace(
        '<script defer src="./app.js"></script>',
        f"{sidecar_scripts}\n  <script>\n{app_js}\n</script>",
    )

    banner = _license_banner()
    if banner:
        html = html.replace("<!DOCTYPE html>", f"<!DOCTYPE html>\n{banner}", 1)
    return html
