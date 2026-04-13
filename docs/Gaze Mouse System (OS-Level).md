# Gaze Mouse System (OS-Level)

A complete, end-to-end OS-level mouse control system designed for users with severe motor impairments. This system utilizes a webcam and eye-tracking to provide **full operating system-level mouse control**.

## Architecture

This system employs a **Bridge Architecture** to enable seamless OS-level control, bypassing browser security sandboxes:

-   **Frontend (The Tracker):** A local HTML/JS web application (`index.html`, `app.js`) running `webgazer.js`. It captures the webcam feed, calculates gaze coordinates, handles custom clickless calibration, applies Exponential Moving Average (EMA) smoothing, and manages the dwell-to-click interaction logic. It communicates with the backend via WebSocket.
-   **Middleware (The Bridge):** A lightweight WebSocket connection (Socket.IO) facilitates real-time, low-latency communication (30+ FPS) between the browser-based frontend and the local Python backend.
-   **Backend (The OS Controller):** A Python script (`server.py`) acts as the WebSocket server. It receives smoothed (x,y) gaze coordinates from the frontend, maps them accurately to the absolute screen resolution of the primary monitor, and executes OS-level mouse movements and clicks using the cross-platform `pyautogui` library.

## Prerequisites

To run this system, you will need:

-   **Python 3.x** (installed and accessible via `python` or `python3`)
-   A modern web browser (Google Chrome or Microsoft Edge recommended) with webcam access.
-   A functional webcam.
-   **Operating System Specifics:**
    -   **Linux/Ubuntu:** `scrot`, `python3-tk`, and `python3-dev` are required for `pyautogui` to function correctly. These can be installed via your package manager.
    -   **Windows:** No additional system-level dependencies are typically required beyond Python.

## Installation

1.  **Clone or Download the Repository:**
    Obtain the project files and navigate to the root directory of the project.

2.  **Install Python Dependencies:**
    Open a terminal or command prompt, navigate to the `backend` directory, and install the required Python packages:
    ```bash
    cd backend
    pip install -r requirements.txt
    cd ..
    ```

3.  **Install Linux/Ubuntu System Dependencies (if applicable):**
    If you are running Linux (e.g., Ubuntu), install the necessary packages for `pyautogui`:
    ```bash
    sudo apt-get update
    sudo apt-get install scrot python3-tk python3-dev
    ```

## Execution Steps

To use the Gaze Mouse System, follow these steps in order:

1.  **Start the Backend Server:**
    Open a terminal or command prompt and run the Python backend script. This must be running *before* you open the frontend.
    ```bash
    python backend/server.py
    ```
    The server will start on `http://localhost:5000` and print your screen resolution. Keep this terminal window open.

2.  **Launch the Frontend:**
    Open the `frontend/index.html` file in your preferred web browser. You can do this by navigating to the file path (e.g., `file:///path/to/gaze-mouse-system/frontend/index.html`) or by using a local web server.

    -   **Allow Webcam Access:** Your browser will prompt you to allow webcam access. Grant permission for the system to function.
    -   **Start Calibration:** Click the "Start Calibration" button on the screen.
    -   **Follow the Calibration Dot:** A red dot will appear at 9 different points on your screen. Look directly at the dot and keep your head as still as possible until it fills with green and moves to the next point. This process trains the eye-tracking model without requiring physical clicks.
    -   **System Activation:** Once calibration is complete, the system will activate, and your gaze will control the OS mouse pointer.

## Features

-   **Custom Clickless Calibration:** An automated, dwell-based calibration wizard that allows users to train the `webgazer.js` model by simply looking at a series of points, eliminating the need for physical mouse clicks.
-   **Gaze Smoothing Algorithm:** Implements an Exponential Moving Average (EMA) filter in the frontend to significantly reduce jitter and provide a smoother, more stable gaze pointer experience.
-   **Dwell-to-Click Interaction:** The system triggers a system-level left click when the gaze remains within a configurable pixel radius for a specified duration, providing a reliable click mechanism for users with motor impairments.
-   **Visual Feedback:** Provides clear visual cues on the frontend for calibration progress and dwell timer status, enhancing user experience and understanding.
-   **OS-Level Control:** Through the WebSocket bridge and `pyautogui`, the system moves the actual operating system mouse pointer, allowing interaction with any application or element on the desktop.

## Configuration

You can adjust the following parameters in `frontend/app.js` to fine-tune the system's behavior:

-   `SMOOTHING_FACTOR`: Controls the balance between responsiveness and stability of the gaze pointer (recommended range: 0.15 - 0.3).
-   `DWELL_TIME`: The duration (in milliseconds) that gaze must be held steady to trigger a click.
-   `DWELL_RADIUS`: The pixel radius within which gaze must remain stable to count towards dwell time.
-   `CALIBRATION_DWELL`: The time (in milliseconds) spent at each calibration point.
-   `COOLDOWN_TIME`: The duration (in milliseconds) after a click during which no new clicks can be registered, preventing accidental double-clicks.

## Important Notes

-   **`pyautogui.FAILSAFE`:** The backend `server.py` has `pyautogui.FAILSAFE` enabled. This means if you move your mouse to any of the four corners of the screen, the `pyautogui` functions will raise an exception and exit. This is a safety feature to regain control of your computer if the gaze control becomes erratic. You can disable it by setting `pyautogui.FAILSAFE = False` in `server.py`, but this is not recommended.
-   **Permissions:** Ensure your operating system grants the necessary permissions for Python scripts to control the mouse and for your browser to access the webcam.

---
*Generated by Manus AI*
