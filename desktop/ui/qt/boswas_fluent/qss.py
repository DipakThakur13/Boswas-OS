"""The application style sheet, generated from the tokens (standard library only, no Qt).

``build(theme, image_dir)`` returns the Qt style sheet that makes the
standard widgets look Fluent. The text below is a template: every value
that comes from the design system is written as ``@name`` and replaced by
``values(theme, image_dir)``. An unknown ``@name`` is an error, so the
sheet cannot ship with a placeholder left in it.

Placeholders

* colours: every semantic role of tokens.json (``@accent``, ``@text-secondary``, ...),
  the derived colours of ``tokens.DERIVED_COLOURS`` (``@card``, ``@flyout``, ...) and
  the translucent layers (``@fill-control``, ``@fill-subtle-hover``, ``@stroke-control``,
  ``@stroke-strong``, ``@text-disabled``, ...); ``@fill-subtle-hover-twice`` is the selection layer
  at the opacity that gives the same result when Qt paints it twice (table cells), and, painted
  once, the faint stripe of alternating rows;
* ``@radius-control``, ``@radius-card``, ``@radius-flyout``, ``@radius-tooltip``;
* ``@sp-1`` ... ``@sp-12``: multiples of the spacing unit; ``@control-h`` the inner
  height of a control (8 units minus its two border pixels);
* ``@font-caption``, ``@font-body``, ``@font-subtitle``; ``@weight-strong``;
* ``@img-<name>``: ``url(...)`` of a generated image (``assets.sources``).

The fixed numbers left in the template are stroke widths (1 px outlines,
the 2 px accent line of a focused text box, the 3 px selection pill).
"""

from __future__ import annotations

import re

from . import assets
from .tokens import DERIVED_COLOURS, FILLS, ROLES, STROKES, Theme, TokenError, css_rgba

_PLACEHOLDER = re.compile(r"@([a-z][a-z0-9-]*)")

