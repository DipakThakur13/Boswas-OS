"""Presentation mode: one page at a time, as large as the screen allows, on black, with nothing around it."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QWidget

import boswas_fluent as fluent

from .document import PdfDocument
from .render import Renderer, Request

_tags = 0


class Presentation(QWidget):
    closed = Signal(int)                      # the page that was showing

    def __init__(self, document: PdfDocument, renderer: Renderer, page: int = 0, rotation: int = 0) -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        global _tags
        _tags += 1
        self._tag = _tags
        self.document, self.renderer = document, renderer
        self.page = max(0, min(page, document.page_count - 1))
        self.rotation = rotation % 4
        self._image = QImage()
        self._image_page = -1
        self._request: Request | None = None
        self.setWindowTitle("Presentation")
        self.setAccessibleName("Presentation")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setCursor(Qt.CursorShape.BlankCursor)
        renderer.rendered.connect(self._rendered)

    def start(self) -> None:
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._ask()

    def _target(self) -> QRectF:
        width, height = self.document.sizes[self.page]
        if self.rotation % 2:
            width, height = height, width
        scale = min(self.width() / width, self.height() / height) if width and height else 1.0
        target = QRectF(0, 0, round(width * scale), round(height * scale))
        target.moveCenter(QRectF(self.rect()).center())
        return QRectF(round(target.left()), round(target.top()), target.width(), target.height())

    def _ask(self) -> None:
        if self.width() <= 1 or self.height() <= 1:
            return
        target = self._target()
        ratio = self.devicePixelRatioF()
        self._request = Request("present", self.page, max(1, round(target.width() * ratio)),
                                max(1, round(target.height() * ratio)), self.rotation, None, self._tag)
        self.renderer.request(self._request, -5)

    def _rendered(self, request: Request, image: QImage) -> None:
        if request == self._request and not image.isNull():
            self._image, self._image_page = image, request.page
            self.update()

    def go_to(self, page: int) -> None:
        page = max(0, min(page, self.document.page_count - 1))
        if page != self.page:
            self.page = page
            self._ask()
            self.update()

    def next(self) -> None:
        self.go_to(self.page + 1)

    def previous(self) -> None:
        self.go_to(self.page - 1)

    def has_image(self) -> bool:
        return self._image_page == self.page and not self._image.isNull()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), fluent.qcolor("black"))
        if self._image.isNull():
            return
        target = self._target()
        if self._image_page != self.page:
            painter.setOpacity(0.35)                    # the page before, dimmed, until the new one is rendered
            size = self._image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(0, 0, size.width(), size.height())
            target.moveCenter(QRectF(self.rect()).center())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self._image)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._ask()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_F5, Qt.Key.Key_Q):
            self.close()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_Return, Qt.Key.Key_Enter,
                     Qt.Key.Key_N) or (key == Qt.Key.Key_Space and not shift):
            self.next()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Backspace, Qt.Key.Key_P) \
                or (key == Qt.Key.Key_Space and shift):
            self.previous()
        elif key == Qt.Key.Key_Home:
            self.go_to(0)
        elif key == Qt.Key.Key_End:
            self.go_to(self.document.page_count - 1)
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.next()
        elif event.button() == Qt.MouseButton.RightButton:
            self.previous()

    def wheelEvent(self, event) -> None:
        if event.angleDelta().y() < 0:
            self.next()
        elif event.angleDelta().y() > 0:
            self.previous()

    def closeEvent(self, event) -> None:
        try:
            self.renderer.rendered.disconnect(self._rendered)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
        self.closed.emit(self.page)
