"""Single-file static HTML viewer renderer for architecture graphs."""

from __future__ import annotations

import importlib.resources
import json
from typing import Any


def safe_json_embed(data: dict[str, Any]) -> str:
    """Serialize data to JSON, safely escaping HTML characters so labels cannot break script tags.

    Standard Unicode escaping replaces '<', '>', and '&' with their '\\u00xx' sequences.
    This guarantees that strings like '</script>' cannot be interpreted as HTML tags by
    the browser, while JSON.parse faithfully recovers the exact original text.
    """
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_viewer(graph: dict[str, Any]) -> str:
    """Render a standalone, single-file HTML architecture flow viewer.

    Reads the HTML template, CSS, and vanilla JS assets via importlib.resources,
    embeds the safely-escaped graph JSON into an application/json script block,
    and returns a complete HTML document that works fully offline over file://.
    """
    assets = importlib.resources.files("arch_indexer.viewer.assets")

    template = assets.joinpath("template.html").read_text(encoding="utf-8")
    css = assets.joinpath("viewer.css").read_text(encoding="utf-8")
    logic_js = assets.joinpath("viewer_logic.js").read_text(encoding="utf-8")
    viewer_js = assets.joinpath("viewer.js").read_text(encoding="utf-8")

    embedded_json = safe_json_embed(graph)

    # Inlining package data into the standalone template
    html = template.replace("/* {{VIEWER_CSS}} */", css)
    html = html.replace("/* {{VIEWER_LOGIC_JS}} */", logic_js)
    html = html.replace("/* {{VIEWER_JS}} */", viewer_js)
    html = html.replace("{{GRAPH_DATA}}", embedded_json)

    return html
