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
               "weekly": ["weekly-prev", "weekly-next"]}
    for name in ("plan", "offset", "settings", "info", "weekly"):
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


def test_early_reset_settings_stepper_and_save_order() -> None:
    page = ElementParser()
    page.feed(PAGE)
    form, = page.by_id["settings-dialog"]["children"]
    _, body, _ = form["children"]
    boxes = body["children"]
    assert boxes.index(page.by_id["ss-box"]) == boxes.index(page.by_id["ar-box"]) + 1
    assert boxes.index(page.by_id["rm-box"]) == boxes.index(page.by_id["ss-box"]) + 1
    assert boxes.index(page.by_id["rh-box"]) == boxes.index(page.by_id["rm-box"]) + 1
    box = page.by_id["rh-box"]
    assert box["attrs"]["class"] == "settings-box"
    row, sub = box["children"]
    assert row["attrs"]["class"] == "settings-row"
    _, copy = row["children"]
    title, subtitle = copy["children"]
    assert title["text"] == "After an early reset"
    assert subtitle["text"] == "Keep the offset if the old window ends within this many minutes; otherwise stoke at once so the window keeps running. 0 always stokes at once."
    assert sub["attrs"]["class"] == "settings-row settings-sub"
    _, _, stepper = sub["children"]
    assert stepper["attrs"]["class"] == "settings-stepper"
    assert [node["attrs"].get("id") for node in stepper["children"]] == ["rh-minus", "rh-value", None, "rh-plus"]
    assert page.by_id["rh-value"]["attrs"] == {
        "id": "rh-value", "class": "field num", "type": "text", "inputmode": "numeric",
        "maxlength": "3", "aria-label": "Early reset hold threshold",
    }
    for control, kind in [("rh-minus", "minus"), ("rh-plus", "plus")]:
        assert page.by_id[control]["attrs"]["type"] == "button"
        assert page.by_id[control]["attrs"]["data-settings-icon"] == kind
    assert "rhBox.hidden = !(tool.refreshable && tool.reset_closes_window);" in PAGE
    auto = PAGE.index("if (autoChanged) await post(name, 'auto', { enabled });")
    refresh = PAGE.index("if (arChanged) await post(name, 'auto-refresh', body);")
    scoped = PAGE.index("if (ssChanged) await post(name, 'show-scoped', { enabled: ssEnabled.checked });")
    hold = PAGE.index("if (rhChanged) await post(name, 'reset-hold', { minutes });")
    mode = PAGE.index("if (rmChanged) await post(name, 'read-mode', { mode });")
    assert auto < refresh < scoped < mode < hold


