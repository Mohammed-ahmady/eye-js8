"""
Gaze Mouse System — Native GTK Calibration Overlay
=====================================================
Displays a fullscreen transparent overlay on the REAL display with
calibration dots. When the user clicks each dot, the real screen
coordinate is sent to server.py, which relays it to WebGazer in the
hidden browser for regression training.

This process runs on the REAL display ($DISPLAY) while the browser
runs on Xvfb (:99). It exits cleanly once all calibration points are done.

Dependencies (system, not pip):
    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdk-3.0
"""

import threading
import time
import sys

# ── GTK ────────────────────────────────────────────────────────────────────────
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib
import math

# ── Socket.IO client ───────────────────────────────────────────────────────────
import socketio as sio_module

SERVER_URL = 'http://localhost:5000'

# 13-point calibration grid
CALIBRATION_POINTS_PCT = [
    (0.025, 0.025), (0.50, 0.025), (0.975, 0.025),
    (0.025, 0.25),  (0.975, 0.25),
    (0.025, 0.50),  (0.50, 0.50),  (0.975, 0.50),
    (0.025, 0.75),  (0.975, 0.75),
    (0.025, 0.975), (0.50, 0.975), (0.975, 0.975),
]

# 5-point validation grid
VALIDATION_POINTS_PCT = [
    (0.50, 0.50), # Center
    (0.05, 0.05), # Top Left
    (0.95, 0.05), # Top Right
    (0.05, 0.95), # Bottom Left
    (0.95, 0.95), # Bottom Right
]

# Visual constants
DOT_OUTER_RADIUS  = 24
DOT_INNER_RADIUS  =  9
FILL_DURATION_MS  = 2200   # ms to complete a point
SAMPLE_INTERVAL_MS = 100   # send a sample every 100ms
VALIDATION_DURATION_MS = 1500 # ms to collect gaze for accuracy check

