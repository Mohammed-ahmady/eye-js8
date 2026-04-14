import ctypes
# Tell Windows this process is DPI-aware so coordinates are physical pixels, not logical
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

"""
Gaze Mouse System — Windows 11 Backend Server
===============================================
Windows port of server.py. Key differences from Linux version:
  - Uses uiautomation instead of pyatspi for accessibility tree snapping
  - Uses flask-socketio with threading mode instead of eventlet
  - No D-Bus / AT-SPI setup needed
  - Same Socket.IO event API so app.js works unchanged
"""

import os
import math
import time
import threading
import subprocess
import pythoncom

import pyautogui
from screeninfo import get_monitors

from flask import Flask
from flask_socketio import SocketIO, emit

try:
    pythoncom.CoInitialize()
except Exception:
    pass

# ── Screen setup ────────────────────────────────────────────────────────────────
pyautogui.FAILSAFE = False  # Disable fail-safe (gaze often hits corners)
pyautogui.PAUSE    = 0

monitors = get_monitors()
VLEFT    = min(m.x for m in monitors)
VTOP     = min(m.y for m in monitors)
VRIGHT   = max(m.x + m.width for m in monitors)
VBOTTOM  = max(m.y + m.height for m in monitors)
SCREEN_WIDTH  = VRIGHT - VLEFT
SCREEN_HEIGHT = VBOTTOM - VTOP
print(
    f"[server] Virtual desktop: left={VLEFT}, top={VTOP}, "
    f"right={VRIGHT}, bottom={VBOTTOM}, size={SCREEN_WIDTH}x{SCREEN_HEIGHT}"
)


def clamp_to_virtual_desktop(x, y):
    return (
        max(VLEFT, min(VRIGHT - 1, x)),
        max(VTOP, min(VBOTTOM - 1, y)),
    )


def map_gaze_to_virtual(x, y):
    # If gaze is already in global screen space, keep it as-is.
    if VLEFT <= x < VRIGHT and VTOP <= y < VBOTTOM:
        return x, y

    # Otherwise treat incoming gaze as local viewport coords and offset into global space.
    return VLEFT + x, VTOP + y

# ── Configuration ───────────────────────────────────────────────────────────────
SERVER_PORT     = 5000
ENABLE_SNAPPING = False   # Same default as Linux version
MOVE_OS_CURSOR  = True   # Same default as Linux version

# ── Flask + SocketIO (threading mode — safe on Windows) ─────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = 'gaze_mouse_win'
sio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ── Session registry ─────────────────────────────────────────────────────────────
_browser_sid     = None
_calibration_sid = None
_hud_sid         = None
_pending_calibration_points = []
_calibration_done_pending   = False
_registry_lock   = threading.Lock()
_last_scan_ms = 0
_cached_snap_target = None
_scan_inflight = False
_scan_lock = threading.Lock()

# ── Head pose state ──────────────────────────────────────────────────────────────
_pose_baseline       = None
_latest_pose         = None
_pose_thread_running = False
_pose_lock           = threading.Lock()

def _start_pose_compensation_thread():
    global _pose_thread_running
    if _pose_thread_running:
        return
    _pose_thread_running = True
    t = threading.Thread(target=_pose_compensation_loop, daemon=True)
    t.start()
    print("[pose] Head pose compensation thread started.")

