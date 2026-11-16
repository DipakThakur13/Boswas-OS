"""The preview pane: the selected entry as text or as a picture, or a line that says why not."""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFontDatabase, QImage, QImageReader, QPainter
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import model, names, previewing

MAX_IMAGE_PIXELS = 64_000_000          # larger pictures are not decoded
MAX_IMAGE_SIDE = 2048                  # decoded pictures are scaled down to this for the pane


def decode_image(data: bytes) -> QImage | None:
    """``data`` as a picture no larger than the pane needs; None when Qt cannot read it. Safe off the GUI thread."""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QBuffer.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    reader.setDecideFormatFromContent(True)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > MAX_IMAGE_PIXELS:
        return None
    if max(size.width(), size.height()) > MAX_IMAGE_SIDE:
        reader.setScaledSize(size.scaled(MAX_IMAGE_SIDE, MAX_IMAGE_SIDE, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return None if image.isNull() else image


class _Picture(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._scaled = QImage()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Picture preview")

    def set_image(self, image: QImage) -> None:
        self._image = image
        self._scaled = QImage()
        self.update()

    def image(self) -> QImage:
        return self._image

    def paintEvent(self, _event) -> None:
        if self._image.isNull():
            return
        ratio = self.devicePixelRatioF()
        room = QSize(max(1, int(self.width() * ratio)), max(1, int(self.height() * ratio)))
        wanted = self._image.size().scaled(room, Qt.AspectRatioMode.KeepAspectRatio)
        if wanted.width() > self._image.width():
            wanted = self._image.size()                     # small pictures are not blown up
        if self._scaled.size() != wanted:
            self._scaled = self._image.scaled(wanted, Qt.AspectRatioMode.IgnoreAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
        width, height = round(wanted.width() / ratio), round(wanted.height() / ratio)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRect((self.width() - width) // 2, (self.height() - height) // 2, width, height),
                          self._scaled)


class PreviewPane(QWidget):
    """Shows one entry. The window fills it: ``show_entry`` first, then one of the ``show_*`` methods."""

    password_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(220)
        self.icon = fluent.IconWidget(None, 32)
        self.name = fluent.Label("", "body-strong")
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.info = fluent.Label("", "caption", tone="secondary")
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.name)
        titles.addWidget(self.info)
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(self.icon)
        header.addLayout(titles, 1)

        self.message = fluent.Label("", "body", tone="secondary", wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.unlock = fluent.Button("Enter password")
        self.unlock.clicked.connect(self.password_requested)
        message_page = QWidget()
        message_layout = QVBoxLayout(message_page)
        message_layout.addStretch(1)
        message_layout.addWidget(self.message)
        message_layout.addWidget(self.unlock, 0, Qt.AlignmentFlag.AlignHCenter)
        message_layout.addStretch(2)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName("Text preview")
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed.setPixelSize(fluent.theme().type("caption").size)
        self.text.setFont(fixed)
        self.note = fluent.Label("", "caption", tone="secondary", wrap=True)
        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        text_layout.addWidget(self.text, 1)
        text_layout.addWidget(self.note)

        self.picture = _Picture()
        self.stack = QStackedWidget()
        self.stack.addWidget(message_page)
        self.stack.addWidget(text_page)
        self.stack.addWidget(self.picture)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 12, 12)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(self.stack, 1)
        self.kind = "message"
        self.show_nothing()

    def _elide(self, label: fluent.Label, text: str) -> None:
        label.setToolTip(text)
        label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, max(60, self.width() - 84)))

    def show_entry(self, entry) -> None:
        """The header for ``entry``; the content follows when it has been read."""
        self.icon.set_icon(fluent.icon(model.icon_name(entry), "unknown"))
        self.icon.show()
        self._name, self._info = names.display(entry.base), model.type_text(entry)
        if entry.kind == model.FILE:
            self._info += " · " + model.human_size(entry.size)
        self._elide(self.name, self._name)
        self._elide(self.info, self._info)

    def show_nothing(self, text: str = "Select a file to preview it.") -> None:
        self._name = self._info = ""
        self.name.setText("")
        self.info.setText("")
        self.icon.hide()
        self.show_message(text)

    def show_message(self, text: str, *, unlock: bool = False) -> None:
        self.kind = "message"
        self.message.setText(text)
        self.unlock.setVisible(unlock)
        self.stack.setCurrentIndex(0)

    def show_text(self, text: str, truncated: bool) -> None:
        self.kind = "text"
        self.text.setPlainText(previewing.clean_text(text))
        self.note.setText(f"The first {model.human_size(previewing.TEXT_LIMIT)} are shown." if truncated else "")
        self.note.setVisible(truncated)
        self.stack.setCurrentIndex(1)

    def show_image(self, image: QImage) -> None:
        self.kind = "image"
        self.picture.set_image(image)
        self._elide(self.info, f"{self._info} · {image.width()} × {image.height()} pixels"
                    if max(image.width(), image.height()) < MAX_IMAGE_SIDE else self._info)
        self.stack.setCurrentIndex(2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._name:
            self._elide(self.name, self._name)
            self._elide(self.info, self.info.toolTip() or self._info)