TEMPLATE = r"""
/* ---- base ------------------------------------------------------------------------------------------ */
QToolTip {
    background: @tooltip;
    color: @text;
    border: 1px solid @stroke-surface;
    border-radius: @radius-tooltip;
    padding: @sp-1 @sp-2;
    font-size: @font-caption;
}
QLabel[tone="secondary"] { color: @text-secondary; }
QLabel[tone="muted"] { color: @text-muted; }
QLabel[tone="accent"] { color: @accent-text; }
QLabel[tone="success"] { color: @success-text; }
QLabel[tone="warning"] { color: @warning-text; }
QLabel[tone="danger"] { color: @danger-text; }
QLabel[tone="info"] { color: @info-text; }
QLabel[tone="on-accent"] { color: @on-accent; }
QLabel:disabled { color: @text-disabled; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QAbstractScrollArea::corner { background: transparent; border: none; }
QSizeGrip { background: transparent; width: @sp-3; height: @sp-3; }
QFrame[frameShape="4"], QFrame[frameShape="5"] { color: @stroke-divider; background: transparent; }

/* ---- buttons --------------------------------------------------------------------------------------- */
QPushButton {
    background: @fill-control;
    color: @text;
    border: 1px solid @stroke-control;
    border-radius: @radius-control;
    padding: 0 @sp-3;
    min-height: @control-h;
    outline: none;
}
QPushButton:hover { background: @fill-control-hover; }
QPushButton:pressed { background: @fill-control-pressed; color: @text-secondary; }
QPushButton:disabled { background: @fill-disabled; color: @text-disabled; }
QPushButton::menu-indicator {
    image: @img-chevron-down;
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: @sp-2;
    width: @sp-3;
    height: @sp-3;
}
QPushButton[accent="true"], QPushButton:default, QPushButton:checked {
    background: @accent;
    color: @on-accent;
    border: 1px solid @accent;
}
QPushButton[accent="true"]:hover, QPushButton:default:hover, QPushButton:checked:hover {
    background: @accent-fill-hover;
    border-color: @accent-fill-hover;
}
QPushButton[accent="true"]:pressed, QPushButton:default:pressed, QPushButton:checked:pressed {
    background: @accent-fill-pressed;
    border-color: @accent-fill-pressed;
    color: @on-accent;
}
QPushButton[accent="true"]:disabled, QPushButton:default:disabled, QPushButton:checked:disabled {
    background: @accent-disabled;
    border-color: transparent;
    color: @text-disabled;
}
QPushButton[subtle="true"], QPushButton:flat { background: transparent; border-color: transparent; }
QPushButton[subtle="true"]:hover, QPushButton:flat:hover { background: @fill-subtle-hover; }
QPushButton[subtle="true"]:pressed, QPushButton:flat:pressed { background: @fill-subtle-pressed; }
QPushButton[subtle="true"]:disabled, QPushButton:flat:disabled { background: transparent; }
QPushButton[hyperlink="true"] { background: transparent; border-color: transparent; color: @accent-text; }
QPushButton[hyperlink="true"]:hover { background: @fill-subtle-hover; color: @accent-text-strong; }
QPushButton[hyperlink="true"]:pressed { background: @fill-subtle-pressed; color: @accent-text-strong; }
QPushButton[hyperlink="true"]:disabled { background: transparent; color: @text-disabled; }

QToolButton {
    background: transparent;
    color: @text;
    border: 1px solid transparent;
    border-radius: @radius-control;
    padding: @sp-1;
    min-height: 22px;
    min-width: 22px;
    outline: none;
}
QToolButton:hover { background: @fill-subtle-hover; }
QToolButton:pressed, QToolButton:open { background: @fill-subtle-pressed; color: @text-secondary; }
QToolButton:checked { background: @fill-control-hover; border-color: @stroke-control; }
QToolButton:disabled { color: @text-disabled; background: transparent; }
QToolButton[accent="true"] { background: @accent; color: @on-accent; }
QToolButton[accent="true"]:hover { background: @accent-fill-hover; }
QToolButton[accent="true"]:pressed { background: @accent-fill-pressed; color: @on-accent; }
QToolButton[popupMode="1"] { padding-right: 20px; }
QToolButton::menu-button {
    border: none;
    border-top-right-radius: @radius-control;
    border-bottom-right-radius: @radius-control;
    width: 18px;
}
QToolButton::menu-button:hover { background: @fill-subtle-hover; }
QToolButton::menu-arrow { image: @img-chevron-down-small; width: @sp-2; height: @sp-2; }
QToolButton::menu-arrow:disabled { image: @img-chevron-down-small-disabled; }
QToolButton::menu-indicator {
    image: @img-chevron-down-small;
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 3px;
    width: @sp-2;
    height: @sp-2;
}
QToolButton[popupMode="2"] { padding-right: 16px; }
QToolButton[noArrow="true"] { padding: @sp-1; }
QToolButton[noArrow="true"]::menu-indicator { image: none; width: 0; }

QDialogButtonBox { button-layout: 0; dialogbuttonbox-buttons-have-icons: 0; }
QDialogButtonBox QPushButton { min-width: 88px; }
QCommandLinkButton {
    background: @fill-control;
    color: @text;
    border: 1px solid @stroke-control;
    border-radius: @radius-control;
    padding: @sp-2 @sp-3;
}
QCommandLinkButton:hover { background: @fill-control-hover; }
QCommandLinkButton:pressed { background: @fill-control-pressed; }

/* ---- text input ------------------------------------------------------------------------------------ */
QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser, QAbstractSpinBox, QComboBox:editable {
    background: @fill-control;
    color: @text;
    border: 1px solid @stroke-control;
    border-bottom: 1px solid @stroke-strong;
    border-radius: @radius-control;
    selection-background-color: @accent;
    selection-color: @on-accent;
}
QLineEdit, QAbstractSpinBox { padding: 0 10px 1px 10px; min-height: 29px; }
QTextEdit, QPlainTextEdit, QTextBrowser { padding: @sp-1 6px 5px 6px; }
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover, QTextBrowser:hover, QAbstractSpinBox:hover,
QComboBox:editable:hover {
    background: @fill-control-hover;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QAbstractSpinBox:focus, QComboBox:editable:focus {
    background: @input-active;
    border-bottom: 2px solid @accent;
}
QLineEdit:focus, QAbstractSpinBox:focus { padding-bottom: 0; }
QTextEdit:focus, QPlainTextEdit:focus { padding-bottom: @sp-1; }
QLineEdit:read-only, QTextEdit:read-only, QPlainTextEdit:read-only, QTextBrowser {
    border-bottom: 1px solid @stroke-control;
}
QLineEdit:read-only:focus, QTextEdit:read-only:focus, QPlainTextEdit:read-only:focus, QTextBrowser:focus {
    background: @fill-control;
    border-bottom: 1px solid @stroke-strong;
}
QLineEdit:read-only:focus { padding-bottom: 1px; }
QTextEdit:read-only:focus, QPlainTextEdit:read-only:focus { padding-bottom: 5px; }
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QTextBrowser:disabled, QAbstractSpinBox:disabled,
QComboBox:editable:disabled {
    background: @fill-disabled;
    color: @text-disabled;
    border-bottom: 1px solid @stroke-control;
}
QLineEdit[frame="false"], QLineEdit[frame="false"]:hover, QLineEdit[frame="false"]:focus {
    background: transparent;
    border: none;
    padding: 0;
}
QLineEdit QToolButton { padding: 2px; min-width: 20px; min-height: 20px; }

/* ---- spin boxes and date/time editors ---------------------------------------------------------------- */
/* Qt adds the box of a spin box twice when it measures it (once for the embedded editor), and puts the
   editor at the left edge whatever the padding: hence the smaller min-height here, and the text margin
   that polish.py gives the editor. */
QAbstractSpinBox { padding: 0 60px 1px 0; min-height: 26px; }
QAbstractSpinBox:focus { padding-bottom: 0; }
QAbstractSpinBox[buttonSymbols="2"] { padding-right: 10px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: center right;
    width: 26px;
    height: 24px;
    border: none;
    border-radius: @radius-control;
    background: transparent;
}
QAbstractSpinBox::up-button { right: 31px; }
QAbstractSpinBox::down-button { right: @sp-1; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: @fill-subtle-hover; }
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed { background: @fill-subtle-pressed; }
QAbstractSpinBox::up-arrow { image: @img-chevron-up; width: @sp-3; height: @sp-3; }
QAbstractSpinBox::down-arrow { image: @img-chevron-down; width: @sp-3; height: @sp-3; }
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off { image: @img-chevron-up-disabled; }
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off { image: @img-chevron-down-disabled; }
QDateTimeEdit[calendarPopup="true"] { padding: 0 @sp-8 1px 10px; }
QDateTimeEdit::drop-down {
    subcontrol-origin: border;
    subcontrol-position: center right;
    right: @sp-1;
    width: 26px;
    height: 24px;
    border: none;
    border-radius: @radius-control;
}
QDateTimeEdit::drop-down:hover { background: @fill-subtle-hover; }
QDateTimeEdit::down-arrow { image: @img-chevron-down; width: @sp-3; height: @sp-3; }

/* ---- combo box ------------------------------------------------------------------------------------- */
QComboBox {
    background: @fill-control;
    color: @text;
    border: 1px solid @stroke-control;
    border-radius: @radius-control;
    padding: 0 @sp-2 0 11px;
    min-height: @control-h;
    combobox-popup: 0;
    outline: none;
}
QComboBox:hover { background: @fill-control-hover; }
QComboBox:on, QComboBox:pressed { background: @fill-control-pressed; color: @text-secondary; }
QComboBox:disabled { background: @fill-disabled; color: @text-disabled; }
QComboBox:editable { padding: 0 @sp-2 1px 10px; min-height: 29px; }
QComboBox:editable:focus { padding-bottom: 0; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: @sp-8;
    border: none;
    background: transparent;
}
QComboBox::down-arrow { image: @img-chevron-down; width: @sp-3; height: @sp-3; }
QComboBox::down-arrow:disabled { image: @img-chevron-down-disabled; }
QComboBox QLineEdit, QComboBox QLineEdit:hover, QComboBox QLineEdit:focus {
    background: transparent;
    border: none;
    padding: 0;
    min-height: 0;
}
QComboBoxPrivateContainer { background: transparent; border: none; }
QComboBox QAbstractItemView {
    background: @flyout;
    color: @text;
    border: 1px solid @stroke-surface;
    border-radius: @radius-flyout;
    padding: 3px 0;
    outline: none;
    selection-background-color: transparent;
    selection-color: @text;
}
QComboBox QAbstractItemView::item {
    min-height: @sp-8;
    padding: 0 @sp-2;
    margin: 1px @sp-1;
    border-radius: @radius-control;
    color: @text;
}
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {
    background: @fill-subtle-hover;
    color: @text;
}
QComboBox QAbstractItemView::item:disabled { color: @text-disabled; }

/* ---- check box, radio button ----------------------------------------------------------------------- */
QCheckBox, QRadioButton { color: @text; spacing: @sp-2; background: transparent; min-height: 22px; outline: none; }
QCheckBox:disabled, QRadioButton:disabled { color: @text-disabled; }
QCheckBox::indicator, QGroupBox::indicator, QAbstractItemView::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid @stroke-strong;
    border-radius: @radius-control;
    background: @fill-control;
}
QCheckBox::indicator:hover, QGroupBox::indicator:hover { background: @fill-control-hover; }
QCheckBox::indicator:pressed, QGroupBox::indicator:pressed {
    background: @fill-control-pressed;
    border-color: @stroke-control;
}
QCheckBox::indicator:checked, QGroupBox::indicator:checked, QAbstractItemView::indicator:checked {
    background: @accent;
    border-color: @accent;
    image: @img-check-on-accent;
}
QCheckBox::indicator:indeterminate, QAbstractItemView::indicator:indeterminate {
    background: @accent;
    border-color: @accent;
    image: @img-dash-on-accent;
}
QCheckBox::indicator:checked:hover, QCheckBox::indicator:indeterminate:hover, QGroupBox::indicator:checked:hover {
    background: @accent-fill-hover;
    border-color: @accent-fill-hover;
}
QCheckBox::indicator:checked:pressed, QCheckBox::indicator:indeterminate:pressed,
QGroupBox::indicator:checked:pressed {
    background: @accent-fill-pressed;
    border-color: @accent-fill-pressed;
}
QCheckBox::indicator:disabled, QGroupBox::indicator:disabled, QAbstractItemView::indicator:disabled {
    background: transparent;
    border-color: @text-disabled;
}
QCheckBox::indicator:checked:disabled, QCheckBox::indicator:indeterminate:disabled,
QGroupBox::indicator:checked:disabled, QAbstractItemView::indicator:checked:disabled {
    background: @accent-disabled;
    border-color: transparent;
}
QCheckBox::indicator:checked:disabled, QGroupBox::indicator:checked:disabled,
QAbstractItemView::indicator:checked:disabled {
    image: @img-check-disabled;
}
QCheckBox::indicator:indeterminate:disabled { image: @img-dash-disabled; }
QRadioButton::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid @stroke-strong;
    border-radius: 10px;
    background: @fill-control;
}
QRadioButton::indicator:hover { background: @fill-control-hover; }
QRadioButton::indicator:pressed { background: @fill-control-pressed; border-color: @stroke-control; }
QRadioButton::indicator:checked { background: @accent; border-color: @accent; image: @img-radio-dot; }
QRadioButton::indicator:checked:hover {
    background: @accent-fill-hover;
    border-color: @accent-fill-hover;
    image: @img-radio-dot-hover;
}
QRadioButton::indicator:checked:pressed {
    background: @accent-fill-pressed;
    border-color: @accent-fill-pressed;
    image: @img-radio-dot-pressed;
}
QRadioButton::indicator:disabled { background: transparent; border-color: @text-disabled; }
QRadioButton::indicator:checked:disabled {
    background: @accent-disabled;
    border-color: transparent;
    image: @img-radio-dot;
}

/* ---- slider ---------------------------------------------------------------------------------------- */
QSlider { background: transparent; }
QSlider:horizontal { min-height: 24px; }
QSlider:vertical { min-width: 24px; }
QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: @stroke-strong; }
QSlider::groove:vertical { width: 4px; border-radius: 2px; background: @stroke-strong; }
QSlider::sub-page:horizontal, QSlider::add-page:vertical { background: @accent; border-radius: 2px; }
QSlider::add-page:horizontal, QSlider::sub-page:vertical { background: transparent; border-radius: 2px; }
QSlider::sub-page:horizontal:disabled, QSlider::add-page:vertical:disabled { background: @text-disabled; }
QSlider::groove:horizontal:disabled, QSlider::groove:vertical:disabled { background: @text-disabled; }
QSlider::handle {
    width: 18px;
    height: 18px;
    border: 1px solid @stroke-surface;
    border-radius: 10px;
    background: @control-solid;
    image: @img-thumb-dot;
}
QSlider::handle:horizontal { margin: -8px 0; }
QSlider::handle:vertical { margin: 0 -8px; }
QSlider::handle:hover { image: @img-thumb-dot-hover; }
QSlider::handle:pressed { image: @img-thumb-dot-pressed; }
QSlider::handle:disabled { image: @img-thumb-dot-disabled; }

/* ---- progress bar ---------------------------------------------------------------------------------- */
QProgressBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 transparent, stop:0.34 transparent,
        stop:0.35 @stroke-strong, stop:0.65 @stroke-strong, stop:0.66 transparent, stop:1 transparent);
    border: none;
    min-height: 3px;
    max-height: 3px;
    color: @text;
    text-align: center;
}
QProgressBar:vertical {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 transparent, stop:0.34 transparent,
        stop:0.35 @stroke-strong, stop:0.65 @stroke-strong, stop:0.66 transparent, stop:1 transparent);
    min-height: 0;
    max-height: 16777215px;
    min-width: 3px;
    max-width: 3px;
}
QProgressBar::chunk { background: @accent; border-radius: 1px; }
QProgressBar[status="paused"]::chunk { background: @warning; }
QProgressBar[status="error"]::chunk { background: @danger; }
QProgressBar::chunk:disabled { background: @text-disabled; }

/* ---- scroll bars ----------------------------------------------------------------------------------- */
QScrollBar { background: transparent; border: none; }
QScrollBar:vertical { width: @sp-3; margin: 0; }
QScrollBar:horizontal { height: @sp-3; margin: 0; }
QScrollBar::handle:vertical {
    background: @stroke-strong;
    min-height: @sp-8;
    border-radius: 1px;
    margin: 3px 5px 3px 5px;
}
QScrollBar::handle:horizontal {
    background: @stroke-strong;
    min-width: @sp-8;
    border-radius: 1px;
    margin: 5px 3px 5px 3px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {
    margin: 3px 3px 3px 3px;
    border-radius: 3px;
}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {
    margin: 3px 3px 3px 3px;
    border-radius: 3px;
}
QScrollBar::handle:pressed { background: @text-muted; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ---- menus ----------------------------------------------------------------------------------------- */
QMenu {
    background: @flyout;
    color: @text;
    border: 1px solid @stroke-surface;
    border-radius: @radius-flyout;
    padding: 3px 0;
    menu-scrollable: 1;
}
QMenu::item {
    background: transparent;
    color: @text;
    padding: 6px @sp-6 6px @sp-3;
    margin: 1px @sp-1;
    border-radius: @radius-control;
    min-width: 96px;
}
QMenu::item:selected { background: @fill-subtle-hover; }
QMenu::item:pressed { background: @fill-subtle-pressed; }
QMenu::item:disabled { color: @text-disabled; background: transparent; }
QMenu::icon { padding-left: @sp-2; }
QMenu::separator { height: 1px; background: @stroke-divider; margin: 3px 0; }
QMenu::indicator { width: @sp-3; height: @sp-3; left: 10px; }
QMenu::indicator:non-exclusive:checked { image: @img-check; }
QMenu::indicator:non-exclusive:checked:disabled { image: @img-check-disabled; }
QMenu::indicator:exclusive:checked { image: @img-menu-dot; }
QMenu::indicator:exclusive:checked:disabled { image: @img-menu-dot-disabled; }
QMenu::right-arrow { image: @img-chevron-right; width: @sp-3; height: @sp-3; right: @sp-2; }
QMenu::right-arrow:disabled { image: @img-chevron-right-disabled; }
QMenu::scroller { height: @sp-4; background: transparent; }
QMenu::up-arrow { image: @img-chevron-up; width: @sp-3; height: @sp-3; }
QMenu::down-arrow { image: @img-chevron-down; width: @sp-3; height: @sp-3; }
QMenu::tearoff { height: 0; }
QMenuBar { background: transparent; color: @text; border: none; padding: 2px @sp-1; }
QMenuBar::item {
    background: transparent;
    color: @text;
    padding: 5px 10px;
    border-radius: @radius-control;
    margin: 1px;
}
QMenuBar::item:selected { background: @fill-subtle-hover; }
QMenuBar::item:pressed { background: @fill-subtle-pressed; color: @text-secondary; }
QMenuBar::item:disabled { color: @text-disabled; }

/* ---- tabs ------------------------------------------------------------------------------------------ */
QTabWidget::pane { border: none; border-top: 1px solid @stroke-divider; background: transparent; top: -1px; }
QTabWidget::pane:bottom { border: none; border-bottom: 1px solid @stroke-divider; }
QTabWidget::pane:left { border: none; border-left: 1px solid @stroke-divider; }
QTabWidget::pane:right { border: none; border-right: 1px solid @stroke-divider; }
QTabWidget::tab-bar { left: 0; }
QTabBar { background: transparent; border: none; outline: none; qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: @text-secondary;
    border: none;
    border-radius: @radius-control;
    padding: @sp-2 @sp-3 10px @sp-3;
    margin: 0 2px 0 0;
    min-height: @sp-5;
}
QTabBar::tab:hover { color: @text; background: @fill-subtle-hover; }
QTabBar::tab:selected { color: @text; background: transparent @img-pill-horizontal no-repeat bottom center; }
QTabBar::tab:selected:hover { background: @fill-subtle-hover @img-pill-horizontal no-repeat bottom center; }
QTabBar::tab:disabled { color: @text-disabled; }
QTabBar::tab:left, QTabBar::tab:right { padding: @sp-2 @sp-3; margin: 0 0 2px 0; }
QTabBar::tab:left:selected { background: @fill-subtle-hover @img-pill-vertical no-repeat left center; }
QTabBar::tab:right:selected { background: @fill-subtle-hover @img-pill-vertical no-repeat right center; }
QTabBar::tab:bottom { padding: 10px @sp-3 @sp-2 @sp-3; }
QTabBar::tab:bottom:selected { background: transparent @img-pill-horizontal no-repeat top center; }
QTabBar::close-button {
    image: @img-close;
    subcontrol-position: right;
    border-radius: @radius-control;
    padding: 2px;
    margin-bottom: 2px;
}
QTabBar::close-button:hover { image: @img-close-hover; background: @fill-subtle-hover; }
QTabBar::tear { width: 0; border: none; }
QTabBar QToolButton { background: @background; border-radius: @radius-control; padding: 2px; margin: 2px 0; }
QTabBar QToolButton::left-arrow { image: @img-chevron-left; }
QTabBar QToolButton::right-arrow { image: @img-chevron-right; }
QTabBar QToolButton::up-arrow { image: @img-chevron-up; }
QTabBar QToolButton::down-arrow { image: @img-chevron-down; }
QTabBar QToolButton::left-arrow:disabled { image: @img-chevron-left-disabled; }
QTabBar QToolButton::right-arrow:disabled { image: @img-chevron-right-disabled; }

/* ---- item views ------------------------------------------------------------------------------------ */
QListView, QTreeView, QTableView, QColumnView, QUndoView {
    background: transparent;
    color: @text;
    border: none;
    outline: none;
    selection-background-color: transparent;
    selection-color: @text;
    alternate-background-color: @fill-subtle-hover-twice;
}
QListView[frameShape="6"], QTreeView[frameShape="6"], QTableView[frameShape="6"] {
    background: @card;
    border: 1px solid @stroke-surface;
    border-radius: @radius-card;
}
QListView::item {
    min-height: @sp-8;
    padding: 0 @sp-2;
    margin: 2px @sp-1;
    border-radius: @radius-control;
    color: @text;
}
QListView::item:hover { background: @fill-subtle-hover; }
QListView::item:selected { background: @fill-subtle-hover @img-pill-vertical no-repeat left center; color: @text; }
QListView::item:selected:hover { background: @fill-subtle-pressed @img-pill-vertical no-repeat left center; }
QListView::item:disabled { color: @text-disabled; }
QListView::item:selected:disabled {
    background: @fill-disabled @img-pill-vertical-disabled no-repeat left center;
}
QListView[viewMode="1"]::item { min-height: 0; padding: @sp-2; margin: 2px; }
QListView[viewMode="1"]::item:selected { background: @fill-control-hover; border: 1px solid @accent; }
QTreeView { show-decoration-selected: 1; }
QTreeView::item, QColumnView::item { min-height: @sp-8; padding: 0 @sp-1; color: @text; border: none; }
QTreeView::item:hover, QColumnView::item:hover { background: @fill-subtle-hover; }
QTreeView::item:selected, QColumnView::item:selected { background: @fill-subtle-hover; color: @text; }
QTreeView::item:disabled { color: @text-disabled; }
QTreeView::item:first, QTreeView::item:only-one {
    border-top-left-radius: @radius-control;
    border-bottom-left-radius: @radius-control;
}
QTreeView::item:last, QTreeView::item:only-one {
    border-top-right-radius: @radius-control;
    border-bottom-right-radius: @radius-control;
}
QTreeView::item:selected:first, QTreeView::item:selected:only-one {
    background: @fill-subtle-hover @img-pill-vertical no-repeat left center;
}
QTreeView::branch { background: transparent; }
QTreeView::branch:has-children:closed { image: @img-chevron-right; }
QTreeView::branch:has-children:open { image: @img-chevron-down; }
QTreeView::branch:has-children:closed:hover { image: @img-chevron-right-hover; }
QTreeView::branch:has-children:open:hover { image: @img-chevron-down-hover; }
QTableView { gridline-color: @stroke-divider; }
QTableView::item { padding: 0 @sp-2; color: @text; border: none; }
QTableView::item:hover { background: @fill-subtle-hover-twice; }
QTableView[selectionBehavior="1"]::item:hover { background: transparent; }
QTableView::item:selected, QTableView[selectionBehavior="1"]::item:selected {
    background: @fill-subtle-hover-twice;
    color: @text;
}
QTableView::item:selected:first, QTableView::item:selected:only-one,
QTableView[selectionBehavior="1"]::item:selected:first, QTableView[selectionBehavior="1"]::item:selected:only-one {
    background: @fill-subtle-hover-twice @img-pill-vertical no-repeat left center;
    border-top-left-radius: @radius-control;
    border-bottom-left-radius: @radius-control;
}
QTableView::item:selected:last, QTableView::item:selected:only-one {
    border-top-right-radius: @radius-control;
    border-bottom-right-radius: @radius-control;
}
QTableView::item:disabled { color: @text-disabled; }
QTableView QTableCornerButton::section { background: transparent; border: none; }
QHeaderView { background: transparent; border: none; }
QHeaderView::section {
    background: transparent;
    color: @text-secondary;
    border: none;
    border-bottom: 1px solid @stroke-divider;
    padding: 6px @sp-2;
    font-size: @font-caption;
}
QHeaderView::section:vertical { border-bottom: none; border-right: 1px solid @stroke-divider; }
QHeaderView::section:hover { color: @text; background: @fill-subtle-hover; }
QHeaderView::section:pressed { background: @fill-subtle-pressed; }
QHeaderView::section:checked { color: @text; }
QHeaderView::up-arrow {
    image: @img-chevron-up-small;
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 6px;
    width: @sp-2;
    height: @sp-2;
}
QHeaderView::down-arrow {
    image: @img-chevron-down-small;
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 6px;
    width: @sp-2;
    height: @sp-2;
}

/* ---- containers ------------------------------------------------------------------------------------ */
QGroupBox {
    background: @card;
    color: @text;
    border: 1px solid @stroke-surface;
    border-radius: @radius-card;
    margin-top: 30px;
    padding: @sp-3 @sp-3 @sp-3 @sp-3;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 2px;
    top: @sp-1;
    padding: 0;
    color: @text;
}
QGroupBox::title:disabled { color: @text-disabled; }
QGroupBox::indicator { margin-right: @sp-1; }
QGroupBox:flat { background: transparent; border: none; border-top: 1px solid @stroke-divider; border-radius: 0; }
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 5px; image: none; }
QSplitter::handle:vertical { height: 5px; image: none; }
QSplitter::handle:hover { background: @fill-subtle-hover; }
QSplitter::handle:pressed { background: @accent; }
QStatusBar { background: transparent; color: @text-secondary; border-top: 1px solid @stroke-divider; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: @text-secondary; font-size: @font-caption; }
QToolBar { background: transparent; border: none; spacing: @sp-1; padding: @sp-1; }
QToolBar::separator:horizontal { width: 1px; background: @stroke-divider; margin: @sp-1 @sp-1; }
QToolBar::separator:vertical { height: 1px; background: @stroke-divider; margin: @sp-1 @sp-1; }
QToolBar::handle:horizontal { image: @img-grip; width: @sp-3; }
QToolBar::handle:vertical { image: @img-grip; height: @sp-3; }
QToolBarExtension, QToolBar QToolButton#qt_toolbar_ext_button { qproperty-icon: none; image: @img-more; }
QDockWidget {
    color: @text;
    font-weight: @weight-strong;
    titlebar-close-icon: @img-close;
    titlebar-normal-icon: @img-restore;
}
QDockWidget::title { background: transparent; padding: @sp-2 @sp-3; text-align: left; }
QDockWidget::close-button, QDockWidget::float-button {
    background: transparent;
    border: none;
    border-radius: @radius-control;
    padding: 2px;
    icon-size: @sp-3;
}
QDockWidget::close-button:hover, QDockWidget::float-button:hover { background: @fill-subtle-hover; }
QDockWidget::close-button:pressed, QDockWidget::float-button:pressed { background: @fill-subtle-pressed; }
QMainWindow::separator { background: transparent; width: 5px; height: 5px; }
QMainWindow::separator:hover { background: @fill-subtle-hover; }
QMdiArea { background: @chrome; }

/* ---- dialogs --------------------------------------------------------------------------------------- */
QMessageBox QLabel { color: @text; }
QMessageBox QLabel#qt_msgbox_label { font-size: @font-body; }
QMessageBox QLabel#qt_msgbox_informativelabel { color: @text-secondary; }
QFileDialog QListView, QFileDialog QTreeView {
    background: @card;
    border: 1px solid @stroke-surface;
    border-radius: @radius-card;
}
QFileDialog QListView#sidebar { background: transparent; border: none; }
QFileDialog QLabel { color: @text; }
QInputDialog QLabel, QColorDialog QLabel, QFontDialog QLabel, QProgressDialog QLabel, QWizard QLabel { color: @text; }
QProgressDialog QProgressBar { min-width: 240px; }

/* ---- calendar -------------------------------------------------------------------------------------- */
QCalendarWidget QWidget#qt_calendar_navigationbar { background: transparent; min-height: @sp-10; }
QCalendarWidget QToolButton {
    color: @text;
    font-weight: @weight-strong;
    padding: @sp-1 @sp-2;
    margin: 2px;
    icon-size: @sp-3;
}
QCalendarWidget QToolButton::menu-indicator { image: none; width: 0; }
QCalendarWidget QToolButton#qt_calendar_prevmonth { qproperty-icon: none; image: @img-chevron-left; min-width: 24px; }
QCalendarWidget QToolButton#qt_calendar_nextmonth {
    qproperty-icon: none;
    image: @img-chevron-right;
    min-width: 24px;
}
QCalendarWidget QSpinBox { min-height: 20px; }
QCalendarWidget QAbstractItemView {
    background: transparent;
    color: @text;
    selection-background-color: @accent;
    selection-color: @on-accent;
    alternate-background-color: transparent;
    outline: none;
}
QCalendarWidget QAbstractItemView:disabled { color: @text-disabled; }
QCalendarWidget QTableView::item { padding: 0; }
"""


