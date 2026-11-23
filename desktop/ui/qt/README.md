# boswas_fluent

The PySide6 (Qt 6 Widgets) toolkit of Boswas OS applications. It makes a
Qt application look and behave like the rest of the Boswas desktop: Fluent
layout, geometry and motion, with the colours, radii, type and durations of
[`../tokens.json`](../tokens.json), the Boswas Prism icons and Inter.

```python
import boswas_fluent as fluent
```

| Path | What it is |
|------|------------|
| `boswas_fluent/` | The library. Installed by `boswas-branding` as `/usr/lib/boswas/python/boswas_fluent`, the module directory every Boswas program puts on `sys.path` |
| `tests/` | Its tests: `cd desktop/ui/qt && python3 -B -m unittest discover -s tests` |
| `python3 -m boswas_fluent.gallery` | Every widget and state; `--grab DIR` renders all pages in both schemes to PNG without a display |

From a checkout, put `desktop/ui/qt` on `PYTHONPATH` (or `sys.path`); the
library then reads `desktop/ui/tokens.json` and `desktop/icons` of the same
tree. Installed, it reads `/usr/share/boswas/ui/tokens.json`. The
environment variable `BOSWAS_UI_TOKENS` names another token file (tests).

Everything public is an attribute of the package and is imported when first
used. Names and signatures listed here are stable; additions are additive.

## A complete application

```python
"""boswas-notes: a small example."""
import sys

from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

import boswas_fluent as fluent

APP_ID = "com.boswas.Notes"          # the desktop file is com.boswas.Notes.desktop


class NotesWindow(fluent.FluentWindow):
    def __init__(self, arguments: list[str]) -> None:
        super().__init__(APP_ID, "Notes", navigation={"search": True})
        self.worker = fluent.Worker(self)
        nav = self.navigation
        nav.add_page("notes", "Notes", self.notes_page(), icon="accessories-text-editor")
        nav.add_page("settings", "Settings", icon="preferences-system", factory=self.settings_page, footer=True)
        self.status = nav.add_banner(fluent.InfoBar("info"))
        self.status.hide()
        self.add_shortcut("find", nav.focus_search)
        self.add_shortcut("quit", self.close)

    def notes_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 0, 32, 32)
        self.commands = fluent.CommandBar()
        self.save_action = self.commands.add_command("Save", "document-open", self.save, shortcut="Ctrl+S")
        self.editor = QPlainTextEdit()
        layout.addWidget(self.commands)
        layout.addWidget(self.editor, 1)
        return page

    def settings_page(self) -> QWidget:                 # built the first time it is shown
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 0, 32, 32)
        self.wrap = fluent.ToggleSwitch(checked=True)
        layout.addWidget(fluent.SettingsCard("Wrap lines", "Break long lines at the window edge",
                                             "preferences-desktop-font", self.wrap))
        layout.addStretch(1)
        return page

    def save(self) -> None:
        path = fluent.data_dir(APP_ID) / "notes.txt"
        self.worker.submit(path.write_text, self.editor.toPlainText(),
                           on_done=lambda _count: self.status.show_message("success", "Saved."),
                           on_error=lambda exc: self.status.show_message("error", f"Not saved: {exc}"))

    def closeEvent(self, event) -> None:
        self.worker.shutdown()
        super().closeEvent(event)


if __name__ == "__main__":
    sys.exit(fluent.run(APP_ID, NotesWindow, display_name="Notes"))
```

`fluent.run` creates the QApplication with the desktop file name, lets only
one instance run, applies the theme, handles Ctrl+C and returns the exit
code. A later start of the program raises the first window and calls its
`handle_arguments(list[str])`, if it has that method. Connect long-lived
signals such as `fluent.manager().changed` to methods of your widgets, not
to lambdas: Qt drops a connection to a method when the widget is destroyed.

## Rules for application authors

