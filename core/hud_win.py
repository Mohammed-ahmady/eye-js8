"""
Gaze Mouse System — Windows 11 HUD Overlay
===========================================
PyQt5 replacement for the GTK3/Cairo HUD overlay.
Shows gaze cursor ring + dwell progress + radial menu.
Click-through is achieved via Qt.WindowTransparentForInput.
"""

import sys
import math
import time
import threading

import socketio as sio_module

from PyQt5.QtWidgets import QApplication, QWidget, QDesktopWidget
from PyQt5.QtCore    import Qt, QTimer, QPointF, pyqtSignal, QObject
from PyQt5.QtGui     import (QPainter, QColor, QPen, QBrush, QFont)

SERVER_URL      = 'http://localhost:5000'
RING_RADIUS     = 35
GLOW_RADIUS     = 55
WOBBLE_STRENGTH = 4
WOBBLE_SPEED    = 10.0


class HudSignals(QObject):
    """Thread-safe signals for Socket.IO → Qt thread communication."""
    hud_update    = pyqtSignal(dict)
    radial_update = pyqtSignal(dict)


class GazeHud(QWidget):
    def __init__(self, sio_client):
        super().__init__()
        self.sio       = sio_client
        self.signals   = HudSignals()
        self.boot_time = time.time()

        # Gaze state
        self.gaze_x        = -100
        self.gaze_y        = -100
        self.dwell_progress = 0.0
        self.is_locked      = False
        self.over_clickable = False

        # Radial menu state
        self.radial_active   = False
        self.radial_x        = 0
        self.radial_y        = 0
        self.radial_slice    = None
        self.radial_progress = 0.0

        # Window setup
        self.setWindowFlags(
            Qt.FramelessWindowHint       |
            Qt.WindowStaysOnTopHint      |
            Qt.Tool                      |
            Qt.WindowTransparentForInput   # ← CLICK-THROUGH: events pass through this window
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)

        # Full virtual desktop across all monitors
        desk = QDesktopWidget().screenGeometry(-1)
        self.W = desk.width()
        self.H = desk.height()
        self.setGeometry(desk)
        self.showFullScreen()

        # Connect signals (ensures Qt calls happen on Qt thread)
        self.signals.hud_update.connect(self._apply_hud_update)
        self.signals.radial_update.connect(self._apply_radial_update)

        # Socket.IO listeners (called from Socket.IO thread)
        def _dispatch_hud_update(payload):
            QTimer.singleShot(0, lambda p=payload: self.signals.hud_update.emit(p))

        def _dispatch_radial_update(payload):
            QTimer.singleShot(0, lambda p=payload: self.signals.radial_update.emit(p))

        self.sio.on('hud_update', _dispatch_hud_update)
        self.sio.on('hud_radial_menu', _dispatch_radial_update)

        # Animation timer
        self._timer = QTimer()
        self._timer.timeout.connect(self.update)
        self._timer.start(16)  # ~60fps

    def _apply_hud_update(self, data):
        self.gaze_x         = data.get('x', self.gaze_x)
        self.gaze_y         = data.get('y', self.gaze_y)
        self.dwell_progress = data.get('dwell', 0.0)
        self.is_locked      = data.get('locked', False)
        self.over_clickable = data.get('over_clickable', False)

    def _apply_radial_update(self, data):
        self.radial_active   = data.get('active', False)
        self.radial_x        = data.get('x', self.radial_x)
        self.radial_y        = data.get('y', self.radial_y)
        self.radial_slice    = data.get('slice', None)
        self.radial_progress = data.get('progress', 0.0)

    def paintEvent(self, event):
        if self.gaze_x < 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        cx = self.gaze_x
        cy = self.gaze_y
        t  = (time.time() - self.boot_time) * WOBBLE_SPEED

        # ── Radial Menu ─────────────────────────────────────────────────────────
        if self.radial_active:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 102))
            rx, ry    = self.radial_x, self.radial_y
            inner_r   = 40
            outer_r   = 150
            num_slices = 5
            labels    = ["Double", "Left", "Scroll", "Right", "Cancel"]

            for i in range(num_slices):
                start_a = -90 + (i * 360 / num_slices)
                span_a  = 360 / num_slices

                path = self._make_donut_slice(rx, ry, inner_r, outer_r, start_a, span_a)

                if self.radial_slice == i:
                    painter.setBrush(QBrush(QColor(51, 179, 255, 230)))
                else:
                    painter.setBrush(QBrush(QColor(26, 26, 38, 217)))

                painter.setPen(QPen(QColor(102, 102, 128, 128), 2.5))
                painter.drawPath(path)

                # Progress fill on selected slice
                if self.radial_slice == i and self.radial_progress > 0:
                    progress_span = span_a * self.radial_progress
                    prog_path = self._make_donut_slice(rx, ry, inner_r, outer_r,
                                                        start_a, progress_span)
                    painter.setBrush(QBrush(QColor(51, 255, 153, 242)))
                    painter.setPen(Qt.NoPen)
                    painter.drawPath(prog_path)

                # Text label
                mid_a = math.radians(start_a + span_a / 2)
                tr    = inner_r + (outer_r - inner_r) / 2
                tx    = rx + math.cos(mid_a) * tr
                ty    = ry + math.sin(mid_a) * tr

                font = QFont('Arial', 11)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor(255, 255, 255, 255))
                fm = painter.fontMetrics()
                lw = fm.horizontalAdvance(labels[i])
                painter.drawText(int(tx - lw / 2),
                                 int(ty + fm.ascent() / 2),
                                 labels[i])

            # Center dot
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 204)))
            painter.drawEllipse(QPointF(rx, ry), 6.0, 6.0)

        # ── Glow layers ──────────────────────────────────────────────────────────
        painter.setPen(Qt.NoPen)
        for i in range(3):
            alpha  = int((0.15 - i * 0.04) * 255)
            radius = GLOW_RADIUS + i * 10
            painter.setBrush(QBrush(QColor(51, 179, 255, alpha)))
            painter.drawEllipse(QPointF(cx, cy), float(radius), float(radius))

        # ── Wobbling ring ────────────────────────────────────────────────────────
        if self.is_locked:
            ring_color = QColor(255, 204, 51, 230)
        elif self.over_clickable:
            ring_color = QColor(51, 255, 153, 230)
        else:
            ring_color = QColor(255, 255, 255, 217)

        pen = QPen(ring_color, 3.5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        points = 60
        polygon_pts = []
        for i in range(points + 1):
            angle  = (i / points) * 2 * math.pi
            wobble = (math.sin(t + angle * 3) * WOBBLE_STRENGTH +
                      math.cos(t * 0.7 + angle * 5) * (WOBBLE_STRENGTH / 2))
            r = RING_RADIUS + wobble
            polygon_pts.append(QPointF(cx + math.cos(angle) * r,
                                       cy + math.sin(angle) * r))

        for i in range(len(polygon_pts) - 1):
            painter.drawLine(polygon_pts[i], polygon_pts[i + 1])

        # ── Inner dot ────────────────────────────────────────────────────────────
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 102)))
        painter.drawEllipse(QPointF(cx, cy), 4.0, 4.0)

        # ── Dwell progress arc ───────────────────────────────────────────────────
        if self.dwell_progress > 0 and not self.radial_active:
            pen2 = QPen(QColor(46, 217, 115, 230), 6)
            pen2.setCapStyle(Qt.RoundCap)
            painter.setPen(pen2)
            painter.setBrush(Qt.NoBrush)
            r = RING_RADIUS + 6
            span = int(-self.dwell_progress * 360 * 16)
            painter.drawArc(
                int(cx - r), int(cy - r),
                r * 2, r * 2,
                90 * 16, span
            )

        painter.end()

    def _make_donut_slice(self, cx, cy, inner_r, outer_r, start_deg, span_deg):
        """Create a donut slice QPainterPath (like Cairo arc/arc_negative)."""
        from PyQt5.QtGui import QPainterPath
        from PyQt5.QtCore import QRectF
        path = QPainterPath()
        # Outer arc
        outer_rect = QRectF(cx - outer_r, cy - outer_r, outer_r * 2, outer_r * 2)
        path.arcMoveTo(outer_rect, -start_deg)
        path.arcTo(outer_rect, -start_deg, -span_deg)
        # Inner arc (reverse)
        inner_rect = QRectF(cx - inner_r, cy - inner_r, inner_r * 2, inner_r * 2)
        path.arcTo(inner_rect, -(start_deg + span_deg), span_deg)
        path.closeSubpath()
        return path


# ── Main ──────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    sio_client = sio_module.Client()

    @sio_client.on('connect')
    def on_connect():
        sio_client.emit('register', {'type': 'hud'})
        print("[hud] Registered with server")

    try:
        sio_client.connect(SERVER_URL)
    except Exception as e:
        print(f"[hud] Connection failed: {e}")
        sys.exit(1)

    qt_app = QApplication(sys.argv)
    win    = GazeHud(sio_client)
    qt_app.exec_()
