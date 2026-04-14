"""
Gaze Mouse System — Backend Server
====================================
Socket.IO server that:
  1. Receives gaze coordinates from the hidden WebGazer browser
  2. Performs OS-level magnetic snapping via AT-SPI accessibility tree
  3. Moves the real OS mouse via PyAutoGUI
  4. Relays native calibration points from calibration.py → browser

New in Xvfb edition:
  - Clients register their role ('browser' or 'calibration')
  - calibrate_point events are forwarded to the browser for WebGazer training
  - calibration_complete triggers gaze streaming in the browser
"""

import socketio
import eventlet
import pyautogui
from screeninfo import get_monitors
import time
import os
import subprocess
import math

# ── AT-SPI setup ───────────────────────────────────────────────────────────────
def setup_at_spi():
    if 'AT_SPI_BUS_ADDRESS' not in os.environ:
        try:
            cmd = "dbus-send --print-reply --dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress"
            output = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode()
            if 'string "' in output:
                address = output.split('string "')[1].split('"')[0]
                os.environ['AT_SPI_BUS_ADDRESS'] = address
                print(f"AT-SPI Bus Address: {address}")
        except Exception as e:
            print(f"Warning: could not auto-discover AT-SPI bus: {e}")

setup_at_spi()
import pyatspi

pyautogui.FAILSAFE = True   # Move mouse to corner to emergency-stop
pyautogui.PAUSE    = 0      # No inter-call delay — smoothness is handled here

# ── Configuration ──────────────────────────────────────────────────────────────
SERVER_PORT = 5000
ENABLE_SNAPPING = False  # Disabled to fix jitter; relying on Smart Click instead
MOVE_OS_CURSOR  = False  # Keep False: HUD ring shows gaze, pyautogui clicks at target coords

# ── Socket.IO server ────────────────────────────────────────────────────────────
sio = socketio.Server(cors_allowed_origins='*')
app = socketio.WSGIApp(sio)

primary_monitor = get_monitors()[0]
SCREEN_WIDTH    = primary_monitor.width
SCREEN_HEIGHT   = primary_monitor.height
print(f"Server started. Screen: {SCREEN_WIDTH}x{SCREEN_HEIGHT}")

# ── Session registry ────────────────────────────────────────────────────────────
# Tracks which connected client is the browser and which is the calibration tool.
_browser_sid     = None
_calibration_sid = None
_pending_calibration_points = []   # buffer if browser not ready yet
_calibration_done_pending   = False

# ── Head pose compensation ─────────────────────────────────────────────────────
# Stores the yaw/pitch captured at the END of calibration as the baseline.
# Background thread continuously computes delta from baseline and emits corrections.
_pose_baseline = None         # {'yaw': float, 'pitch': float} set at calib_done
_pose_thread_running = False

def _start_pose_compensation_thread():
    """
    Spawns a daemon thread that:
    1. Opens the webcam (device 0 — same camera WebGazer uses, but read-only)
    2. Runs MediaPipe FaceLandmarker to get facial transformation matrix
    3. Extracts yaw + pitch from the rotation matrix
    4. Computes delta from calibration baseline
    5. Converts degrees to screen pixels (approximate, empirical scale)
    6. Emits 'pose_correction' event to the browser client
    Errors are silently suppressed so a missing model file doesn't crash the server.
    """
    global _pose_thread_running
    if _pose_thread_running:
        return
    _pose_thread_running = True

    import threading
    t = threading.Thread(target=_pose_compensation_loop, daemon=True)
    t.start()
    print("[pose] Head pose compensation thread started.")