def test_reset_provider_capability_controls_settings_visibility_and_save() -> None:
    helper = re.search(r"function openSettings\(name\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert helper
    assert "arBox.hidden = !tool.refreshable;" in helper[1]
    assert "rhBox.hidden = !(tool.refreshable && tool.reset_closes_window);" in helper[1]
    assert "const rhChanged = tool.refreshable && tool.reset_closes_window && minutes !== tool.reset_hold_minutes;" in PAGE


def test_early_reset_settings_rejects_invalid_threshold() -> None:
    helper = re.search(r"function syncSettings\(\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert helper
    code = helper[1]
    assert "const rhBad = !rhBox.hidden && (!/^\\d{1,3}$/.test(rhValue.value) || Number(rhValue.value) > 300);" in code
    assert "const bad = arBad || rhBad;" in code
    assert "settingsSave.disabled = settingsSaving || bad;" in code
    assert "rhBad ? 'Enter 0 to 300 minutes.'" in code


def test_reset_rows_separate_expiry_and_remaining_time() -> None:
    assert "added by you" not in PAGE
    assert "reset-source" not in PAGE
    assert "el('span', '', `expires ${when(credit.expires_at, current)}`)" in PAGE
    assert "el('span', 'reset-left', `in ${span(left)}`)" in PAGE


def test_vendor_reset_tile_status_cooldown_and_footer() -> None:
    assert not re.search(r"manual_resets|manualResets|resets\.manual|resets/(update|remove)|openReset|reset-dialog|reset-blocked|reset-actions|reset-tools|reset-note|if \(false\)", PAGE)
    assert "if (tool.refreshable) stats.append(resetTile(tool));" in PAGE
    assert "resets: tool.resets" in PAGE
    assert "if (credit.usable === false)" in PAGE
    assert "item.classList.add('blocked');" in PAGE
    name = PAGE.index("item.append(el('span', 'reset-name', credit.title));")
    chip = PAGE.index("item.append(el('span', 'chip small', 'Not usable'));")
    expiry = PAGE.index("item.append(expiry);")
    assert name < chip < expiry
    assert ".reset-item.blocked .reset-name { color: var(--text-2); }" in PAGE
    assert "resets.cooldown_until > current" in PAGE
    assert "`cooldown until ${clock(resets.cooldown_until)}`" in PAGE
    assert "Cooldown until ${when(resets.cooldown_until, current)}: no reset can be used before then." in PAGE
    assert "note.append(icon('clock')" in PAGE
    assert "'reset-footer'" in PAGE
    assert re.search(r"\.reset-footer\s*\{[^}]*border-top: 1px solid var\(--line\);[^}]*padding: 10px 12px 0", PAGE)
    assert "`As of ${readTime(resets.read_at, current)}` : 'Not read yet.'" in PAGE
    assert "Use them in the ${tool.label} app or CLI. Stoke never spends one." in PAGE
    assert "Refresh usage to read available resets. Stoke never spends one." in PAGE


def test_scoped_limits_and_plan_heading_without_product_shares() -> None:
    assert "scoped: tool.scoped_limits ?? []" in PAGE
    assert "show_scoped: tool.show_scoped ?? true" in PAGE
    assert "planName: tool.plan_name ?? null" in PAGE
    assert "if (weekly && tool.show_scoped !== false) tool.scoped.forEach(limit =>" in PAGE
    scoped = PAGE.index("`Weekly · ${limit.label}`")
    weekly_end = PAGE.index("if (weekly) group.append(text('span', 'weekly-reset'")
    assert scoped < weekly_end
    assert "Math.abs(limit.resets_at - tool.weeklyAt) > 3600" in PAGE
    assert "`resets ${when(limit.resets_at, now())}`" in PAGE
    history = PAGE.index("'Weekly resets'", PAGE.index("function infoContent"))
    signin = PAGE.index("'Sign-in'", history)
    assert history < signin
    assert "Weekly use by product" not in PAGE
    assert "breakdown" not in PAGE
    assert "if (tool.planName) h.append(el('span', 'plan-name', `${tool.planName} plan`));" in PAGE
    assert re.search(r"\.signin-h \.plan-name\s*\{[^}]*text-transform: none", PAGE)


def test_scoped_limit_settings_switch_visibility_label_and_save() -> None:
    page = ElementParser()
    page.feed(PAGE)
    box = page.by_id["ss-box"]
    assert box["attrs"]["class"] == "settings-box"
    row, = box["children"]
    assert row["attrs"]["class"] == "settings-row"
    tile, copy, switch = row["children"]
    assert tile["tag"] == "span"
    assert tile["attrs"] == {"class": "settings-tile", "data-settings-icon": "info"}
    assert copy["attrs"]["class"] == "settings-copy"
    title, subtitle = copy["children"]
    assert title is page.by_id["ss-title"] and title["tag"] == "strong"
    assert subtitle["text"] == "Shows the model's own weekly limit on the card"
    assert switch["attrs"] == {"id": "ss-enabled", "type": "checkbox", "class": "switch"}
    helper = re.search(r"function openSettings\(name\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert helper
    assert "ssBox.hidden = !(tool.refreshable && tool.scoped_limits?.length);" in helper[1]
    assert "const ssTitle = `${tool.scoped_limits?.[0]?.label ?? ''} weekly limit`;" in helper[1]
    assert "document.querySelector('#ss-title').textContent = ssTitle;" in helper[1]
    assert "ssEnabled.setAttribute('aria-label', ssTitle);" in helper[1]
    assert "ssEnabled.checked = tool.show_scoped ?? true;" in helper[1]
    sync = re.search(r"function syncSettings\(\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert sync and "ssEnabled.disabled = settingsSaving || ssBox.hidden;" in sync[1]
    assert "const ssChanged = !ssBox.hidden && ssEnabled.checked !== (tool.show_scoped ?? true);" in PAGE


def test_login_expiry_chips_and_warning() -> None:
    helper = re.search(r"function signinState\(tool, current = now\(\)\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert helper
    assert "left < 30 * 86400" in helper[1] and "left < 3 * 86400" in helper[1]
    assert "'warning'" not in helper[1]
    assert "? 'cross' : null" in helper[1]
    assert "soon && level === 'ok' ? 'warn' : level" in helper[1]
    assert "`${span(left)} left`" in helper[1]
    assert "chipLine(line, row.login.chip)" in PAGE
    assert "grid-template-columns: minmax(0, 1fr) auto" in PAGE
    assert "['read_login_expiring', ['Usage login expires soon', '']]" in PAGE
    assert "left <= 0 ? 'Usage login expired' : 'Usage login expires soon'" in PAGE
    assert "The account login that reads ${tool.label} usage" in PAGE
    assert "has expired; usage and reset credits are no longer read." in PAGE
    assert "['read_signed_out', 'read_login_expiring'].includes(tool.warningKey)" in PAGE
    assert "caution: ['token_expiring', 'read_login_expiring'].includes(tool.warning)" in PAGE


def test_read_method_settings_and_refresh_interval_bounds() -> None:
    assert "rmBox.hidden = !tool.refreshable;" in PAGE
    assert "r.value === (tool.read_mode ?? 'direct')" in PAGE
    assert "r.disabled = settingsSaving || rmBox.hidden" in PAGE
    assert "const rmChanged = tool.refreshable && mode !== tool.read_mode;" in PAGE
    assert "!autoChanged && !arChanged && !ssChanged && !rmChanged && !rhChanged" in PAGE
    for hint in ("Asks the usage endpoint directly; falls back to the CLI if needed",
                 "Asks the official CLI only. Reset credits are not available", "Asks the official CLI only"):
        assert hint in PAGE
    assert "seconds < 600 || seconds > 3600" in PAGE
    assert "arMinus.disabled = disabled || Number(arValue.value) <= 10;" in PAGE
    assert "Math.max(10, Math.round(ar.seconds / 60))" in PAGE
    assert "Math.max(10, (direction > 0 ? Math.floor(minutes / 5) + 1 : Math.ceil(minutes / 5) - 1) * 5)" in PAGE
    assert "Enter 10 to 60 minutes." in PAGE
    assert "Anthropic limits how often usage can be read: Claude usage is read at most once every 10 minutes." in PAGE


def test_plan_and_read_settings_cards_and_segmented_choices() -> None:
    page = ElementParser()
    page.feed(PAGE)

    def descendants(node: dict) -> list[dict]:
        return [child for child in node["children"]] + [
            item for child in node["children"] for item in descendants(child)
        ]

    groups = {}
    for name, count in (("plan", 3), ("settings", 5)):
        form, = page.by_id[f"{name}-dialog"]["children"]
        _, body, _ = form["children"]
        boxes = [node for node in body["children"] if node["attrs"].get("class") == "settings-box"]
        assert len(boxes) == count
        assert not any(node["tag"] in {"fieldset", "legend"} for node in descendants(body))
        for node in descendants(body):
            if "segmented" not in node["attrs"].get("class", "").split():
                continue
            assert node["tag"] == "div"
            radios = []
            for label in node["children"]:
                assert label["tag"] == "label"
                radio, = label["children"]
                assert radio["tag"] == "input"
                assert radio["attrs"]["type"] == "radio"
                assert label["text"].strip()
                radios.append(radio["attrs"])
            group, = {radio["name"] for radio in radios}
            groups[group] = radios
    plan_form, = page.by_id["plan-dialog"]["children"]
    _, plan_body, _ = plan_form["children"]
    for control, classes in (("plan-hint", {"settings-caution"}),
                             ("plan-block", {"settings-caution", "danger"})):
        caution, = [node for node in plan_body["children"] if page.by_id[control] in node["children"]]
        assert set(caution["attrs"]["class"].split()) == classes
    assert set(groups) == {"repeat", "auto_after", "read_mode"}
    for name, values in (("repeat", ["once", "daily"]),
                         ("auto_after", ["keep", "on", "off"]),
                         ("read_mode", ["direct", "cli"])):
        assert [radio["value"] for radio in groups[name]] == values
        assert [radio["value"] for radio in groups[name] if "checked" in radio] == values[:1]
    assert page.by_id["plan-block"]["attrs"]["role"] == "status"
    assert "hidden" in page.by_id["plan-block"]["attrs"]
    assert "document.querySelectorAll('[data-settings-icon]')" in PAGE


def test_toast_is_a_manual_popover_outside_app() -> None:
    page = ElementParser()
    page.feed(PAGE)
    toast = page.by_id["toast"]
    assert toast["tag"] == "div"
    assert toast["attrs"]["popover"] == "manual"
    html, = page.root["children"]
    body, = [node for node in html["children"] if node["tag"] == "body"]
    assert toast in body["children"]


def test_rate_limit_failure_toast_uses_server_time_and_keeps_fallback() -> None:
    post = re.search(r"async function post\(name, action, body\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert post
    assert "failure.retry_at = result?.retry_at;" in post[1]
    assert "failure.limit = result?.limit;" in post[1]
    action = re.search(r"async function act\(name, action, body\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert action
    assert "failure.code === 'rate_limited' && typeof failure.retry_at === 'number'" in action[1]
    assert "`${context}: limit of ${failure.limit} per hour reached. Try again at ${clock(failure.retry_at)}.`" in action[1]
    assert ": `${context}: ${failure.message}`" in action[1]
    assert "toast('fail', message);" in action[1]


def test_failure_toast_has_accessible_dismiss_and_no_timer() -> None:
    helper = re.search(r"function toast\(kind, message\) \{(.*?)\n    \}", PAGE, re.DOTALL)
    assert helper
    code = helper[1]
    assert "hideToast();" in code
    assert "setAttribute('role', failed ? 'alert' : 'status')" in code
    assert "icon(failed ? 'warning' : 'check')" in code
    assert re.search(r"if \(failed\) \{.*?el\('button', 'dialog-x'\).*?"
                     r"setAttribute\('aria-label', 'Dismiss'\).*?append\(icon\('cross'\)\).*?"
                     r"addEventListener\('click', hideToast\)", code, re.DOTALL)
    assert "if (!failed) toastTimer = setTimeout(hideToast, 2500);" in code
    assert "raiseToast();" in code
    assert re.search(r"function hideToast\(\).*?clearTimeout\(toastTimer\).*?"
                     r"toastBox.hidePopover\(\)", PAGE, re.DOTALL)


def test_toast_stays_above_new_dialogs_and_actions_leave_error_bar_alone() -> None:
    assert PAGE.count(".showModal()") == 1
    assert re.search(r"dialog.showModal\(\);\s*if \(toastVisible\) raiseToast\(\);", PAGE)
    assert re.search(r"function raiseToast\(\).*?hidePopover\(\).*?"
                     r"querySelectorAll\('dialog\[open\]'\).*?append\(toastBox\).*?"
                     r"showPopover\(\)", PAGE, re.DOTALL)
    assert "if (toastVisible && toastBox.parentElement === dialog) raiseToast();" in PAGE
    assert PAGE.count("showError(") == 3  # Definition and the two state-refresh paths.
    assert "showError(`State unavailable: ${failure.message}`)" in PAGE
    assert not re.search(r"plan-message|reset-message|offset-error|showPlanMessage", PAGE)
    assert "settingsMessage.textContent = failure.message" not in PAGE
