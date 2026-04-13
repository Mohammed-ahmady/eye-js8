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
Gaze Mouse System — Windows 11 Launcher
=========================================
Starts all components of the gaze mouse system on Windows 11:
  1. server_win.py      (Flask + Socket.IO backend)
  2. Python HTTP server (serves index.html + WebGazer locally)
  3. Chrome/Edge        (hidden offscreen, runs WebGazer.js)
  4. hud_win.py         (PyQt5 gaze HUD overlay)
  5. calibration_win.py (PyQt5 calibration overlay)

No Xvfb needed on Windows — Chrome is placed at (-32000, -32000) offscreen.
"""

import subprocess
import sys
import os
import time
import signal

from screeninfo import get_monitors

# ── Configuration ────────────────────────────────────────────────────────────────
SERVER_PORT = 5000
HTTP_PORT   = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, '..'))
CORE_DIR = os.path.join(ROOT_DIR, 'core')
APP_URL  = f'http://localhost:{HTTP_PORT}/web/index.html'

# Windows subprocess creation flags
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS         = 0x00000008
CREATE_NO_WINDOW         = subprocess.CREATE_NO_WINDOW


def get_real_screen_resolution():
    try:
        m = get_monitors()[0]
        return m.width, m.height
    except Exception:
        print("[launcher] Warning: could not detect resolution — defaulting to 1920x1080")
        return 1920, 1080


def find_browser():
    """Find Chrome or Edge on Windows. Returns path or None."""
    candidates = [
        # Google Chrome
        os.path.expandvars(r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
        # Microsoft Edge (fallback — same Chromium engine)
        os.path.expandvars(r'%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe'),
    ]
    for path in candidates:
        if os.path.exists(path):
            print(f"[launcher] Found browser: {path}")
            return path
    return None


def start_backend():
    """Start server_win.py as a subprocess."""
    print("[launcher] Starting gaze backend server ...")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(CORE_DIR, 'server_win.py')],
        cwd=ROOT_DIR,
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    )
    time.sleep(2.5)  # Wait for Flask to bind port
    return proc


def start_http_server():
    """Start Python's built-in HTTP server to serve WebGazer files."""
    proc = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(HTTP_PORT)],
        cwd=ROOT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    )
    return proc


def start_browser_offscreen(browser_path, width, height):
    """
    Launch Chrome/Edge completely offscreen using --window-position=-32000,-32000.
    This is the Windows equivalent of running Chromium on an Xvfb virtual display.

    The browser is fully functional (WebGL, camera, JS) but invisible to the user.
    --use-fake-ui-for-media-stream auto-grants camera permission without a dialog.
    --use-angle=d3d11 is CRITICAL on Windows — the GL backend causes WebGL failure.

    Now using a dedicated user-data-dir to ensure the offscreen position is respected.
    """
    user_data_dir = os.path.join(ROOT_DIR, 'chrome_profile')
    if not os.path.exists(user_data_dir):
        os.makedirs(user_data_dir)

    args = [
        browser_path,
        f'--app={APP_URL}',
        f'--user-data-dir={user_data_dir}',    # Force isolated, hidden process
        f'--window-size={width},{height}',
        '--window-position=-32000,-32000',   # Place window offscreen (invisible)
        '--no-first-run',
        '--disable-infobars',
        '--disable-session-crashed-bubble',
        # Zero-throttle (identical to Linux version)
        '--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows',
        '--disable-features=CalculateNativeWinOcclusion',
        # GPU / WebGL flags — WINDOWS SPECIFIC
        '--ignore-gpu-blocklist',
        '--enable-gpu-rasterization',
        '--enable-zero-copy',
        '--use-gl=angle',
        '--use-angle=d3d11',               # ← MUST be d3d11 on Windows, NOT gl
        '--enable-webgl',
        # Camera permission auto-grant
        '--use-fake-ui-for-media-stream',  # ← Auto-grants camera without popup
        # Sandboxing (same as Linux)
        '--no-sandbox',
        '--disable-dev-shm-usage',
        # Logging
        '--enable-logging=stderr',
        '--log-level=0',
    ]

    # Safety guard: disabling GPU overrides ANGLE and breaks WebGL tracking.
    args = [arg for arg in args if arg != '--disable-gpu']

    print(f"[launcher] Launching browser offscreen at (-32000, -32000) ...")
    return subprocess.Popen(
        args,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        stdout=open(os.path.join(ROOT_DIR, 'chrome.log'), 'wb'),
        stderr=subprocess.STDOUT,
    )


def start_hud():
    """Launch the PyQt5 HUD overlay."""
    print("[launcher] Launching HUD overlay ...")
    return subprocess.Popen(
        [sys.executable, os.path.join(CORE_DIR, 'hud_win.py')],
        cwd=ROOT_DIR,
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    )


def start_calibration():
    """Launch the PyQt5 calibration overlay."""
    print("[launcher] Launching calibration overlay ...")
    return subprocess.Popen(
        [sys.executable, os.path.join(CORE_DIR, 'calibration_win.py')],
        cwd=ROOT_DIR,
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    )


def kill_proc(proc):
    """Kill a process and all its children on Windows."""
    try:
        subprocess.call(
            ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ── Entry point ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Check for face_landmarker.task
    if not os.path.exists(os.path.join(ROOT_DIR, 'face_landmarker.task')):
        print("[launcher] WARNING: face_landmarker.task not found!")
        print("[launcher] Run scripts\\download_model.bat to download it (~30MB).")
        print("[launcher] Head pose compensation will be disabled.")
        print()

    browser = find_browser()
    if not browser:
        print("[launcher] ERROR: No Chrome or Edge found on this system.")
        print("[launcher] Install Google Chrome from https://www.google.com/chrome/")
        sys.exit(1)

    sw, sh = get_real_screen_resolution()
    print(f"[launcher] Screen resolution: {sw}x{sh}")

    procs = []
    try:
        backend   = start_backend()
        procs.append(backend)

        http_proc = start_http_server()
        procs.append(http_proc)

        browser_proc = start_browser_offscreen(browser, sw, sh)
        procs.append(browser_proc)

        hud = start_hud()
        procs.append(hud)

        print("[launcher] Waiting for WebGazer to initialize (5s) ...")
        time.sleep(5)

        calib = start_calibration()
        procs.append(calib)

        print()
        print("=" * 60)
        print("  Gaze Mouse System (Windows 11) is running!")
        print(f"  Browser  : hidden offscreen (-32000, -32000)")
        print(f"  Camera   : real webcam — full WebGazer accuracy")
        print(f"  Calibrate: PyQt5 fullscreen overlay")
        print(f"  HUD      : active (gaze ring + dwell visible)")
        print("  Gaze control activates after calibration.")
        print("  Press Ctrl+C to stop everything.")
        print("=" * 60)
        print()

        calib.wait()
        print("[launcher] Calibration complete — gaze control is now active.")
        print("[launcher] Press Ctrl+C to stop.\n")

        browser_proc.wait()

    except KeyboardInterrupt:
        print("\n[launcher] Shutting down ...")

    finally:
        for p in procs:
            kill_proc(p)
        print("[launcher] All services stopped.")
