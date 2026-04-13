"""
Gaze Mouse System — Xvfb Launcher
===================================
Runs WebGazer.js inside a completely headless Chromium instance on a
virtual framebuffer (Xvfb). The browser is 100% invisible to the OS:
  - Does NOT appear in the taskbar or alt-tab
  - Does NOT show a window on any display
  - Uses the real webcam feed for full WebGazer accuracy

Calibration is handled by a native GTK fullscreen overlay on the REAL
display, so the user sees clean OS-native dots — not a browser page.

System requirements:
    sudo apt install xvfb chromium-browser python3-gi python3-gi-cairo
                     gir1.2-gtk-3.0 gir1.2-gdk-3.0
"""

import subprocess
import sys
import os
import signal
import time

from screeninfo import get_monitors

# ── Configuration ──────────────────────────────────────────────────────────────
VIRTUAL_DISPLAY = ':99'
SERVER_PORT     = 5000
HTTP_PORT       = 8000
APP_URL         = f'http://localhost:{HTTP_PORT}/index.html'

# Save the real display BEFORE anything modifies it (usually ':0')
REAL_DISPLAY = os.environ.get('DISPLAY', ':0')
# ───────────────────────────────────────────────────────────────────────────────


def get_real_screen_resolution():
    """Detect the real monitor resolution.
    Xvfb is started at the same resolution so WebGazer's viewport maths
    stay consistent with the real screen coordinate space."""
    try:
        m = get_monitors()[0]
        return m.width, m.height
    except Exception:
        print("Warning: could not detect screen resolution — defaulting to 1920x1080")
        return 1920, 1080


def find_chromium():
    for name in ['chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable']:
        try:
            path = subprocess.check_output(
                ['which', name], stderr=subprocess.DEVNULL
            ).decode().strip()
            if path:
                print(f"Found browser: {path}")
                return path
        except subprocess.CalledProcessError:
            continue
    return None


def start_xvfb(width, height):
    """Start a virtual framebuffer at the real screen resolution.
    Running at the same resolution prevents any coordinate scaling mismatch
    inside WebGazer's ridge regression model."""
    res = f'{width}x{height}x24'
    print(f"Starting virtual display {VIRTUAL_DISPLAY} at {res} ...")
    proc = subprocess.Popen(
        ['Xvfb', VIRTUAL_DISPLAY, '-screen', '0', res, '-nolisten', 'tcp'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    time.sleep(1.2)
    print("Virtual display ready.")
    return proc


def start_backend():
    python = './.venv/bin/python' if os.path.exists('./.venv/bin/python') else sys.executable
    print("Starting gaze backend server ...")
    proc = subprocess.Popen(
        [python, 'server.py'],
        stdout=sys.stdout,
        stderr=sys.stderr,
        preexec_fn=os.setsid,
    )
    time.sleep(2)
    return proc

def start_http_server():
    proc = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(HTTP_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    return proc


def start_chromium_headless(browser_path, width, height):
    env = os.environ.copy()
    env['DISPLAY'] = VIRTUAL_DISPLAY

    args = [
        browser_path,
        f'--app={APP_URL}',
        f'--window-size={width},{height}',
        '--start-maximized',
        '--no-first-run',
        '--disable-infobars',
        '--disable-session-crashed-bubble',
        # Zero-throttle flags
        '--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows',
        '--disable-features=CalculateNativeWinOcclusion',
        # GPU / Accuracy Flags
        '--ignore-gpu-blocklist',
        '--enable-gpu-rasterization',
        '--enable-zero-copy',
        '--use-gl=angle',
        '--use-angle=gl',
        '--enable-webgl',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--use-fake-ui-for-media-stream',
        '--enable-logging=stderr',
        '--log-level=0',
    ]

    print(f"Launching Chromium on virtual display {VIRTUAL_DISPLAY} ...")
    return subprocess.Popen(
        args, env=env, preexec_fn=os.setsid,
        stdout=open('chrome.log','wb'), stderr=subprocess.STDOUT
    )

def start_hud():
    """Launch the gaze HUD overlay on the REAL display."""
    python = './.venv/bin/python' if os.path.exists('./.venv/bin/python') else sys.executable
    env = os.environ.copy()
    env['DISPLAY'] = REAL_DISPLAY
    print(f"Launching Gaze HUD overlay on {REAL_DISPLAY} ...")
    return subprocess.Popen(
        [python, 'hud.py'],
        env=env, stdout=sys.stdout, stderr=sys.stderr,
        preexec_fn=os.setsid,
    )


def start_calibration():
    """Run the native GTK calibration overlay on the REAL display.
    Spawned as a separate process so GTK can own its main thread."""
    python = './.venv/bin/python' if os.path.exists('./.venv/bin/python') else sys.executable
    env = os.environ.copy()
    env['DISPLAY'] = REAL_DISPLAY  # GTK must run on the real screen

    print(f"Launching native GTK calibration overlay on {REAL_DISPLAY} ...")
    return subprocess.Popen(
        [python, 'calibration.py'],
        env=env, stdout=sys.stdout, stderr=sys.stderr,
        preexec_fn=os.setsid,
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    browser = find_chromium()
    if not browser:
        print("ERROR: No Chromium/Chrome browser found on this system.")
        print("Install with: sudo apt install chromium-browser")
        sys.exit(1)

    sw, sh = get_real_screen_resolution()
    print(f"Real screen resolution detected: {sw}x{sh}")

    procs = []
    try:
        xvfb      = start_xvfb(sw, sh)
        procs.append(xvfb)

        backend   = start_backend()
        procs.append(backend)

        http_proc = start_http_server()
        procs.append(http_proc)

        chromium  = start_chromium_headless(browser, sw, sh)
        procs.append(chromium)
        
        # Start HUD early so it can show calibration feedback too
        hud       = start_hud()
        procs.append(hud)

        # Give WebGazer time to load its TF.js models and open the camera
        print("Waiting for WebGazer to initialize (5 s) ...")
        time.sleep(5)

        calib = start_calibration()
        procs.append(calib)

        print("\n" + "=" * 60)
        print("  Gaze Mouse System is running!")
        print(f"  Browser  : hidden (virtual display {VIRTUAL_DISPLAY})")
        print(f"  Camera   : real webcam — full WebGazer accuracy")
        print(f"  Calibrate: native GTK overlay on {REAL_DISPLAY}")
        print(f"  HUD      : active on {REAL_DISPLAY} (Dwell progress visible)")
        print("  Gaze control activates after calibration.")
        print("  Press Ctrl+C to stop everything.")
        print("=" * 60 + "\n")

        # Block until GTK calibration window closes
        calib.wait()
        print("Calibration complete — gaze control is now active.")
        print("Press Ctrl+C to stop the system.\n")

        # Keep alive until Chromium exits or Ctrl+C
        chromium.wait()

    except KeyboardInterrupt:
        print("\nShutting down ...")

    finally:
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
        print("All services stopped.")
