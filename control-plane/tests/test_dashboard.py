"""Static checks of the Control Plane web dashboard (control-plane/dashboard).

The dashboard is served by boswas_cp.api with a strict Content-Security-Policy
(script-src/style-src 'self', connect-src 'self', form-action 'none', ...).
These tests make sure the files stay compatible with it and with the
security rules of the dashboard:

  - only index.html, app.js, app.css and favicon.svg, all same-origin;
  - no inline scripts, inline styles, event-handler attributes, eval, HTML
    injection sinks, external URLs or persistent browser storage;
  - the operator token lives in sessionStorage and is sent as a bearer token;
  - every API call targets an existing operator route of boswas_cp.api with
    the right method, and path parameters are URL-encoded;
  - the UI offers typed commands only (no free-text command of any kind);
  - the dashboard's constants match the agent's command, event and policy
    definitions.

Run: python3 -B -m unittest discover -s tests -p "test_dashboard.py" -v
"""

from __future__ import annotations

import itertools
import re
import sys
import types
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve()
CONTROL_PLANE = HERE.parents[1]
REPO = HERE.parents[2]
for _path in (CONTROL_PLANE, REPO / "packages/boswas-device-agent", REPO / "packages/boswas-compat"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from boswas_agent import commands as agent_commands  # noqa: E402
from boswas_agent import policydoc  # noqa: E402
from boswas_agent.models import EventType  # noqa: E402
from boswas_compat import manifest as compat_manifest  # noqa: E402
from boswas_cp import api as cp_api  # noqa: E402

DASHBOARD = CONTROL_PLANE / "dashboard"
FILES = ("index.html", "app.js", "app.css", "favicon.svg")
ASSETS = {"app.js", "app.css", "favicon.svg"}
OPERATOR_ACCESS = ("viewer", "operator", "admin")

# Values that satisfy the path-parameter patterns of boswas_cp.api
# (device/command UUIDs, token IDs, application IDs, policy and operator names).
PATH_SAMPLES = ("6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b", "0123456789ab", "com.example.app", "default", "alice")

HANDLER_NAMES = re.compile(
    r"(?i)^on(abort|animation\w+|auxclick|before\w+|blur|cancel|change|click|close|contextmenu|copy|cut|dblclick|"
    r"drag\w*|drop|error|focus\w*|hashchange|input|invalid|key\w+|load\w*|message|mouse\w+|paste|pointer\w+|"
    r"popstate|reset|resize|scroll|select|submit|toggle|touch\w+|transition\w+|unload|wheel)$")

FORBIDDEN_UI_WORDS = re.compile(
    r"\b(shell|terminal|console|scripts?|bash|powershell|zsh|cmd|command[- ]line|run command|"
    r"remote command|execute|exec|arbitrary|sudo|ssh)\b", re.IGNORECASE)


def read(name: str) -> str:
    return (DASHBOARD / name).read_text(encoding="utf-8")


# --- a small JavaScript lexer: string, template and regex literals; comments ------------------

class Literal(NamedTuple):
    kind: str        # '"', "'" or "`"
    text: str        # raw content between the delimiters (templates keep ${...})
    start: int       # offset of the opening delimiter


_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = {"return", "typeof", "case", "in", "of", "new", "delete", "void", "throw", "else", "do",
                      "yield", "await"}


def _skip_regex(src: str, i: int) -> int:
    j, in_class = i + 1, False
    while True:
        if j >= len(src) or src[j] == "\n":
            raise ValueError(f"unterminated regular expression at {i}")
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "/":
            break
        j += 1
    j += 1
    while j < len(src) and src[j].isalpha():
        j += 1
    return j


def _template(src: str, i: int, out: list[Literal]) -> int:
    j = i + 1
    while True:
        if j >= len(src):
            raise ValueError(f"unterminated template literal at {i}")
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == "`":
            break
        if src.startswith("${", j):
            j = _scan(src, j + 2, out, in_template=True)
            continue
        j += 1
    out.append(Literal("`", src[i + 1:j], i))
    return j + 1


def _scan(src: str, i: int, out: list[Literal], in_template: bool = False) -> int:
    depth, last, n = 0, "", len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                raise ValueError(f"unterminated comment at {i}")
            i = end + 2
        elif c in "\"'":
            j = i + 1
            while True:
                if j >= n or src[j] == "\n":
                    raise ValueError(f"unterminated string at {i}")
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
            out.append(Literal(c, src[i + 1:j], i))
            i, last = j + 1, "literal"
        elif c == "`":
            i, last = _template(src, i, out), "literal"
        elif c == "/":
            if last == "" or last in _REGEX_AFTER or last in _REGEX_AFTER_WORDS:
                i, last = _skip_regex(src, i), "literal"
            else:
                i, last = i + 1, "/"
        elif c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            i, last = j, src[i:j]
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                if in_template and depth == 0:
                    return i + 1
                depth -= 1
            i, last = i + 1, c
    if in_template:
        raise ValueError("unterminated template expression")
    return i


def js_literals(src: str) -> list[Literal]:
    out: list[Literal] = []
    _scan(src, 0, out)
    return sorted(out, key=lambda lit: lit.start)


def js_array(src: str, name: str) -> list[str]:
    m = re.search(rf"const {name} = \[(.*?)\];", src, re.DOTALL)
    if not m:
        raise AssertionError(f"app.js has no array constant {name}")
    return re.findall(r'"([^"]*)"', m.group(1))


# --- index.html -----------------------------------------------------------------------------------

class _Html(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.script_content: list[str] = []
        self.text: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script_content.append(data)
        else:
            self.text.append(data)


class DashboardFilesTests(unittest.TestCase):
    def test_the_four_served_files_exist(self):
        for name in FILES:
            path = DASHBOARD / name
            self.assertTrue(path.is_file(), f"missing {path}")
            self.assertGreater(path.stat().st_size, 0, f"{name} is empty")
            self.assertNotIn(b"\r\n", path.read_bytes(), f"{name} must use LF line endings")
            path.read_bytes().decode("utf-8")
        self.assertEqual({name for name, _ in cp_api.STATIC.values()}, set(FILES),
                         "the server serves exactly these dashboard files")

    def test_index_references_only_same_origin_assets(self):
        html = read("index.html")
        parser = _Html()
        parser.feed(html)
        referenced = set()
        for tag, attrs in parser.tags:
            self.assertNotIn(tag, ("style", "iframe", "object", "embed", "base", "frame"), f"<{tag}> is not allowed")
            for name, value in attrs.items():
                self.assertFalse(name.lower().startswith("on"), f"event-handler attribute {name} on <{tag}>")
                self.assertNotEqual(name.lower(), "style", f"inline style on <{tag}>")
                if name in ("src", "href", "action", "formaction", "data", "poster", "srcset", "xlink:href"):
                    if value.startswith("#"):
                        continue
                    self.assertIn(value, ASSETS, f"<{tag} {name}={value!r}> is not a same-origin dashboard asset")
                    referenced.add(value)
            if tag == "script":
                self.assertEqual(attrs.get("src"), "app.js")
                self.assertIn("defer", attrs)
            if tag == "link":
                self.assertIn(attrs.get("href"), ("app.css", "favicon.svg"))
            if tag == "form":
                self.assertNotIn("action", attrs, "forms are handled by app.js (form-action 'none')")
            if tag == "input" and attrs.get("id") == "signin-token":
                self.assertEqual(attrs.get("type"), "password")
                self.assertNotIn("name", attrs, "the token field must never be submitted as a form value")
        self.assertEqual(referenced, ASSETS)
        self.assertEqual("".join(parser.script_content).strip(), "", "inline script content")
        self.assertNotRegex(html, r"(?i)https?://", "external URL in index.html")
        self.assertIn("<noscript>", html)
        self.assertRegex(html, r'<html lang="en">')

    def test_stylesheet_and_icon_are_self_contained(self):
        css = read("app.css")
        self.assertNotIn("@import", css)
        self.assertNotIn("url(", css)
        self.assertNotIn("expression(", css)
        self.assertNotRegex(css, r"(?i)https?://")
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("max-width: 800px", css)
        self.assertIn("system-ui", css)
        svg = read("favicon.svg")
        self.assertTrue(svg.lstrip().startswith("<svg"))
        for forbidden in ("<script", "href", "url(", "foreignObject", "<image", "on"):
            if forbidden == "on":
                self.assertNotRegex(svg, r"\son[a-z]+\s*=", "event-handler attribute in favicon.svg")
            else:
                self.assertNotIn(forbidden, svg)
        self.assertEqual(re.findall(r"https?://[^\"]*", svg), ["http://www.w3.org/2000/svg"])


class AppScriptSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read("app.js")
        cls.literals = js_literals(cls.src)

    def test_lexer_sees_the_whole_file(self):
        self.assertGreater(len(self.literals), 300)
        self.assertTrue(any(lit.text == "/api/v1/me" for lit in self.literals))

    def test_strict_mode(self):
        self.assertTrue(re.match(r"(?s)\s*/\*.*?\*/\s*\"use strict\";", self.src), "app.js starts with \"use strict\"")

    def test_no_dynamic_code_html_sinks_or_external_urls(self):
        forbidden = {
            r"\beval\s*\(": "eval",
            r"\bnew\s+Function\b": "new Function",
            r"\bFunction\s*\(": "Function constructor",
            r"document\.write": "document.write",
            r"localStorage": "localStorage",
            r"document\.cookie": "cookies",
            r"indexedDB": "indexedDB",
            r"innerHTML": "innerHTML",
            r"outerHTML": "outerHTML",
            r"insertAdjacentHTML": "insertAdjacentHTML",
            r"createContextualFragment": "createContextualFragment",
            r"DOMParser": "DOMParser",
            r"(?i)javascript:": "javascript: URL",
            r"(?i)https?://": "absolute URL",
            r"set(Timeout|Interval)\s*\(\s*[\"'`]": "string timer",
            r"\bimport\s*\(": "dynamic import",
            r"XMLHttpRequest|WebSocket|EventSource|sendBeacon": "other network APIs",
            r"\.style\b|cssText": "inline style",
            r"\bwindow\.open\s*\(": "window.open",
        }
        for pattern, what in forbidden.items():
            self.assertNotRegex(self.src, pattern, f"app.js must not use {what}")
        self.assertNotRegex(self.src, r"\.on[a-z]+\s*=(?!=)", "event-handler properties: use addEventListener")
        for lit in self.literals:
            self.assertNotRegex(lit.text, HANDLER_NAMES, f"event-handler attribute name {lit.text!r} in app.js")
            self.assertNotRegex(lit.text, r"(?i)\bon[a-z]+\s*=", f"inline handler markup {lit.text!r} in app.js")

    def test_single_network_helper_same_origin(self):
        self.assertEqual(len(re.findall(r"\bfetch\s*\(", self.src)), 1, "all requests go through api()")
        self.assertRegex(self.src, r'mode:\s*"same-origin"')
        self.assertRegex(self.src, r'credentials:\s*"same-origin"')
        self.assertRegex(self.src, r"headers\.Authorization = `Bearer \$\{token\}`")

    def test_token_is_kept_in_session_storage_only(self):
        for call in ("sessionStorage.getItem", "sessionStorage.setItem", "sessionStorage.removeItem"):
            self.assertIn(call, self.src)
        # A 401 answer signs out (which clears the token).
        self.assertRegex(self.src, r"response\.status === 401\)\s*\{\s*signOut\(")
        clear = re.search(r"function signOut\(.*?\n  \}", self.src, re.DOTALL)
        self.assertIsNotNone(clear)
        self.assertIn("clearToken();", clear.group(0))
        # The token never goes into a URL.
        self.assertNotRegex(self.src, r"[?&]token=")

    def test_dom_helper_refuses_handlers_and_styles(self):
        self.assertRegex(self.src, r"FORBIDDEN_ATTR_RE = /\^\(on\|style\$")
        h = re.search(r"function h\(tag, attrs, \.\.\.children\) \{.*?\n  \}", self.src, re.DOTALL)
        self.assertIsNotNone(h, "the h() DOM helper exists")
        self.assertIn("FORBIDDEN_ATTR_RE.test(name)", h.group(0))
        self.assertIn("SAFE_HREF_RE", h.group(0))
        self.assertIn("textContent", self.src)
        # Only h() sets attributes by variable name; other calls use fixed ARIA names.
        dynamic = re.findall(r"setAttribute\(\s*([A-Za-z_$][\w$]*)\s*,", self.src)
        self.assertEqual(dynamic, ["name"])
        for name in re.findall(r"setAttribute\(\s*\"([^\"]+)\"", self.src):
            self.assertTrue(name.startswith("aria-"), f"setAttribute({name!r})")

    def test_api_paths_exist_with_the_right_method(self):
        routes = cp_api.Api(types.SimpleNamespace(max_artifact_bytes=1), None, max_upload=1).routes
        # The bare prefix is api()'s own guard (const API_PREFIX); every other mention is a request path.
        self.assertEqual(len([lit for lit in self.literals if lit.text == "/api/v1/"]), 1)
        api_literals = [lit for lit in self.literals if "/api/" in lit.text and lit.text != "/api/v1/"]
        self.assertGreater(len(api_literals), 25)
        used = []
        for lit in api_literals:
            path = lit.text
            self.assertTrue(path.startswith("/api/v1/"), f"{path!r} is not an /api/v1 path")
            self.assertNotRegex(path, r"[?#\s]", f"{path!r}: pass query parameters separately")
            before = self.src[max(0, lit.start - 80):lit.start]
            m = re.search(r'\bapi\(\s*"(GET|POST|PATCH|PUT|DELETE)"\s*,\s*$', before)
            method = m.group(1) if m else ("GET" if re.search(r"\bapiGet\(\s*$", before) else None)
            self.assertIsNotNone(method, f"{path!r} must be passed directly to api() or apiGet()")
            placeholders = re.findall(r"\$\{([^}]*)\}", path)
            for expr in placeholders:
                self.assertRegex(expr, r"^enc\([\w.\[\]]+\)$", f"path parameter {expr!r} in {path!r} must be URL-encoded")
            template = re.sub(r"\$\{[^}]*\}", "\0", path)
            matched = None
            for values in itertools.product(PATH_SAMPLES, repeat=len(placeholders)):
                candidate = template
                for value in values:
                    candidate = candidate.replace("\0", value, 1)
                matched = next((r for r in routes if r.method == method and r.pattern.match(candidate)), None)
                if matched:
                    break
            self.assertIsNotNone(matched, f"{method} {path} is not a route of boswas_cp.api")
            self.assertIn(matched.access, OPERATOR_ACCESS, f"{method} {path} is not an operator route")
            used.append(matched)
        # The management functions the dashboard must offer.
        device = f"/api/v1/devices/{PATH_SAMPLES[0]}"
        required = [
            ("GET", "/api/v1/me"), ("GET", "/api/v1/summary"), ("GET", "/api/v1/devices"), ("GET", device),
            ("PATCH", device), ("POST", f"{device}/retire"), ("GET", f"{device}/status"),
            ("GET", f"{device}/inventory"), ("GET", f"{device}/applications"), ("GET", f"{device}/commands"),
            ("POST", f"{device}/commands"), ("POST", f"{device}/commands/{PATH_SAMPLES[0]}/cancel"),
            ("GET", f"{device}/events"), ("GET", "/api/v1/commands"), ("GET", "/api/v1/applications"),
            ("POST", "/api/v1/applications"), ("DELETE", "/api/v1/applications/com.example.app"),
            ("GET", "/api/v1/artifacts"), ("POST", "/api/v1/artifacts"), ("GET", "/api/v1/policies"),
            ("POST", "/api/v1/policies"), ("GET", "/api/v1/policies/default"), ("GET", "/api/v1/events"),
            ("GET", "/api/v1/events/verify"), ("GET", "/api/v1/enrollment-tokens"),
            ("POST", "/api/v1/enrollment-tokens"), ("POST", "/api/v1/enrollment-tokens/0123456789ab/revoke"),
            ("GET", "/api/v1/operators"), ("POST", "/api/v1/operators"), ("POST", "/api/v1/operators/alice/disable"),
        ]
        for method, path in required:
            self.assertTrue(any(r.method == method and r.pattern.match(path) for r in used),
                            f"the dashboard does not call {method} {path}")

    def test_upload_sends_the_raw_file(self):
        self.assertRegex(self.src, r'headers\["Content-Type"\] = "application/octet-stream"')
        self.assertRegex(self.src, r'"X-Boswas-File-Name": encodeURIComponent\(file\.name\)')

    def test_typed_commands_only(self):
        for lit in self.literals:
            text = re.sub(r"\$\{[^}]*\}", " ", lit.text)          # the words, not the template's code
            self.assertNotRegex(text, FORBIDDEN_UI_WORDS, f"UI text {lit.text!r} suggests a free-text command")
        parser = _Html()
        parser.feed(read("index.html"))
        visible = " ".join(parser.text) + " " + " ".join(
            v for _, attrs in parser.tags for k, v in attrs.items() if k in ("title", "aria-label", "placeholder", "alt"))
        self.assertNotRegex(visible, FORBIDDEN_UI_WORDS)
        # The command body has only the typed fields the API accepts.
        builder = re.search(r"function buildCommandBody\(.*?\n  \}", self.src, re.DOTALL)
        self.assertIsNotNone(builder)
        fields = set(re.findall(r"body\.(\w+) =", builder.group(0))) | {"type"}
        self.assertLessEqual(fields, {"type", "application_id", "version", "ttl_seconds", "idempotency_key"})
        # Every command type named in app.js is a real typed command, and all of them are known.
        types_in_js = set(js_array(self.src, "APPLICATION_COMMANDS")) | set(js_array(self.src, "DEVICE_COMMANDS"))
        self.assertEqual(types_in_js, {c.value for c in agent_commands.CommandType})
        command_like = {lit.text for lit in self.literals
                        if re.fullmatch(r"(?:[A-Z]+_)+(?:APPLICATION|INVENTORY|POLICY|AGENT)", lit.text)}
        self.assertLessEqual(command_like, types_in_js)


class AppScriptConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read("app.js")

    def test_statuses_and_event_types_match_the_agent(self):
        self.assertEqual(js_array(self.src, "COMMAND_STATUSES"), [s.value for s in agent_commands.CommandStatus])
        self.assertEqual(set(js_array(self.src, "EVENT_TYPES")), {e.value for e in EventType})

    def test_policy_editor_matches_the_policy_document(self):
        self.assertEqual(js_array(self.src, "COMPAT_STATUSES"), list(policydoc.COMPAT_STATUSES))
        self.assertEqual(js_array(self.src, "POLICY_DEVICES"), list(policydoc.DEVICES))
        self.assertEqual(js_array(self.src, "POLICY_FOLDERS"), list(compat_manifest.FOLDERS))
        default = policydoc.default_policy()
        for section in ("compat", "agent", "updates"):
            for key in default[section]:
                self.assertRegex(self.src, rf"\b{key}:", f"policy field {section}.{key} is missing from app.js")
        # Confinement cannot be switched off from the dashboard.
        self.assertEqual(re.findall(r"require_apparmor:\s*(\w+)", self.src), ["true", "true"])
        for value in policydoc.AGENT_UPDATES:
            self.assertIn(f'value: "{value}"', self.src)

    def test_routes_cover_the_requested_pages(self):
        for route in ("overview", "devices", "applications", "policies", "commands", "events", "enrollment",
                      "operators"):
            self.assertIn(f"/^{route}$/", self.src)
            self.assertIn(f'href="#/{route}"', read("index.html"))
        self.assertIn(r"/^devices\/([0-9a-f-]{36})$/", self.src)


if __name__ == "__main__":
    unittest.main()
