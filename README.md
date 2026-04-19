
# eye-js8 — Gaze-Controlled Mouse (Windows Port)

> **Graduation Project — SHA GP24**  
> Supervisor: Dr. Mohammed Hussien  
> Platform: Windows 11  

OS-Level mouse control using eye tracking with WebGazer.js and MediaPipe.  
Designed as assistive technology for fully paralyzed users.

---

## Branches

| Branch | Platform | Description |
|--------|----------|-------------|
| `main` | Linux (Ubuntu) | Original Linux version using Xvfb + AT-SPI |
| `windows` | Windows 11 | **This branch** — full Windows port |

---

## How It Works

1. Chrome launches hidden offscreen at (-32000, -32000) using a per-run profile
2. WebGazer.js captures webcam frames and predicts gaze coordinates
3. Gaze coordinates are sent via Socket.IO to the Python backend
4. Backend smooths gaze and runs precision-assist targeting + bar context scan
5. UIAutomation triggers click actions (optionally without moving the OS cursor)
6. A PyQt5 HUD overlay shows the gaze ring, precision bubble, and menus

---

## Windows Branch Highlights

- Precision assist (bubble pointer) locks on nearby UI elements for easier targeting
- Bar menu for toolbars/taskbar with radial selection, paging, and zoom preview
- Calibration upgrades: two passes + corner refinement, stability sampling, and validation gate
- WebGazer startup: camera probe + auto-retry, optional raw direct mode, debug logging

---

## Project Structure

```
eye-js8/
├── core/                  # Python backend
│   ├── server_win.py      # Flask + Socket.IO gaze server
│   ├── hud_win.py         # PyQt5 HUD overlay
│   ├── launcher_win.py    # Process launcher
│   └── calibration_win.py # Calibration overlay
├── web/                   # Browser frontend
│   ├── app.js             # WebGazer gaze logic
│   └── index.html         # Hidden browser page
├── models/                # TensorFlow.js model files
│   ├── blazeface/
│   └── facemesh/
├── scripts/               # Setup and run scripts
│   ├── install_win.bat
│   ├── download_model.bat
│   └── start.bat
├── docs/                  # Documentation
│   ├── FIX_LOG.md         # Full bug fix history
│   ├── hud_runtime_log.jsonl        # Runtime HUD telemetry (generated)
│   └── precision_runtime_log.jsonl  # Precision telemetry (generated)
├── linux_reference/       # Original Linux files (reference only)
├── tests/
└── face_landmarker.task   # MediaPipe model (download separately)
```

---

## Setup (Windows 11)

### Option A: One-step installer (recommended)
```bat
scripts\install_win.bat
```

### Option B: Manual steps
1. Install Python dependencies
```bash
pip install -r requirements_win.txt
```

2. Download MediaPipe model
```bash
scripts\download_model.bat
```

### Run the system
```bash
scripts\start.bat
```

Or directly:
```bash
python core\launcher_win.py
```

---

## Requirements

- Windows 10/11
- Python 3.10+
- Google Chrome or Microsoft Edge
- Webcam
- NVIDIA or Intel GPU (DirectX 11 support required)

---

## Key Bug Fixes (Windows Port)

| Fix | Impact |
|-----|--------|
| Virtual desktop coordinate model | Dual-monitor support (secondary left monitor) |
| `--use-angle=d3d11` GPU flag | TensorFlow.js WebGL works on Windows |
| Qt thread safety via QTimer | Eliminates random crashes |
| UIAutomation off hot path | Removes gaze lag spikes |
| Kalman filter disabled | Reduces cursor latency |
| Pose baseline from live head pose | Accurate gaze after head movement |
| Per-thread COM initialization | UIAutomation works in daemon threads |
| PID-targeted process cleanup | Safe shutdown without killing other apps |

See `docs/FIX_LOG.md` for full details.

---

## Architecture

```
[Webcam]
	↓
[WebGazer.js in hidden Chrome]
	↓ Socket.IO (port 5000)
[server_win.py — Flask backend]
	↓                    ↓
[pyautogui]      [UIAutomation snap]
	↓
[OS Cursor]
	↓
[PyQt5 HUD overlay — gaze ring + dwell]
```

---

## Calibration

1. System launches automatically with calibration overlay
2. Two training passes + corner refinement run in sequence
3. Validation runs next (gaze preview only, actions disabled)
4. After validation, gaze actions enable and dwell click works

---

## Runtime Logs

- HUD telemetry: `docs/hud_runtime_log.jsonl`
- Precision telemetry: `docs/precision_runtime_log.jsonl`

---

## License

Academic project — SHA University, 2026.

---