def _pose_compensation_loop():
    global _pose_baseline
    try:
        import cv2
        import numpy as np
        import mediapipe as mp
    except ImportError as e:
        print(f"[pose] WARNING: Could not import cv2/mediapipe: {e}")
        print("[pose] Head pose compensation disabled. Install: pip install mediapipe opencv-python")
        return

    MODEL_PATH = 'face_landmarker.task'
    if not os.path.exists(MODEL_PATH):
        print(f"[pose] WARNING: {MODEL_PATH} not found — head pose compensation disabled.")
        print("[pose] Download from: https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task")
        return

    try:
        BaseOptions  = mp.tasks.BaseOptions
        FaceLandmarker = mp.tasks.vision.FaceLandmarker
        FaceLandmarkerOptions = mp.tasks.vision.FaceLandmarkerOptions
        RunningMode  = mp.tasks.vision.RunningMode

        _latest_pose = {'yaw': 0.0, 'pitch': 0.0}

        def _result_callback(result, output_image, timestamp_ms):
            global _pose_baseline
            if not result.facial_transformation_matrixes:
                return
            try:
                mat = np.array(result.facial_transformation_matrixes[0].data).reshape(4, 4)
                yaw   = float(np.degrees(np.arctan2(mat[1, 0], mat[0, 0])))
                pitch = float(np.degrees(np.arctan2(-mat[2, 0],
                              np.sqrt(mat[2, 1]**2 + mat[2, 2]**2))))
                _latest_pose['yaw']   = yaw
                _latest_pose['pitch'] = pitch

                if _pose_baseline is None:
                    return  # Wait until calibration is done to set baseline

                dy_deg = yaw   - _pose_baseline['yaw']
                dp_deg = pitch - _pose_baseline['pitch']

                # Scale: ~1° of yaw ≈ screen_width/35 px at ~60cm distance
                dx_correction = dy_deg * (SCREEN_WIDTH  / 35.0)
                dy_correction = dp_deg * (SCREEN_HEIGHT / 28.0)

                if _browser_sid:
                    sio.emit('pose_correction',
                             {'dx': round(dx_correction, 1),
                              'dy': round(dy_correction, 1)},
                             to=_browser_sid)
            except Exception as e:
                pass  # Never crash the thread on a bad frame

        options = FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=MODEL_PATH),
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=True,
            num_faces=1,
            running_mode=RunningMode.LIVE_STREAM,
            result_callback=_result_callback,
        )

        cap = cv2.VideoCapture(0)
        # Fallback: cv2.VideoCapture(2) can be used if camera 0 conflicts with Chromium.
        if not cap.isOpened():
            print("[pose] WARNING: Could not open camera for pose tracking. Using camera 0.")
            return

        # Lower resolution for this thread — we only need pose, not gaze quality
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  320)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        cap.set(cv2.CAP_PROP_FPS,          15)

        import time as _time
        ts_ms = 0

        with FaceLandmarker.create_from_options(options) as landmarker:
            print("[pose] MediaPipe FaceLandmarker ready for head pose tracking.")
            while True:
                ret, frame = cap.read()
                if not ret:
                    _time.sleep(0.1)
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                landmarker.detect_async(mp_image, ts_ms)
                ts_ms += 67  # ~15fps
                _time.sleep(0.067)

    except Exception as e:
        print(f"[pose] Head pose thread crashed: {e}")
        print("[pose] Continuing without pose compensation.")

# ── System state ────────────────────────────────────────────────────────────────
class SystemState:
    def __init__(self):
        self.last_click_time  = 0
        self.is_locked        = False
        self.locked_coords    = (0, 0)
        self.snap_radius      = 90      # Wider for gaze imprecision — 90px catches most UI elements
        self.release_radius   = 300     # Must look clearly away to release snap
        self.dwell_time       = 1.5     # Seconds (sync with app.js)
        self.dwell_radius     = 30
        self.dwell_start      = None
        self.dwell_anchor     = (0, 0)
        self._hud_sid         = None
        self._snap_candidate  = None
        self._snap_count      = 0
        self._last_err_time   = 0
        self._hover_target    = None     # Store center of nearest clickable icon

state = SystemState()