def _pose_compensation_loop():
    """
    Identical logic to Linux version. MediaPipe works on Windows.
    Opens camera at low resolution, runs FaceLandmarker in LIVE_STREAM mode,
    emits pose_correction events to the browser client.
    """
    global _pose_baseline, _latest_pose
    try:
        import cv2
        import numpy as np
        import mediapipe as mp
    except ImportError as e:
        print(f"[pose] WARNING: Could not import cv2/mediapipe: {e}")
        print("[pose] Head pose compensation disabled.")
        return

    MODEL_PATH = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'face_landmarker.task')
    )
    if not os.path.exists(MODEL_PATH):
        print(f"[pose] WARNING: {MODEL_PATH} not found — head pose disabled.")
        print("[pose] Run scripts\\download_model.bat to get it.")
        return

    try:
        BaseOptions           = mp.tasks.BaseOptions
        FaceLandmarker        = mp.tasks.vision.FaceLandmarker
        FaceLandmarkerOptions = mp.tasks.vision.FaceLandmarkerOptions
        RunningMode           = mp.tasks.vision.RunningMode

        def _result_callback(result, output_image, timestamp_ms):
            global _pose_baseline, _latest_pose
            if not result.facial_transformation_matrixes:
                return
            try:
                mat   = np.array(result.facial_transformation_matrixes[0].data).reshape(4, 4)
                yaw   = float(np.degrees(np.arctan2(mat[1, 0], mat[0, 0])))
                pitch = float(np.degrees(np.arctan2(
                    -mat[2, 0], np.sqrt(mat[2, 1]**2 + mat[2, 2]**2)
                )))

                with _pose_lock:
                    _latest_pose = {'yaw': yaw, 'pitch': pitch}
                    baseline = dict(_pose_baseline) if _pose_baseline is not None else None

                if baseline is None:
                    return

                dy_deg = yaw   - baseline['yaw']
                dp_deg = pitch - baseline['pitch']

                dx_correction = dy_deg * (SCREEN_WIDTH  / 35.0)
                dy_correction = dp_deg * (SCREEN_HEIGHT / 28.0)

                with _registry_lock:
                    bsid = _browser_sid
                if bsid:
                    sio.emit('pose_correction', {
                        'dx': round(dx_correction, 1),
                        'dy': round(dy_correction, 1)
                    }, to=bsid)
            except Exception:
                pass

        options = FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=MODEL_PATH),
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=True,
            num_faces=1,
            running_mode=RunningMode.LIVE_STREAM,
            result_callback=_result_callback,
        )

        # On Windows, camera 0 may be shared with Chrome/WebGazer.
        # Try camera 0 first; if it's exclusively locked, fall back gracefully.
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)  # DSHOW backend allows sharing on Windows
        if not cap.isOpened():
            cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("[pose] WARNING: Could not open camera — head pose disabled.")
            print("[pose] Try closing other camera apps.")
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  320)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        cap.set(cv2.CAP_PROP_FPS,          15)

        ts_ms = 0
        with FaceLandmarker.create_from_options(options) as landmarker:
            print("[pose] MediaPipe FaceLandmarker ready.")
            while True:
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.1)
                    continue
                rgb      = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                landmarker.detect_async(mp_image, ts_ms)
                ts_ms += 67
                time.sleep(0.067)

    except Exception as e:
        print(f"[pose] Thread crashed: {e}")
        print("[pose] Continuing without pose compensation.")


# ── System state ─────────────────────────────────────────────────────────────────
class SystemState:
    def __init__(self):
        self.last_click_time  = 0
        self.is_locked        = False
        self.locked_coords    = (0, 0)
        self.snap_radius      = 90
        self.release_radius   = 300
        self.dwell_time       = 1.5
        self.dwell_radius     = 30
        self.dwell_start      = None
        self.dwell_anchor     = (0, 0)
        self._snap_candidate  = None
        self._snap_count      = 0
        self._last_err_time   = 0
        self._hover_target    = None

state = SystemState()