def values(theme: Theme, image_dir: str) -> dict[str, str]:
    """Every placeholder of the template and its value for ``theme``."""
    table: dict[str, str] = {}
    for role in ROLES + DERIVED_COLOURS:
        table[role] = theme.color(role)
    for name in FILLS:
        table[f"fill-{name}"] = theme.css(f"fill-{name}")
    for name in STROKES:
        table[f"stroke-{name}"] = theme.css(f"stroke-{name}")
    for name in ("stroke-strong", "text-disabled", "accent-disabled", "accent-subtle"):
        table[name] = theme.css(name)
    # QTableView paints the background of a cell twice (the row, then the item): a layer at this
    # opacity, painted twice, looks like fill-subtle-hover painted once.
    colour, alpha = theme.rgba("fill-subtle-hover")
    table["fill-subtle-hover-twice"] = css_rgba(colour, 1 - (1 - alpha) ** 0.5)
    for name in ("control", "card", "flyout", "tooltip"):
        table[f"radius-{name}"] = f"{theme.radius(name)}px"
    for steps in range(1, 13):
        table[f"sp-{steps}"] = f"{theme.spacing(steps)}px"
    table["control-h"] = f"{theme.spacing(8) - 2}px"
    for name in ("caption", "body", "subtitle"):
        table[f"font-{name}"] = f"{theme.type(name).size}px"
    table["weight-strong"] = str(theme.type("body-strong").weight)
    directory = image_dir.rstrip("/")
    if '"' in directory or "\\" in directory or "\n" in directory:
        raise TokenError(f"the image directory cannot be named in a style sheet: {directory!r}")
    for name in assets.sources(theme):
        table[f"img-{name}"] = f'url("{directory}/{name}.png")'
    return table


def placeholders(template: str = TEMPLATE) -> set[str]:
    """The names of the placeholders the template uses."""
    return set(_PLACEHOLDER.findall(template))


def render(template: str, table: dict[str, str]) -> str:
    """Replace every ``@name`` of ``template``; an unknown name raises TokenError."""
    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name not in table:
            raise TokenError(f"the style sheet uses @{name}, which no token provides")
        return table[name]
    return _PLACEHOLDER.sub(substitute, template)


def build(theme: Theme, image_dir: str) -> str:
    """The application style sheet for ``theme``; images are named inside ``image_dir``."""
    return render(TEMPLATE, values(theme, image_dir))
