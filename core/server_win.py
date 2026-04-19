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
import json
import pythoncom
try:
    import win32con
    import win32gui
    import win32process
except Exception:
    win32con = None
    win32gui = None
    win32process = None

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
MOVE_OS_CURSOR  = False
PRECISION_MODE  = True
CLICK_WITHOUT_MOUSE_MOVE = True
ENABLE_HEAD_POSE_COMP = True
ENABLE_BAR_MENU = True
BAR_CONTEXT_SCAN_MS = 900
BAR_CONTEXT_EMIT_MS = 900
BAR_CONTEXT_MAX_ITEMS = 40
BAR_CONTEXT_PAGE_SIZE = 8
BAR_CONTEXT_USE_SHELL = True
BAR_CONTEXT_FALLBACK = True
BAR_CONTEXT_STALE_MS = 2200
LOG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'docs', 'precision_runtime_log.jsonl')
)

_log_lock = threading.Lock()


def log_event(event, **fields):
    """Append structured runtime telemetry for post-run debugging."""
    record = {
        'ts': round(time.time(), 3),
        'src': 'server_win',
        'event': event,
    }
    record.update(fields)
    line = json.dumps(record, ensure_ascii=True)
    try:
        with _log_lock:
            with open(LOG_PATH, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
    except Exception:
        # Never let logging impact cursor control.
        pass

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

_bar_context_lock = threading.Lock()
_bar_context = {
    'active': False,
    'bar_type': None,
    'bar_label': '',
    'items': [],
    'page_size': BAR_CONTEXT_PAGE_SIZE,
    'ts_ms': 0,
}
_bar_items_by_id = {}
_bar_context_dirty = False
_bar_last_emit_sig = None
_bar_last_emit_ms = 0
_bar_last_scan_ms = 0
_bar_scan_inflight = False
_bar_scan_lock = threading.Lock()

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
        self.locked_name      = None
        self.snap_radius      = 160
        self.release_radius   = 300
        self.dwell_time       = 1.5
        self.dwell_radius     = 30
        self.dwell_start      = None
        self.dwell_anchor     = (0, 0)
        self._snap_candidate  = None
        self._snap_count      = 0
        self._last_err_time   = 0
        self._hover_target    = None
        self.precision_mode = PRECISION_MODE
        self.precision_lock_ms = 320
        self.precision_release_ms = 240
        self.precision_min_conf = 0.24
        self.raw_filter_alpha = 0.28
        self.raw_smoothed_x = None
        self.raw_smoothed_y = None
        self._precision_candidate_sig = None
        self._precision_candidate_since = 0.0
        self._precision_last_seen = 0.0
        self._precision_locked_sig = None
        self._last_logged_candidate_sig = None

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
            def _candidate_score(dist, width, height):
                # Weighted nearest + size scoring (distance dominates).
                dist_score = max(0.0, 1.0 - (dist / max(radius, 1)))
                area = max(1.0, float(width) * float(height))
                size_norm = min(1.0, area / max((radius * radius * 4.0), 1.0))
                return (0.75 * dist_score) + (0.25 * size_norm)

            x, y = clamp_to_virtual_desktop(x, y)

            # Primary: get element directly at the gaze point
            ctrl = auto.ControlFromPoint(int(x), int(y))
            if ctrl:
                # Walk up the tree to find a clickable ancestor (max 8 levels)
                candidate = ctrl
                best = None
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
                                    w = rect.width()
                                    h = rect.height()
                                    score = _candidate_score(dist, w, h)
                                    info = {
                                        'x': cx,
                                        'y': cy,
                                        'name': name,
                                        'width': w,
                                        'height': h,
                                        'area': w * h,
                                        'score': round(score, 4),
                                    }
                                    if best is None or info['score'] > best['score']:
                                        best = info
                        except Exception:
                            pass
                    try:
                        candidate = candidate.GetParentControl()
                    except Exception:
                        break
                if best is not None:
                    return best

            # Fallback: search within radius using FindAllControl at root level
            # This is slower but catches elements the point-test misses
            root = auto.GetRootControl()
            best_score = -1.0
            best_result = None

            def _scan(ctrl, depth=0):
                nonlocal best_score, best_result
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
                        if dist <= radius:
                            w = rect.width()
                            h = rect.height()
                            score = _candidate_score(dist, w, h)
                            if score > best_score:
                                best_score = score
                                name = ctrl.Name or ctrl.AutomationId or ctrl_type
                                best_result = {
                                    'x': cx,
                                    'y': cy,
                                    'name': name,
                                    'width': w,
                                    'height': h,
                                    'area': w * h,
                                    'score': round(score, 4),
                                }
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


mapper = WindowsAccessibilityMapper()

# ── Bar Detection (UIAutomation + Taskbar fallback) ────────────────────────────
BAR_CONTAINER_TYPES = {
    'ToolBarControl',
    'MenuBarControl',
    'TabControl',
    'PaneControl',
    'GroupControl',
    'ListControl',
}

BAR_ITEM_TYPES = {
    'ButtonControl',
    'MenuItemControl',
    'TabItemControl',
    'ListItemControl',
    'TreeItemControl',
    'EditControl',
    'ComboBoxControl',
    'SplitButtonControl',
    'CheckBoxControl',
    'RadioButtonControl',
}


def _rect_dims(rect):
    try:
        return rect.width(), rect.height()
    except Exception:
        try:
            return (rect.right - rect.left), (rect.bottom - rect.top)
        except Exception:
            return 0, 0


def _rect_center(rect):
    try:
        return rect.xcenter(), rect.ycenter()
    except Exception:
        try:
            return ((rect.left + rect.right) / 2), ((rect.top + rect.bottom) / 2)
        except Exception:
            return 0, 0


def _is_bar_like_rect(rect, ctrl_type=None):
    w, h = _rect_dims(rect)
    if w <= 0 or h <= 0:
        return False
    if ctrl_type in {'MenuBarControl', 'ToolBarControl', 'TabControl'}:
        return True
    major = max(w, h)
    minor = min(w, h)
    if major < 120 or minor < 24:
        return False
    # Accept long, thin strips (horizontal or vertical bars).
    if major / max(minor, 1) >= 3.0:
        return True
    # Wide sidebars can be thicker but still bar-like.
    if major / max(minor, 1) >= 2.2 and minor <= 420:
        return True
    return False


def _classify_bar_type(ctrl_type, rect):
    if ctrl_type == 'MenuBarControl':
        return 'menubar'
    if ctrl_type == 'ToolBarControl':
        return 'toolbar'
    if ctrl_type in {'TabControl', 'TabItemControl'}:
        return 'tabbar'
    w, h = _rect_dims(rect)
    if h > w * 2:
        return 'sidebar'
    if w > h * 2:
        return 'toolbar'
    return 'bar'


def _clean_label(text, fallback='Item'):
    if text is None:
        return fallback
    name = str(text).strip()
    return name if name else fallback


def _get_taskbar_rects():
    if win32gui is None:
        return []
    rects = []

    def _enum_cb(hwnd, out):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            class_name = win32gui.GetClassName(hwnd)
            if class_name in {'Shell_TrayWnd', 'Shell_SecondaryTrayWnd'}:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                out.append({'hwnd': hwnd, 'rect': (left, top, right, bottom)})
        except Exception:
            return

    try:
        win32gui.EnumWindows(_enum_cb, rects)
    except Exception:
        return rects
    return rects


def _point_in_rect(x, y, rect):
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


def _list_running_windows(max_items=20):
    if win32gui is None:
        return []
    items = []

    def _enum_cb(hwnd, out):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetParent(hwnd) != 0:
                return
            title = win32gui.GetWindowText(hwnd)
            if not title or title.strip() == 'Program Manager':
                return
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if exstyle & win32con.WS_EX_TOOLWINDOW:
                return
            out.append({'label': title.strip(), 'hwnd': hwnd, 'action': 'activate_window'})
        except Exception:
            return

    try:
        win32gui.EnumWindows(_enum_cb, items)
    except Exception:
        return []

    # Most-recent windows tend to be near the end; keep the newest titles.
    items = items[-max_items:]
    return items


def _fallback_bar_items():
    return [
        {'label': 'Start Menu', 'action': 'hotkey', 'keys': ['win']},
        {'label': 'Task View', 'action': 'hotkey', 'keys': ['win', 'tab']},
        {'label': 'Switch App', 'action': 'hotkey', 'keys': ['alt', 'tab']},
        {'label': 'Show Desktop', 'action': 'hotkey', 'keys': ['win', 'd']},
        {'label': 'Close Window', 'action': 'hotkey', 'keys': ['alt', 'f4']},
    ]


def _find_bar_container_at(x, y):
    try:
        import uiautomation as auto
    except Exception:
        return None

    try:
        ctrl = auto.ControlFromPoint(int(x), int(y))
        if ctrl is None:
            return None
        candidate = ctrl
        for _ in range(12):
            if candidate is None:
                break
            ctrl_type = type(candidate).__name__
            rect = candidate.BoundingRectangle
            if ctrl_type in BAR_CONTAINER_TYPES and _is_bar_like_rect(rect, ctrl_type):
                return candidate
            try:
                candidate = candidate.GetParentControl()
            except Exception:
                break
    except Exception:
        return None
    return None


def _collect_bar_items(container, max_items=BAR_CONTEXT_MAX_ITEMS):
    if container is None:
        return []
    items = []
    seen = set()
    try:
        container_rect = container.BoundingRectangle
        c_w, c_h = _rect_dims(container_rect)
    except Exception:
        container_rect = None
        c_w, c_h = 0, 0

    def _visit(ctrl, depth):
        if depth > 3 or len(items) >= max_items:
            return
        try:
            ctrl_type = type(ctrl).__name__
            rect = ctrl.BoundingRectangle
            w, h = _rect_dims(rect)
            if w <= 6 or h <= 6:
                return
            skip_self = False
            if container_rect is not None and c_w > 0 and c_h > 0:
                if w >= (c_w * 0.95) and h >= (c_h * 0.95):
                    # Skip the container itself, but still scan children.
                    skip_self = True
            if ctrl_type in BAR_ITEM_TYPES and not skip_self:
                name = _clean_label(getattr(ctrl, 'Name', None) or getattr(ctrl, 'AutomationId', None) or ctrl_type)
                cx, cy = _rect_center(rect)
                sig = (name, int(cx), int(cy))
                if sig in seen:
                    return
                seen.add(sig)
                items.append({
                    'label': name,
                    'x': int(cx),
                    'y': int(cy),
                    'action': 'uia_click',
                })
            if len(items) >= max_items:
                return
            for child in ctrl.GetChildren():
                _visit(child, depth + 1)
        except Exception:
            return

    _visit(container, 0)
    return items


def _build_bar_context(x, y):
    bar_detected = False
    bar_type = None
    bar_label = ''
    items = []

    taskbar_rects = _get_taskbar_rects() if BAR_CONTEXT_USE_SHELL else []
    for t in taskbar_rects:
        if _point_in_rect(x, y, t['rect']):
            bar_detected = True
            bar_type = 'taskbar'
            bar_label = 'Taskbar'
            break

    container = _find_bar_container_at(x, y)
    if container is not None:
        bar_detected = True
        try:
            ctrl_type = type(container).__name__
        except Exception:
            ctrl_type = None
        try:
            rect = container.BoundingRectangle
        except Exception:
            rect = None
        if bar_type is None:
            bar_type = _classify_bar_type(ctrl_type or 'bar', rect) if rect is not None else 'bar'
        if not bar_label:
            bar_label = _clean_label(getattr(container, 'Name', None) or getattr(container, 'AutomationId', None) or bar_type, bar_type)
        items = _collect_bar_items(container)

    if bar_type == 'taskbar' and BAR_CONTEXT_USE_SHELL:
        if not items:
            items = _list_running_windows(max_items=BAR_CONTEXT_MAX_ITEMS)

    if bar_detected and not items and BAR_CONTEXT_FALLBACK:
        items = _fallback_bar_items()

    active = bool(bar_detected and items)
    payload_items = []
    items_by_id = {}
    if active:
        for idx, item in enumerate(items):
            label = _clean_label(item.get('label'), f"Item {idx + 1}")
            if item.get('action') == 'activate_window':
                item_id = f"shell:{item.get('hwnd', idx)}"
            elif item.get('action') == 'hotkey':
                item_id = f"hotkey:{idx}"
            else:
                item_id = f"uia:{idx}"
            item['id'] = item_id
            item['label'] = label
            items_by_id[item_id] = dict(item)
            payload_items.append({'id': item_id, 'label': label})

    ctx = {
        'active': active,
        'bar_type': bar_type,
        'bar_label': bar_label,
        'items': payload_items,
        'page_size': BAR_CONTEXT_PAGE_SIZE,
        'ts_ms': int(time.time() * 1000),
    }
    return ctx, items_by_id


def _scan_bar_context(x, y):
    global _bar_context, _bar_context_dirty, _bar_scan_inflight, _bar_items_by_id
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:
        ctx, items_by_id = _build_bar_context(x, y)
    except Exception:
        ctx, items_by_id = ({
            'active': False,
            'bar_type': None,
            'bar_label': '',
            'items': [],
            'page_size': BAR_CONTEXT_PAGE_SIZE,
            'ts_ms': int(time.time() * 1000),
        }, {})

    with _bar_context_lock:
        _bar_context = ctx
        _bar_items_by_id = items_by_id
        _bar_context_dirty = True

    with _bar_scan_lock:
        _bar_scan_inflight = False


def _candidate_signature(candidate):
    if not candidate:
        return None
    return (
        int(candidate.get('x', 0)),
        int(candidate.get('y', 0)),
        str(candidate.get('name', '')),
    )


def _resolve_precision_target(raw_x, raw_y, cached_target, now_ts):
    """
    Bubble-style assist: lock to the nearest stable candidate instead of requiring
    exact pixel-level gaze placement.
    """
    candidate = None
    confidence = 0.0

    if cached_target:
        cx = float(cached_target.get('x', raw_x))
        cy = float(cached_target.get('y', raw_y))
        dist = math.hypot(raw_x - cx, raw_y - cy)
        if dist <= state.snap_radius:
            dist_score = max(0.0, 1.0 - (dist / max(state.snap_radius, 1)))
            area = float(cached_target.get('area', 0.0) or 0.0)
            size_score = min(1.0, area / max((state.snap_radius * state.snap_radius * 4.0), 1.0))
            confidence = (0.75 * dist_score) + (0.25 * size_score)
            candidate = {
                'x': int(cx),
                'y': int(cy),
                'name': str(cached_target.get('name', 'target')),
                'distance': round(dist, 2),
                'area': int(area),
                'score': round(float(cached_target.get('score', 0.0) or 0.0), 4),
            }

    sig = _candidate_signature(candidate)

    if sig != state._precision_candidate_sig:
        state._precision_candidate_sig = sig
        state._precision_candidate_since = now_ts

    if candidate:
        state._precision_last_seen = now_ts

    if state.precision_mode and not state.is_locked and candidate:
        lock_elapsed = (now_ts - state._precision_candidate_since) * 1000.0
        if lock_elapsed >= state.precision_lock_ms and confidence >= state.precision_min_conf:
            state.is_locked = True
            state.locked_coords = (candidate['x'], candidate['y'])
            state.locked_name = candidate['name']
            state._precision_locked_sig = sig
            log_event(
                'precision_locked',
                x=candidate['x'],
                y=candidate['y'],
                name=candidate['name'],
                confidence=round(confidence, 3),
                score=candidate.get('score', 0.0),
            )

    release_due_to_loss = (
        state.is_locked and
        (now_ts - state._precision_last_seen) * 1000.0 > state.precision_release_ms
    )
    release_due_to_switch = (
        state.is_locked and
        sig is not None and
        state._precision_locked_sig is not None and
        sig != state._precision_locked_sig
    )

    if release_due_to_loss or release_due_to_switch:
        log_event(
            'precision_released',
            reason='loss' if release_due_to_loss else 'switch',
            previous_name=state.locked_name,
        )
        state.is_locked = False
        state.locked_coords = (0, 0)
        state.locked_name = None
        state._precision_locked_sig = None

    if sig != state._last_logged_candidate_sig:
        if candidate:
            log_event(
                'precision_candidate',
                x=candidate['x'],
                y=candidate['y'],
                name=candidate['name'],
                distance=candidate['distance'],
                confidence=round(confidence, 3),
                score=candidate.get('score', 0.0),
            )
        else:
            log_event('precision_candidate_clear')
        state._last_logged_candidate_sig = sig

    if state.is_locked and state.locked_coords != (0, 0):
        target_x, target_y = state.locked_coords
    else:
        target_x, target_y = raw_x, raw_y

    lock_progress = 1.0
    if not state.is_locked and candidate:
        elapsed = (now_ts - state._precision_candidate_since) * 1000.0
        lock_progress = max(0.0, min(elapsed / max(state.precision_lock_ms, 1), 1.0))

    return {
        'target_x': target_x,
        'target_y': target_y,
        'candidate': candidate,
        'confidence': round(confidence, 3),
        'lock_progress': round(lock_progress, 3),
    }


def _try_uia_action_at(x, y, atype):
    """Try to invoke click actions without moving the physical mouse cursor."""
    try:
        import uiautomation as auto
    except Exception:
        return False, 'uiautomation_missing'

    try:
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:
        ctrl = auto.ControlFromPoint(int(x), int(y))
        if ctrl is None:
            return False, 'no_control_at_point'

        if atype == 'left':
            if hasattr(ctrl, 'Click'):
                ctrl.Click(simulateMove=False, waitTime=0)
                return True, 'uia_click'
            if hasattr(ctrl, 'Invoke'):
                ctrl.Invoke()
                return True, 'uia_invoke'
        elif atype == 'right' and hasattr(ctrl, 'RightClick'):
            ctrl.RightClick(simulateMove=False, waitTime=0)
            return True, 'uia_right_click'
        elif atype == 'double' and hasattr(ctrl, 'DoubleClick'):
            ctrl.DoubleClick(simulateMove=False, waitTime=0)
            return True, 'uia_double_click'
    except Exception as e:
        return False, f'uia_exception:{e}'

    return False, 'unsupported_action'


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
    global _browser_sid, _calibration_done_pending
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
                _calibration_done_pending = False


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
            log_event('hud_registered', sid=sid)
        else:
            print(f"[server] Unknown role '{role}' from {sid}")


@sio.on('debug_log')
def on_debug_log(data):
    if not isinstance(data, dict):
        return
    src = str(data.get('source', 'unknown'))
    event = str(data.get('event', 'event'))
    details = data.get('details', {})
    if not isinstance(details, dict):
        details = {'payload': str(details)}
    details_safe = {
        str(k): (v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
        for k, v in details.items()
    }
    log_event('client_debug', source=src, name=event, **details_safe)


@sio.on('webgazer_ready')
def on_webgazer_ready(data):
    from flask import request
    sid = request.sid
    global _browser_sid, _calibration_done_pending
    with _registry_lock:
        _browser_sid = sid
        print(f"[server] WebGazer ready — browser: {sid}")
        if ENABLE_HEAD_POSE_COMP:
            _start_pose_compensation_thread()
        if _pending_calibration_points:
            for point in _pending_calibration_points:
                sio.emit('native_calibration_point', point, to=sid)
            _pending_calibration_points.clear()
        if _calibration_done_pending:
            sio.emit('native_calibration_done', {}, to=sid)
            _calibration_done_pending = False
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
        with _registry_lock:
            _calibration_done_pending = False
    else:
        with _registry_lock:
            _calibration_done_pending = True

    with _pose_lock:
        _pose_baseline = dict(_latest_pose) if _latest_pose is not None else {'yaw': 0.0, 'pitch': 0.0}
        baseline_snapshot = dict(_pose_baseline)
    print(f"[pose] Baseline set: {baseline_snapshot}")


_gaze_logged = False
_gaze_count  = 0
_last_hud_missing_log_ts = 0.0

@sio.on('move_mouse')
def on_move_mouse(data):
    global _gaze_logged, _gaze_count, _last_scan_ms, _scan_inflight, _last_hud_missing_log_ts
    global _bar_last_scan_ms, _bar_scan_inflight, _bar_last_emit_ms, _bar_last_emit_sig, _bar_context_dirty
    try:
        if not _gaze_logged:
            print("[server] Gaze signals received! Active control starting.")
            _gaze_logged = True
            log_event('first_move_mouse', x=data.get('x'), y=data.get('y'))

        _gaze_count += 1

        x  = data.get('x')
        y  = data.get('y')

        # Guard: Ignore uninitialized gaze points
        if x is None or y is None:
            return

        # Filter startup-origin noise without breaking negative virtual coordinates.
        if abs(x) <= 1 and abs(y) <= 1:
            return

        raw_x, raw_y = map_gaze_to_virtual(x, y)
        if state.raw_smoothed_x is None or state.raw_smoothed_y is None:
            state.raw_smoothed_x = raw_x
            state.raw_smoothed_y = raw_y
        else:
            a = state.raw_filter_alpha
            state.raw_smoothed_x = (a * raw_x) + ((1.0 - a) * state.raw_smoothed_x)
            state.raw_smoothed_y = (a * raw_y) + ((1.0 - a) * state.raw_smoothed_y)

        bubble_x = state.raw_smoothed_x
        bubble_y = state.raw_smoothed_y
        target_x, target_y = bubble_x, bubble_y

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
                args=(bubble_x, bubble_y, state.snap_radius),
                daemon=True,
            ).start()

        if ENABLE_BAR_MENU:
            bar_should_scan = False
            with _bar_scan_lock:
                if (now_ms - _bar_last_scan_ms) > BAR_CONTEXT_SCAN_MS and not _bar_scan_inflight:
                    _bar_last_scan_ms = now_ms
                    _bar_scan_inflight = True
                    bar_should_scan = True

            if bar_should_scan:
                threading.Thread(
                    target=_scan_bar_context,
                    args=(bubble_x, bubble_y),
                    daemon=True,
                ).start()

            with _registry_lock:
                bsid = _browser_sid
            if bsid:
                emit_bar = False
                with _bar_context_lock:
                    ctx = dict(_bar_context)
                    dirty = _bar_context_dirty
                    sig = (
                        ctx.get('active'),
                        ctx.get('bar_type'),
                        tuple((i.get('id'), i.get('label')) for i in ctx.get('items', [])),
                    )
                    if dirty or sig != _bar_last_emit_sig or (now_ms - _bar_last_emit_ms) > BAR_CONTEXT_EMIT_MS:
                        _bar_context_dirty = False
                        _bar_last_emit_sig = sig
                        _bar_last_emit_ms = now_ms
                        emit_bar = True
                if emit_bar:
                    sio.emit('bar_context', ctx, to=bsid)

        with _scan_lock:
            cached_target = _cached_snap_target

        now_ts = time.time()
        precision = _resolve_precision_target(bubble_x, bubble_y, cached_target, now_ts)
        target_x = precision['target_x']
        target_y = precision['target_y']

        candidate = precision['candidate']
        is_over_clickable = candidate is not None
        state._hover_target = (candidate['x'], candidate['y']) if candidate else None

        # Dwell calculation
        dx = bubble_x - state.dwell_anchor[0]
        dy = bubble_y - state.dwell_anchor[1]
        dwell_pct = 0.0
        if dx*dx + dy*dy < state.dwell_radius**2:
            if not state.dwell_start:
                state.dwell_start  = now_ts
                state.dwell_anchor = (bubble_x, bubble_y)
            else:
                elapsed   = now_ts - state.dwell_start
                dwell_pct = min(elapsed / state.dwell_time, 1.0)
        else:
            state.dwell_start  = now_ts
            state.dwell_anchor = (bubble_x, bubble_y)

        # Relay to HUD
        with _registry_lock:
            hsid = _hud_sid
            csid = _calibration_sid

        if hsid:
            sio.emit('hud_update', {
                'x': bubble_x, 'y': bubble_y,
                'raw_x': raw_x, 'raw_y': raw_y,
                'dwell': dwell_pct,
                'locked': state.is_locked,
                'over_clickable': is_over_clickable,
                'precision': {
                    'active': bool(candidate) or state.is_locked,
                    'candidate_x': candidate['x'] if candidate else state.locked_coords[0],
                    'candidate_y': candidate['y'] if candidate else state.locked_coords[1],
                    'candidate_name': candidate['name'] if candidate else state.locked_name,
                    'confidence': precision['confidence'],
                    'bubble_radius': state.snap_radius,
                    'lock_progress': precision['lock_progress'],
                    'score': candidate['score'] if candidate else 0.0,
                }
            }, to=hsid)
            if _gaze_count % 120 == 0:
                log_event(
                    'hud_update_heartbeat',
                    gaze_count=_gaze_count,
                    x=int(bubble_x),
                    y=int(bubble_y),
                    locked=bool(state.is_locked),
                )
        else:
            now = time.time()
            if now - _last_hud_missing_log_ts > 2.0:
                _last_hud_missing_log_ts = now
                log_event('hud_not_connected', gaze_count=_gaze_count)
        if csid:
            sio.emit('current_gaze', {'x': bubble_x, 'y': bubble_y}, to=csid)

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
            click_source = 'direct'

            if state.is_locked and state.locked_coords != (0, 0):
                x, y = state.locked_coords
                click_source = 'locked_target'
            elif state._hover_target:
                x, y = state._hover_target
                click_source = 'nearest_target'

            if x is None or y is None:
                return

            x, y = clamp_to_virtual_desktop(x, y)

            if CLICK_WITHOUT_MOUSE_MOVE:
                ok, mode = _try_uia_action_at(x, y, atype)
                if not ok:
                    log_event(
                        'trigger_action_failed',
                        action=atype,
                        x=int(x),
                        y=int(y),
                        reason=mode,
                    )
                    return
                click_source = f'{click_source}:{mode}'
            else:
                if atype == 'left':
                    pyautogui.click(x, y)
                elif atype == 'right':
                    pyautogui.rightClick(x, y)
                elif atype == 'double':
                    pyautogui.doubleClick(x, y)

            with _registry_lock:
                bsid = _browser_sid
            if bsid:
                sio.emit('click_learned', {'x': x, 'y': y}, to=bsid)

            state.last_click_time = now
            print(f"[server] {atype} click at ({x}, {y})")
            log_event(
                'trigger_action',
                action=atype,
                x=int(x),
                y=int(y),
                source=click_source,
                locked=bool(state.is_locked),
            )
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


@sio.on('bar_menu_select')
def on_bar_menu_select(data):
    if not isinstance(data, dict):
        return
    item_id = data.get('id')
    if not item_id:
        return
    with _bar_context_lock:
        item = _bar_items_by_id.get(item_id)
    if not item:
        return

    action = item.get('action')
    label = item.get('label')
    if action == 'uia_click':
        x = item.get('x')
        y = item.get('y')
        if x is None or y is None:
            return
        x, y = clamp_to_virtual_desktop(x, y)
        ok, mode = _try_uia_action_at(x, y, 'left')
        log_event('bar_menu_select', id=item_id, label=label, action=action, ok=bool(ok), mode=mode)
        return

    if action == 'activate_window' and win32gui is not None:
        hwnd = item.get('hwnd')
        if hwnd:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                log_event('bar_menu_select', id=item_id, label=label, action=action, ok=True)
            except Exception as e:
                log_event('bar_menu_select', id=item_id, label=label, action=action, ok=False, error=str(e))
        return

    if action == 'hotkey':
        keys = item.get('keys') or []
        if keys:
            try:
                pyautogui.hotkey(*keys)
                log_event('bar_menu_select', id=item_id, label=label, action=action, ok=True)
            except Exception as e:
                log_event('bar_menu_select', id=item_id, label=label, action=action, ok=False, error=str(e))


# ── Entry point ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"[server] Starting on port {SERVER_PORT} ...")
    log_event(
        'server_start',
        port=SERVER_PORT,
        virtual_left=VLEFT,
        virtual_top=VTOP,
        virtual_right=VRIGHT,
        virtual_bottom=VBOTTOM,
        precision_mode=PRECISION_MODE,
    )
    sio.run(app, host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)
