"""可创建、移动和缩放的矩形选框覆盖层。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .coordinate_mapper import (
    Rect,
    calculate_video_content_rect,
    normalize_video_rect,
    preview_to_video_point,
    video_to_preview_rect,
)


class SelectionOverlay(QWidget):
    selectionChanged = Signal(object)
    selectionsChanged = Signal(object)

    HANDLE_SIZE = 10.0
    MIN_SIZE = 4.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._video_width = 0
        self._video_height = 0
        self._selections: list[Rect] = []
        self._active_index = -1
        self._force_create = False
        self._drag_mode: str | None = None
        self._press_video = (0.0, 0.0)
        self._original: Rect | None = None

    @property
    def selection(self) -> Rect | None:
        if 0 <= self._active_index < len(self._selections):
            return self._selections[self._active_index]
        return None

    @property
    def selections(self) -> tuple[Rect, ...]:
        return tuple(self._selections)

    @property
    def active_index(self) -> int:
        return self._active_index

    def _emit_selection(self) -> None:
        self.selectionChanged.emit(self.selection)
        self.selectionsChanged.emit(self.selections)

    def set_video_size(self, width: int, height: int) -> None:
        self._video_width = width
        self._video_height = height
        if width > 0 and height > 0:
            self._selections = [
                normalize_video_rect(rect, width, height)
                for rect in self._selections
            ]
        self.update()

    def clear_selection(self) -> None:
        self._selections.clear()
        self._active_index = -1
        self._force_create = False
        self._drag_mode = None
        self._emit_selection()
        self.update()

    def begin_reselect(self) -> None:
        self.clear_selection()
        self.begin_add_selection()

    def begin_add_selection(self) -> None:
        self._force_create = True
        self.setCursor(Qt.CursorShape.CrossCursor)

    def delete_active_selection(self) -> None:
        if not (0 <= self._active_index < len(self._selections)):
            return
        del self._selections[self._active_index]
        self._active_index = min(
            self._active_index,
            len(self._selections) - 1,
        )
        self._emit_selection()
        self.update()

    def set_selection(self, rect: Rect | None) -> None:
        self._selections = [] if rect is None else [rect]
        self._active_index = 0 if rect is not None else -1
        self._emit_selection()
        self.update()

    def set_selections(self, rects: list[Rect] | tuple[Rect, ...]) -> None:
        self._selections = list(rects)
        self._active_index = len(self._selections) - 1
        self._emit_selection()
        self.update()

    def _content(self) -> Rect:
        return calculate_video_content_rect(
            self.width(), self.height(), self._video_width, self._video_height
        )

    def _selection_preview(self) -> QRectF:
        selection = self.selection
        if selection is None:
            return QRectF()
        mapped = video_to_preview_rect(
            selection,
            self._content(),
            self._video_width,
            self._video_height,
        )
        return QRectF(mapped.x, mapped.y, mapped.width, mapped.height)

    def _video_point(self, position: QPointF) -> tuple[float, float]:
        return preview_to_video_point(
            position.x(),
            position.y(),
            self._content(),
            self._video_width,
            self._video_height,
        )

    def _inside_content(self, position: QPointF) -> bool:
        c = self._content()
        return c.x <= position.x() <= c.right and c.y <= position.y() <= c.bottom

    def _handle_at(self, position: QPointF) -> str | None:
        rect = self._selection_preview()
        if rect.isNull():
            return None
        x, y = position.x(), position.y()
        left, right, top, bottom = rect.left(), rect.right(), rect.top(), rect.bottom()
        threshold = self.HANDLE_SIZE
        near_l = abs(x - left) <= threshold
        near_r = abs(x - right) <= threshold
        near_t = abs(y - top) <= threshold
        near_b = abs(y - bottom) <= threshold
        within_x = left - threshold <= x <= right + threshold
        within_y = top - threshold <= y <= bottom + threshold
        if near_l and near_t:
            return "top_left"
        if near_r and near_t:
            return "top_right"
        if near_l and near_b:
            return "bottom_left"
        if near_r and near_b:
            return "bottom_right"
        if near_l and within_y:
            return "left"
        if near_r and within_y:
            return "right"
        if near_t and within_x:
            return "top"
        if near_b and within_x:
            return "bottom"
        if rect.contains(position):
            return "move"
        return None

    def _selection_index_at(self, position: QPointF) -> int:
        content = self._content()
        for index in range(len(self._selections) - 1, -1, -1):
            mapped = video_to_preview_rect(
                self._selections[index],
                content,
                self._video_width,
                self._video_height,
            )
            if QRectF(mapped.x, mapped.y, mapped.width, mapped.height).contains(
                position
            ):
                return index
        return -1

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() != Qt.MouseButton.LeftButton
            or self._video_width <= 0
            or not self._inside_content(event.position())
        ):
            return
        self.setFocus()
        self._press_video = self._video_point(event.position())
        self._original = self.selection
        self._drag_mode = None if self._force_create else self._handle_at(
            event.position()
        )
        if self._drag_mode is None and not self._force_create:
            hit_index = self._selection_index_at(event.position())
            if hit_index >= 0:
                self._active_index = hit_index
                self._original = self.selection
                self._drag_mode = "move"
                self._emit_selection()
        if self._drag_mode is None:
            self._drag_mode = "create"
        if self._drag_mode == "create":
            x, y = self._press_video
            self._selections.append(Rect(x, y, 0, 0))
            self._active_index = len(self._selections) - 1
            self._force_create = False
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._drag_mode:
            mode = self._handle_at(event.position())
            cursors = {
                "move": Qt.CursorShape.SizeAllCursor,
                "left": Qt.CursorShape.SizeHorCursor,
                "right": Qt.CursorShape.SizeHorCursor,
                "top": Qt.CursorShape.SizeVerCursor,
                "bottom": Qt.CursorShape.SizeVerCursor,
                "top_left": Qt.CursorShape.SizeFDiagCursor,
                "bottom_right": Qt.CursorShape.SizeFDiagCursor,
                "top_right": Qt.CursorShape.SizeBDiagCursor,
                "bottom_left": Qt.CursorShape.SizeBDiagCursor,
            }
            self.setCursor(cursors.get(mode, Qt.CursorShape.CrossCursor))
            return
        current = self._video_point(event.position())
        sx, sy = self._press_video
        cx, cy = current
        original = self._original
        if self._drag_mode == "create" or original is None:
            rect = Rect(min(sx, cx), min(sy, cy), abs(cx - sx), abs(cy - sy))
        elif self._drag_mode == "move":
            dx, dy = cx - sx, cy - sy
            x = min(max(original.x + dx, 0), self._video_width - original.width)
            y = min(max(original.y + dy, 0), self._video_height - original.height)
            rect = Rect(x, y, original.width, original.height)
        else:
            left, top, right, bottom = (
                original.x,
                original.y,
                original.right,
                original.bottom,
            )
            mode = self._drag_mode
            if "left" in mode:
                left = min(cx, right - self.MIN_SIZE)
            if "right" in mode:
                right = max(cx, left + self.MIN_SIZE)
            if "top" in mode:
                top = min(cy, bottom - self.MIN_SIZE)
            if "bottom" in mode:
                bottom = max(cy, top + self.MIN_SIZE)
            rect = Rect(left, top, right - left, bottom - top)
        updated = normalize_video_rect(
            rect, self._video_width, self._video_height
        )
        if 0 <= self._active_index < len(self._selections):
            self._selections[self._active_index] = updated
        self._emit_selection()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_mode:
            self._drag_mode = None
            selection = self.selection
            if selection and (
                selection.width < self.MIN_SIZE
                or selection.height < self.MIN_SIZE
            ):
                del self._selections[self._active_index]
                self._active_index = min(
                    self._active_index,
                    len(self._selections) - 1,
                )
            self._emit_selection()
            self.update()
            event.accept()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        content = self._content()
        if content.width > 0:
            painter.setPen(QPen(QColor(80, 160, 255, 100), 1))
            painter.drawRect(QRectF(content.x, content.y, content.width, content.height))
        if not self._selections:
            return
        for index, selection in enumerate(self._selections):
            mapped = video_to_preview_rect(
                selection,
                content,
                self._video_width,
                self._video_height,
            )
            rect = QRectF(mapped.x, mapped.y, mapped.width, mapped.height)
            active = index == self._active_index
            color = QColor(255, 55, 55) if active else QColor(255, 170, 40)
            painter.fillRect(
                rect,
                QColor(color.red(), color.green(), color.blue(), 35),
            )
            painter.setPen(QPen(color, 2 if active else 1))
            painter.drawRect(rect)
            if active:
                painter.setBrush(QColor(255, 255, 255))
                painter.setPen(QPen(QColor(220, 30, 30), 1))
                points = [
                    rect.topLeft(),
                    rect.topRight(),
                    rect.bottomLeft(),
                    rect.bottomRight(),
                    QPointF(rect.center().x(), rect.top()),
                    QPointF(rect.center().x(), rect.bottom()),
                    QPointF(rect.left(), rect.center().y()),
                    QPointF(rect.right(), rect.center().y()),
                ]
                size = 6
                for point in points:
                    painter.drawRect(
                        QRectF(
                            point.x() - size / 2,
                            point.y() - size / 2,
                            size,
                            size,
                        )
                    )
            label = (
                f"#{index + 1}  X {round(selection.x)}  "
                f"Y {round(selection.y)}  W {round(selection.width)}  "
                f"H {round(selection.height)}"
            )
            label_rect = QRectF(
                rect.left(),
                max(content.y, rect.top() - 25),
                310,
                22,
            )
            painter.fillRect(label_rect, QColor(0, 0, 0, 180))
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                label_rect.adjusted(6, 0, -2, 0),
                Qt.AlignmentFlag.AlignVCenter,
                label,
            )
