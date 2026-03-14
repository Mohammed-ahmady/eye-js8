import socketio
import eventlet
import pyautogui
from screeninfo import get_monitors
import time
import os
import subprocess

# --- AT-SPI Environment Setup ---
def setup_at_spi():
    """Ensures AT_SPI_BUS_ADDRESS is set correctly for pyatspi."""
    if 'AT_SPI_BUS_ADDRESS' not in os.environ:
        try:
            # Query the bus address via dbus-send
            cmd = "dbus-send --print-reply --dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress"
            output = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode()
            if 'string "' in output:
                address = output.split('string "')[1].split('"')[0]
                os.environ['AT_SPI_BUS_ADDRESS'] = address
                print(f"AT-SPI Bus Address discovered: {address}")
        except Exception as e:
            print(f"Warning: Could not auto-discover AT-SPI bus address: {e}")

setup_at_spi()

# Import pyatspi after environment is set up
import pyatspi


# Disable PyAutoGUI fail-safe for this specific accessibility use case. 
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
        self.snap_radius = 80 # slightly smaller for better control
        self.release_radius = 180

state = SystemState()

# --- OS Accessibility Mapper ---
class AccessibilityMapper:
    def __init__(self):
        self.clickable_roles = [
            'button', 'menu item', 'link', 'push button', 
            'toggle button', 'check box', 'radio button', 'tab', 'icon',
            'list item', 'page tab', 'menu'
        ]
        try:
            self.reg = pyatspi.Registry
            self.desktop = self.reg.getDesktop(0)
            print(f"Accessibility Mapper initialized on desktop: {self.desktop.name}")
        except Exception as e:
            print(f"Critical Error: Failed to connect to AT-SPI Registry: {e}")
            self.desktop = None

    def _is_point_near_extents(self, x, y, ext, radius):
        """Checks if (x, y) is within 'radius' of the bounding box."""
        # Expand bounds by radius
        return (ext.x - radius <= x <= ext.x + ext.width + radius) and \
               (ext.y - radius <= y <= ext.y + ext.height + radius)

    def _find_in_tree(self, root, x, y, radius, depth=0):
        """Recursively search for clickable elements near (x, y) with radius awareness."""
        if depth > 20: return None # Safety limit
        
        best_child = None
        
        try:
            # Check if root is near the gaze
            ext = root.get_extents(pyatspi.XY_SCREEN)
            if not self._is_point_near_extents(x, y, ext, radius):
                return None
                
            # If clickable, it's a candidate
            if root.get_role_name() in self.clickable_roles:
                best_child = root
            
            # Search children (reverse order usually helps find buttons on top)
            for i in range(root.childCount - 1, -1, -1):
                child = root.getChildAtIndex(i)
                found = self._find_in_tree(child, x, y, radius, depth + 1)
                if found:
                    return found
        except:
            pass
            
        return best_child

    def find_nearest_clickable(self, x, y, radius):
        """Finds the nearest clickable element around (x, y) across all OS apps."""
        if not self.desktop:
            return None
            
        # 1. Faster point-based lookup first
        target = None
        try:
            target = self.desktop.get_accessible_at_point(int(x), int(y), pyatspi.XY_SCREEN)
        except:
            pass

        # 2. Robust Radius-Aware Fallback
        if not target:
            # First, check apps that are most likely to have system UI (gnome-shell)
            # and then check all others.
            apps = []
            for i in range(self.desktop.childCount):
                try:
                    app = self.desktop.getChildAtIndex(i)
                    if "gnome-shell" in app.name.lower():
                        apps.insert(0, app) # Move system shell to front
                    else:
                        apps.append(app)
                except: continue

            for app in apps:
                try:
                    # Search windows of the app
                    for j in range(app.childCount):
                        win = app.getChildAtIndex(j)
                        found = self._find_in_tree(win, x, y, radius)
                        if found:
                            target = found
                            print(f"Deep Search Fallback: Found {found.name} in {app.name}")
                            break
                    if target: break
                except:
                    continue

        if not target:
            return None

        # 3. Resolve to clickable element
        best_target = None
        min_dist = radius + 1
        
        try:
            curr = target
            for _ in range(10): # Check deeper for better results
                if not curr: break
                role = curr.get_role_name()
                
                if role in self.clickable_roles:
                    ext = curr.get_extents(pyatspi.XY_SCREEN)
                    if ext.width <= 0 or ext.height <= 0: return None
                    
                    cx = ext.x + ext.width // 2
                    cy = ext.y + ext.height // 2
                    
                    dist = ((x - cx)**2 + (y - cy)**2)**0.5
                    if dist < min_dist:
                        min_dist = dist
                        best_target = {'x': cx, 'y': cy, 'name': curr.name or role}
                    break
                curr = curr.parent
        except:
            pass
            
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
    Now using absolute screen offsets provided by the browser.
    """
    try:
        x = data.get('x')
        y = data.get('y')
        sl = data.get('screen_left', 0)
        st = data.get('screen_top', 0)
        dpr = data.get('device_pixel_ratio', 1)

        if x is None or y is None:
            return

        # Calculate absolute screen coordinates
        # Note: Browsers usually provide x/y in 'CSS pixels'. 
        # On Ubuntu/GTK, screen coordinates might depend on display scaling.
        # We'll try raw Sl + x first, but monitor logs.
        raw_x = sl + x
        raw_y = st + y
        
        target_x, target_y = raw_x, raw_y

        # Backend Snapping Logic with Debouncing
        if not state.is_locked:
            nearest = mapper.find_nearest_clickable(raw_x, raw_y, state.snap_radius)
            if nearest:
                # Debounce: Must see the same candidate for 2 frames
                if hasattr(state, '_snap_candidate') and state._snap_candidate == nearest['name']:
                    state._snap_count += 1
                else:
                    state._snap_candidate = nearest['name']
                    state._snap_count = 1
                
                if state._snap_count >= 2:
                    state.is_locked = True
                    state.locked_coords = (nearest['x'], nearest['y'])
                    target_x, target_y = state.locked_coords
                    sio.emit('snapped', {'name': nearest['name']}, to=sid)
                    print(f"!!! SNAPPED to: {nearest.get('name')} at ({target_x}, {target_y})")
            else:
                state._snap_candidate = None
                state._snap_count = 0
        else:
            # Check for Look-Away Force release
            dist = ((raw_x - state.locked_coords[0])**2 + (raw_y - state.locked_coords[1])**2)**0.5
            if dist > state.release_radius:
                state.is_locked = False
                sio.emit('released', {}, to=sid)
                print(">>> Magnetic lock released")
                state._snap_candidate = None
                state._snap_count = 0
            else:
                target_x, target_y = state.locked_coords

        # Execute mouse move
        # Clamp to screen bounds
        target_x = max(0, min(SCREEN_WIDTH - 1, target_x))
        target_y = max(0, min(SCREEN_HEIGHT - 1, target_y))
        
        # Jitter Reduction: Only move if delta is meaningful (> 2 pixels)
        if not state.is_locked:
            last_x, last_y = pyautogui.position()
            if abs(target_x - last_x) < 2 and abs(target_y - last_y) < 2:
                return

        pyautogui.moveTo(target_x, target_y, duration=0)
        
    except Exception as e:
        if not hasattr(state, '_last_err_time'): state._last_err_time = 0
        if time.time() - state._last_err_time > 5.0:
            print(f"ERROR in move_mouse: {e}")
            state._last_err_time = time.time()

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
