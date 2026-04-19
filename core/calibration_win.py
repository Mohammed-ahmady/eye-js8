"""
Gaze Mouse System — Windows 11 Calibration Overlay
====================================================
PyQt5 replacement for the GTK3/Cairo calibration overlay.
13-point calibration (2 passes) + corner refinement + 5-point validation.
"""

import sys
import math
import time
import threading

import socketio as sio_module

from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore    import Qt, QTimer, QPointF
from PyQt5.QtGui     import (QPainter, QColor, QPen, QBrush, QPainterPath,
                              QFont, QFontMetrics)

SERVER_URL = 'http://localhost:5000'

CALIBRATION_POINTS_PCT = [
    (0.025, 0.025), (0.50, 0.025), (0.975, 0.025),
    (0.025, 0.25),  (0.975, 0.25),
    (0.025, 0.50),  (0.50, 0.50),  (0.975, 0.50),
    (0.025, 0.75),  (0.975, 0.75),
    (0.025, 0.975), (0.50, 0.975), (0.975, 0.975),
]

CORNER_REFINEMENT_POINTS_PCT = [
    (0.02, 0.02), (0.10, 0.10),
    (0.98, 0.02), (0.90, 0.10),
    (0.02, 0.98), (0.10, 0.90),
    (0.98, 0.98), (0.90, 0.90),
]

CALIBRATION_PASSES = 2
REFINE_CORNERS_ENABLED = True

VALIDATION_POINTS_PCT = [
    (0.50, 0.50),
    (0.05, 0.05), (0.95, 0.05),
    (0.05, 0.95), (0.95, 0.95),
]

DOT_OUTER_RADIUS   = 24
DOT_INNER_RADIUS   = 9
FILL_DURATION_MS   = 1200
CALIBRATION_SAMPLE_AT_MS = 500
VALIDATION_DURATION_MS = 1500
INTER_POINT_DELAY_MS = 500
PHASE_TRANSITION_DELAY_MS = 500


