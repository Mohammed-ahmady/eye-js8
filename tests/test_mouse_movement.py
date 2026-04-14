import pyautogui
import time
import math

print("==================================================")
print("  Gaze Mouse - Mouse Movement Permission Test  ")
print("==================================================")
print("This will move your mouse in a circle in 3 seconds.")
print("If this works, PyAutoGUI has correct permissions.")
print("Press Ctrl+C to stop.")

time.sleep(3)

# Disable Fail-Safe for this test
pyautogui.FAILSAFE = False

width, height = pyautogui.size()
center_x = width // 2
center_y = height // 2

try:
    for i in range(200):
        angle = i * 0.1
        # Use math.cos/math.sin (lowercase)
        x = center_x + int(math.cos(angle) * 200)
        y = center_y + int(math.sin(angle) * 200)
        pyautogui.moveTo(x, y, duration=0.01)
        if i % 20 == 0:
            print(f"Moving to: ({x}, {y})")
    print("\nSUCCESS: Mouse moved successfully!")
except Exception as e:
    print(f"\nERROR: Could not move mouse: {e}")
except KeyboardInterrupt:
    print("\nTest stopped by user.")
