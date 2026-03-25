from __future__ import annotations
from typing import Optional

from PyQt6.QtCore    import Qt, QPointF, QRectF, pyqtSignal, QPoint
from PyQt6.QtGui     import (QColor, QPen, QBrush, QPixmap, QImage,
                              QCursor, QPainter, QTransform)
from PyQt6.QtWidgets import (QGraphicsView, QGraphicsScene,
                              QGraphicsPixmapItem, QGraphicsRectItem,
                              QGraphicsItem, QSizePolicy)

from pdf_handler import PDFHandler, CropRegion


# ── tuning constants ──────────────────────────────────────────────────────────
BASE_RENDER_ZOOM : float = 2.0
BASE_SCROLL_STEP : int   = 80
ZOOM_STEP        : float = 1.18
ZOOM_MIN         : float = 0.08
ZOOM_MAX         : float = 10.0
PAGE_MARGIN      : float = 40.0

# ── visual style ──────────────────────────────────────────────────────────────
SEL_FILL   = QColor( 99, 179, 237,  55)
SEL_BORDER = QColor( 99, 179, 237, 220)
SEL_HANDLE = QColor(255, 255, 255, 230)
SHADOW_CLR = QColor(  0,   0,   0,  70)
BG_LIGHT   = QColor( 40,  40,  46)
BG_DARK    = QColor( 32,  32,  38)


# ── selection overlay ─────────────────────────────────────────────────────────
class _SelectionItem(QGraphicsRectItem):
    """
    Rubber-band selection drawn in scene coordinates.
    Handles its own fill, dashed border, and corner-handle painting.
    """
    HANDLE = 8

    def __init__(self):
        super().__init__()
        pen = QPen(SEL_BORDER, 0)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(SEL_FILL))
        self.setZValue(10)

    def paint(self, painter: QPainter, option, widget=None):
        super().paint(painter, option, widget)
        scale = painter.transform().m11() or 1.0
        h = self.HANDLE / scale
        r = self.rect().normalized()
        painter.save()
        painter.setBrush(QBrush(SEL_HANDLE))
        painter.setPen(Qt.PenStyle.NoPen)
        for cx, cy in [(r.left(),  r.top()),    (r.right(),  r.top()),
                       (r.left(),  r.bottom()), (r.right(), r.bottom())]:
            painter.drawRect(QRectF(cx - h/2, cy - h/2, h, h))
        painter.restore()


