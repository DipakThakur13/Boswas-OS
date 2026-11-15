"""Text entry with a job: SearchBox, PasswordBox, NumberBox."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDoubleSpinBox, QLineEdit, QWidget

from .. import icons


class SearchBox(QLineEdit):
    """A text box for a search.

    ``query_submitted(str)`` is emitted when the user presses Enter or the
    search button; ``textChanged`` (QLineEdit) while they type. A clear
    button appears when there is text; Escape clears too.
    """

    query_submitted = Signal(str)
    cleared = Signal()

    def __init__(self, parent: QWidget | None = None, *, placeholder: str = "Search") -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setAccessibleName(placeholder)
        self.search_action = QAction(icons.icon("system-search-symbolic", color="text-secondary"), "Search", self)
        self.clear_action = QAction(icons.glyph_icon("close"), "Clear", self)
        self.addAction(self.search_action, QLineEdit.ActionPosition.TrailingPosition)
        self.addAction(self.clear_action, QLineEdit.ActionPosition.TrailingPosition)
        self.clear_action.setVisible(False)
        self.search_action.triggered.connect(self.submit)
        self.clear_action.triggered.connect(self.clear_query)
        self.returnPressed.connect(self.submit)
        self.textChanged.connect(lambda text: self.clear_action.setVisible(bool(text)))

    def query(self) -> str:
        """The text, without surrounding white space."""
        return self.text().strip()

    def submit(self) -> None:
        self.query_submitted.emit(self.query())

    def clear_query(self) -> None:
        if self.text():
            self.clear()
            self.cleared.emit()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear_query()
            return
        super().keyPressEvent(event)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(max(hint.width(), 120), hint.height())


class PasswordBox(QLineEdit):
    """A text box that hides what is typed, with a button that reveals it while it is switched on."""

    def __init__(self, parent: QWidget | None = None, *, placeholder: str = "", reveal_button: bool = True):
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setInputMethodHints(Qt.InputMethodHint.ImhHiddenText | Qt.InputMethodHint.ImhSensitiveData
                                 | Qt.InputMethodHint.ImhNoPredictiveText | Qt.InputMethodHint.ImhNoAutoUppercase)
        if placeholder:
            self.setPlaceholderText(placeholder)
        self.setAccessibleName(placeholder or "Password")
        self.reveal_action = QAction(icons.icon("eye-open-symbolic", color="text-secondary"), "Show password", self)
        self.reveal_action.setCheckable(True)
        self.reveal_action.toggled.connect(self._reveal)
        self.addAction(self.reveal_action, QLineEdit.ActionPosition.TrailingPosition)
        self.reveal_action.setVisible(reveal_button)

    def password(self) -> str:
        return self.text()

    def is_revealed(self) -> bool:
        return self.echoMode() == QLineEdit.EchoMode.Normal

    def set_revealed(self, revealed: bool) -> None:
        self.reveal_action.setChecked(revealed)

    def set_reveal_button_visible(self, visible: bool) -> None:
        if not visible:
            self.reveal_action.setChecked(False)
        self.reveal_action.setVisible(visible)

    def _reveal(self, revealed: bool) -> None:
        self.setEchoMode(QLineEdit.EchoMode.Normal if revealed else QLineEdit.EchoMode.Password)
        name = "eye-closed-symbolic" if revealed else "eye-open-symbolic"
        self.reveal_action.setIcon(icons.icon(name, color="text-secondary"))
        self.reveal_action.setText("Hide password" if revealed else "Show password")


class NumberBox(QDoubleSpinBox):
    """A number with inline step buttons (``valueChanged(float)`` of QDoubleSpinBox).

    Whole numbers by default (``decimals=0``; ``int_value()``). The mouse
    wheel changes the value only while the box has the focus, so scrolling
    a page never changes a setting by accident.
    """

    def __init__(self, parent: QWidget | None = None, *, value: float = 0, minimum: float = 0,
                 maximum: float = 100, step: float = 1, decimals: int = 0, suffix: str = "") -> None:
        super().__init__(parent)
        self.setDecimals(decimals)
        self.setRange(minimum, maximum)
        self.setSingleStep(step)
        self.setValue(value)
        if suffix:
            self.setSuffix(suffix)
        self.setAccelerated(True)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def int_value(self) -> int:
        return round(self.value())

    def wheelEvent(self, event) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(max(hint.width(), 124), hint.height())

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(max(hint.width(), 124), hint.height())