1. **No colour, radius, font size or duration of your own.** Take them from
   the theme: `fluent.theme().color("accent")`, `fluent.qcolor("fill-control")`,
   `fluent.font("subtitle")`, `fluent.duration("fast")`. No `setStyleSheet`
   on widgets: standard widgets are styled by the application style sheet,
   and a widget that needs another look paints itself with `fluent.paint`.
   The tests of the library fail if one of its modules contains a colour
   value; do the same check in your application.
2. **Standard widgets first.** `QPushButton`, `QLineEdit`, `QComboBox`,
   `QCheckBox`, `QListView`, `QTabWidget`, `QMenu`, ... already look right.
   Variants are dynamic properties: `fluent.Button(text, accent=True)` (one
   primary action per page or dialog), `subtle=True`; a `QProgressBar` takes
   the property `status` = `paused` or `error`; a framed item view
   (the Qt default) is drawn as a card, `setFrameShape(QFrame.NoFrame)` makes
   it transparent.
3. **Text from outside is plain text.** `fluent.Label`, `InfoBar`,
   `ContentDialog` and every row widget use `Qt.PlainText`. Keep it so.
4. **Nothing blocks the GUI thread.** Disk, network, D-Bus, subprocesses:
   `Worker.submit` / `fluent.run_in_thread`, results arrive in callbacks on
   the GUI thread.
5. **Every control has a name and works from the keyboard.** Icon-only
   buttons take a tool tip (it is the accessible name); a control inside a
   `SettingsCard` is named after the card's title unless you name it. The
   toolkit draws the focus ring; do not draw your own.
6. **Programs never run as root** (`fluent.run` refuses), start other
   programs only with argument lists, and open files and addresses with
   `fluent.open_path` / `fluent.open_url`.
