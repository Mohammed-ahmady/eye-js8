"""
Gaze Mouse System — Permanent HUD Overlay
===========================================
Displays a transparent overlay on the REAL display that shows:
  1. A cursor at the current smoothed gaze position.
  2. A progress ring for dwell-to-click.

This process runs on the REAL display ($DISPLAY).
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib
import cairo
import math
import time
import socketio as sio_module
import sys

# ── Configuration ──────────────────────────────────────────────────────────────
SERVER_URL = 'http://localhost:5000'
RING_RADIUS      = 35
GLOW_RADIUS      = 55
WOBBLE_STRENGTH  = 4
WOBBLE_SPEED     = 10.0  # Hz-ish

class GazeHud(Gtk.Window):
    def __init__(self, sio_client):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.sio = sio_client
        self.gaze_x = -100
        self.gaze_y = -100
        self.current_x = -100
        self.current_y = -100
        self.dwell_progress = 0.0
        self.is_locked = False
        self.over_clickable = False
        self.boot_time = time.time()

        # Radial Menu State
        self.radial_active = False
        self.radial_x = 0
        self.radial_y = 0
        self.radial_slice = None
        self.radial_progress = 0.0

        # Screen geometry
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor()
        if not monitor:
            screen = display.get_default_screen()
            monitor = display.get_monitor_at_window(screen.get_active_window())
        
        geometry = monitor.get_geometry()
        self.W = geometry.width
        self.H = geometry.height

        # Window setup
        self.set_default_size(self.W, self.H)
        self.move(0, 0)
        self.set_keep_above(True)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_accept_focus(False)
        self.set_can_focus(False)
        
        # Enable transparency
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

        # Drawing area
        self.canvas = Gtk.DrawingArea()
        self.canvas.connect('draw', self._on_draw)
        self.add(self.canvas)

        # Socket events
        self.sio.on('hud_update', self._on_hud_update)
        self.sio.on('hud_radial_menu', self._on_radial_menu)
        
        self.show_all()
        
        # Click-through: Define an empty input region
        try:
            surface = Gdk.Window.create_similar_surface(
                self.get_window(), 
                Gdk.Window.get_content(self.get_window()),
                1, 1
            )
            self.get_window().input_shape_combine_region(None)
        except Exception as e:
            print(f"Non-critical: input_shape setup: {e}")

        # Animation loop
        GLib.timeout_add(16, self._tick)

    def _on_hud_update(self, data):
        self.gaze_x = data.get('x', self.gaze_x)
        self.gaze_y = data.get('y', self.gaze_y)
        self.dwell_progress = data.get('dwell', 0.0)
        self.is_locked = data.get('locked', False)
        self.over_clickable = data.get('over_clickable', False)

    def _on_radial_menu(self, data):
        self.radial_active = data.get('active', False)
        self.radial_x = data.get('x', self.radial_x)
        self.radial_y = data.get('y', self.radial_y)
        self.radial_slice = data.get('slice', None)
        self.radial_progress = data.get('progress', 0.0)

    def _tick(self):
        # REMOVED: extra EMA smoothing that was adding 40-120ms of lag.
        self.current_x = self.gaze_x
        self.current_y = self.gaze_y
        self.canvas.queue_draw()
        return True

    def _on_draw(self, widget, cr):
        # 1. Clear background
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(0) # CLEAR
        cr.paint()
        cr.set_operator(1) # OVER

        if self.current_x < 0: return

        cx, cy = self.current_x, self.current_y
        t = (time.time() - self.boot_time) * WOBBLE_SPEED

        # ── Radial Menu Layer ──────────────────────────────────────────────────
        if self.radial_active:
            # Draw semi-transparent dimming background
            cr.set_source_rgba(0, 0, 0, 0.4)
            cr.paint()
            
            rx, ry = self.radial_x, self.radial_y
            inner_r = 40
            outer_r = 150
            num_slices = 5
            labels = ["Double", "Left", "Scroll", "Right", "Cancel"]
            
            for i in range(num_slices):
                start_angle = -math.pi/2 + (i * 2 * math.pi / num_slices)
                end_angle = -math.pi/2 + ((i + 1) * 2 * math.pi / num_slices)
                
                # Draw slice path
                cr.new_path()
                cr.arc(rx, ry, outer_r, start_angle, end_angle)
                cr.arc_negative(rx, ry, inner_r, end_angle, start_angle)
                cr.close_path()
                
                if self.radial_slice == i:
                    cr.set_source_rgba(0.2, 0.7, 1.0, 0.9) # Highlight Blue
                    cr.fill_preserve()
                    # Progress overlay
                    if self.radial_progress > 0:
                        p_angle = start_angle + (end_angle - start_angle) * self.radial_progress
                        cr.new_path()
                        cr.arc(rx, ry, outer_r, start_angle, p_angle)
                        cr.arc_negative(rx, ry, inner_r, p_angle, start_angle)
                        cr.close_path()
                        cr.set_source_rgba(0.2, 1.0, 0.6, 0.95) # Green
                        cr.fill()
                        
                        # Re-stroke outline for neatness
                        cr.new_path()
                        cr.arc(rx, ry, outer_r, start_angle, end_angle)
                        cr.arc_negative(rx, ry, inner_r, end_angle, start_angle)
                        cr.close_path()
                else:
                    cr.set_source_rgba(0.1, 0.1, 0.15, 0.85) # Dark slate
                    cr.fill_preserve()
                    
                cr.set_source_rgba(0.4, 0.4, 0.5, 0.5)
                cr.set_line_width(2.5)
                cr.stroke()
                
                # Draw text
                mid_angle = (start_angle + end_angle) / 2
                text_r = inner_r + (outer_r - inner_r) / 2
                tx = rx + math.cos(mid_angle) * text_r
                ty = ry + math.sin(mid_angle) * text_r
                
                cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
                cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(15)
                te = cr.text_extents(labels[i])
                cr.move_to(tx - te.width/2, ty + te.height/2)
                cr.show_text(labels[i])

            # Draw center point
            cr.set_source_rgba(1, 1, 1, 0.8)
            cr.arc(rx, ry, 6, 0, 2 * math.pi)
            cr.fill()

        # ── Standard Cursor Layer ─────────────────────────────────────────────
        # 2. Glow Layer (Diffusion)
        for i in range(3):
            alpha = (0.15 - (i * 0.04))
            radius = GLOW_RADIUS + (i * 10)
            cr.set_source_rgba(0.2, 0.7, 1.0, alpha)
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.fill()

        # 3. The Liquid Ring
        cr.set_line_width(3.5)
        if self.is_locked:
            cr.set_source_rgba(1.0, 0.8, 0.2, 0.9)  # Gold
        elif self.over_clickable:
            cr.set_source_rgba(0.2, 1.0, 0.6, 0.9)  # Spring Green for clickable
        else:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.85) # White

        points = 60
        for i in range(points + 1):
            angle = (i / points) * 2 * math.pi
            wobble = math.sin(t + angle * 3) * WOBBLE_STRENGTH
            wobble += math.cos(t * 0.7 + angle * 5) * (WOBBLE_STRENGTH / 2)
            r = RING_RADIUS + wobble
            px = cx + math.cos(angle) * r
            py = cy + math.sin(angle) * r
            if i == 0:
                cr.move_to(px, py)
            else:
                cr.line_to(px, py)
        cr.stroke()

        # 4. Inner Dot
        cr.set_source_rgba(1, 1, 1, 0.4)
        cr.arc(cx, cy, 4, 0, 2 * math.pi)
        cr.fill()

        # 5. Dwell Progress
        if self.dwell_progress > 0 and not self.radial_active:
            prog_angle = -math.pi / 2
            end_ang = prog_angle + 2 * math.pi * self.dwell_progress
            cr.set_line_width(6)
            cr.set_source_rgba(0.18, 0.85, 0.45, 0.9)
            cr.arc(cx, cy, RING_RADIUS + 6, prog_angle, end_ang)
            cr.stroke()

if __name__ == '__main__':
    sio_client = sio_module.Client()
    
    @sio_client.on('connect')
    def on_connect():
        sio_client.emit('register', {'type': 'hud'})
        print("HUD registered with server")

    # Important: HUD must run on the display used for control (:0)
    try:
        sio_client.connect(SERVER_URL)
        win = GazeHud(sio_client)
        Gtk.main()
    except Exception as e:
        print(f"HUD Error: {e}")
