# Gaze Mouse OS Control System

A professional OS-level mouse control system using eye tracking. This project bridges a high-accuracy, calibrated WebGazer.js tracker (running in Chromium App Mode) with a Python-based OS interaction engine.

## 🚀 Key Features

*   **Universal Snapping**: Magnetically snaps the cursor to clickable OS elements (buttons, icons, menu items) across ALL applications using Linux AT-SPI accessibility tree.
*   **Chromium App Mode**: Runs in a dedicated, frameless window. Treated by the OS as a desktop application, bypassing typical browser throttling.
*   **Zero-Throttling Architecture**:
    *   Injected **Wake Lock API** to keep JS execution priority high.
    *   Chrome flags (`--disable-background-timer-throttling`, etc.) to ensure 60fps tracking even when multitasking.
*   **Accuracy & Stability**:
    *   **Calibrated Regression**: Uses WebGazer's native calibration for superior precision over raw facial models.
    *   **Smoothing & Jitter Control**: Real-time smoothing filter + micro-movement jitter reduction.
    *   **Snap Debouncing**: Requires target persistence to prevent cursor flickering.

## 🛠 Prerequisites

This system is designed for **Linux (Ubuntu/GNOME)**.

```bash
# Install system dependencies
sudo apt update
sudo apt install chromium-browser xdotool wmctrl python3-pyatspi
```

## 📦 Setup

1.  **Clone the repository**:
2.  **Create a Virtual Environment**:
    ```bash
    python3 -m venv .venv --system-site-packages
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

## 🚦 How to Run

Simply launch the multi-process launcher:
```bash
./.venv/bin/python launcher.py
```

1.  **Calibration**: Click the dots that appear on the screen to calibrate the tracker to your eyes.
2.  **Activation**: Once calibration is complete, the tracker window will automatically shrink to a tiny corner widget to stay alive in the background.
3.  **Control**: Your gaze now controls the mouse. Dwell on any clickable element to trigger a system-level click.

## 🏗 Architecture

*   **Front-end (JS)**: WebGazer.js + Socket.IO Client. Communicates screen coordinates.
*   **Back-end (Python)**: Eventlet server + PyAutoGUI + PyAtSpi. Performs the heavy lifting of OS inspection and pointer control.
*   **Launcher**: Manages the lifecycle of both servers and the Chromium App process.