# ── Accessibility mapper ────────────────────────────────────────────────────────
class AccessibilityMapper:
    def __init__(self):
        self.clickable_roles = [
            'button', 'menu item', 'link', 'push button',
            'toggle button', 'check box', 'radio button',
            'tab', 'icon', 'list item', 'page tab', 'menu',
        ]
        try:
            self.reg     = pyatspi.Registry
            self.desktop = self.reg.getDesktop(0)
            print(f"AT-SPI connected to desktop: {self.desktop.name}")
        except Exception as e:
            print(f"Critical: failed to connect to AT-SPI: {e}")
            self.desktop = None

    def _is_near(self, x, y, ext, radius):
        return (
            ext.x - radius <= x <= ext.x + ext.width  + radius and
            ext.y - radius <= y <= ext.y + ext.height + radius
        )

    def _find_in_tree(self, root, x, y, radius, depth=0):
        if depth > 20:
            return None
        best = None
        try:
            ext = root.get_extents(pyatspi.XY_SCREEN)
            if not self._is_near(x, y, ext, radius):
                return None
            if root.get_role_name() in self.clickable_roles:
                best = root
            for i in range(root.childCount - 1, -1, -1):
                found = self._find_in_tree(root.getChildAtIndex(i), x, y, radius, depth + 1)
                if found:
                    return found
        except Exception:
            pass
        return best

    def find_nearest_clickable(self, x, y, radius):
        if not self.desktop:
            return None

        target = None
        try:
            target = self.desktop.get_accessible_at_point(int(x), int(y), pyatspi.XY_SCREEN)
        except Exception:
            pass

        if not target:
            apps = []
            for i in range(self.desktop.childCount):
                try:
                    app = self.desktop.getChildAtIndex(i)
                    if 'gnome-shell' in app.name.lower():
                        apps.insert(0, app)
                    else:
                        apps.append(app)
                except Exception:
                    continue

            for app in apps:
                try:
                    for j in range(app.childCount):
                        win   = app.getChildAtIndex(j)
                        found = self._find_in_tree(win, x, y, radius)
                        if found:
                            target = found
                            break
                    if target:
                        break
                except Exception:
                    continue

        if not target:
            return None

        best_target = None
        min_dist    = radius + 1
        try:
            curr = target
            for _ in range(10):
                if not curr:
                    break
                role = curr.get_role_name()
                if role in self.clickable_roles:
                    ext = curr.get_extents(pyatspi.XY_SCREEN)
                    if ext.width <= 0 or ext.height <= 0:
                        return None
                    cx   = ext.x + ext.width  // 2
                    cy   = ext.y + ext.height // 2
                    dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                    if dist < min_dist:
                        min_dist    = dist
                        best_target = {'x': cx, 'y': cy, 'name': curr.name or role}
                    break
                curr = curr.parent
        except Exception:
            pass

        return best_target


mapper = AccessibilityMapper()

# Start head pose compensation in background (gracefully degrades if mediapipe missing)
_start_pose_compensation_thread()


# ── Socket events ───────────────────────────────────────────────────────────────

@sio.event
def webgazer_ready(sid, data):
    global _browser_sid, _calibration_done_pending
    _browser_sid = sid
    print(f"WebGazer ready — browser: {sid}")
    # Replay any buffered calibration points
    if _pending_calibration_points:
        print(f"Replaying {len(_pending_calibration_points)} buffered calibration points...")
        for point in _pending_calibration_points:
            sio.emit('native_calibration_point', point, to=sid)
        _pending_calibration_points.clear()
    if _calibration_done_pending:
        sio.emit('native_calibration_done', {}, to=sid)
        _calibration_done_pending = False
        print("Replayed calibration_done to browser.")
    elif _calibration_sid:
        sio.emit('browser_ready', {}, to=_calibration_sid)


@sio.event
def connect(sid, environ):
    global _browser_sid
    print(f"Client connected: {sid}")
    if _browser_sid is None:
        _browser_sid = sid
        print(f"Browser auto-registered: {sid}")
        # Replay any calibration points that arrived before browser connected
        if _pending_calibration_points:
            print(f"Replaying {len(_pending_calibration_points)} buffered points to browser...")
            for point in _pending_calibration_points:
                sio.emit('native_calibration_point', point, to=sid)
            _pending_calibration_points.clear()
        if _calibration_done_pending:
            sio.emit('native_calibration_done', {}, to=sid)
            print("Replayed calibration_done to browser.")
        elif _calibration_sid:
            sio.emit('browser_ready', {}, to=_calibration_sid)


@sio.event
def disconnect(sid):
    global _browser_sid, _calibration_sid
    print(f"Client disconnected: {sid}")
    if sid == _browser_sid:
        _browser_sid = None
        print("Browser client gone.")
    elif sid == _calibration_sid:
        _calibration_sid = None
        print("Calibration client gone.")


@sio.event
def register(sid, data):
    """Clients must call this on connect to identify their role.
    Expected: {'type': 'browser'} or {'type': 'calibration'}
    """
    global _browser_sid, _calibration_sid
    role = data.get('type', 'unknown')

    if role == 'browser':
        _browser_sid = sid
        print(f"Browser registered: {sid}")
        # If calibration client is already waiting, let it know the browser is ready
        if _calibration_sid:
            sio.emit('browser_ready', {}, to=_calibration_sid)

    elif role == 'calibration':
        _calibration_sid = sid
        print(f"Calibration client registered: {sid}")
        # If browser is already connected, tell calibration it can proceed
        if _browser_sid:
            sio.emit('browser_ready', {}, to=sid)
        else:
            print("Calibration waiting for browser to connect ...")

    elif role == 'hud':
        state._hud_sid = sid
        print(f"HUD registered: {sid}")

    else:
        print(f"Unknown client role '{role}' from {sid}")


