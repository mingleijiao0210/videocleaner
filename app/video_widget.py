"""QMediaPlayer 视频帧显示与透明选框层组成的预览组件。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QVideoFrame, QVideoSink
from PySide6.QtWidgets import QStackedLayout, QWidget

from .coordinate_mapper import calculate_video_content_rect
from .selection_overlay import SelectionOverlay


class VideoFrameWidget(QWidget):
    """把 QVideoSink 提供的本地帧绘制到普通 QWidget，确保覆盖层稳定。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._video_width = 0
        self._video_height = 0
        self.setAutoFillBackground(False)

    def set_video_size(self, width: int, height: int) -> None:
        self._video_width = width
        self._video_height = height
        self.update()

    def set_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._image = image.copy()
        self.update()

    def clear(self) -> None:
        self._image = QImage()
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))
        if self._image.isNull() or self._video_width <= 0:
            painter.setPen(QColor(180, 180, 180))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "选择视频后可在这里预览并框选区域",
            )
            return
        content = calculate_video_content_rect(
            self.width(),
            self.height(),
            self._video_width,
            self._video_height,
        )
        target = QRectF(content.x, content.y, content.width, content.height)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self._image)


class VideoPreview(QWidget):
    selectionChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.frame_widget = VideoFrameWidget(self)
        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self.frame_widget.set_frame)
        self.overlay = SelectionOverlay(self)
        layout = QStackedLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        layout.addWidget(self.frame_widget)
        layout.addWidget(self.overlay)
        self.overlay.raise_()
        self.overlay.selectionChanged.connect(self.selectionChanged)

    def set_video_size(self, width: int, height: int) -> None:
        self.frame_widget.set_video_size(width, height)
        self.overlay.set_video_size(width, height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.overlay.raise_()
        self.overlay.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.overlay.raise_()