# ── main graphics view ────────────────────────────────────────────────────────
class PDFGraphicsView(QGraphicsView):
    """
    Interactive PDF/Image viewer.

    Public API (unchanged from original):
      load_page(index)       render and show a page; resets selection
      set_view_zoom(factor)  set display scale (1.0 = 100%)
      clear_selection()      remove current selection
      make_crop_region()     return CropRegion or None
      current_page           int property
      current_scale          float property

    Signals:
      selection_changed(QRectF)  — document-coordinate rect of the selection
      selection_cleared()
      zoom_changed(int)          — new zoom percentage
    """

    selection_changed = pyqtSignal(QRectF)
    selection_cleared = pyqtSignal()
    zoom_changed      = pyqtSignal(int)

    def __init__(self, handler: PDFHandler, parent=None):
        super().__init__(parent)

        self._handler       = handler
        self._page_index    = 0
        self._current_scale = 1.0

        # ── scene ────────────────────────────────────────────────────
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self._page_item = QGraphicsPixmapItem()
        self._page_item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation)
        self._page_item.setPos(PAGE_MARGIN, PAGE_MARGIN)
        self._page_item.setZValue(0)
        self._scene.addItem(self._page_item)

        self._sel_item  = _SelectionItem()
        self._sel_item.hide()
        self._scene.addItem(self._sel_item)
        self._sel_start: Optional[QPointF] = None

        self._pan_last: Optional[QPoint] = None

        # ── view config ──────────────────────────────────────────────
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter)

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing |
            QPainter.RenderHint.SmoothPixmapTransform)

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setInteractive(False)

        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(400, 400)

        self.setStyleSheet("background: #141720; border: none;")
        self.setBackgroundBrush(QBrush(QColor(20, 23, 32)))

    # ================================================================== #
    #  Public API
    # ================================================================== #

    def load_page(self, page_index: int):
        """Render and display the given page/image. Clears the selection."""
        self._page_index = page_index
        self._clear_selection()
        self._refresh_page()

    def set_view_zoom(self, factor: float):
        """
        Set the display zoom to an absolute factor.
        Resets the transform cleanly so scales never compound.
        """
        factor = max(ZOOM_MIN, min(factor, ZOOM_MAX))
        self.setTransform(QTransform())
        self.scale(factor, factor)
        self._current_scale = factor

    @property
    def current_page(self) -> int:
        return self._page_index

    @property
    def current_scale(self) -> float:
        return self._current_scale

    def clear_selection(self):
        self._clear_selection()

    def make_crop_region(self) -> Optional[CropRegion]:
        rect = self._selection_in_doc_coords()
        return CropRegion(page_index=self._page_index, rect=rect) \
               if rect else None

    # ================================================================== #
    #  Private helpers
    # ================================================================== #

    def _refresh_page(self):
        """Re-render and push the new pixmap into the scene."""
        if not self._handler.is_open:
            self._page_item.setPixmap(QPixmap())
            self._update_scene_rect()
            return

        img: Optional[QImage] = self._handler.render_page(
            self._page_index, BASE_RENDER_ZOOM)
        if img is None:
            return
        self._page_item.setPixmap(QPixmap.fromImage(img))
        self._update_scene_rect()

    def _update_scene_rect(self):
        """Expand scene rect to page + margins so scrollbars work correctly."""
        pm = self._page_item.pixmap()
        if pm.isNull():
            self.setSceneRect(QRectF(0, 0, 800, 1000))
            return
        self.setSceneRect(QRectF(0, 0,
                                 pm.width()  + PAGE_MARGIN * 2,
                                 pm.height() + PAGE_MARGIN * 2))

    def _clear_selection(self):
        self._sel_start = None
        self._sel_item.hide()
        self._sel_item.setRect(QRectF())

    def _selection_in_doc_coords(self) -> Optional[QRectF]:
        """
        Convert the scene-space selection rect to document-coordinate space.

        The page pixmap sits at (PAGE_MARGIN, PAGE_MARGIN) in scene space.
        Scene coordinates are in pixels at BASE_RENDER_ZOOM.

        For PDFs:  doc unit = PDF point;  formula: pdf = (scene - margin) / zoom
        For images: doc unit = pixel;     same formula — pixels rendered at zoom
        """
        if not self._sel_item.isVisible():
            return None
        sr = self._sel_item.rect().normalized()
        if sr.width() < 3 or sr.height() < 3:
            return None

        x0 = (sr.x()      - PAGE_MARGIN) / BASE_RENDER_ZOOM
        y0 = (sr.y()      - PAGE_MARGIN) / BASE_RENDER_ZOOM
        x1 = (sr.right()  - PAGE_MARGIN) / BASE_RENDER_ZOOM
        y1 = (sr.bottom() - PAGE_MARGIN) / BASE_RENDER_ZOOM

        pw, ph = self._handler.get_page_size(self._page_index)
        x0 = max(0.0, min(x0, pw)); y0 = max(0.0, min(y0, ph))
        x1 = max(0.0, min(x1, pw)); y1 = max(0.0, min(y1, ph))

        if (x1 - x0) < 2 or (y1 - y0) < 2:
            return None
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    # ================================================================== #
    #  Qt painting overrides
    # ================================================================== #

    def drawBackground(self, painter: QPainter, rect: QRectF):
        """Checkerboard drawn in viewport coords — stable at all zoom levels."""
        painter.save()
        painter.resetTransform()
        TILE = 24
        vr   = self.viewport().rect()
        x0 = (vr.left()   // TILE) * TILE
        y0 = (vr.top()    // TILE) * TILE
        x1 =  vr.right()  + TILE
        y1 =  vr.bottom() + TILE
        sx = int(self.horizontalScrollBar().value() // TILE)
        sy = int(self.verticalScrollBar().value()   // TILE)
        for row, y in enumerate(range(y0, y1, TILE)):
            for col, x in enumerate(range(x0, x1, TILE)):
                light = ((row + col + sx + sy) % 2 == 0)
                painter.fillRect(x, y, TILE, TILE,
                                 BG_LIGHT if light else BG_DARK)
        painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF):
        """Drop shadow behind the page, drawn in scene space."""
        pm = self._page_item.pixmap()
        if pm.isNull():
            return
        shadow = QRectF(PAGE_MARGIN + 5, PAGE_MARGIN + 5,
                        pm.width(), pm.height())
        painter.fillRect(shadow, SHADOW_CLR)

    # ================================================================== #
    #  Mouse events
    # ================================================================== #

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.pos())
            self._sel_start = sp
            self._sel_item.setRect(QRectF(sp, sp))
            self._sel_item.show()
            self.selection_cleared.emit()

        elif event.button() == Qt.MouseButton.RightButton:
            self._pan_last = event.pos()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def mouseMoveEvent(self, event):
        if (self._sel_start is not None and
                event.buttons() & Qt.MouseButton.LeftButton):
            sp = self.mapToScene(event.pos())
            self._sel_item.setRect(
                QRectF(self._sel_start, sp).normalized())

        elif (self._pan_last is not None and
              event.buttons() & Qt.MouseButton.RightButton):
            delta = event.pos() - self._pan_last
            self._pan_last = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._sel_start:
            self._sel_start = None
            doc_rect = self._selection_in_doc_coords()
            if doc_rect:
                self.selection_changed.emit(doc_rect)
            else:
                self._clear_selection()
                self.selection_cleared.emit()

        elif event.button() == Qt.MouseButton.RightButton:
            self._pan_last = None
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    # ================================================================== #
    #  Wheel event — zoom or scroll
    # ================================================================== #

    def wheelEvent(self, event):
        """
        Ctrl + Scroll  → Zoom towards cursor.
        Plain Scroll   → Scroll (zoom-adaptive).
        Shift + Scroll → Horizontal scroll.
        """
        delta = event.angleDelta().y()

        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step_factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
            new_scale   = self._current_scale * step_factor
            new_scale   = max(ZOOM_MIN, min(new_scale, ZOOM_MAX))
            actual      = new_scale / self._current_scale

            if abs(actual - 1.0) < 1e-6:
                event.accept()
                return

            self.scale(actual, actual)
            self._current_scale = new_scale
            self.zoom_changed.emit(int(round(self._current_scale * 100)))
            event.accept()

        else:
            current_scale = self.transform().m11()
            raw_step = BASE_SCROLL_STEP / max(current_scale, 0.01)
            step     = int(max(BASE_SCROLL_STEP * 0.20,
                               min(raw_step, float(BASE_SCROLL_STEP))))
            notches  = delta / 120.0

            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                bar = self.horizontalScrollBar()
            else:
                bar = self.verticalScrollBar()

            bar.setValue(bar.value() - int(notches * step))
            event.accept()
