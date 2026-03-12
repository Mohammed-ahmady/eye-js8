import socketio
import eventlet
import pyautogui
from screeninfo import get_monitors
import time
import pyatspi

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

# --- Global State ---
class SystemState:
    def __init__(self):
        self.last_click_time = 0
        self.is_locked = False
        self.locked_coords = (0, 0)
        self.last_raw_gaze = (0, 0)
        self.snap_radius = 100
        self.release_radius = 200

state = SystemState()

# --- OS Accessibility Mapper ---
class AccessibilityMapper:
    def __init__(self):
        self.reg = pyatspi.Registry
        self.desktop = self.reg.getDesktop(0)
        self.clickable_roles = [
            'button', 'menu item', 'link', 'push button', 
            'toggle button', 'check box', 'radio button', 'tab'
        ]

    def find_nearest_clickable(self, x, y, radius):
        """Finds the nearest clickable element around (x, y) using AT-SPI."""
        # Performance trick: Check center and a small cross offset
        check_offsets = [
            (0, 0), 
            (-radius//2, 0), (radius//2, 0), 
            (0, -radius//2), (0, radius//2)
        ]
        
        best_target = None
        min_dist = radius + 1

        for dx, dy in check_offsets:
            try:
                target = self.desktop.get_accessible_at_point(int(x + dx), int(y + dy), pyatspi.XY_SCREEN)
                if not target: continue
                
                # Check target and its parents (max 3 levels up) for clickable roles
                curr = target
                for _ in range(3):
                    if not curr: break
                    role = curr.get_role_name()
                    if role in self.clickable_roles:
                        ext = curr.get_extents(pyatspi.XY_SCREEN)
                        cx = ext.x + ext.width // 2
                        cy = ext.y + ext.height // 2
                        dist = ((x - cx)**2 + (y - cy)**2)**0.5
                        if dist < min_dist:
                            min_dist = dist
                            best_target = {'x': cx, 'y': cy, 'name': curr.name}
                        break
                    curr = curr.parent
            except:
                continue
        
        return best_target

mapper = AccessibilityMapper()

@sio.event
def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.event
def disconnect(sid):
    print(f"Client disconnected: {sid}")

@sio.event
def move_mouse(sid, data):
    """
    Handles gaze data, performs OS-level snapping, and moves the mouse.
    """
    try:
        x = data.get('x')
        y = data.get('y')
        vw = data.get('viewport_width')
        vh = data.get('viewport_height')

        if None in (x, y, vw, vh):
            return

        # Map viewport coordinates to absolute screen coordinates
        raw_x = max(0, min(SCREEN_WIDTH - 1, (x / vw) * SCREEN_WIDTH))
        raw_y = max(0, min(SCREEN_HEIGHT - 1, (y / vh) * SCREEN_HEIGHT))
        
        target_x, target_y = raw_x, raw_y

        # Backend Snapping Logic
        if not state.is_locked:
            nearest = mapper.find_nearest_clickable(raw_x, raw_y, state.snap_radius)
            if nearest:
                state.is_locked = True
                state.locked_coords = (nearest['x'], nearest['y'])
                target_x, target_y = state.locked_coords
                sio.emit('snapped', {'name': nearest['name']}, to=sid)
                print(f"Snapped to: {nearest['name']} at {state.locked_coords}")
        else:
            # Check for Look-Away Force release
            dist = ((raw_x - state.locked_coords[0])**2 + (raw_y - state.locked_coords[1])**2)**0.5
            if dist > state.release_radius:
                state.is_locked = False
                sio.emit('released', {}, to=sid)
                print("Lock released by force")
            else:
                target_x, target_y = state.locked_coords

        # Execute mouse move
        pyautogui.moveTo(target_x, target_y, duration=0)
        
    except Exception as e:
        # Avoid spamming console with AT-SPI errors
        pass

@sio.event
def trigger_click(sid, data):
    """
    Triggers a system-level left click.
    """
    current_time = time.time()
    if current_time - state.last_click_time > 1.0:
        try:
            pyautogui.click()
            state.last_click_time = current_time
            print("System Click Triggered")
        except Exception as e:
            print(f"Error clicking: {e}")

if __name__ == '__main__':
    eventlet.wsgi.server(eventlet.listen(('0.0.0.0', 5000)), app)