class CalibrationOverlay(QWidget):
    def __init__(self, sio_client):
        super().__init__()
        self.sio = sio_client

        self.point_index  = 0
        self.fill_start   = None
        self.fill_active  = False
        self.confirmed    = False
        self._point_sent  = False

        self.mode = "CALIBRATING"
        self.calibration_pass = 1
        self.validation_errors = []
        self.current_gaze      = None
        self.final_accuracy    = None

        # Window flags: frameless, always on top, transparent background
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # Full screen geometry
        screen = QApplication.primaryScreen().geometry()
        self.W = screen.width()
        self.H = screen.height()
        self.setGeometry(screen)
        self.showFullScreen()

        # Register Socket.IO listener
        self.sio.on('current_gaze', self._on_gaze)

        # Animation timer (60fps equivalent)
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)

        # Start first dot after 1.5s delay
        QTimer.singleShot(1500, self._start_first_dot)
        QTimer.singleShot(1500, lambda: self._timer.start(16))

    def _on_gaze(self, data):
        self.current_gaze = (data['x'], data['y'])

    def _start_first_dot(self):
        self.fill_active = True
        self.fill_start  = time.time()
        self._point_sent = False

    def _current_points(self):
        if self.mode == "CALIBRATING":
            return CALIBRATION_POINTS_PCT
        if self.mode == "CALIBRATING_PASS2":
            return CALIBRATION_POINTS_PCT
        if self.mode == "REFINE_CORNERS":
            return CORNER_REFINEMENT_POINTS_PCT
        return VALIDATION_POINTS_PCT

    def _current_xy(self):
        pts = self._current_points()
        if self.point_index >= len(pts):
            return 0, 0
        px, py = pts[self.point_index]
        return int(px * self.W), int(py * self.H)

    def _tick(self):
        self.update()  # triggers paintEvent
        now = time.time()

        if not self.fill_active or self.fill_start is None:
            return

        elapsed = (now - self.fill_start) * 1000

        if self.mode in ("CALIBRATING", "CALIBRATING_PASS2", "REFINE_CORNERS"):
            # Use one clean sample per point to avoid over-weighting static labels.
            if not self.confirmed and not self._point_sent and elapsed >= CALIBRATION_SAMPLE_AT_MS:
                x, y = self._current_xy()
                self.sio.emit('calibrate_point', {'x': x, 'y': y})
                self._point_sent = True

            if elapsed >= FILL_DURATION_MS and not self.confirmed:
                self.confirmed = True
                QTimer.singleShot(250, self._move_to_next)

        elif self.mode == "VALIDATING":
            if elapsed > 500 and self.current_gaze:
                tx, ty = self._current_xy()
                gx, gy = self.current_gaze
                dist = math.sqrt((tx - gx)**2 + (ty - gy)**2)
                self.validation_errors.append(dist)
            if elapsed >= VALIDATION_DURATION_MS and not self.confirmed:
                self.confirmed = True
                QTimer.singleShot(250, self._move_to_next)

    def _move_to_next(self):
        self.point_index += 1
        self.confirmed    = False
        self.fill_active  = False
        self.fill_start   = None

        pts = self._current_points()

        if self.point_index >= len(pts):
            if self.mode == "CALIBRATING":
                if CALIBRATION_PASSES > 1:
                    print("[calib] Training pass 1 complete. Starting pass 2 ...")
                    self.mode = "CALIBRATING_PASS2"
                    self.calibration_pass = 2
                    self.point_index = 0
                    QTimer.singleShot(PHASE_TRANSITION_DELAY_MS, self._activate_next)
                elif REFINE_CORNERS_ENABLED:
                    print("[calib] Training complete. Refining corners ...")
                    self.mode = "REFINE_CORNERS"
                    self.point_index = 0
                    QTimer.singleShot(PHASE_TRANSITION_DELAY_MS, self._activate_next)
                else:
                    print("[calib] Training complete. Starting validation ...")
                    self.sio.emit('calibration_complete', {})
                    self.mode        = "VALIDATING"
                    self.point_index = 0
                    QTimer.singleShot(PHASE_TRANSITION_DELAY_MS, self._activate_next)
            elif self.mode == "CALIBRATING_PASS2":
                if REFINE_CORNERS_ENABLED:
                    print("[calib] Training pass 2 complete. Refining corners ...")
                    self.mode = "REFINE_CORNERS"
                    self.point_index = 0
                    QTimer.singleShot(PHASE_TRANSITION_DELAY_MS, self._activate_next)
                else:
                    print("[calib] Training complete. Starting validation ...")
                    self.sio.emit('calibration_complete', {})
                    self.mode        = "VALIDATING"
                    self.point_index = 0
                    QTimer.singleShot(PHASE_TRANSITION_DELAY_MS, self._activate_next)
            elif self.mode == "REFINE_CORNERS":
                print("[calib] Corner refinement complete. Starting validation ...")
                self.sio.emit('calibration_complete', {})
                self.mode        = "VALIDATING"
                self.point_index = 0
                QTimer.singleShot(PHASE_TRANSITION_DELAY_MS, self._activate_next)
            else:
                self._finish()
        else:
            QTimer.singleShot(INTER_POINT_DELAY_MS, self._activate_next)

    def _activate_next(self):
        self.fill_active = True
        self.fill_start  = time.time()
        self._point_sent = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Semi-transparent dark background
        painter.fillRect(self.rect(), QColor(0, 0, 0, 178))

        if self.mode == "DONE":
            self._draw_final_report(painter)
            painter.end()
            return

        x, y = self._current_xy()

        # Label text
        font = QFont('Arial', 18)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 204))

        if self.mode == "CALIBRATING":
            label = (f"Training Pass 1: Point {self.point_index + 1} of "
                     f"{len(CALIBRATION_POINTS_PCT)}")
            sub   = "Look directly at the red dot"
        elif self.mode == "CALIBRATING_PASS2":
            label = (f"Training Pass 2: Point {self.point_index + 1} of "
                     f"{len(CALIBRATION_POINTS_PCT)}")
            sub   = "Hold steady for stronger fit"
        elif self.mode == "REFINE_CORNERS":
            label = (f"Refine Corners: Point {self.point_index + 1} of "
                     f"{len(CORNER_REFINEMENT_POINTS_PCT)}")
            sub   = "Focus on corner accuracy"
        else:
            label = f"Accuracy Check {self.point_index + 1} of {len(VALIDATION_POINTS_PCT)}"
            sub   = "Stay focused for precision measurement"

        fm = QFontMetrics(font)
        lw = fm.horizontalAdvance(label)
        painter.drawText(int((self.W - lw) / 2), self.H // 2 - 100, label)

        font.setPointSize(13)
        painter.setFont(font)
        fm2 = QFontMetrics(font)
        sw  = fm2.horizontalAdvance(sub)
        painter.drawText(int((self.W - sw) / 2), self.H // 2 - 70, sub)

        # Outer white ring (background)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 51))
        painter.drawEllipse(
            QPointF(x, y),
            float(DOT_OUTER_RADIUS),
            float(DOT_OUTER_RADIUS)
        )

        # Progress arc
        if self.fill_active and self.fill_start is not None:
            elapsed = (time.time() - self.fill_start) * 1000
            dur     = (FILL_DURATION_MS if self.mode in ("CALIBRATING", "CALIBRATING_PASS2", "REFINE_CORNERS")
                       else VALIDATION_DURATION_MS)
            progress = min(elapsed / dur, 1.0)

            if self.mode in ("CALIBRATING", "CALIBRATING_PASS2", "REFINE_CORNERS"):
                arc_color = QColor(46, 217, 115, 242)
            else:
                arc_color = QColor(51, 179, 255, 242)

            pen = QPen(arc_color, 4)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)

            # drawArc uses 1/16th degrees, starts at 90° (top), goes CCW
            span = int(-progress * 360 * 16)
            painter.drawArc(
                int(x - DOT_OUTER_RADIUS + 2),
                int(y - DOT_OUTER_RADIUS + 2),
                (DOT_OUTER_RADIUS - 2) * 2,
                (DOT_OUTER_RADIUS - 2) * 2,
                90 * 16, span
            )

        # Inner dot (red = uncollected, green = done)
        painter.setPen(Qt.NoPen)
        if self.confirmed:
            painter.setBrush(QColor(46, 217, 115, 255))
        else:
            painter.setBrush(QColor(255, 71, 87, 255))
        painter.drawEllipse(
            QPointF(x, y),
            float(DOT_INNER_RADIUS),
            float(DOT_INNER_RADIUS)
        )

        painter.end()

    def _draw_final_report(self, painter):
        painter.setPen(QColor(255, 255, 255, 255))

        font = QFont('Arial', 32)
        font.setBold(True)
        painter.setFont(font)
        title = "Calibration Perfected"
        fm    = QFontMetrics(font)
        painter.drawText(int((self.W - fm.horizontalAdvance(title)) / 2),
                         self.H // 2 - 40, title)

        font.setPointSize(18)
        font.setBold(False)
        painter.setFont(font)
        score = (f"Accuracy Score: {int(self.final_accuracy)}px"
                 if self.final_accuracy else "Success")
        fm2   = QFontMetrics(font)
        painter.drawText(int((self.W - fm2.horizontalAdvance(score)) / 2),
                         self.H // 2 + 20, score)

        msg = "Ready to start gaze control."
        painter.drawText(int((self.W - fm2.horizontalAdvance(msg)) / 2),
                         self.H // 2 + 60, msg)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            QApplication.quit()

    def _finish(self):
        if self.validation_errors:
            self.final_accuracy = sum(self.validation_errors) / len(self.validation_errors)
        self.mode = "DONE"
        self.update()
        QTimer.singleShot(2000, QApplication.quit)


# ── Main ─────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    sio_client  = sio_module.Client(logger=False, engineio_logger=False)
    ready_event = threading.Event()

    @sio_client.on('browser_ready')
    def on_browser_ready(data):
        print("[calib] Browser is ready — launching calibration overlay.")
        ready_event.set()

    @sio_client.on('connect')
    def on_connect():
        print("[calib] Connected — registering ...")
        sio_client.emit('register', {'type': 'calibration'})

    print("[calib] Connecting to gaze server ...")
    retries = 0
    while retries < 20:
        try:
            sio_client.connect(SERVER_URL)
            break
        except Exception as e:
            retries += 1
            print(f"[calib] Waiting for server ({retries}/20): {e}")
            time.sleep(1)
    else:
        print("[calib] ERROR: Could not connect after 20 attempts.")
        sys.exit(1)

    print("[calib] Waiting for browser (WebGazer) ...")
    ready_event.wait(timeout=15)
    if not ready_event.is_set():
        print("[calib] Warning: browser_ready timeout — proceeding anyway.")

    qt_app = QApplication(sys.argv)
    win    = CalibrationOverlay(sio_client)
    qt_app.exec_()

    try:
        sio_client.disconnect()
    except Exception:
        pass
    print("[calib] Calibration process exiting.")
