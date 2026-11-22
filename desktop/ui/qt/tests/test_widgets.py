"""The widgets: behaviour, signals, keyboard, accessible names (Qt, offscreen)."""

from __future__ import annotations

import unittest

import support

if support.PYSIDE:
    from PySide6.QtCore import QPoint, QSize, Qt, QTimer
    from PySide6.QtGui import QImage, QKeySequence, QShortcut
    from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

    import boswas_fluent as fluent
    from boswas_fluent import paint, testing
    from boswas_fluent.widgets.basic import initials

    Base = testing.FluentTestCase
else:
    Base = unittest.TestCase


class Signals:
    """Collects what a signal emitted."""

    def __init__(self, signal) -> None:
        self.calls: list[tuple] = []
        signal.connect(lambda *args: self.calls.append(args))

    def __len__(self) -> int:
        return len(self.calls)


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class BasicTest(Base):
    def test_label_is_plain_text_in_the_type_ramp(self):
        label = self.keep(fluent.Label("<b>not bold</b>", "subtitle", tone="secondary", wrap=True))
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(label.text(), "<b>not bold</b>")
        self.assertEqual(label.font().pixelSize(), fluent.theme().type("subtitle").size)
        self.assertEqual(label.property("tone"), "secondary")
        self.assertEqual(label.styleSheet(), "")
        self.show(label)
        self.assertEqual(label.palette().color(label.foregroundRole()).name().upper(),
                         fluent.theme().color("text-secondary"))
        label.set_tone("danger")
        testing.process()
        self.assertEqual(label.palette().color(label.foregroundRole()).name().upper(),
                         fluent.theme().color("danger-text"))
        label.set_style("caption")
        self.assertEqual(label.font().pixelSize(), 12)

    def test_buttons(self):
        accent = self.show(fluent.Button("Save", accent=True))
        plain = self.show(fluent.Button("Cancel"))
        self.assertTrue(accent.property("accent"))
        self.assertEqual(accent.height(), 32)
        self.assertEqual(accent.grab().toImage().pixelColor(8, 16).name().upper(), fluent.theme().color("accent"))
        self.assertNotEqual(plain.grab().toImage().pixelColor(8, 16).name().upper(), fluent.theme().color("accent"))
        plain.set_accent(True)
        testing.process()
        self.assertEqual(plain.grab().toImage().pixelColor(8, 16).name().upper(), fluent.theme().color("accent"))
        icon_button = self.keep(fluent.IconButton("view-refresh", "Refresh"))
        self.assertEqual(icon_button.accessibleName(), "Refresh")
        self.assertEqual(icon_button.toolTip(), "Refresh")
        self.assertEqual(icon_button.size(), QSize(32, 32))
        self.assertEqual(icon_button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertFalse(icon_button.icon().isNull())

    def test_hyperlink_button_opens_only_safe_addresses(self):
        from boswas_fluent import desktop
        opened = []
        original = desktop._xdg_open
        desktop._xdg_open = lambda argument: opened.append(argument) or True
        self.addCleanup(setattr, desktop, "_xdg_open", original)
        link = self.keep(fluent.HyperlinkButton("Docs", "https://example.org/guide?page=2"))
        self.assertTrue(link.property("hyperlink"))
        link.click()
        self.assertEqual(opened, ["https://example.org/guide?page=2"])
        for refused in ("javascript:alert(1)", "file:///etc/passwd", "https://example.org/a b", "https:///nohost",
                        "http://example.org/\nX", "ftp://example.org/", "", None):
            link.set_url(refused)
            link.click()
        self.assertEqual(len(opened), 1, "nothing else was opened")

    def test_toggle_switch(self):
        switch = self.show(fluent.ToggleSwitch("Night light"))
        toggled = Signals(switch.toggled)
        self.assertEqual(switch.accessibleName(), "Night light")
        self.assertTrue(switch.isCheckable())
        self.assertFalse(switch.isChecked())
        self.assertEqual(switch.state_text(), "Off")
        self.assertEqual(switch.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        testing.click(switch)
        self.assertTrue(switch.isChecked())
        self.assertEqual(switch.state_text(), "On")
        testing.key(switch, Qt.Key.Key_Space)
        self.assertFalse(switch.isChecked())
        testing.key(switch, Qt.Key.Key_Return)
        self.assertTrue(switch.isChecked())
        self.assertEqual(toggled.calls, [(True,), (False,), (True,)])
        self.assertTrue(testing.wait_until(lambda: switch.knob_position() == 1.0, 2), "the knob arrives")
        switch.setEnabled(False)
        testing.click(switch)
        self.assertTrue(switch.isChecked())
        wide, narrow = switch.sizeHint().width(), fluent.ToggleSwitch(show_state=False).sizeHint().width()
        self.assertEqual(narrow, fluent.ToggleSwitch.TRACK.width())
        self.assertGreater(wide, narrow)

    def test_toggle_switch_follows_a_state_set_without_signals(self):
        switch = self.show(fluent.ToggleSwitch())
        heard = []
        switch.toggled.connect(heard.append)
        blocked = switch.blockSignals(True)
        switch.setChecked(True)
        switch.blockSignals(blocked)
        self.assertEqual(heard, [], "nothing was announced")
        self.assertEqual(switch.knob_position(), 1.0, "but the knob stands where the state says")
        on = switch.grab().toImage()
        blocked = switch.blockSignals(True)
        switch.setChecked(False)
        switch.blockSignals(blocked)
        self.assertEqual(switch.knob_position(), 0.0)
        self.assertNotEqual(switch.grab().toImage(), on)

    def test_toggle_switch_animates_unless_motion_is_reduced(self):
        switch = self.show(fluent.ToggleSwitch())
        switch.setChecked(True)
        self.assertLess(switch.knob_position(), 1.0, "it moves over time")
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        switch.setChecked(False)
        self.assertEqual(switch.knob_position(), 0.0, "with reduced motion it jumps")

    def test_progress_ring_spins_only_while_visible(self):
        ring = self.keep(fluent.ProgressRing())
        self.assertTrue(ring.is_indeterminate())
        self.assertFalse(ring.is_animating(), "not before it is shown")
        testing.show(ring)
        self.assertTrue(ring.is_animating())
        first = ring.grab().toImage()
        testing.process(120)
        self.assertNotEqual(first, ring.grab().toImage(), "the ring moves")
        ring.hide()
        self.assertFalse(ring.is_animating(), "it stops when hidden")
        ring.show()
        self.assertTrue(ring.is_animating())
        ring.set_value(40)
        self.assertFalse(ring.is_animating())
        self.assertEqual((ring.value(), ring.is_indeterminate()), (40.0, False))
        ring.set_value(400)
        self.assertEqual(ring.value(), 100.0)
        ring.set_indeterminate(True)
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        ring.hide()
        ring.show()
        self.assertFalse(ring.is_animating(), "no spinning with reduced motion")
        self.assertTrue(ring.accessibleName())

    def test_badges(self):
        badge = self.keep(fluent.Badge("Beta", kind="warning"))
        self.assertEqual((badge.text(), badge.kind(), badge.accessibleName()), ("Beta", "warning", "Beta"))
        self.assertEqual(badge.colours(), ("warning-tint", "text"))
        badge.set_kind("nonsense")
        self.assertEqual(badge.kind(), "neutral")
        badge.set_text("Stable")
        self.assertEqual(badge.accessibleName(), "Stable")
        self.assertEqual(fluent.Badge("x", kind="accent").colours(), ("accent", "on-accent"))
        dot, count, many = fluent.InfoBadge(), fluent.InfoBadge(7), fluent.InfoBadge(250)
        self.assertEqual((dot.text(), count.text(), many.text()), ("", "7", "99+"))
        self.assertEqual(dot.sizeHint(), QSize(8, 8))
        self.assertGreater(many.sizeHint().width(), count.sizeHint().width())
        self.assertEqual((dot.accessibleName(), many.accessibleName()), ("New", "99+"))

    def test_tag_chip(self):
        chip = self.show(fluent.TagChip("Filter", checkable=True, closable=True))
        toggled, removed = Signals(chip.toggled), Signals(chip.removed)
        self.assertEqual(chip.accessibleName(), "Filter")
        testing.click(chip, QPoint(12, chip.height() // 2))
        self.assertTrue(chip.isChecked())
        testing.click(chip, QPoint(chip.width() - 12, chip.height() // 2))       # the remove mark
        self.assertEqual((len(removed), chip.isChecked()), (1, True))
        testing.key(chip, Qt.Key.Key_Delete)
        self.assertEqual(len(removed), 2)
        testing.key(chip, Qt.Key.Key_Return)
        self.assertEqual(toggled.calls, [(True,), (False,)])
        plain = self.keep(fluent.TagChip("Plain"))
        gone = Signals(plain.removed)
        testing.key(plain, Qt.Key.Key_Delete)
        self.assertEqual(len(gone), 0, "only closable chips are removed")

    def test_avatar_initials_and_picture(self):
        self.assertEqual([initials(n) for n in ("Ada Lovelace", "ada", "  ", "Jean-Luc Picard", "a.b.c", "")],
                         ["AL", "A", "", "JP", "AC", ""])
        avatar = self.show(fluent.Avatar("Ada Lovelace", size=40))
        self.assertEqual((avatar.accessibleName(), avatar.size()), ("Ada Lovelace", QSize(40, 40)))
        self.assertEqual(avatar.hue(), fluent.Avatar("Ada Lovelace").hue(), "the hue of a name is stable")
        self.assertIn(avatar.hue(), fluent.theme().tokens["icon"]["hues"])
        corner = avatar.grab().toImage().pixelColor(0, 0)
        centre = avatar.grab().toImage().pixelColor(4, 20)
        self.assertNotEqual(corner, centre, "it is a circle")
        self.assertFalse(avatar.has_picture())
        picture = QImage(8, 8, QImage.Format.Format_RGB32)
        picture.fill(Qt.GlobalColor.transparent)
        avatar.set_picture(picture)
        self.assertTrue(avatar.has_picture())
        avatar.set_picture("/nonexistent/picture.png")
        self.assertFalse(avatar.has_picture(), "an unreadable file falls back to the initials")

    def test_status_icon_and_skeleton(self):
        icon = self.keep(fluent.StatusIcon("error", 20))
        self.assertEqual((icon.kind(), icon.accessibleName()), ("danger", "Error"))
        icon.set_kind("ok")
        self.assertEqual((icon.kind(), icon.accessibleName()), ("success", "Success"))
        self.assertEqual(paint.status_kind("whatever"), "info")
        shapes = set()
        for kind in paint.STATUS_KINDS:
            icon.set_kind(kind)
            testing.show(icon)
            shapes.add(bytes(icon.grab().toImage().constBits()))
        self.assertEqual(len(shapes), len(paint.STATUS_KINDS), "every state has its own picture")
        skeleton = self.keep(fluent.Skeleton(width=100))
        self.assertFalse(skeleton.is_animating())
        testing.show(skeleton)
        self.assertTrue(skeleton.is_animating())
        skeleton.hide()
        self.assertFalse(skeleton.is_animating())


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class InputTest(Base):
    def test_search_box(self):
        box = self.show(fluent.SearchBox(placeholder="Find"))
        submitted, cleared = Signals(box.query_submitted), Signals(box.cleared)
        self.assertEqual((box.accessibleName(), box.placeholderText()), ("Find", "Find"))
        self.assertFalse(box.clear_action.isVisible())
        testing.type_text(box, "  wifi ")
        self.assertEqual(box.text(), "  wifi ")
        self.assertTrue(box.clear_action.isVisible())
        testing.key(box, Qt.Key.Key_Return)
        box.search_action.trigger()
        self.assertEqual(submitted.calls, [("wifi",), ("wifi",)])
        box.clear_action.trigger()
        self.assertEqual((box.text(), len(cleared), box.clear_action.isVisible()), ("", 1, False))
        box.setText("again")
        testing.key(box, Qt.Key.Key_Escape)
        self.assertEqual((box.text(), len(cleared)), ("", 2))

    def test_password_box(self):
        box = self.show(fluent.PasswordBox(placeholder="Password"))
        box.setText("hunter2")
        self.assertEqual(box.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(box.displayText(), "●" * 7 if box.displayText()[0] == "●" else box.displayText())
        self.assertNotIn("hunter2", box.displayText())
        self.assertTrue(box.inputMethodHints() & Qt.InputMethodHint.ImhSensitiveData)
        self.assertEqual(box.reveal_action.text(), "Show password")
        box.reveal_action.trigger()
        self.assertTrue(box.is_revealed())
        self.assertEqual((box.displayText(), box.reveal_action.text()), ("hunter2", "Hide password"))
        box.set_revealed(False)
        self.assertFalse(box.is_revealed())
        box.set_revealed(True)
        box.set_reveal_button_visible(False)
        self.assertFalse(box.is_revealed(), "hiding the button hides the password")
        self.assertEqual(box.password(), "hunter2")

    def test_number_box(self):
        box = self.show(fluent.NumberBox(value=5, minimum=0, maximum=10))
        changed = Signals(box.valueChanged)
        self.assertEqual((box.int_value(), box.decimals()), (5, 0))
        testing.key(box, Qt.Key.Key_Up)
        self.assertEqual(box.int_value(), 6)
        box.setValue(99)
        self.assertEqual(box.int_value(), 10)
        self.assertEqual(changed.calls, [(6.0,), (10.0,)])
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QWheelEvent
        box.clearFocus()
        wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(box, wheel)
        self.assertEqual(box.int_value(), 10, "the wheel does nothing without focus")
        self.assertGreaterEqual(box.sizeHint().width(), 124)


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class CardTest(Base):
    def test_card(self):
        card = fluent.Card(title="Storage")
        label = card.add(fluent.Label("content"))
        self.show(card, 300, 120)
        self.assertEqual((card.accessibleName(), card.title_label.text()), ("Storage", "Storage"))
        self.assertIs(label.parentWidget(), card)
        image = card.grab().toImage()
        self.assertEqual(image.pixelColor(150, 60).name().upper(), fluent.theme().color("card"))
        self.assertNotEqual(image.pixelColor(0, 0).name().upper(), fluent.theme().color("card"), "round corners")

    def test_settings_card_with_a_control(self):
        switch = fluent.ToggleSwitch()
        card = self.show(fluent.SettingsCard("Wi-Fi", "Connected", "network-wireless", switch), 480, 68)
        self.assertEqual((card.title(), card.description(), card.accessibleName()), ("Wi-Fi", "Connected", "Wi-Fi"))
        self.assertEqual((switch.accessibleName(), switch.accessibleDescription()), ("Wi-Fi", "Connected"))
        self.assertIs(card.control(), switch)
        self.assertFalse(card.is_clickable())
        self.assertIsNone(card.button)
        self.assertEqual(card.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertGreaterEqual(card.height(), fluent.SettingsCard.MIN_HEIGHT)
        self.assertTrue(card.icon_widget.isVisibleTo(card))
        testing.click(switch)
        self.assertTrue(switch.isChecked(), "the control works")
        card.set_description("")
        self.assertFalse(card.description_label.isVisibleTo(card))
        card.set_title("Network")
        self.assertEqual(card.accessibleName(), "Network")
        for label in card.findChildren(QLabel):
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)

    def test_clickable_settings_card(self):
        card = self.show(fluent.SettingsCard("Display", "Monitors", "video-display", clickable=True), 480, 68)
        clicks = Signals(card.clicked)
        self.assertTrue(card.is_clickable())
        self.assertIsInstance(card.button, QAbstractButton)
        self.assertEqual((card.button.accessibleName(), card.button.accessibleDescription()),
                         ("Display", "Monitors"))
        self.assertEqual(card.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertIs(card.focusProxy(), card.button)
        self.assertTrue(card.action_widget.isVisibleTo(card), "the chevron")
        testing.click(card, QPoint(120, 30))                      # on the title: the click reaches the row
        card.click()
        testing.key(card, Qt.Key.Key_Return)
        testing.key(card.button, Qt.Key.Key_Space)
        self.assertEqual(len(clicks), 4)
        host, rect = card.button.fluent_focus_rect()
        self.assertEqual(rect, card.geometry(), "the focus ring goes around the whole row")
        card.setEnabled(False)
        card.click()
        self.assertEqual(len(clicks), 4)
        card.setEnabled(True)
        card.set_clickable(False)
        self.assertFalse(card.is_clickable())
        self.assertFalse(card.action_widget.isVisibleTo(card))

    def test_a_control_on_a_clickable_card_keeps_its_clicks(self):
        button = fluent.Button("Rename")
        card = self.show(fluent.SettingsCard("Device name", control=button, clickable=True), 480, 68)
        row, inner = Signals(card.clicked), Signals(button.clicked)
        testing.click(button)
        self.assertEqual((len(row), len(inner)), (0, 1))

    def test_expander(self):
        expander = fluent.Expander("Advanced", description="Rarely needed")
        body = expander.add_widget(fluent.Label("content"))
        self.show(expander, 400, 200)
        changed = Signals(expander.expanded_changed)
        self.assertFalse(expander.is_expanded())
        self.assertFalse(body.isVisibleTo(expander))
        self.assertIn("Collapsed", expander.header.button.accessibleDescription())
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        expander.header.click()
        self.assertTrue(expander.is_expanded())
        self.assertTrue(body.isVisibleTo(expander))
        self.assertIn("Expanded", expander.header.button.accessibleDescription())
        testing.key(expander.header.button, Qt.Key.Key_Space)
        self.assertFalse(body.isVisibleTo(expander))
        self.assertEqual(changed.calls, [(True,), (False,)])
        replacement = fluent.Label("other")
        expander.set_content(replacement)
        expander.set_expanded(True, animate=False)
        self.assertTrue(replacement.isVisibleTo(expander))
        self.assertEqual(expander.title(), "Advanced")

    def test_expander_animates(self):
        expander = fluent.Expander("Animated")
        expander.add_widget(fluent.Label("line\nline\nline\nline"))
        self.show(expander, 400, 300)
        expander.set_expanded(True)
        self.assertLess(expander.content.maximumHeight(), 100, "it opens over time")
        self.assertTrue(testing.wait_until(lambda: expander.content.maximumHeight() > 10000, 2))
        expander.set_expanded(False)
        self.assertTrue(testing.wait_until(lambda: not expander.content.isVisible(), 2))

    def test_settings_expander(self):
        expander = fluent.SettingsExpander("Volume", "Output", "audio-card", expanded=True)
        first = expander.add_row("Speakers", control=fluent.ToggleSwitch())
        second = expander.add_card(fluent.SettingsCard("More", clickable=True))
        self.show(expander, 480, 260)
        self.assertEqual(expander.cards(), [first, second])
        self.assertTrue(expander.is_expanded())
        self.assertTrue(first.isVisibleTo(expander))
        self.assertEqual(first.minimumHeight(), fluent.SettingsCard.ROW_MIN_HEIGHT)
        clicks = Signals(second.clicked)
        second.click()
        self.assertEqual(len(clicks), 1)
        self.assertEqual(expander.accessibleName(), "Volume")

    def test_info_bar(self):
        bar = fluent.InfoBar("warning", "Low space", "2 GB left", action_text="Free up")
        self.show(bar, 480, 48)
        closed, action = Signals(bar.closed), Signals(bar.action_triggered)
        self.assertEqual((bar.severity(), bar.title(), bar.message()), ("warning", "Low space", "2 GB left"))
        self.assertEqual(bar.accessibleName(), "Low space 2 GB left")
        self.assertEqual(bar.accessibleDescription(), "Warning")
        self.assertEqual(bar.status_icon.kind(), "warning")
        self.assertEqual(bar.message_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(bar.grab().toImage().pixelColor(240, 4).name().upper(),
                         fluent.theme().color("warning-tint"))
        bar.action_button.click()
        self.assertEqual(len(action), 1)
        self.assertEqual(bar.close_button.accessibleName(), "Close message")
        bar.close_button.click()
        self.assertEqual((len(closed), bar.isHidden()), (1, True))
        bar.show_message("error", "<i>It failed</i>")
        self.assertEqual((bar.severity(), bar.title(), bar.message(), bar.isHidden()),
                         ("error", "", "<i>It failed</i>", False))
        self.assertFalse(bar.title_label.isVisibleTo(bar))
        for name, expected in (("ok", "success"), ("danger", "error"), ("nonsense", "info")):
            bar.set_severity(name)
            self.assertEqual(bar.severity(), expected)
        self.assertIsNone(fluent.InfoBar("info", closable=False).close_button)
        bar.set_action(None)
        self.assertIsNone(bar.action_button)

    def test_list_item(self):
        item = self.show(fluent.ListItem("Ada", "ada@example.org", "avatar-default", fluent.Badge("Admin")), 320, 56)
        clicks = Signals(item.clicked)
        self.assertEqual((item.title(), item.subtitle(), item.accessibleName()), ("Ada", "ada@example.org", "Ada"))
        self.assertEqual(item.height(), 56)
        testing.click(item, QPoint(100, 20))
        testing.key(item, Qt.Key.Key_Return)
        self.assertEqual(len(clicks), 2)
        self.assertFalse(item.is_selected())
        before = item.grab().toImage()
        item.set_selected(True)
        self.assertTrue(item.is_selected())
        after = item.grab().toImage()
        self.assertNotEqual(before, after)
        self.assertEqual(after.pixelColor(1, 28).name().upper(), fluent.theme().color("accent"), "the pill")
        item.set_subtitle("")
        self.assertEqual(item.height(), 40)
        item.set_leading(fluent.Avatar("Ada"))
        self.assertFalse(item.icon_widget.isVisibleTo(item))
        static = self.keep(fluent.ListItem("Static", clickable=False))
        self.assertIsNone(static.button)

    def test_empty_state(self):
        empty = self.show(fluent.EmptyState("Nothing here", "Add something.", "folder", "Add"), 400, 300)
        action = Signals(empty.action_triggered)
        self.assertEqual((empty.accessibleName(), empty.accessibleDescription()), ("Nothing here", "Add something."))
        self.assertTrue(empty.action_button.property("accent"))
        empty.action_button.click()
        self.assertEqual(len(action), 1)
        self.assertIsNone(fluent.EmptyState("x").action_button)


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class SelectorTest(Base):
    def selector(self, cls):
        control = cls()
        for key, text in (("all", "All"), ("installed", "Installed"), ("updates", "Updates")):
            control.add_item(key, text)
        return self.show(control, 360, 44)

    def test_pivot_and_segmented_control(self):
        for cls in (fluent.Pivot, fluent.SegmentedControl):
            control = self.selector(cls)
            changed = Signals(control.current_changed)
            self.assertEqual((control.keys(), control.current(), control.count()),
                             (["all", "installed", "updates"], "all", 3), cls.__name__)
            self.assertEqual(control.item("installed").accessibleName(), "Installed")
            testing.click(control.item("installed"))
            self.assertEqual(control.current(), "installed")
            testing.click(control.item("installed"))                  # a second click keeps it selected
            self.assertTrue(control.item("installed").isChecked())
            testing.key(control.item("installed"), Qt.Key.Key_Right)
            self.assertEqual(control.current(), "updates")
            testing.key(control.item("updates"), Qt.Key.Key_Right)    # no wrap at the end
            self.assertEqual(control.current(), "updates")
            testing.key(control.item("updates"), Qt.Key.Key_Home)
            self.assertEqual(control.current(), "all")
            testing.key(control.item("all"), Qt.Key.Key_End)
            control.set_current("installed")
            self.assertEqual(changed.calls, [("installed",), ("updates",), ("all",), ("updates",), ("installed",)])
            self.assertEqual([key for key in control.keys() if control.item(key).isChecked()], ["installed"])
            tab_stops = [key for key in control.keys()
                         if control.item(key).focusPolicy() != Qt.FocusPolicy.NoFocus]
            self.assertEqual(tab_stops, ["installed"], "one tab stop: the current item")
            with self.assertRaises(KeyError):
                control.set_current("nope")
            with self.assertRaises(ValueError):
                control.add_item("all", "Again")
            control.set_item_text("updates", "Updates (3)")
            self.assertEqual(control.item("updates").text(), "Updates (3)")

    def test_the_indicator_slides(self):
        pivot = self.selector(fluent.Pivot)
        start = pivot.indicator_rect()
        pivot.set_current("updates")
        target = pivot.indicator_for(pivot.item("updates"))
        self.assertNotEqual(pivot.indicator_rect(), target, "it does not jump")
        self.assertTrue(testing.wait_until(lambda: pivot.indicator_rect() == target, 2))
        self.assertNotEqual(start, target)

    def test_breadcrumb_bar(self):
        bar = self.show(fluent.BreadcrumbBar(items=["Home", "Documents", "Projects", "Report"]), 600, 32)
        clicked = Signals(bar.item_clicked)
        self.assertEqual((bar.items(), bar.count(), bar.hidden_count()),
                         (["Home", "Documents", "Projects", "Report"], 4, 0))
        crumbs = bar._crumbs
        self.assertEqual([c.accessibleName() for c in crumbs], bar.items())
        testing.click(crumbs[1])
        testing.key(crumbs[0], Qt.Key.Key_Return)
        testing.click(crumbs[3])                                  # the current place is not a link
        self.assertEqual(clicked.calls, [(1,), (0,)])
        self.assertEqual(crumbs[3].focusPolicy(), Qt.FocusPolicy.NoFocus)
        bar.resize(150, 32)
        testing.process()
        self.assertGreater(bar.hidden_count(), 0, "the first crumbs move into the menu")
        self.assertTrue(bar.overflow_button.isVisibleTo(bar))
        self.assertEqual([a.text() for a in bar.overflow_button.menu().actions()],
                         bar.items()[:bar.hidden_count()])
        bar.overflow_button.menu().actions()[0].trigger()
        self.assertEqual(clicked.calls[-1], (0,))
        self.assertTrue(crumbs[3].isVisibleTo(bar), "the current place always stays")
        bar.resize(600, 32)
        testing.process()
        self.assertEqual(bar.hidden_count(), 0)
        bar.push("Draft")
        self.assertEqual(bar.items()[-1], "Draft")
        bar.set_items([])
        self.assertEqual(bar.count(), 0)

    def test_a_new_path_replaces_the_old_crumbs_at_once(self):
        bar = self.show(fluent.BreadcrumbBar(items=["System", "Display"]), 600, 32)
        old = list(bar._crumbs)
        bar.set_items(["Personalization", "Taskbar", "Behaviours"])
        # Before the old widgets are deleted (the next turn of the event loop) they must not show any more.
        visible = [crumb.accessibleName() for crumb in bar.findChildren(type(old[0])) if crumb.isVisibleTo(bar)]
        self.assertEqual(visible, ["Personalization", "Taskbar", "Behaviours"])
        self.assertTrue(all(not crumb.isVisibleTo(bar) for crumb in old))
        before = bar.grab().toImage()
        testing.process()
        self.assertEqual(bar.grab().toImage(), before, "nothing changes once the old crumbs are gone")


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class CommandBarTest(Base):
    def bar(self):
        bar = fluent.CommandBar()
        self.ran = []
        new = bar.add_command("New", "list-add", lambda: self.ran.append("new"), shortcut="Ctrl+N")
        open_ = bar.add_command("Open", "document-open", lambda: self.ran.append("open"))
        bar.add_separator()
        delete = bar.add_command("Delete", "edit-delete", lambda: self.ran.append("delete"))
        properties = bar.add_command("Properties", triggered=lambda: self.ran.append("properties"), primary=False)
        return bar, new, open_, delete, properties

    def test_commands(self):
        bar, new, open_, delete, properties = self.bar()
        self.show(bar, 600, 32)
        self.assertEqual(bar.commands(), [new, open_, delete, properties])
        self.assertEqual(bar.overflowed(), [])
        button = bar.button(new)
        self.assertEqual(button.accessibleName(), "New")
        self.assertIn("Ctrl+N", button.toolTip())
        self.assertEqual(button.toolButtonStyle(), Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        testing.click(button)
        testing.key(bar.button(open_), Qt.Key.Key_Space)
        self.assertEqual(self.ran, ["new", "open"])
        delete.setEnabled(False)
        self.assertFalse(bar.button(delete).isEnabled())
        self.assertIsNone(bar.button(properties))
        self.assertTrue(bar.more_button.isVisibleTo(bar), "secondary commands live in the More menu")
        self.assertEqual(bar.more_button.accessibleName(), "More commands")
        bar.more_menu.aboutToShow.emit()
        self.assertEqual([a.text() for a in bar.more_menu.actions()], ["Properties"])
        properties.trigger()
        self.assertEqual(self.ran[-1], "properties")

    def test_overflow(self):
        bar, new, open_, delete, properties = self.bar()
        self.show(bar, 600, 32)
        bar.resize(130, 32)
        testing.process()
        self.assertEqual(bar.overflowed(), [open_, delete])
        self.assertTrue(bar.button(new).isVisibleTo(bar))
        self.assertFalse(bar.button(delete).isVisibleTo(bar))
        bar.more_menu.aboutToShow.emit()
        actions = bar.more_menu.actions()
        self.assertEqual([a.text() for a in actions if not a.isSeparator()], ["Open", "Delete", "Properties"])
        actions[0].trigger()
        self.assertEqual(self.ran, ["open"])
        bar.resize(600, 32)
        testing.process()
        self.assertEqual(bar.overflowed(), [])
        open_.setVisible(False)
        self.assertFalse(bar.button(open_).isVisibleTo(bar))
        self.assertNotIn(open_, bar.overflowed())

    def test_trailing_widget_and_icon_only(self):
        bar = fluent.CommandBar(labels=False)
        action = bar.add_command("Refresh", "view-refresh")
        search = bar.add_widget(fluent.SearchBox())
        self.show(bar, 500, 32)
        self.assertEqual(bar.button(action).toolButtonStyle(), Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.assertEqual(bar.button(action).accessibleName(), "Refresh")
        self.assertGreater(search.x(), bar.button(action).x())
        self.assertEqual(bar.overflowed(), [])


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class DialogTest(Base):
    def host(self):
        window = QWidget()
        layout = QVBoxLayout(window)
        self.behind = QPushButton("Behind", window)
        self.edit = QLineEdit(window)
        layout.addWidget(self.behind)
        layout.addWidget(self.edit)
        self.show(window, 640, 480)
        window.activateWindow()
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        return window

    def test_content_dialog_layout_and_buttons(self):
        window = self.host()
        dialog = fluent.ContentDialog(self.behind, "Delete?", "This cannot be undone.", primary="Delete",
                                      secondary="Archive", close="Cancel")
        finished = Signals(dialog.finished)
        self.assertIs(dialog.parentWidget(), window, "the dialog covers the window of its parent")
        dialog.open()
        testing.process()
        self.assertTrue(dialog.is_open())
        self.assertEqual(dialog.geometry(), window.rect())
        self.assertTrue(dialog.card.geometry().width() <= fluent.ContentDialog.MAX_WIDTH)
        self.assertEqual(dialog.card.geometry().center().x(), window.rect().center().x())
        self.assertEqual((dialog.accessibleName(), dialog.accessibleDescription()),
                         ("Delete?", "This cannot be undone."))
        self.assertEqual([b.text() for b in (dialog.primary_button, dialog.secondary_button, dialog.close_button)],
                         ["Delete", "Archive", "Cancel"])
        self.assertIs(dialog.default_button(), dialog.primary_button)
        self.assertTrue(dialog.primary_button.property("accent"))
        self.assertFalse(dialog.close_button.property("accent"))
        self.assertEqual(dialog.title_label.textFormat(), Qt.TextFormat.PlainText)
        smoke = window.grab().toImage().pixelColor(4, 4)
        self.assertLess(smoke.lightness(), fluent.qcolor("background").lightness() + 1, "the window is dimmed")
        dialog.secondary_button.click()
        self.assertFalse(dialog.is_open())
        self.assertTrue(dialog.isHidden())
        self.assertEqual(finished.calls, [(fluent.ContentDialog.Result.SECONDARY,)])
        self.assertEqual(dialog.result(), fluent.ContentDialog.Result.SECONDARY)
        dialog.done(fluent.ContentDialog.Result.PRIMARY)          # closing twice changes nothing
        self.assertEqual(len(finished), 1)

    def test_keyboard_enter_escape_and_the_focus_stays_inside(self):
        window = self.host()
        self.behind.setFocus()
        dialog = fluent.ContentDialog(window, "Question", "Text", primary="Yes", close="No", default="close")
        dialog.open()
        testing.process()
        focused = QApplication.focusWidget()
        if focused is None:
            self.skipTest("this platform gives no keyboard focus to hidden windows")
        self.assertIs(focused, dialog.close_button, "the default button has the focus")
        self.assertTrue(dialog.close_button.property("accent"))
        seen = set()
        for _ in range(6):
            seen.add(testing.press_tab())
        self.assertEqual(seen, {dialog.primary_button, dialog.close_button}, "Tab never leaves the dialog")
        self.assertIn(testing.press_tab(backwards=True), seen)
        testing.key(dialog.primary_button, Qt.Key.Key_Escape)
        self.assertEqual(dialog.result(), fluent.ContentDialog.Result.NONE)
        self.assertIs(QApplication.focusWidget(), self.behind, "focus returns to where it was")
        dialog.open()
        testing.process()
        testing.key(dialog, Qt.Key.Key_Return)
        self.assertEqual(dialog.result(), fluent.ContentDialog.Result.NONE, "Enter presses the default: No")
        dialog.set_default("primary")
        dialog.open()
        testing.process()
        testing.key(dialog, Qt.Key.Key_Return)
        self.assertEqual(dialog.result(), fluent.ContentDialog.Result.PRIMARY)

    def test_the_window_behind_is_out_of_reach(self):
        window = self.host()
        fired = []
        shortcut = QShortcut(QKeySequence("Ctrl+K"), window)
        shortcut.activated.connect(lambda: fired.append("shortcut"))
        self.behind.clicked.connect(lambda: fired.append("click"))
        dialog = fluent.ContentDialog(window, "Busy", "Wait.", primary="OK", close=None)
        dialog.open()
        testing.process()
        self.assertEqual(dialog.geometry(), window.rect())
        for child in (self.behind, self.edit):
            on_top = window.childAt(child.geometry().center())
            self.assertTrue(on_top is dialog or dialog.isAncestorOf(on_top), "the dialog covers everything")
        testing.click(window, QPoint(8, 8))                       # on the dimmed part
        testing.click(window, self.behind.geometry().center())
        from PySide6.QtGui import QShortcutEvent
        QApplication.sendEvent(shortcut, QShortcutEvent(QKeySequence("Ctrl+K"), 0))
        self.assertEqual(fired, [], "neither clicks nor shortcuts reach the window")
        dialog.accept()
        QApplication.sendEvent(shortcut, QShortcutEvent(QKeySequence("Ctrl+K"), 0))
        self.assertEqual(fired, ["shortcut"])
        self.assertIsNone(dialog.secondary_button)
        self.assertIsNone(dialog.close_button)

    def test_exec_and_ask(self):
        window = self.host()
        dialog = fluent.ContentDialog(window, "Run", "Now?", primary="Run")
        QTimer.singleShot(20, dialog.primary_button.click)
        self.assertEqual(dialog.exec(), fluent.ContentDialog.Result.PRIMARY)

        def answer(with_primary: bool) -> None:
            shown = [d for d in window.findChildren(fluent.ContentDialog) if d.is_open()]
            (shown[0].primary_button if with_primary else shown[0].close_button).click()

        QTimer.singleShot(20, lambda: answer(True))
        self.assertTrue(fluent.ask(window, "Delete?", "Really?", "Delete"))
        QTimer.singleShot(20, lambda: answer(False))
        self.assertFalse(fluent.ask(window, "Delete?", "Really?", "Delete"))
        QTimer.singleShot(20, lambda: answer(False))
        fluent.message(window, "Done", "It is done.")
        extra = fluent.ContentDialog(window, "With content")
        field = extra.set_content(QLineEdit())
        extra.open()
        testing.process()
        self.assertTrue(field.isVisibleTo(window))
        extra.reject()

    def test_the_dialog_follows_the_window_size(self):
        window = self.host()
        dialog = fluent.ContentDialog(window, "Title", "Text " * 80)
        dialog.open()
        window.resize(360, 300)
        testing.process()
        self.assertEqual(dialog.geometry(), window.rect())
        self.assertLessEqual(dialog.card.width(), 360 - 48)
        self.assertTrue(window.rect().contains(dialog.card.geometry()))
        dialog.reject()

    def test_flyout(self):
        window = self.host()
        flyout = fluent.Flyout(window)
        label = flyout.add_widget(fluent.Label("Flyout text"))
        closed = Signals(flyout.closed)
        flyout.show_at(self.behind)
        testing.process()
        self.assertTrue(flyout.isVisible())
        self.assertTrue(flyout.windowFlags() & Qt.WindowType.Popup)
        self.assertTrue(flyout.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(label.isVisibleTo(flyout))
        anchor = self.behind.mapToGlobal(QPoint(0, 0))
        surface_top = flyout.mapToGlobal(flyout.surface.geometry().topLeft()).y()
        self.assertGreaterEqual(surface_top, anchor.y() + self.behind.height(), "below the anchor")
        above = flyout.position_for(self.behind, flyout.size(), "top")
        self.assertLess(above.y(), flyout.y())
        testing.key(flyout, Qt.Key.Key_Escape)
        self.assertFalse(flyout.isVisible())
        self.assertEqual(len(closed), 1)

    def test_teaching_tip_and_menu_flyout(self):
        window = self.host()
        tip = fluent.TeachingTip("Pin it", "Pinned things stay.", window, action_text="Got it")
        action = Signals(tip.action_triggered)
        tip.show_at(self.behind, "right")
        testing.process()
        self.assertEqual((tip.accessibleName(), tip.accessibleDescription()), ("Pin it", "Pinned things stay."))
        tip.action_button.click()
        self.assertEqual(len(action), 1)
        tip.close_button.click()
        self.assertFalse(tip.isVisible())

        ran = []
        menu = fluent.MenuFlyout(window)
        open_ = menu.add_item("Open", lambda: ran.append("open"), icon="document-open", shortcut="Ctrl+O")
        check = menu.add_item("Hidden files", checkable=True, checked=True)
        off = menu.add_item("Off", enabled=False)
        sub = menu.add_submenu("Sort by")
        sub.add_item("Name", lambda: ran.append("name"))
        self.assertIsInstance(sub, fluent.MenuFlyout)
        self.assertEqual(open_.shortcut().toString(), "Ctrl+O")
        self.assertTrue(check.isChecked())
        self.assertFalse(off.isEnabled())
        open_.trigger()
        sub.actions()[0].trigger()
        self.assertEqual(ran, ["open", "name"])
        menu.show_at(self.behind)
        testing.process()
        self.assertTrue(menu.isVisible())
        self.assertEqual(menu.pos().y(), self.behind.mapToGlobal(QPoint(0, self.behind.height() + 4)).y())
        menu.hide()


if __name__ == "__main__":
    unittest.main()