class CalibrationOverlay(Gtk.Window):
    def __init__(self, sio_client):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.sio          = sio_client
        self.point_index  = 0
        self.fill_start   = None
        self.fill_active  = False
        self.confirmed    = False
        self._anim_id     = None
        self._last_sample_time = 0

        # Validation state
        self.mode = "CALIBRATING" # "CALIBRATING", "VALIDATING", "DONE"
        self.validation_errors = []
        self.current_gaze = None
        self.final_accuracy = None

        # Screen geometry
        display  = Gdk.Display.get_default()
        monitor  = display.get_primary_monitor()
        if not monitor:
            screen = display.get_default_screen()
            monitor = display.get_monitor_at_window(screen.get_active_window())
            
        geometry = monitor.get_geometry()
        self.W   = geometry.width
        self.H   = geometry.height

        # Window setup
        self.set_default_size(self.W, self.H)
        self.move(0, 0)
        self.fullscreen()
        self.set_keep_above(True)
        self.set_app_paintable(True)
        self.set_decorated(False)

        # RGBA visual for transparency
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

        # Drawing area
        self.canvas = Gtk.DrawingArea()
        self.canvas.connect('draw', self._on_draw)
        self.add(self.canvas)

        # Socket events
        self.sio.on('current_gaze', self._on_gaze)

        # Input
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.connect('key-press-event',      self._on_key)
        self.connect('delete-event',         Gtk.main_quit)

        self.show_all()
        GLib.timeout_add(1500, self._start_first_dot)
        self._anim_id = GLib.timeout_add(16, self._tick)

    def _on_gaze(self, data):
        self.current_gaze = (data['x'], data['y'])

    def _start_first_dot(self):
        self.fill_active = True
        self.fill_start = time.time()
        return False

    def _current_xy(self):
        pts = CALIBRATION_POINTS_PCT if self.mode == "CALIBRATING" else VALIDATION_POINTS_PCT
        if self.point_index >= len(pts):
            return 0, 0
        px, py = pts[self.point_index]
        return int(px * self.W), int(py * self.H)

    def _tick(self):
        self.canvas.queue_draw()
        now = time.time()

        if self.fill_active and self.fill_start is not None:
            elapsed = (now - self.fill_start) * 1000
            
            if self.mode == "CALIBRATING":
                if not self.confirmed and (now - self._last_sample_time) * 1000 >= SAMPLE_INTERVAL_MS:
                    if elapsed > 400:
                        x, y = self._current_xy()
                        self.sio.emit('calibrate_point', {'x': x, 'y': y})
                        self._last_sample_time = now

                if elapsed >= FILL_DURATION_MS and not self.confirmed:
                    self.confirmed = True
                    GLib.timeout_add(250, self._move_to_next)

            elif self.mode == "VALIDATING":
                # High-frequency data collection for accuracy check
                if elapsed > 500 and self.current_gaze:
                    tx, ty = self._current_xy()
                    gx, gy = self.current_gaze
                    dist = math.sqrt((tx - gx)**2 + (ty - gy)**2)
                    self.validation_errors.append(dist)

                if elapsed >= VALIDATION_DURATION_MS and not self.confirmed:
                    self.confirmed = True
                    GLib.timeout_add(250, self._move_to_next)

        return True

    def _move_to_next(self):
        self.point_index += 1
        self.confirmed = False
        self.fill_active = False
        self.fill_start = None
        
        pts = CALIBRATION_POINTS_PCT if self.mode == "CALIBRATING" else VALIDATION_POINTS_PCT
        
        if self.point_index >= len(pts):
            if self.mode == "CALIBRATING":
                print("Training complete. Starting validation ...")
                self.sio.emit('calibration_complete', {}) # Trigger streaming in browser
                self.mode = "VALIDATING"
                self.point_index = 0
                GLib.timeout_add(1000, self._activate_next)
            else:
                self._finish()
        else:
            GLib.timeout_add(400, self._activate_next)
        return False

    def _activate_next(self):
        self.fill_active = True
        self.fill_start = time.time()
        self._last_sample_time = 0
        return False

    def _on_draw(self, widget, cr):
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.70)
        cr.paint()

        if self.mode == "DONE":
            self._draw_final_report(cr)
            return

        x, y = self._current_xy()
        
        # Labels
        cr.set_source_rgba(1, 1, 1, 0.8)
        cr.select_font_face('Sans', 0, 0)
        if self.mode == "CALIBRATING":
            label = f"Training Point {self.point_index + 1} of {len(CALIBRATION_POINTS_PCT)}"
            sub = "Look directly at the red dot"
        else:
            label = f"Accuracy Check {self.point_index + 1} of {len(VALIDATION_POINTS_PCT)}"
            sub = "Stay focused for precision measurement"

        cr.set_font_size(24)
        ext = cr.text_extents(label)
        cr.move_to((self.W - ext[2]) / 2, self.H / 2 - 100)
        cr.show_text(label)
        
        cr.set_font_size(16)
        ext = cr.text_extents(sub)
        cr.move_to((self.W - ext[2]) / 2, self.H / 2 - 70)
        cr.show_text(sub)

        # Dot
        cr.set_source_rgba(1, 1, 1, 0.2)
        cr.arc(x, y, DOT_OUTER_RADIUS, 0, 2 * math.pi)
        cr.fill()

        if self.fill_active and self.fill_start is not None:
            elapsed = (time.time() - self.fill_start) * 1000
            dur = FILL_DURATION_MS if self.mode == "CALIBRATING" else VALIDATION_DURATION_MS
            progress = min(elapsed / dur, 1.0)
            angle = -math.pi / 2
            end_ang = angle + 2 * math.pi * progress

            color = (0.18, 0.85, 0.45) if self.mode == "CALIBRATING" else (0.2, 0.7, 1.0)
            cr.set_source_rgba(*color, 0.95)
            cr.set_line_width(4)
            cr.arc(x, y, DOT_OUTER_RADIUS - 2, angle, end_ang)
            cr.stroke()

        if self.confirmed:
            cr.set_source_rgba(0.18, 0.85, 0.45, 1.0)
        else:
            cr.set_source_rgba(1.0, 0.28, 0.34, 1.0)
        cr.arc(x, y, DOT_INNER_RADIUS, 0, 2 * math.pi)
        cr.fill()

    def _draw_final_report(self, cr):
        cr.set_source_rgba(1, 1, 1, 1.0)
        cr.select_font_face('Sans', 1, 0)
        cr.set_font_size(42)
        title = "Calibration Perfected"
        ext = cr.text_extents(title)
        cr.move_to((self.W - ext[2]) / 2, self.H / 2 - 40)
        cr.show_text(title)

        cr.set_font_size(24)
        score = f"Accuracy Score: {int(self.final_accuracy)}px" if self.final_accuracy else "Success"
        ext = cr.text_extents(score)
        cr.move_to((self.W - ext[2]) / 2, self.H / 2 + 20)
        cr.show_text(score)
        
        cr.set_font_size(18)
        msg = "Ready to start gaze control."
        ext = cr.text_extents(msg)
        cr.move_to((self.W - ext[2]) / 2, self.H / 2 + 60)
        cr.show_text(msg)

    def _on_key(self, widget, event):
        from gi.repository import Gdk as _Gdk
        if event.keyval == _Gdk.KEY_Escape:
            Gtk.main_quit()

    def _finish(self):
        if self.validation_errors:
            self.final_accuracy = sum(self.validation_errors) / len(self.validation_errors)
        self.mode = "DONE"
        self.canvas.queue_draw()
        
        # Close automatically after 2 seconds or instant handoff
        GLib.timeout_add(2000, Gtk.main_quit)
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Single persistent client — no double connect/disconnect dance.
    # No transport restriction so polling fallback works if needed.
    sio_client  = sio_module.Client(logger=False, engineio_logger=False)
    ready_event = threading.Event()

    @sio_client.on('browser_ready')
    def on_browser_ready(data):
        print("Browser is ready — launching calibration overlay.")
        ready_event.set()

    @sio_client.on('connect')
    def on_connect():
        print("Calibration client connected — registering ...")
        sio_client.emit('register', {'type': 'calibration'})

    # Retry connecting until the server is up (server.py may still be starting)
    print("Connecting to gaze server ...")
    retries = 0
    while retries < 20:
        try:
            sio_client.connect(SERVER_URL)   # no transport restriction
            break
        except Exception as e:
            retries += 1
            print(f"  Waiting for server ({retries}/20): {e}")
            time.sleep(1)
    else:
        print("ERROR: Could not connect to gaze server after 20 attempts.")
        print("Make sure server.py started without errors.")
        sys.exit(1)

    # Wait for the browser to register with the server
    print("Waiting for browser (WebGazer) to be ready ...")
    ready_event.wait(timeout=15)
    if not ready_event.is_set():
        print("Warning: browser_ready timeout — proceeding with calibration anyway.")

    # Launch GTK on the main thread (must be main thread on Linux)
    win = CalibrationOverlay(sio_client)
    Gtk.main()

    # Cleanup
    try:
        sio_client.disconnect()
    except Exception:
        pass

    print("Calibration process exiting.")