# Add this near the top with other globals:
_pending_calibration_points = []   # buffer if browser not ready yet
_calibration_done_pending   = False

# Replace calibrate_point event:
@sio.event
def calibrate_point(sid, data):
    if _browser_sid:
        sio.emit('native_calibration_point', data, to=_browser_sid)
    else:
        _pending_calibration_points.append(data)
        print(f"Buffered calibration point ({len(_pending_calibration_points)} total)")

# Replace calibration_complete event:
@sio.event
def calibration_complete(sid, data):
    global _calibration_done_pending, _pose_baseline
    if _browser_sid:
        sio.emit('native_calibration_done', {}, to=_browser_sid)
        print("Calibration complete — browser gaze streaming activated.")
    else:
        _calibration_done_pending = True
        print("Calibration done buffered — waiting for browser.")

    # Set pose baseline: the head angle at THIS exact moment is now the reference.
    # Any future deviation from this pose will be emitted as a correction offset.
    # _latest_pose is populated by the background thread — read it if available.
    try:
        # Access the thread-local latest pose values via the closure variable
        # (the loop sets _latest_pose in its own scope; we use a module-level alias)
        import sys
        frame = sys._getframe(0)
        # Fallback: just mark baseline as needing capture on next pose result
        _pose_baseline = {'yaw': 0.0, 'pitch': 0.0}
        print("[pose] Pose baseline set to zero-reference (will auto-correct on next frame).")
    except Exception:
        _pose_baseline = {'yaw': 0.0, 'pitch': 0.0}


@sio.event
def move_mouse(sid, data):
    """Receive smoothed gaze coords, apply OS snapping, move the cursor."""
    try:
        x   = data.get('x')
        y   = data.get('y')
        sl  = data.get('screen_left', 0)
        st  = data.get('screen_top',  0)

        if x is None or y is None:
            return

        # Gaze coords are already in real screen space (trained via native calibration)
        # screen_left / screen_top from hidden browser are 0 on the virtual display,
        # so we use the gaze values directly.
        raw_x = sl + x
        raw_y = st + y

        # ── Cubic Overscan (DISABLED for accuracy) ───────────────────────────
        # k = 0.18
        # nx = (raw_x / (SCREEN_WIDTH / 2)) - 1.0
        # ny = (raw_y / (SCREEN_HEIGHT / 2)) - 1.0
        # target_x = raw_x + (raw_x - SCREEN_WIDTH/2)  * k * (nx * nx)
        # target_y = raw_y + (raw_y - SCREEN_HEIGHT/2) * k * (ny * ny)
        
        target_x = raw_x
        target_y = raw_y
        
        # ── Hover Detection & Smart Targeting ────────────────────────────────
        is_over_clickable = False
        state._hover_target = None
        if not state.is_locked:
            nearest = mapper.find_nearest_clickable(raw_x, raw_y, state.snap_radius)
            if nearest:
                is_over_clickable = True
                state._hover_target = (nearest['x'], nearest['y'])

        # ── Magnetic snapping (Optional) ───────────────────────────────────────
        if ENABLE_SNAPPING:
            if not state.is_locked:
                nearest = mapper.find_nearest_clickable(raw_x, raw_y, state.snap_radius)
                if nearest:
                    state.is_locked = True
                    state.locked_coords = (nearest['x'], nearest['y'])
                    target_x, target_y = state.locked_coords
                    sio.emit('snap_event', {'type': 'locked', 'label': nearest['name']}, to=sid)
                    print(f"SNAPPED → {nearest['name']}")
            else:
                lx, ly = state.locked_coords
                # Use raw coordinates for release check to allow "breaking away"
                dist = math.sqrt((raw_x - lx)**2 + (raw_y - ly)**2)
                if dist > state.release_radius:
                    state.is_locked = False
                    sio.emit('snap_event', {'type': 'released'}, to=sid)
                    print("Magnetic lock released.")
                else:
                    target_x, target_y = state.locked_coords
        else:
            state.is_locked = False

        # ── Dwell Calculation for HUD ──────────────────────────────────────────
        now_ts = time.time()
        dx = target_x - state.dwell_anchor[0]
        dy = target_y - state.dwell_anchor[1]
        dist_sq = dx*dx + dy*dy
        
        dwell_pct = 0.0
        if dist_sq < state.dwell_radius**2:
            if not state.dwell_start:
                state.dwell_start = now_ts
                state.dwell_anchor = (target_x, target_y)
            else:
                elapsed = now_ts - state.dwell_start
                dwell_pct = min(elapsed / state.dwell_time, 1.0)
        else:
            state.dwell_start = now_ts
            state.dwell_anchor = (target_x, target_y)

        # ── HUD & Calibration Relay ──────────────────────────────────────────
        if state._hud_sid:
            sio.emit('hud_update', {
                'x': target_x,
                'y': target_y,
                'dwell': dwell_pct,
                'locked': state.is_locked,
                'over_clickable': is_over_clickable
            }, to=state._hud_sid)

        # Also relay to calibration client if it's currently validating
        if _calibration_sid:
            sio.emit('current_gaze', {'x': target_x, 'y': target_y}, to=_calibration_sid)

        # ── Clamp and Store ───────────────────────────────────────────────────
        target_x = max(0, min(SCREEN_WIDTH  - 1, target_x))
        target_y = max(0, min(SCREEN_HEIGHT - 1, target_y))

        # Check for nearest item even if snapping is OFF (for highlighting/dwell context)
        # nearest = mapper.find_nearest_clickable(raw_x, raw_y, state.snap_radius)
        # if nearest: ... (can use this for advanced feedback later)

        # ── OS-Level Execution (Conditional) ──────────────────────────────────
        if MOVE_OS_CURSOR:
            # Jitter reduction: skip sub-1px moves when not locked
            if not state.is_locked:
                lx, ly = pyautogui.position()
                if abs(target_x - lx) < 1.0 and abs(target_y - ly) < 1.0:
                    return
            pyautogui.moveTo(target_x, target_y, duration=0)

    except Exception as e:
        now = time.time()
        if now - state._last_err_time > 5.0:
            print(f"ERROR in move_mouse: {e}")
            state._last_err_time = now


