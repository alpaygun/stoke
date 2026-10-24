"""Static panel contracts that need no browser or running server."""

from html.parser import HTMLParser
from pathlib import Path
import re

import pytest


PAGE = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
SCHEMES = re.compile(r':root\[data-theme="(dark|light)"\]\s*\{([^{}]*)\}')
COLOUR = re.compile(
    r"(?<![\w-])\#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3})(?![\w-])"
    r"|\b(?:rgba?|hsla?)\s*\(", re.IGNORECASE,
)


def colour_literals(source: str) -> list[str]:
    # Whole-token boundaries exclude IDs such as #app and #plan-dialog. CSS
    # selectors, URL fragments and selector API arguments are references, not
    # colour values, even when their IDs happen to contain only hex digits.
    source = re.sub(r"url\([^)]*\)|https?://[^\s\"'<>]+", "", source)
    source = re.sub(r"\b(?:href|src)\s*=\s*(['\"])[^'\"]*#[^'\"]*\1", "", source)
    source = re.sub(r"\bquerySelector(?:All)?\(\s*(['\"]).*?\1\s*\)", "", source)

    def declarations(match: re.Match) -> str:
        return re.sub(r"[^{}]+\{", "{", match[0])
    source = re.sub(r"<style\b[^>]*>.*?</style>", declarations, source, flags=re.DOTALL)
    return COLOUR.findall(source)


def test_colours_only_in_scheme_blocks() -> None:
    blocks = SCHEMES.findall(PAGE)
    assert sorted(mode for mode, _ in blocks) == ["dark", "light"]
    for mode, body in blocks:
        assert re.search(rf"\bcolor-scheme:\s*{mode}\s*;", body)
    assert not colour_literals(SCHEMES.sub("", PAGE))


@pytest.mark.parametrize("literal", [
    "#abc", "#abcd", "#abcdef", "#abcdef12", "rgb(1,2,3)",
    "rgba(1,2,3,.5)", "hsl(0,0%,0%)", "hsla(0,0%,0%,.5)",
])
def test_colour_detector_recognises_values(literal: str) -> None:
    assert colour_literals(f"<style>.sample {{ color: {literal}; }}</style>")
    assert colour_literals(f"const colour = '{literal}';")


def test_colour_detector_ignores_references() -> None:
    assert not colour_literals('''<style>#abc, #app, #plan-dialog { color: var(--text); }
        .sample { background: url(sprite.svg#abcd); }</style>
        document.querySelector('#abcdef'); <a href="guide#abcd">Guide</a>''')


def test_scheme_custom_properties_match() -> None:
    blocks = dict(SCHEMES.findall(PAGE))
    properties = lambda body: set(re.findall(r"(--[\w-]+)\s*:", body))
    assert properties(blocks["dark"])
    assert properties(blocks["dark"]) == properties(blocks["light"])


class HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_head = False
        self.inline_script = False
        self.scripts: list[str] = []
        self.theme_colour = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "head":
            self.in_head = True
        if self.in_head and tag == "meta" and attributes.get("name") == "theme-color":
            self.theme_colour = True
        if self.in_head and tag == "script" and "src" not in attributes:
            self.inline_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False
        if tag == "script":
            self.inline_script = False

    def handle_data(self, data: str) -> None:
        if self.inline_script:
            self.scripts.append(data)


def test_theme_initialises_in_head() -> None:
    head = HeadParser()
    head.feed(PAGE)
    assert head.theme_colour
    assert any(re.search(r"setAttribute\(['\"]data-theme['\"]\s*,", script) for script in head.scripts)


def test_no_html_injection_or_text_icons() -> None:
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML", PAGE)
    assert not any(glyph in PAGE for glyph in "↻✎⚠↺×")


class ElementParser(HTMLParser):
    """A small tree for checking static dialog structure without a browser."""

    def __init__(self) -> None:
        super().__init__()
        self.root = {"tag": "root", "attrs": {}, "children": [], "text": ""}
        self.stack = [self.root]
        self.by_id = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {"tag": tag, "attrs": dict(attrs), "children": [], "text": ""}
        self.stack[-1]["children"].append(node)
        if "id" in node["attrs"]:
            assert node["attrs"]["id"] not in self.by_id
            self.by_id[node["attrs"]["id"]] = node
        if tag not in {"meta", "link", "input", "br", "hr", "img"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack[-1]["tag"] == tag
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1]["text"] += data


def test_dialog_layout_and_minutes_input() -> None:
    page = ElementParser()
    page.feed(PAGE)
    assert "ar-unit" not in page.by_id
    assert page.by_id["ar-value"]["attrs"] == {
        "id": "ar-value", "class": "field num", "type": "text",
        "inputmode": "numeric", "maxlength": "2", "aria-label": "Refresh interval",
    }
    assert page.by_id["weekly-page"]["attrs"]["inputmode"] == "numeric"
    footers = {"plan": ["plan-remove", "plan-save"], "settings": ["settings-save"],
               "reset": ["reset-save"], "weekly": ["weekly-prev", "weekly-next"]}
    for name in ("plan", "offset", "settings", "info", "reset", "weekly"):
        dialog = page.by_id[f"{name}-dialog"]
        assert dialog["tag"] == "dialog"
        form, = dialog["children"]
        assert form["attrs"]["class"] == "plan-form"
        head, body, *footer = form["children"]
        assert head["attrs"]["class"] == "dialog-head"
        assert body["attrs"]["class"] == "dialog-body"
        title, close = head["children"]
        assert title["tag"] == "h2"
        assert close["attrs"] == {"id": f"{name}-close", "type": "button",
                                  "class": "dialog-x", "aria-label": "Close"}
        assert not close["text"].strip()
        if name in footers:
            actions, = footer
            assert "dialog-actions" in actions["attrs"]["class"].split()
            buttons = [child for child in actions["children"] if child["tag"] == "button"]
            assert [button["attrs"]["id"] for button in buttons] == footers[name]
            assert all(button["text"].strip() != "Close" for button in buttons)
        else:
            assert not footer


def test_settings_controls_and_no_card_menu() -> None:
    page = ElementParser()
    page.feed(PAGE)
    form, = page.by_id["settings-dialog"]["children"]
    _, body, _ = form["children"]

    def descendants(node: dict) -> list[dict]:
        return [child for child in node["children"]] + [
            item for child in node["children"] for item in descendants(child)
        ]

    controls = {node["attrs"].get("id"): node for node in descendants(body)}
    assert controls["auto-enabled"]["attrs"] == {
        "id": "auto-enabled", "type": "checkbox", "class": "switch",
        "aria-label": "Auto-stoke",
    }
    assert controls["ar-enabled"]["attrs"]["aria-label"] == "Read usage automatically"
    refresh_controls = {node["attrs"].get("id") for node in descendants(controls["ar-box"])}
    assert {"ar-enabled", "ar-value", "ar-minus", "ar-plus"} <= refresh_controls
    for control, label in (("ar-minus", "Fewer minutes"), ("ar-plus", "More minutes")):
        assert controls[control]["tag"] == "button"
        assert controls[control]["attrs"]["type"] == "button"
        assert controls[control]["attrs"]["aria-label"] == label
    assert "settings-caution" in controls
    assert "settings-message" in controls
    assert "ar-hint" not in page.by_id
    assert not re.search(r"more-menu|menu-pop|menu-separator|moreMenu|autoControl", PAGE)
