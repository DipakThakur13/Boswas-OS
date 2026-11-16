"""WCAG 2.1 contrast of everything the toolkit draws text on (standard library only).

Text: at least 4.5:1 (1.4.3) on every surface it can appear on, in every
state of the control. Exempt, as in WCAG: disabled controls and their text;
placeholder text is held to 3:1 here. Boundaries and marks of controls
(1.4.11): at least 3:1.
"""

from __future__ import annotations

import unittest

import support  # noqa: F401

from boswas_fluent.tokens import SCHEMES, TEXT_CONTRAST, Theme, contrast

PAGE = ("background", "layer", "surface")                     # what a page is made of
SURFACES = PAGE + ("surface-raised", "flyout", "tooltip", "input-active", "card")
STATE_LAYERS = ("fill-control", "fill-control-hover", "fill-control-pressed", "fill-subtle-hover",
                "fill-subtle-pressed", "fill-disabled")
STATUSES = ("info", "success", "warning", "danger")


class ContrastTest(unittest.TestCase):
    def check(self, theme: Theme, foreground: str, background: str, minimum: float, what: str) -> None:
        ratio = contrast(foreground, background)
        self.assertGreaterEqual(ratio, minimum, f"{theme.scheme}: {what}: {foreground} on {background} is "
                                                 f"{ratio:.2f}:1, needs {minimum}:1")

    def controls(self, theme: Theme) -> dict[str, str]:
        """Every opaque colour a control can have behind its text: each state layer on each page surface."""
        return {f"{layer} on {base}": theme.over(layer, base) for layer in STATE_LAYERS
                for base in PAGE + ("flyout",)}

    def test_body_text_on_every_surface_and_state(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            backgrounds = {name: theme.color(name) for name in SURFACES} | self.controls(theme)
            backgrounds |= {f"{status}-tint": theme.color(f"{status}-tint") for status in STATUSES}
            for text in ("text", "text-secondary"):
                for name, colour in backgrounds.items():
                    self.check(theme, theme.color(text), colour, TEXT_CONTRAST, f"{text} on {name}")

    def test_muted_text(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            for name in SURFACES:
                self.check(theme, theme.color("text-muted"), theme.color(name), TEXT_CONTRAST, f"muted on {name}")
            # Placeholder text in a text box (rest, hover, focused): the documented exemption, held to 3:1.
            for base in PAGE:
                for layer in ("fill-control", "fill-control-hover"):
                    self.check(theme, theme.color("text-muted"), theme.over(layer, base), 3.0,
                               f"placeholder on {layer} on {base}")

    def test_text_on_the_accent(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            for fill in ("accent", "accent-fill-hover", "accent-fill-pressed"):
                self.check(theme, theme.color("on-accent"), theme.color(fill), TEXT_CONTRAST, f"on-accent on {fill}")
            for status in STATUSES:
                self.check(theme, theme.color("on-accent"), theme.color(f"{status}-fill"), TEXT_CONTRAST,
                           f"badge text on {status}-fill")

    def test_coloured_text(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            for name in ("accent-text", "info-text", "success-text", "warning-text", "danger-text"):
                for surface in PAGE:
                    self.check(theme, theme.color(name), theme.color(surface), TEXT_CONTRAST, f"{name} on {surface}")
            # A hyperlink while hovered or pressed.
            for layer in ("fill-subtle-hover", "fill-subtle-pressed"):
                for base in ("background", "surface"):
                    self.check(theme, theme.color("accent-text-strong"), theme.over(layer, base), TEXT_CONTRAST,
                               f"link on {layer} on {base}")

    def test_control_boundaries_and_marks(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            for base in PAGE:
                surface = theme.color(base)
                self.check(theme, theme.over("stroke-strong", base), surface, 3.0, f"strong stroke on {base}")
                self.check(theme, theme.color("accent"), surface, 3.0, f"accent mark on {base}")
                self.check(theme, theme.color("focus"), surface, 3.0, f"focus ring on {base}")
                for status in STATUSES:
                    self.check(theme, theme.color(status), surface, 3.0, f"{status} icon on {base}")
            for status in STATUSES:                               # the glyph inside a status icon
                self.check(theme, theme.color("on-accent"), theme.color(status), 3.0, f"glyph on {status}")
            # Between the ring and the control lies a line in the page colour, so the ring stands out
            # next to a control of any colour (an accent button, a status bar).
            self.check(theme, theme.color("focus"), theme.color("background"), 3.0, "focus ring on its inner line")

    def test_disabled_text_stays_visible(self):
        """Exempt from the 4.5:1 rule, but it must not vanish."""
        for scheme in SCHEMES:
            theme = Theme(scheme)
            for base in PAGE:
                self.check(theme, theme.over("text-disabled", base), theme.color(base), 2.0, f"disabled on {base}")


if __name__ == "__main__":
    unittest.main()
