import pytest
from PySide6.QtCore import QPoint, Qt

from app.selection_overlay import SelectionOverlay
from app.coordinate_mapper import Rect


def test_mouse_create_move_and_resize_selection(qtbot):
    overlay = SelectionOverlay()
    overlay.resize(640, 360)
    overlay.set_video_size(1280, 720)
    overlay.show()
    qtbot.addWidget(overlay)

    # 在预览坐标 (100,100) 到 (300,200) 拖动创建。
    qtbot.mousePress(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100)
    )
    qtbot.mouseMove(overlay, QPoint(300, 200))
    qtbot.mouseRelease(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 200)
    )
    created = overlay.selection
    assert created is not None
    assert created.x == pytest.approx(200, abs=3)
    assert created.y == pytest.approx(200, abs=3)
    assert created.width == pytest.approx(400, abs=3)
    assert created.height == pytest.approx(200, abs=3)

    # 从框内部拖动，原视频坐标应按相同比例移动。
    qtbot.mousePress(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(200, 150)
    )
    qtbot.mouseMove(overlay, QPoint(250, 170))
    qtbot.mouseRelease(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(250, 170)
    )
    moved = overlay.selection
    assert moved is not None
    assert moved.x == pytest.approx(300, abs=3)
    assert moved.y == pytest.approx(240, abs=3)
    assert moved.width == pytest.approx(400, abs=3)
    assert moved.height == pytest.approx(200, abs=3)

    # 拖动右下角放大。
    qtbot.mousePress(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(350, 220)
    )
    qtbot.mouseMove(overlay, QPoint(390, 250))
    qtbot.mouseRelease(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(390, 250)
    )
    resized = overlay.selection
    assert resized is not None
    assert resized.x == pytest.approx(moved.x, abs=3)
    assert resized.y == pytest.approx(moved.y, abs=3)
    assert resized.width > moved.width
    assert resized.height > moved.height


def test_multiple_selections_can_be_added_activated_and_deleted(qtbot):
    overlay = SelectionOverlay()
    overlay.resize(640, 360)
    overlay.set_video_size(1280, 720)
    overlay.show()
    qtbot.addWidget(overlay)

    overlay.set_selection(Rect(100, 100, 200, 100))
    overlay.begin_add_selection()
    qtbot.mousePress(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(400, 200)
    )
    qtbot.mouseMove(overlay, QPoint(520, 280))
    qtbot.mouseRelease(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(520, 280)
    )

    assert len(overlay.selections) == 2
    assert overlay.active_index == 1

    # 点击第一个框，将它设为当前选框，然后只删除当前框。
    qtbot.mouseClick(
        overlay, Qt.MouseButton.LeftButton, pos=QPoint(75, 75)
    )
    assert overlay.active_index == 0
    overlay.delete_active_selection()

    assert len(overlay.selections) == 1
    assert overlay.selection == overlay.selections[0]

    overlay.clear_selection()
    assert overlay.selections == ()
    assert overlay.selection is None