# ── Windows Accessibility Mapper (replaces pyatspi AccessibilityMapper) ──────────
class WindowsAccessibilityMapper:
    """
    Uses the Windows UIAutomation COM API to find clickable UI elements
    near a given screen coordinate. This is the Windows equivalent of the
    Linux pyatspi AccessibilityMapper.

    uiautomation wraps the IUIAutomation COM interface which is built into
    Windows and works across ALL applications: Win32, WPF, UWP, Electron, etc.

    IMPORTANT: uiautomation COM calls are synchronous and can be slow (~5-30ms).
    They run on a background thread here to avoid blocking the Socket.IO loop.
    """

    # Windows control types that correspond to clickable elements
    # These match the Linux pyatspi 'clickable_roles' list
    CLICKABLE_CONTROL_TYPES = {
        'ButtonControl',
        'MenuItemControl',
        'HyperlinkControl',
        'CheckBoxControl',
        'RadioButtonControl',
        'TabItemControl',
        'ListItemControl',
        'TreeItemControl',
        'MenuBarControl',
        'ToolBarControl',
    }

    def __init__(self):
        self._cache_lock = threading.Lock()
        self._last_result = None
        self._last_query_coords = (-9999, -9999)
        self._busy = False
        print("[atspi-win] Windows UIAutomation accessibility mapper initialized.")

    def find_nearest_clickable(self, x, y, radius):
        """
        Find the nearest clickable UI element within `radius` pixels of (x, y).
        Returns {'x': cx, 'y': cy, 'name': label} or None.

        Uses uiautomation's ElementFromPoint to get the topmost element,
        then walks up the tree to find a clickable ancestor if needed.
        """
        try:
            import uiautomation as auto
        except ImportError:
            return None

        try:
            x, y = clamp_to_virtual_desktop(x, y)

            # Primary: get element directly at the gaze point
            ctrl = auto.ControlFromPoint(int(x), int(y))
            if ctrl:
                # Walk up the tree to find a clickable ancestor (max 8 levels)
                candidate = ctrl
                for _ in range(8):
                    if candidate is None:
                        break
                    ctrl_type = type(candidate).__name__
                    if ctrl_type in self.CLICKABLE_CONTROL_TYPES:
                        try:
                            rect = candidate.BoundingRectangle
                            if rect.width() > 0 and rect.height() > 0:
                                cx = rect.xcenter()
                                cy = rect.ycenter()
                                dist = math.sqrt((x - cx)**2 + (y - cy)**2)
                                if dist <= radius:
                                    name = (candidate.Name or
                                            candidate.AutomationId or
                                            ctrl_type)
                                    return {'x': cx, 'y': cy, 'name': name}
                        except Exception:
                            pass
                    try:
                        candidate = candidate.GetParentControl()
                    except Exception:
                        break

            # Fallback: search within radius using FindAllControl at root level
            # This is slower but catches elements the point-test misses
            root = auto.GetRootControl()
            best_dist = radius + 1
            best_result = None

            def _scan(ctrl, depth=0):
                nonlocal best_dist, best_result
                if depth > 6:
                    return
                try:
                    rect = ctrl.BoundingRectangle
                    # Quick reject: element's bounding rect is nowhere near gaze
                    if (rect.right  < x - radius or rect.left > x + radius or
                        rect.bottom < y - radius or rect.top  > y + radius):
                        return
                    ctrl_type = type(ctrl).__name__
                    if ctrl_type in self.CLICKABLE_CONTROL_TYPES:
                        cx = rect.xcenter()
                        cy = rect.ycenter()
                        dist = math.sqrt((x - cx)**2 + (y - cy)**2)
                        if dist < best_dist:
                            best_dist = dist
                            name = ctrl.Name or ctrl.AutomationId or ctrl_type
                            best_result = {'x': cx, 'y': cy, 'name': name}
                    for child in ctrl.GetChildren():
                        _scan(child, depth + 1)
                except Exception:
                    pass

            # Only scan the focused application to keep it fast
            try:
                focused = auto.GetFocusedControl()
                if focused:
                    top = focused
                    for _ in range(10):
                        parent = top.GetParentControl()
                        if parent is None or parent == root:
                            break
                        top = parent
                    _scan(top)
            except Exception:
                pass

            return best_result

        except Exception as e:
            return None


# Windows Camera sharing is flaky. By default, we disable the pose thread 
# so WebGazer (Chrome) can have exclusive access to the camera.
# mapper = WindowsAccessibilityMapper()
# _start_pose_compensation_thread()

mapper = WindowsAccessibilityMapper()


def _scan_clickable_target(scan_x, scan_y, radius):
    global _cached_snap_target, _scan_inflight
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass
    target = mapper.find_nearest_clickable(scan_x, scan_y, radius)
    with _scan_lock:
        _cached_snap_target = target
        _scan_inflight = False


# ── Socket.IO event handlers ─────────────────────────────────────────────────────
# These are IDENTICAL to server.py so app.js works without any changes.

