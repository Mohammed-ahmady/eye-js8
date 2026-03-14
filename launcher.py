"""
Gaze Mouse System - Launcher
==============================
Starts the backend server and opens the tracker UI in a dedicated
Chromium App Mode window. This approach:
  1. Uses Chrome's full JS engine (WebGazer fully compatible)
  2. Avoids background-tab throttling (app-mode windows are treated as desktop apps)
  3. After calibration, auto-resizes to a tiny always-on-top corner widget
"""

import subprocess
import sys
import os
import signal
import time

# --- Configuration ---
SERVER_HOST = 'localhost'
SERVER_PORT = 5000
HTTP_PORT   = 8000         # Static file server port
APP_URL     = f'http://{SERVER_HOST}:{HTTP_PORT}/index.html'

# After calibration the window shrinks to this size and sticks to the corner
MINI_WIDTH  = 1
MINI_HEIGHT = 1

# ------------------------------------------------------------------ #

def find_chromium():
    """Return the path to a suitable Chromium/Chrome binary."""
    candidates = [
        'chromium-browser',
        'chromium',
        'google-chrome',
        'google-chrome-stable',
    ]
    for name in candidates:
        try:
            path = subprocess.check_output(['which', name], stderr=subprocess.DEVNULL).decode().strip()
            if path:
                print(f"Found browser: {path}")
                return path
        except subprocess.CalledProcessError:
            continue
    return None


def start_backend():
    """Start server.py using the virtual environment Python."""
    python = './.venv/bin/python' if os.path.exists('./.venv/bin/python') else sys.executable
    print(f"Starting backend server with: {python} server.py")
    proc = subprocess.Popen(
        [python, 'server.py'],
        stdout=sys.stdout,
        stderr=sys.stderr,
        preexec_fn=os.setsid,
    )
    time.sleep(2)  # Give the server time to bind
    return proc


def start_http_server():
    """Start a simple static file server for index.html / app.js."""
    proc = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(HTTP_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    return proc


def open_chromium_app(browser_path):
    """
    Open the tracker in Chromium App Mode.
    --app=URL   : no tab bar / address bar — looks like a native window
    --no-first-run, --disable-infobars: cleaner startup
    Returns the Popen process.
    """
    args = [
        browser_path,
        f'--app={APP_URL}',
        '--no-first-run',
        '--disable-infobars',
        '--disable-session-crashed-bubble',
        '--disable-background-timer-throttling',   # KEY: prevent JS throttling
        '--disable-renderer-backgrounding',         # KEY: prevent background slowdown
        '--disable-backgrounding-occluded-windows', # KEY: keep active when hidden
    ]
    print(f"Launching Chromium App window → {APP_URL}")
    return subprocess.Popen(args, preexec_fn=os.setsid)


def minimize_to_corner():
    """
    Use xdotool to shrink the app window to a tiny always-on-top widget
    in the bottom-right corner. Call this after calibration is complete.
    """
    try:
        # Find the window by its title (set in index.html <title>)
        win_id = subprocess.check_output(
            ['xdotool', 'search', '--name', 'Gaze Mouse System'],
            stderr=subprocess.DEVNULL
        ).decode().strip().split('\n')[0]

        if not win_id:
            print("Warning: could not find window to minimize.")
            return

        # Get screen resolution
        screen_w = int(subprocess.check_output(
            ['xdotool', 'getdisplaygeometry'],
            stderr=subprocess.DEVNULL
        ).decode().split()[0])

        x_pos = screen_w - MINI_WIDTH - 5
        y_pos = 5  # top-right corner

        cmds = [
            # Resize to tiny
            ['xdotool', 'windowsize', win_id, str(MINI_WIDTH), str(MINI_HEIGHT)],
            # Move to top-right corner
            ['xdotool', 'windowmove', win_id, str(x_pos), str(y_pos)],
            # Keep always on top so it is never minimized by the OS
            ['xdotool', 'windowfocus', win_id],
        ]
        for cmd in cmds:
            subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)

        # Set always-on-top via wmctrl
        subprocess.run(
            ['wmctrl', '-i', '-r', win_id, '-b', 'add,above'],
            check=False, stderr=subprocess.DEVNULL
        )
        print("Window shrunk to corner widget — tracker running in background.")
    except Exception as e:
        print(f"Warning: could not resize window with xdotool: {e}")


# ------------------------------------------------------------------ #

if __name__ == '__main__':
    browser = find_chromium()
    if not browser:
        print("ERROR: No Chromium/Chrome browser found on this system.")
        print("Install with: sudo apt install chromium-browser")
        sys.exit(1)

    procs = []

    try:
        # 1. Start backend services
        backend = start_backend()
        procs.append(backend)

        # Only start HTTP server if the user isn't already running one
        http_proc = start_http_server()
        procs.append(http_proc)

        # 2. Open Chromium in App Mode
        browser_proc = open_chromium_app(browser)
        procs.append(browser_proc)

        print("\n" + "="*55)
        print("  Gaze Mouse System is running!")
        print("  Calibrate in the window, then gaze control activates.")
        print("  Press Ctrl+C here to stop everything.")
        print("="*55 + "\n")

        # 3. Wait for browser to close
        browser_proc.wait()

    except KeyboardInterrupt:
        print("\nShutting down...")

    finally:
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
        print("All services stopped.")