7. **Honour reduced motion**: animate with `fluent.Animation` (see
   [Motion](#motion-animation)), never with `QVariantAnimation` or
   `QPropertyAnimation`: its steps follow the screen's refresh rate, and it
   goes straight to the end when the desktop asks for reduced motion. Take
   its length from `fluent.duration(...)` (0 with reduced motion) and its
   curve from `fluent.easing(...)`.
8. **Text grows, the rest does not.** Accessibility > Text size makes the
   type ramp up to 225 % larger: take sizes from `fluent.font(...)` and lay
   rows out with layouts, not fixed pixel heights, so that larger text has
   room.

### Adding a page

```python
nav.add_page("storage", "Storage", icon="drive-harddisk", factory=StoragePage,   # or a widget
             keywords=("disk", "space"), tooltip="Disks and free space")
```

A page is any `QWidget`. The NavigationView shows the page title above it;
give the page side margins of 32 px and put long content into a
`QScrollArea`. Rows of a settings page are `SettingsCard`s 4 px apart;
messages go to `nav.add_banner(fluent.InfoBar(...))`.

### Testing

```python
from boswas_fluent import testing

class WindowTest(testing.FluentTestCase):          # unittest; offscreen; private XDG directories; no session bus
    def test_save(self):
        window = self.show(NotesWindow([]), 1000, 700)
        testing.type_text(window.editor, "hello")
        testing.click(window.commands.button(window.save_action))
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(window.status.severity(), "success")
        testing.grab(window, self.tmp / "window.png")       # look at it
```

`testing.app()`, `show`, `grab`, `click`, `key`, `type_text`, `press_tab`,
`process`, `wait_until`, `set_scheme`. Import `boswas_fluent.testing` before
anything creates a QApplication (it selects Qt's offscreen platform).
`ContentDialog.exec()` blocks: in a test answer it with
`QTimer.singleShot(20, dialog.primary_button.click)`, or route questions
through an attribute of your window that tests replace.

## Reference

### Theme: `tokens`, `appearance`, `theming`

| Name | What it does |
|------|--------------|
| `Theme(scheme="dark", tokens=None, *, accent=None, text_scale=100)` | The tokens through one scheme, with the desktop's accent colour (a hue of `ACCENTS`: `iris`, the default, `azure`, `sky`, `mint`, `sun`, `coral`, `orchid`, `slate`) and text size (100 to 225 percent of the type ramp). `color(name)` → `#RRGGBB` for a role (`accent`, `text-secondary`, ...), a derived colour (`layer`, `card`, `flyout`, `warning-tint`, `success-text`, `accent-fill-hover`, ...) or a palette reference (`iris.500`); `rgba(name)` → (colour, opacity) for the layers `fill-control[-hover/-pressed]`, `fill-subtle-hover/-pressed`, `fill-disabled`, `stroke-control/-surface/-divider/-window`, `stroke-strong`, `text-disabled`, `accent-disabled`, `smoke`; `over(layer, base)`, `css(name)`, `radius(name)`, `spacing(steps)`, `type(name)` → `TypeStyle(size, line_height, weight)`, `families`, `duration(name)`, `curve(name)`, `elevation(name)`, `is_dark` |
| `fluent.theme()` | The current `Theme` |
| `fluent.apply(app=None, *, scheme=None, watch=True)` | Style the application: Fusion as base, palette, generated style sheet, Inter at the body size, icon theme `Boswas-Prism` / `Boswas-Prism-Light` (fallback Breeze), focus ring, round popups; then follow the desktop. Returns the manager. `FluentWindow` and `run` call it for you |
| `fluent.manager()` | The `ThemeManager`: `scheme`, `theme`, `reduced_motion`, `always_show_scrollbars`, `follows_desktop`, `applied`, `font_family()`, `set_scheme("dark"/"light"/None)`, `set_reduced_motion(bool/None)`, `reload()`, signal `changed(Theme)` |
| `fluent.font(style="body")` | QFont of a ramp step: `caption` 12, `body` 14, `body-strong`, `body-large` 16, `subtitle` 20, `title` 28, `title-large` 40 |
| `fluent.qcolor(name)` | QColor of a colour or layer (with its opacity) |
| `fluent.duration(name)`, `fluent.easing(name)` | `fast` 150, `normal` 200, `slow` 250 ms (0 with reduced motion); `decelerate`, `accelerate`, `standard` as QEasingCurve |
| `fluent.repolish(widget)` | After changing a dynamic property that the style sheet looks at |
| `read_appearance()`, `Appearance` | The desktop's look: `scheme`, `reduced_motion` and `accent` of `[appearance]`; `text_scale` and `always_show_scrollbars` of `[accessibility]` |

The scheme comes from the `[appearance]` table the compositor reads:
`/usr/share/boswas/compositor/compositor.toml`, `/etc/boswas/compositor.toml`,
`~/.config/boswas/compositor.toml`, then `~/.config/boswas/appearance.toml`
(written by `boswas-preset`). The two user files are watched; a change
re-styles every open window. Dark is the default.

The same files give the rest of what Settings changes for every
application (`boswas-compositor(1)`):

* **Accent colour** (`[appearance] accent`, Personalization > Colours).
  The roles `accent`, `accent-hover` and `accent-pressed` take the same
  shades of the chosen hue as the token file's accent takes of iris;
  `accent` is moved towards the text colour just far enough for
  `on-accent` text to keep 4.5:1, and every colour derived from it
  (`accent-text`, `accent-fill-*`, ...) is derived as before.
  `tests/test_contrast.py` checks every rule for every accent in both
  schemes.
* **Text size** (`[accessibility] text_scale`, Accessibility > Text size):
  every step of the type ramp, and with it `fluent.font(...)`, the
  application font and the style sheet's font sizes, is that much larger.
  Radii, spacing and icons stay.
* **Always show scroll bars** (`[accessibility] always_show_scrollbars`,
  Accessibility > Visual effects): scroll bars are drawn at the width they
  take under the pointer instead of as thin lines.

Custom-painted widgets read `fluent.theme()` in `paintEvent`; they need no
code for a scheme change.

### Motion: `animation`

Qt's own animations (`QVariantAnimation`, `QPropertyAnimation`) step on
one application timer of 16 ms: 60 times a second, also on a monitor that
shows 144 or 240. PySide6 exposes nothing to change that, so the toolkit
and the Boswas applications animate with `fluent.Animation`, which steps
once per refresh of the screen its widget is on.

```python
animation = fluent.Animation(self)                  # parent: the widget that moves
animation.setStartValue(0.0)
animation.setEndValue(1.0)
animation.setDuration(fluent.duration("fast"))      # 0 with reduced motion
animation.setEasingCurve(fluent.easing("decelerate"))
animation.valueChanged.connect(self._set_position)
animation.finished.connect(self._arrived)
animation.start()
```

| Name | What it does |
|------|--------------|
| `fluent.Animation(parent=None, *, clock=None)` | The part of `QVariantAnimation` Boswas programs use, under its names: `setStartValue`, `setEndValue`, `setKeyValueAt(step, value)`, `setDuration(ms)`, `setEasingCurve`, `setLoopCount` (-1: until stopped), `setDirection`, `start()`, `stop()` (no `finished`), `state()` (`Animation.State.Running` or `Stopped`), `currentValue()`, `refresh_rate()`; signals `valueChanged(value)` and `finished()`. Values: `int`, `float`, `QPointF`, `QRectF`. The parent is the widget that moves, or an object under it: its screen sets the pace. `clock` (monotonic seconds) is for tests |
| `fluent.refresh_rate(widget)` | How many steps a second animations of `widget` take: the refresh rate of its screen (`QScreen.refreshRate()`), 60 when the screen reports no usable rate, 500 at most |

* **Paced to the screen:** a step per refresh, at a fixed pace on a
  precise timer. The rate is read again at every step, so an animation of
  a window moved to another monitor, or of a monitor whose mode changed,
  follows it.
* **Driven by the clock:** each value comes from the time since `start()`
  on a monotonic clock. A step that comes late (the application was busy)
  never slows the animation down; steps missed are skipped; the last value
  is the end value exactly.
* **Reduced motion:** `start()` goes straight to the end value and emits
  `finished` before it returns.
* Do not use Qt's animation classes, or a `QTimer` at a fixed 16 or 33 ms,
  for motion: `tests/test_animation.py` fails if a module of the toolkit
  does.

### Icons: `icons`

| Name | What it does |
|------|--------------|
| `fluent.icon(name, *fallbacks, color=None, symbolic=None)` | A freedesktop icon name from Boswas Prism, then the icon a program installed for itself in `hicolor` (so another program shows its own icon, not Breeze's drawing of it; Qt's own loader would search `hicolor` last), then Qt's icon theme. `*-symbolic` icons are painted in the text colour of the current scheme (disabled: the disabled colour; `color="accent"`: a token colour) and follow a scheme change. `"go-next"` finds `go-next-symbolic` when there is no coloured icon of that name; `symbolic=True` prefers the symbolic one |
| `fluent.glyph_icon(glyph, color="text-secondary")` | The toolkit's own glyphs: `chevron-down/up/left/right`, `check`, `dash`, `close`, `more`, `restore`, `grip` |
| `fluent.pixmap(icon_or_name, size, *, color=None, enabled=True, ratio=1.0)` | A rendered pixmap (does not follow a scheme change: prefer `IconWidget`) |
| `fluent.has_icon(name)`, `fluent.icon_path(name)`, `fluent.missing_icons()` | Whether and where; the names asked for and not found |

### Widgets

Constructor arguments after `*` are keyword-only. Every widget takes
`parent` where shown.

| Widget | Use |
|--------|-----|
| `FluentWindow(app_id="", title="", parent=None, *, navigation=False, icon=None, size=(1100, 720), minimum_size=(480, 360), remember_geometry=True)` | Main window. `navigation=True` or a dict of NavigationView options → `window.navigation`; else `set_content(widget)`. `add_shortcut(keys, slot)`, `ask(title, text, confirm, cancel, *, default="cancel") -> bool`, `tell(title, text)`. Remembers its size in `$XDG_STATE_HOME/<app_id>/window.ini` |
| `NavigationView(parent=None, *, search=False, back_button=False, pane_toggle=False, header=True, pane_width=280, compact_below=720, content_layer=False)` | `add_page(key, title, widget=None, *, icon, factory, footer, tooltip, heading, keywords)`, `add_action(key, title, *, icon, footer=True)`, `add_section(title)`, `add_separator()`, `set_current(key)`, `current()`, `page(key)`, `keys()`, `item(key)`, `set_item_visible/enabled/text/icon/badge`, `set_title(text or None)`, `add_header_widget(w)`, `add_banner(w)`, `set_pane_header(w)`, `focus_search()`, `set_search_filters_items(bool)`, `set_compact(bool or None)`, `go_back()`. Attributes `list`, `footer_list`, `stack`, `search_box`, `back_button`, `title_label`. Signals `current_changed(key)`, `item_invoked(key)`, `back_requested()`, `search_changed(text)`, `search_submitted(text)` |
| `SettingsCard(title, description="", icon=None, control=None, parent=None, *, clickable=False)` | A settings row. `set_title/description/icon/control`, `add_control(w)`, `set_clickable(bool)`, `click()`, signal `clicked`; `button` is the row's button when clickable |
| `SettingsExpander(title, description="", icon=None, control=None, parent=None, *, expanded=False)` | A row that opens: `add_row(title, description, control, icon, *, clickable) -> SettingsCard`, `add_card(card)`, `cards()`, `set_expanded(bool)`, `is_expanded()`, `toggle()`, signal `expanded_changed(bool)`; `header` is its SettingsCard |
| `Expander(title, parent=None, *, description, icon, control, expanded)` | Same head, any content: `add_widget(w)`, `set_content(w)`, `content_layout` |
| `Card(parent=None, *, title="", padding=16, spacing=12)` | A surface: `add(widget)`, `add_layout(layout)`, `body` |
| `InfoBar(severity="info", title="", message="", parent=None, *, closable=True, action_text=None)` | `info`, `success`, `warning`, `error`. `show_message(severity, message, title="")`, `set_severity/title/message`, `set_action(text)`, `add_widget(w)`, `dismiss()`; signals `closed`, `action_triggered` |
| `ContentDialog(parent, title="", text="", *, primary="OK", secondary=None, close="Cancel", default="primary")` | Modal inside the window. `exec() -> ContentDialog.Result` (`PRIMARY`, `SECONDARY`, `NONE`), `open()` + signal `finished(result)`, `set_content(widget)`, `content_layout`, `set_default(name)`, `primary_button`, `secondary_button`, `close_button`. Shortcuts: `fluent.ask(parent, title, text, confirm, cancel, *, default="cancel") -> bool`, `fluent.message(parent, title, text)` |
| `Flyout(parent=None, *, padding=16)` | Popup beside a widget: `add_widget(w)`, `body`, `show_at(anchor, placement="bottom")`; signal `closed` |
| `TeachingTip(title, text, parent=None, *, action_text=None, closable=True)` | A Flyout with title, text, action; signal `action_triggered` |
| `MenuFlyout(parent)` | QMenu with `add_item(text, triggered=None, *, icon, shortcut, checkable, checked, enabled) -> QAction`, `add_submenu(text, icon)`, `show_at(anchor)`, `exec_at(anchor)` |
| `CommandBar(parent=None, *, labels=True)` | `add_command(text, icon=None, triggered=None, *, shortcut, tooltip, primary=True, checkable=False) -> QAction`, `add_action(action, *, primary=True)`, `add_separator()`, `add_widget(w)`, `button(action)`, `commands()`, `overflowed()`. What does not fit moves into the "More" menu |
| `BreadcrumbBar(parent=None, *, items=(), style="body")` | `set_items(list)`, `push(text)`, `items()`; signal `item_clicked(index)` |
| `Pivot(parent=None)`, `SegmentedControl(parent=None)` | `add_item(key, text, icon=None)`, `set_current(key)`, `current()`, `keys()`, `item(key)`, `set_item_text`; signal `current_changed(key)` |
| `SearchBox(parent=None, *, placeholder="Search")` | QLineEdit; signals `query_submitted(text)`, `cleared`; `query()` |
| `PasswordBox(parent=None, *, placeholder="", reveal_button=True)` | QLineEdit; `password()`, `set_revealed(bool)`, `is_revealed()` |
| `NumberBox(parent=None, *, value=0, minimum=0, maximum=100, step=1, decimals=0, suffix="")` | QDoubleSpinBox; `int_value()`; signal `valueChanged(float)` |
| `ToggleSwitch(name="", parent=None, *, checked=False, on_text="On", off_text="Off", show_state=True)` | QAbstractButton: `isChecked()`, `setChecked()`, signal `toggled(bool)` |
| `Button(text="", parent=None, *, icon=None, accent=False, subtle=False)` | QPushButton; `set_accent(bool)`, `set_subtle(bool)` |
| `IconButton(icon=None, tooltip="", parent=None, *, size=32, icon_size=16, checkable=False)` | QToolButton with an icon only |
| `HyperlinkButton(text="", url=None, parent=None)` | Opens `url` with `open_url` when clicked |
| `ProgressRing(parent=None, *, size=32, stroke=None, indeterminate=True)` | `set_value(0..100)`, `set_indeterminate(bool)`; spins only while visible |
| `Label(text="", style="body", parent=None, *, tone=None, wrap=False, selectable=False)` | Plain-text QLabel in a ramp step; tones `secondary`, `muted`, `accent`, `success`, `warning`, `danger`, `info`, `on-accent`; `set_style`, `set_tone` |
| `IconWidget(icon=None, size=20, parent=None)` | Shows an icon; follows scheme and enabled state; `set_icon`, `set_icon_size` |
| `StatusIcon(kind="info", size=16, parent=None)` | `info`, `success`, `warning`, `danger`, `attention`, `neutral`, `busy`: a shape per state |
| `Badge(text="", parent=None, *, kind="neutral", icon=None)`, `InfoBadge(value=None, parent=None, *, kind="attention")` | A word in a pill; a dot or a count |
| `TagChip(text="", parent=None, *, icon=None, closable=False, checkable=False)` | Signals `toggled(bool)`, `removed` |
| `Avatar(name="", parent=None, *, size=32, picture=None)` | Picture, else initials on an icon hue |
| `ListItem(title="", subtitle="", icon=None, trailing=None, parent=None, *, clickable=True)` | A row for widget lists: `set_leading(w)`, `set_trailing(w)`, `set_selected(bool)`; signal `clicked` |
| `EmptyState(title="", text="", icon=None, action_text=None, parent=None)` | Signal `action_triggered`; `action_button` |
| `Skeleton(parent=None, *, width=None, height=16, radius=None)`, `Separator(orientation=Qt.Horizontal, parent=None)` | A loading placeholder; a divider |

`fluent.paint`: `rounded_rect`, `card`, `shadow`, `glyph`, `status_badge`,
`elided` for widgets that draw themselves, all with token colour names.

### Helpers

| Name | What it does |
|------|--------------|
| `fluent.run(app_id, window_factory, *, argv=None, display_name=None, version=None, icon=None, single_instance=True, allow_root=False) -> int` | See above. `window_factory(arguments) -> QWidget`. Returns 4 when run as root, 0 when another instance took over |
| `fluent.SingleInstance(app_id)` | `start(arguments) -> bool` (False: another instance was told), signal `message_received(list)`, `close()`. Socket: `$XDG_RUNTIME_DIR/boswas-fluent/<app_id>.sock` |
| `fluent.Worker(parent=None, max_threads=4)` | `submit(fn, *args, on_done=None, on_error=None, key=None, **kwargs)`, `busy(key)`, `pending()`, `wait(timeout)`, `discard(task_id)`, `shutdown()` |
| `fluent.run_in_thread(fn, on_done=None, on_error=None)` | One call on a shared Worker |
| `fluent.notify(summary, body="", *, app_name=None, icon="", timeout_ms=-1, replaces_id=0, desktop_entry=None) -> int` | org.freedesktop.Notifications; returns the ID, 0 (and does nothing) without a service |
| `fluent.open_url(url)`, `fluent.open_path(path)` | Through the desktop portal, else `xdg-open` with one argument. `open_url` takes `http`, `https`, `mailto` only. Return whether the request was handed over |
| `fluent.config_dir(app_id)`, `state_dir`, `cache_dir`, `data_dir`, `runtime_dir()` | The XDG directories of an application (created, mode 0700) |
| `fluent.add_shortcut(widget, keys, slot)`, `fluent.add_shortcuts(widget, find=..., refresh=..., quit=...)` | Standard names: `new`, `open`, `save`, `save_as`, `print`, `close`, `quit`, `undo`, `redo`, `cut`, `copy`, `paste`, `select_all`, `delete`, `find`, `find_next`, `find_previous`, `replace`, `refresh`, `zoom_in`, `zoom_out`, `zoom_reset`, `back`, `forward`, `help`, `fullscreen`, `preferences`, `rename`, `properties`, `new_tab`, `next_tab`, `previous_tab` |

### What `apply` does to standard widgets

Besides the style sheet, a small application event filter (`polish.py`)
gives menus, combo box popups and tool tips translucent windows (so their
corners are round), lays item views out again once they are styled, gives
tables 36 px rows and a header that does not turn bold, gives trees a
delegate that draws one selection pill per row, insets the text of spin
boxes, and shows the **focus ring**: 2 px in the `focus` colour with an
inner line in the page colour, around the widget (row, tab) that has the
keyboard focus. As in WinUI the ring follows keyboard navigation only; text
boxes show focus with their accent underline. A widget can place its ring
with `fluent_focus_rect() -> (host, QRect)` or turn it off with the dynamic
property `fluentFocusRing = False`.

### Contrast

`tests/test_contrast.py` holds text to 4.5:1 on every surface and state
layer of both schemes, and control boundaries, marks and the focus ring to
3:1. Exempt, as in WCAG: disabled controls; placeholder text is held to
3:1. Where a token colour misses 4.5:1 as text (today: `success` in the
light scheme, white on `accent-hover` in the light scheme), the derived
colours `*-text`, `*-fill` and `accent-fill-hover/-pressed` move it just far
enough towards the text colour; widgets use those.

## Not implemented

* **Mica and acrylic.** Windows are filled with the Mica tint and popups
  with the acrylic tint, opaque: blurring the wallpaper is the compositor's
  job and Qt has no way to ask for it. Window frames and title bars are the
  compositor's too.
* **Shadows of menus, combo box popups and tool tips.** They have the
  one-pixel outline only (`Flyout`, `TeachingTip` and `ContentDialog` draw
  their own shadow). `TeachingTip` has no tail and closes on a click
  elsewhere.
* **Animated state changes of standard widgets** (hover fades, the check
  mark drawing itself): style sheets do not animate. The toolkit's own
  widgets animate (switch, selection pills, expander, pane, dialog smoke),
  at the screen's refresh rate (`fluent.Animation`); Qt's indeterminate
  `QProgressBar` still steps at about 60 Hz, because Fusion animates it on
  Qt's own timer.
* **Transparency effects** (Personalization > Colours) change nothing in
  Qt applications: their surfaces are opaque tints already (see Mica and
  acrylic above).
* **Tick marks of a styled `QSlider`**, the arrows of scroll bars, and
  whole-row hover in a `QTableView` (a table that selects rows has no hover
  highlight; one that selects cells highlights the cell).
* **The urgency of a notification** and its action buttons: the hint needs
  a D-Bus byte, which PySide's QtDBus cannot send. `notify` waits up to
  1.5 s for the service's answer.
* **Activating the first window on a second start** is requested with the
  second start's `XDG_ACTIVATION_TOKEN`; whether the window comes to the
  front is the compositor's decision.
* `NumberBox` does not evaluate expressions; `RatingControl`, `InfoBar`
  stacking with animation, `TabView` with tear-off tabs and a tree-shaped
  NavigationView (nested items) do not exist.
* Line heights of the type ramp are not applied to `QLabel` (Qt lays text
  out with the font's own line height).