@sio.on('connect')
def on_connect():
    sid = emit.__self__.sid if hasattr(emit, '__self__') else None
    # flask-socketio provides request context
    from flask import request
    sid = request.sid
    global _browser_sid
    print(f"[server] Client connected: {sid}")
    with _registry_lock:
        if _browser_sid is None:
            _browser_sid = sid
            print(f"[server] Browser auto-registered: {sid}")
            if _pending_calibration_points:
                for point in _pending_calibration_points:
                    sio.emit('native_calibration_point', point, to=sid)
                _pending_calibration_points.clear()
            if _calibration_done_pending:
                sio.emit('native_calibration_done', {}, to=sid)


@sio.on('disconnect')
def on_disconnect():
    from flask import request
    sid = request.sid
    global _browser_sid, _calibration_sid, _hud_sid
    print(f"[server] Client disconnected: {sid}")
    with _registry_lock:
        if sid == _browser_sid:
            _browser_sid = None
        elif sid == _calibration_sid:
            _calibration_sid = None
        elif sid == _hud_sid:
            _hud_sid = None


@sio.on('register')
def on_register(data):
    from flask import request
    sid = request.sid
    global _browser_sid, _calibration_sid, _hud_sid
    role = data.get('type', 'unknown')
    with _registry_lock:
        if role == 'browser':
            _browser_sid = sid
            print(f"[server] Browser registered: {sid}")
            if _calibration_sid:
                sio.emit('browser_ready', {}, to=_calibration_sid)
        elif role == 'calibration':
            _calibration_sid = sid
            print(f"[server] Calibration registered: {sid}")
            if _browser_sid:
                sio.emit('browser_ready', {}, to=sid)
        elif role == 'hud':
            _hud_sid = sid
            print(f"[server] HUD registered: {sid}")
        else:
            print(f"[server] Unknown role '{role}' from {sid}")


@sio.on('webgazer_ready')
def on_webgazer_ready(data):
    from flask import request
    sid = request.sid
    global _browser_sid
    with _registry_lock:
        _browser_sid = sid
        print(f"[server] WebGazer ready — browser: {sid}")
        if _pending_calibration_points:
            for point in _pending_calibration_points:
                sio.emit('native_calibration_point', point, to=sid)
            _pending_calibration_points.clear()
        if _calibration_done_pending:
            sio.emit('native_calibration_done', {}, to=sid)
        elif _calibration_sid:
            sio.emit('browser_ready', {}, to=_calibration_sid)


@sio.on('calibrate_point')
def on_calibrate_point(data):
    with _registry_lock:
        bsid = _browser_sid
    if bsid:
        sio.emit('native_calibration_point', data, to=bsid)
    else:
        with _registry_lock:
            _pending_calibration_points.append(data)
            pending_count = len(_pending_calibration_points)
        print(f"[server] Buffered calibration point ({pending_count} total)")


@sio.on('calibration_complete')
def on_calibration_complete(data):
    global _calibration_done_pending, _pose_baseline, _latest_pose
    with _registry_lock:
        bsid = _browser_sid
    if bsid:
        sio.emit('native_calibration_done', {}, to=bsid)
        print("[server] Calibration complete — gaze streaming activated.")
    else:
        with _registry_lock:
            _calibration_done_pending = True

    with _pose_lock:
        _pose_baseline = dict(_latest_pose) if _latest_pose is not None else {'yaw': 0.0, 'pitch': 0.0}
        baseline_snapshot = dict(_pose_baseline)
    print(f"[pose] Baseline set: {baseline_snapshot}")


_gaze_logged = False
_gaze_count  = 0

