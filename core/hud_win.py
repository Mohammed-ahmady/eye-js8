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
import os
import json

import socketio as sio_module

from PyQt5.QtWidgets import QApplication, QWidget, QDesktopWidget
from PyQt5.QtCore    import Qt, QTimer, QPointF, QPoint, pyqtSignal, QObject, QRectF
from PyQt5.QtGui     import (QPainter, QColor, QPen, QBrush, QFont, QGuiApplication, QPixmap)

SERVER_URL      = 'http://localhost:5000'
RING_RADIUS     = 35
GLOW_RADIUS     = 55
WOBBLE_STRENGTH = 4
WOBBLE_SPEED    = 10.0
ZOOM_PANEL_SIZE = 220
ZOOM_SAMPLE_SIZE = 48
RADIAL_MENU_SCALE = 1.3
RADIAL_MENU_RING_PADDING = 90
RADIAL_MENU_ITEM_RADIUS = 22
RADIAL_MENU_CANCEL_SCALE = 1.35
RADIAL_MENU_LABEL_PAD = 10
BAR_MENU_BLUR_SCALE = 0.08
BAR_MENU_BLUR_REFRESH_MS = 900
BAR_MENU_DIM_ALPHA = 120
HUD_LOG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'docs', 'hud_runtime_log.jsonl')
)


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
        self.W = 0
        self.H = 0

        # Gaze state
        self.gaze_x        = -100
        self.gaze_y        = -100
        self.raw_gaze_x    = -100
        self.raw_gaze_y    = -100
        self.dwell_progress = 0.0
        self.is_locked      = False
        self.over_clickable = False

        # Precision assist state
        self.precision_active = False
        self.precision_x = None
        self.precision_y = None
        self.precision_name = ''
        self.precision_confidence = 0.0
        self.precision_radius = 160
        self.precision_lock_progress = 0.0
        self.precision_score = 0.0
        self.hud_updates = 0
        self.last_hud_update_ts = 0.0

        # Radial menu state
        self.radial_active   = False
        self.radial_x        = 0
        self.radial_y        = 0
        self.radial_slice    = None
        self.radial_progress = 0.0
        self.radial_target_x = None
        self.radial_target_y = None
        self.radial_menu_type = 'default'
        self.radial_items = None
        self.radial_title = ''
        self.radial_page = 1
        self.radial_pages = 1

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
        self.screen_left = desk.x()
        self.screen_top = desk.y()
        self.setGeometry(desk)
        self.showFullScreen()
        self.gaze_x = self.W // 2
        self.gaze_y = self.H // 2
        self.raw_gaze_x = self.W // 2
        self.raw_gaze_y = self.H // 2

        # Bar menu blur cache
        self.bar_blur_cache = None
        self.bar_blur_ts = 0.0

        # Connect signals (ensures Qt calls happen on Qt thread)
        self.signals.hud_update.connect(self._apply_hud_update)
        self.signals.radial_update.connect(self._apply_radial_update)

        # Socket.IO listeners (called from Socket.IO thread)
        # Emit Qt signals directly; Qt will marshal across threads.
        def _dispatch_hud_update(payload):
            self.signals.hud_update.emit(payload)

        def _dispatch_radial_update(payload):
            self.signals.radial_update.emit(payload)

        self.sio.on('hud_update', _dispatch_hud_update)
        self.sio.on('hud_radial_menu', _dispatch_radial_update)

        # Animation timer
        self._timer = QTimer()
        self._timer.timeout.connect(self.update)
        self._timer.start(16)  # ~60fps
        self._log_event('hud_started', width=self.W, height=self.H)

    def _log_event(self, event, **fields):
        record = {
            'ts': round(time.time(), 3),
            'src': 'hud_win',
            'event': event,
        }
        record.update(fields)
        try:
            with open(HUD_LOG_PATH, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(record, ensure_ascii=True) + '\n')
        except Exception:
            pass

    def _apply_hud_update(self, data):
        self.hud_updates += 1
        self.last_hud_update_ts = time.time()
        self.gaze_x         = data.get('x', self.gaze_x)
        self.gaze_y         = data.get('y', self.gaze_y)
        self.raw_gaze_x     = data.get('raw_x', self.raw_gaze_x)
        self.raw_gaze_y     = data.get('raw_y', self.raw_gaze_y)
        self.dwell_progress = data.get('dwell', 0.0)
        self.is_locked      = data.get('locked', False)
        self.over_clickable = data.get('over_clickable', False)

        precision = data.get('precision') or {}
        self.precision_active = precision.get('active', False)
        self.precision_x = precision.get('candidate_x', self.precision_x)
        self.precision_y = precision.get('candidate_y', self.precision_y)
        self.precision_name = precision.get('candidate_name', self.precision_name) or ''
        self.precision_confidence = float(precision.get('confidence', 0.0) or 0.0)
        self.precision_radius = int(precision.get('bubble_radius', self.precision_radius or 0) or 0)
        self.precision_lock_progress = float(precision.get('lock_progress', 0.0) or 0.0)
        self.precision_score = float(precision.get('score', self.precision_score) or 0.0)
        if self.hud_updates == 1:
            self._log_event(
                'first_hud_update',
                x=self.gaze_x,
                y=self.gaze_y,
                precision_active=self.precision_active,
            )

    def _apply_radial_update(self, data):
        self.radial_active   = data.get('active', False)
        self.radial_x        = data.get('x', self.radial_x)
        self.radial_y        = data.get('y', self.radial_y)
        self.radial_slice    = data.get('slice', None)
        self.radial_progress = data.get('progress', 0.0)
        menu_type = data.get('menu_type')
        if menu_type:
            self.radial_menu_type = menu_type
        if 'items' in data:
            self.radial_items = data.get('items') or []
            if not menu_type:
                self.radial_menu_type = 'bar'
        if 'title' in data:
            self.radial_title = data.get('title') or ''
        if 'page' in data:
            try:
                self.radial_page = int(data.get('page') or 1)
            except Exception:
                self.radial_page = 1
        if 'pages' in data:
            try:
                self.radial_pages = int(data.get('pages') or 1)
            except Exception:
                self.radial_pages = 1
        if 'target_x' in data:
            self.radial_target_x = data.get('target_x')
        if 'target_y' in data:
            self.radial_target_y = data.get('target_y')

        if not (self.radial_active and self.radial_menu_type == 'bar'):
            self.bar_blur_cache = None
            self.bar_blur_ts = 0.0

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        self._draw_hud_diagnostics(painter)

        cx = self.gaze_x
        cy = self.gaze_y
        t  = (time.time() - self.boot_time) * WOBBLE_SPEED

        self._draw_precision_assist(painter)

        # ── Radial Menu ─────────────────────────────────────────────────────────
        if self.radial_active:
            if self.radial_menu_type == 'bar':
                self._draw_bar_menu_backdrop(painter)
            else:
                painter.fillRect(self.rect(), QColor(0, 0, 0, 102))
            rx, ry    = self.radial_x, self.radial_y
            if self.radial_menu_type == 'bar' and self.radial_items:
                labels = list(self.radial_items)
            else:
                labels = ["Scroll", "Cancel", "Right", "Double", "Left"]
            if not labels:
                labels = ["Cancel"]
            num_slices = len(labels)
            span_a = 360 / num_slices
            ring_r = (ZOOM_PANEL_SIZE / 2) + (RADIAL_MENU_RING_PADDING * RADIAL_MENU_SCALE)
            item_r = RADIAL_MENU_ITEM_RADIUS * RADIAL_MENU_SCALE
            cancel_r = item_r * RADIAL_MENU_CANCEL_SCALE

            for i in range(num_slices):
                mid_deg = -90 + (i * span_a)
                mid_a = math.radians(mid_deg)
                bx = rx + math.cos(mid_a) * ring_r
                by = ry + math.sin(mid_a) * ring_r
                is_cancel = labels[i] == "Cancel"
                radius = cancel_r if is_cancel else item_r
                is_selected = (self.radial_slice == i)

                # Floating button
                shadow = QColor(0, 0, 0, 120)
                painter.setPen(Qt.NoPen)
                painter.setBrush(shadow)
                painter.drawEllipse(QPointF(bx + 2, by + 2), radius + 2, radius + 2)

                base_color = QColor(20, 20, 28, 220)
                if is_selected:
                    if self.radial_menu_type == 'bar':
                        base_color = QColor(46, 201, 255, 230)
                    else:
                        base_color = QColor(51, 179, 255, 230)
                painter.setBrush(QBrush(base_color))
                painter.setPen(QPen(QColor(110, 110, 140, 140), 2.4))
                painter.drawEllipse(QPointF(bx, by), radius, radius)

                # Progress ring on selected item
                if is_selected and self.radial_progress > 0:
                    pen = QPen(QColor(51, 255, 153, 242), 3.5)
                    pen.setCapStyle(Qt.RoundCap)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    span = int(-self.radial_progress * 360 * 16)
                    r = radius + 6
                    painter.drawArc(int(bx - r), int(by - r), int(r * 2), int(r * 2), 90 * 16, span)

                # Text label placement
                label = labels[i]
                font = QFont('Arial', int(11 * RADIAL_MENU_SCALE))
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor(255, 255, 255, 235))
                fm = painter.fontMetrics()
                lw = fm.horizontalAdvance(label)
                pad = radius + (RADIAL_MENU_LABEL_PAD * RADIAL_MENU_SCALE)

                cx_off = math.cos(mid_a)
                cy_off = math.sin(mid_a)
                if abs(cx_off) < 0.35:
                    tx = bx - (lw / 2)
                    ty = by + (pad if cy_off > 0 else -pad)
                elif cx_off > 0:
                    tx = bx + pad
                    ty = by + (fm.ascent() / 2)
                else:
                    tx = bx - pad - lw
                    ty = by + (fm.ascent() / 2)
                painter.drawText(int(tx), int(ty), label)

            # Center dot
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 204)))
            painter.drawEllipse(QPointF(rx, ry), 8.0, 8.0)

            if self.radial_menu_type == 'bar':
                title = self.radial_title or 'Bar'
                title_font = QFont('Arial', int(11 * RADIAL_MENU_SCALE))
                title_font.setBold(True)
                painter.setFont(title_font)
                painter.setPen(QColor(230, 240, 248, 235))
                fm = painter.fontMetrics()
                tw = fm.horizontalAdvance(title)
                painter.drawText(int(rx - (tw / 2)), int(ry - 14), title)
                if self.radial_pages and self.radial_pages > 1:
                    page_txt = f"{self.radial_page}/{self.radial_pages}"
                    page_font = QFont('Arial', int(9 * RADIAL_MENU_SCALE))
                    painter.setFont(page_font)
                    painter.setPen(QColor(180, 207, 224, 220))
                    pw = painter.fontMetrics().horizontalAdvance(page_txt)
                    painter.drawText(int(rx - (pw / 2)), int(ry + 24), page_txt)

            # Centered zoom panel focused on the selected target area.
            target_x = self.radial_target_x if self.radial_target_x is not None else cx
            target_y = self.radial_target_y if self.radial_target_y is not None else cy
            self._draw_zoom_panel(painter, rx, ry, target_x, target_y)

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

    def _draw_hud_diagnostics(self, painter):
        panel = QRectF(24, 24, 360, 92)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(8, 12, 18, 190)))
        painter.drawRoundedRect(panel, 10, 10)

        painter.setPen(QPen(QColor(86, 128, 158, 220), 1.2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(panel, 10, 10)

        age = -1.0
        if self.last_hud_update_ts > 0:
            age = time.time() - self.last_hud_update_ts

        status = 'WAITING_GAZE'
        if self.last_hud_update_ts > 0 and age < 1.0:
            status = 'LIVE'
        elif self.last_hud_update_ts > 0:
            status = 'STALE'

        line1 = f"HUD {status} | updates={self.hud_updates}"
        if age >= 0:
            line2 = f"last_update={age:.2f}s | gaze=({int(self.gaze_x)}, {int(self.gaze_y)})"
        else:
            line2 = 'last_update=never | gaze=(n/a)'
        line3 = f"precision_active={self.precision_active} locked={self.is_locked} conf={int(self.precision_confidence * 100)}%"

        title_font = QFont('Arial', 10)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(238, 246, 255, 245))
        painter.drawText(int(panel.x() + 12), int(panel.y() + 26), line1)

        body_font = QFont('Arial', 9)
        painter.setFont(body_font)
        painter.setPen(QColor(183, 209, 226, 235))
        painter.drawText(int(panel.x() + 12), int(panel.y() + 50), line2)
        painter.drawText(int(panel.x() + 12), int(panel.y() + 72), line3)

    def _draw_precision_assist(self, painter):
        tx = self.precision_x if self.precision_x is not None else self.raw_gaze_x
        ty = self.precision_y if self.precision_y is not None else self.raw_gaze_y
        gx = self.raw_gaze_x if self.raw_gaze_x >= 0 else self.gaze_x
        gy = self.raw_gaze_y if self.raw_gaze_y >= 0 else self.gaze_y

        if self.precision_radius > 0:
            bubble_color = QColor(255, 204, 51, 200) if self.is_locked else QColor(51, 179, 255, 188)
            bubble_pen = QPen(bubble_color, 3.2)
            painter.setPen(bubble_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(gx, gy), float(self.precision_radius), float(self.precision_radius))

        if self.precision_active or self.is_locked:
            # Visual tie between current gaze estimate and selected candidate.
            painter.setPen(QPen(QColor(255, 255, 255, 128), 1.8))
            painter.drawLine(QPointF(gx, gy), QPointF(tx, ty))

            marker_color = QColor(255, 204, 51, 235) if self.is_locked else QColor(51, 255, 153, 220)
            painter.setPen(QPen(marker_color, 3.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(tx, ty), 18.0, 18.0)

        # Lock progress around candidate center.
        progress = 1.0 if self.is_locked else max(0.0, min(self.precision_lock_progress, 1.0))
        if progress > 0 and (self.precision_active or self.is_locked):
            pen = QPen(QColor(46, 217, 115, 245), 4)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            r = 24
            span = int(-progress * 360 * 16)
            painter.drawArc(int(tx - r), int(ty - r), r * 2, r * 2, 90 * 16, span)

        # Zoom panel is drawn during radial menu only.

    def _draw_bar_menu_backdrop(self, painter):
        self._ensure_bar_blur_cache()
        if self.bar_blur_cache is not None:
            painter.drawPixmap(0, 0, self.bar_blur_cache)
        painter.fillRect(self.rect(), QColor(0, 0, 0, BAR_MENU_DIM_ALPHA))

    def _ensure_bar_blur_cache(self):
        now = time.time()
        if self.bar_blur_cache is not None:
            age_ms = (now - self.bar_blur_ts) * 1000.0
            if age_ms < BAR_MENU_BLUR_REFRESH_MS:
                return

        self.bar_blur_cache = self._capture_blurred_background()
        self.bar_blur_ts = now

    def _capture_blurred_background(self):
        screens = QGuiApplication.screens()
        if not screens:
            return None

        composite = QPixmap(self.W, self.H)
        composite.fill(QColor(0, 0, 0, 255))

        painter = QPainter(composite)
        for screen in screens:
            try:
                geom = screen.geometry()
                shot = screen.grabWindow(0)
                if shot.isNull():
                    continue
                dx = geom.x() - self.screen_left
                dy = geom.y() - self.screen_top
                painter.drawPixmap(int(dx), int(dy), shot)
            except Exception:
                continue
        painter.end()

        blur_w = max(1, int(self.W * BAR_MENU_BLUR_SCALE))
        blur_h = max(1, int(self.H * BAR_MENU_BLUR_SCALE))
        down = composite.scaled(blur_w, blur_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return down.scaled(self.W, self.H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def _draw_zoom_panel(self, painter, center_x, center_y, tx, ty):
        panel_w = ZOOM_PANEL_SIZE
        panel_h = ZOOM_PANEL_SIZE + 58
        panel_x = center_x - (panel_w / 2)
        panel_y = center_y - (panel_h / 2)

        panel_x = max(12, min(panel_x, self.W - panel_w - 12))
        panel_y = max(12, min(panel_y, self.H - panel_h - 12))
        panel = QRectF(panel_x, panel_y, panel_w, panel_h)
        panel_img = QRectF(panel.x() + 12, panel.y() + 12, panel.width() - 24, panel.width() - 24)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(10, 14, 18, 214)))
        painter.drawRoundedRect(panel, 12, 12)

        painter.setPen(QPen(QColor(98, 132, 153, 220), 1.5))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(panel_img, 8, 8)

        # Capture the nearest-target neighborhood and draw a magnified preview.
        screen = QGuiApplication.screenAt(QPoint(int(tx), int(ty)))
        if screen is None:
            screen = QGuiApplication.primaryScreen()

        if screen is not None:
            geom = screen.geometry()
            local_x = int(tx - geom.x() - ZOOM_SAMPLE_SIZE / 2)
            local_y = int(ty - geom.y() - ZOOM_SAMPLE_SIZE / 2)
            local_x = max(0, min(local_x, max(0, geom.width() - ZOOM_SAMPLE_SIZE)))
            local_y = max(0, min(local_y, max(0, geom.height() - ZOOM_SAMPLE_SIZE)))

            pix = screen.grabWindow(0, local_x, local_y, ZOOM_SAMPLE_SIZE, ZOOM_SAMPLE_SIZE)
            if not pix.isNull():
                scaled = pix.scaled(
                    int(panel_img.width()),
                    int(panel_img.height()),
                    Qt.IgnoreAspectRatio,
                    Qt.FastTransformation,
                )
                painter.drawPixmap(int(panel_img.x()), int(panel_img.y()), scaled)

        # Crosshair in the zoom panel center.
        cx = int(panel_img.center().x())
        cy = int(panel_img.center().y())
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1.3))
        painter.drawLine(cx - 14, cy, cx + 14, cy)
        painter.drawLine(cx, cy - 14, cx, cy + 14)

        title = 'Locked' if self.is_locked else 'Bubble Pointer'
        confidence_txt = f"Conf: {int(max(0.0, min(self.precision_confidence, 1.0)) * 100)}%"
        score_txt = f"Score: {int(max(0.0, min(self.precision_score, 1.0)) * 100)}%"
        label = self.precision_name[:26] if self.precision_name else 'No candidate'

        painter.setPen(QColor(235, 243, 251, 245))
        title_font = QFont('Arial', 10)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(int(panel.x() + 12), int(panel.y() + panel.height() - 34), title)

        sub_font = QFont('Arial', 9)
        painter.setFont(sub_font)
        painter.setPen(QColor(180, 207, 224, 230))
        painter.drawText(int(panel.x() + 12), int(panel.y() + panel.height() - 30), f"{label}")
        painter.drawText(int(panel.x() + 12), int(panel.y() + panel.height() - 14), f"{confidence_txt}  |  {score_txt}")

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
