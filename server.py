import socketio
import eventlet
import pyautogui
from screeninfo import get_monitors
import time

# Disable PyAutoGUI fail-safe for this specific accessibility use case, 
# but we should be careful. 
pyautogui.FAILSAFE = True  # Move mouse to corner to stop in emergency
pyautogui.PAUSE = 0  # Minimize delay for smoother movement

# Initialize Socket.IO server
sio = socketio.Server(cors_allowed_origins='*')
app = socketio.WSGIApp(sio)

# Get primary monitor resolution for mapping
primary_monitor = get_monitors()[0]
SCREEN_WIDTH = primary_monitor.width
SCREEN_HEIGHT = primary_monitor.height

print(f"Server started. Screen Resolution: {SCREEN_WIDTH}x{SCREEN_HEIGHT}")

# Dwell-to-click state
last_click_time = 0
CLICK_COOLDOWN = 1.0  # seconds

@sio.event
def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.event
def disconnect(sid):
    print(f"Client disconnected: {sid}")

@sio.event
def move_mouse(sid, data):
    """
    Expects data: {'x': float, 'y': float, 'viewport_width': int, 'viewport_height': int}
    """
    try:
        x = data.get('x')
        y = data.get('y')
        vw = data.get('viewport_width')
        vh = data.get('viewport_height')

        if None in (x, y, vw, vh):
            return

        # Map viewport coordinates to screen coordinates
        # Ensure coordinates are within screen bounds
        target_x = max(0, min(SCREEN_WIDTH - 1, (x / vw) * SCREEN_WIDTH))
        target_y = max(0, min(SCREEN_HEIGHT - 1, (y / vh) * SCREEN_HEIGHT))

        # Move mouse with zero duration for instant response
        pyautogui.moveTo(target_x, target_y, duration=0)
    except Exception as e:
        print(f"Error moving mouse: {e}")

@sio.event
def trigger_click(sid, data):
    """
    Triggers a system-level left click.
    """
    global last_click_time
    current_time = time.time()
    
    if current_time - last_click_time > CLICK_COOLDOWN:
        try:
            pyautogui.click()
            last_click_time = current_time
            print("System Click Triggered")
        except Exception as e:
            print(f"Error clicking: {e}")

if __name__ == '__main__':
    eventlet.wsgi.server(eventlet.listen(('0.0.0.0', 5000)), app)