@sio.on('move_mouse')
def on_move_mouse(data):
    global _gaze_logged, _gaze_count, _last_scan_ms, _scan_inflight
    try:
        if not _gaze_logged:
            print("[server] Gaze signals received! Active control starting.")
            _gaze_logged = True

        x  = data.get('x')
        y  = data.get('y')

        # Guard: Ignore uninitialized gaze points
        if x is None or y is None:
            return

        # Filter startup-origin noise without breaking negative virtual coordinates.
        if abs(x) <= 1 and abs(y) <= 1:
            return

        raw_x, raw_y = map_gaze_to_virtual(x, y)
        target_x, target_y = raw_x, raw_y

        # UIAutomation is intentionally off the hot path:
        # refresh cached target at most every 120ms in a daemon worker.
        now_ms = int(time.time() * 1000)
        should_scan = False
        with _scan_lock:
            if (now_ms - _last_scan_ms) > 120 and not _scan_inflight:
                _last_scan_ms = now_ms
                _scan_inflight = True
                should_scan = True

        if should_scan:
            threading.Thread(
                target=_scan_clickable_target,
                args=(target_x, target_y, state.snap_radius),
                daemon=True,
            ).start()

        with _scan_lock:
            cached_target = _cached_snap_target

        is_over_clickable = cached_target is not None
        state._hover_target = (cached_target['x'], cached_target['y']) if cached_target else None

        # Snapping / Clicking detection (DISABLED for maximum smoothness)
        state.is_locked = False

        # Dwell calculation
        now_ts = time.time()
        dx = target_x - state.dwell_anchor[0]
        dy = target_y - state.dwell_anchor[1]
        dwell_pct = 0.0
        if dx*dx + dy*dy < state.dwell_radius**2:
            if not state.dwell_start:
                state.dwell_start  = now_ts
                state.dwell_anchor = (target_x, target_y)
            else:
                elapsed   = now_ts - state.dwell_start
                dwell_pct = min(elapsed / state.dwell_time, 1.0)
        else:
            state.dwell_start  = now_ts
            state.dwell_anchor = (target_x, target_y)

        # Relay to HUD
        with _registry_lock:
            hsid = _hud_sid
            csid = _calibration_sid

        if hsid:
            sio.emit('hud_update', {
                'x': target_x, 'y': target_y,
                'dwell': dwell_pct,
                'locked': state.is_locked,
                'over_clickable': is_over_clickable
            }, to=hsid)
        if csid:
            sio.emit('current_gaze', {'x': target_x, 'y': target_y}, to=csid)

        target_x, target_y = clamp_to_virtual_desktop(target_x, target_y)

        if MOVE_OS_CURSOR:
            pyautogui.moveTo(target_x, target_y, duration=0)

    except Exception as e:
        now = time.time()
        if now - state._last_err_time > 5.0:
            print(f"[server] ERROR in move_mouse: {e}")
            state._last_err_time = now


@sio.on('trigger_click')
def on_trigger_click(data):
    on_trigger_action({'x': data.get('x'), 'y': data.get('y'), 'type': 'left'})


@sio.on('trigger_action')
def on_trigger_action(data):
    now = time.time()
    if now - state.last_click_time > 1.0:
        try:
            x, y  = data.get('x'), data.get('y')
            atype = data.get('type', 'left')

            if x is None or y is None:
                return

            if state._hover_target and not MOVE_OS_CURSOR:
                x, y = state._hover_target
                print(f"[server] Smart Target: clicking ({x}, {y})")

            x, y = clamp_to_virtual_desktop(x, y)

            if atype == 'left':
                pyautogui.click(x, y) if not MOVE_OS_CURSOR else pyautogui.click()
            elif atype == 'right':
                pyautogui.rightClick(x, y) if not MOVE_OS_CURSOR else pyautogui.rightClick()
            elif atype == 'double':
                pyautogui.doubleClick(x, y) if not MOVE_OS_CURSOR else pyautogui.doubleClick()

            with _registry_lock:
                bsid = _browser_sid
            if bsid:
                sio.emit('click_learned', {'x': x, 'y': y}, to=bsid)

            state.last_click_time = now
            print(f"[server] {atype} click at ({x}, {y})")
        except Exception as e:
            print(f"[server] Action error: {e}")


@sio.on('trigger_scroll')
def on_trigger_scroll(data):
    try:
        direction = data.get('direction', 'down')
        amount    = data.get('amount', 3)
        val = amount if direction == 'up' else -amount
        pyautogui.scroll(val)
    except Exception as e:
        print(f"[server] Scroll error: {e}")


@sio.on('radial_menu_state')
def on_radial_menu_state(data):
    with _registry_lock:
        hsid = _hud_sid
    if hsid:
        sio.emit('hud_radial_menu', data, to=hsid)


# ── Entry point ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"[server] Starting on port {SERVER_PORT} ...")
    sio.run(app, host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)
