# FIX_LOG

## 2026-04-13
- server_win.py: removed duplicate clamp_to_virtual_desktop() in on_move_mouse; kept only the clamp before pyautogui.moveTo.
- server_win.py: moved pythoncom.CoInitialize() out of per-frame on_move_mouse into one-time module-level initialization.
- hud_win.py: switched overlay bounds from primaryScreen() to QDesktopWidget().screenGeometry(-1) so HUD spans all monitors.
- server_win.py: added pythoncom.CoInitialize() at start of _scan_clickable_target so each daemon scan thread initializes COM for UIAutomation.