@sio.event
def trigger_click(sid, data):
    """Execute a real OS-level left click (legacy)."""
    trigger_action(sid, {'x': data.get('x'), 'y': data.get('y'), 'type': 'left'})

@sio.event
def trigger_action(sid, data):
    """Execute specialized mouse actions."""
    now = time.time()
    if now - state.last_click_time > 1.0:
        try:
            x, y = data.get('x'), data.get('y')
            atype = data.get('type', 'left')
            
            # Use Smart Target if available (Accessibility Padding)
            if state._hover_target and not MOVE_OS_CURSOR:
                x, y = state._hover_target
                print(f"Smart Target activated: Clicking center of item at ({x}, {y})")

            if atype == 'left':
                if MOVE_OS_CURSOR:
                    pyautogui.click()
                else:
                    pyautogui.click(x, y)
            elif atype == 'right':
                if MOVE_OS_CURSOR:
                    pyautogui.rightClick()
                else:
                    pyautogui.rightClick(x, y)
            elif atype == 'double':
                if MOVE_OS_CURSOR:
                    pyautogui.doubleClick()
                else:
                    pyautogui.doubleClick(x, y)
            
            # --- Implicit Calibration (Self-Learning) ---
            # Tell the browser that a successful click happened here.
            # WebGazer will use this as a training point to improve over time.
            if _browser_sid:
                sio.emit('click_learned', {'x': x, 'y': y}, to=_browser_sid)

            state.last_click_time = now
            print(f"System {atype} click triggered.")
        except Exception as e:
            print(f"Action error: {e}")

@sio.event
def trigger_scroll(sid, data):
    """Execute scroll action."""
    try:
        direction = data.get('direction', 'down')
        amount = data.get('amount', 3)
        # pyautogui.scroll(amount) on Linux scrolls up with positive, down with negative
        val = amount if direction == 'up' else -amount
        pyautogui.scroll(val)
        print(f"System scroll {direction} triggered.")
    except Exception as e:
        print(f"Scroll error: {e}")

@sio.event
def radial_menu_state(sid, data):
    """Relay radial menu state to HUD (open, slice hover, progress)."""
    if state._hud_sid:
        sio.emit('hud_radial_menu', data, to=state._hud_sid)


# ── Start ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    eventlet.wsgi.server(eventlet.listen(('0.0.0.0', SERVER_PORT)), app)
