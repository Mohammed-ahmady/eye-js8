/**
 * Gaze Mouse System - Frontend Logic
 * Senior Full-Stack Engineer & HCI Specialist
 * OS-Level Control via Bridge Architecture
 */

// --- Configuration ---
const CONFIG = {
    SERVER_URL: 'http://localhost:5000',
    SMOOTHING_FACTOR: 0.2, // Alpha for EMA (0.15 to 0.3)
    DWELL_TIME: 1500, // ms to trigger click
    DWELL_RADIUS: 30, // pixels
    CALIBRATION_DWELL: 2500, // ms for each calibration point
    MAGNETIC_RADIUS: 100, // pixels to trigger snap
    BREAKAWAY_DISTANCE: 180, // pixels to break magnetic bond
    COOLDOWN_TIME: 1500, // ms after click to prevent repeats
};

// --- State Management ---
let state = {
    isCalibrated: false,
    isCalibrating: false,
    lastGaze: { x: 0, y: 0 },
    smoothedGaze: { x: 0, y: 0 },
    dwellStart: null,
    dwellPoint: { x: 0, y: 0 },
    lastClickTime: 0,
    socket: null,
    calibrationPoints: [
        { x: 10, y: 10 }, { x: 50, y: 10 }, { x: 90, y: 10 },
        { x: 10, y: 50 }, { x: 50, y: 50 }, { x: 90, y: 50 },
        { x: 10, y: 90 }, { x: 50, y: 90 }, { x: 90, y: 90 }
    ],
    currentPointIndex: 0,
    isLocked: false,
    lockedTarget: null,
    lastBlinkTime: 0,
    isPaused: false
};

// --- UI Elements ---
const elements = {
    overlay: document.getElementById('calibration-overlay'),
    dot: document.getElementById('calibration-dot'),
    startBtn: document.getElementById('start-btn'),
    statusText: document.getElementById('status-text'),
    serverStatus: document.getElementById('server-status'),
    dwellIndicator: document.getElementById('dwell-indicator'),
    dwellProgress: document.getElementById('dwell-progress'),
    testArea: document.getElementById('test-area'),
    pauseBtn: document.getElementById('pause-btn'),
    debugMeshBtn: document.getElementById('debug-mesh-btn')
};

// --- Initialization ---
function init() {
    // Initialize Socket.IO for OS-level communication
    state.socket = io(CONFIG.SERVER_URL);

    state.socket.on('connect', () => {
        elements.serverStatus.textContent = 'Connected';
        elements.serverStatus.style.color = '#2ed573';
        console.log('Connected to OS controller backend');
    });

    state.socket.on('disconnect', () => {
        elements.serverStatus.textContent = 'Disconnected';
        elements.serverStatus.style.color = '#ff4757';
        console.log('Disconnected from OS controller backend');
    });

    state.socket.on('error', (error) => {
        console.error('Socket error:', error);
        elements.serverStatus.textContent = 'Error';
        elements.serverStatus.style.color = '#ff6348';
    });

    elements.startBtn.addEventListener('click', startCalibration);
    elements.pauseBtn.addEventListener('click', togglePause);
    elements.debugMeshBtn.addEventListener('click', toggleDebugMesh);

    // Initialize WebGazer with specific tracker for stable landmarks
    webgazer.setTracker("clmtrackr");

    webgazer.setGazeListener((data, elapsedTime) => {
        if (data == null) return;
        handleGaze(data.x, data.y);
    }).begin();

    // Calibration Safeguard: Override recordScreenPosition to prevent 
    // polluting calibration during magnetic snaps
    const originalRecord = webgazer.recordScreenPosition;
    webgazer.recordScreenPosition = function (x, y) {
        if (state.isLocked) return; // Don't train while snapped
        return originalRecord.apply(this, arguments);
    };

    // Hide video preview and face mesh for clean UI
    webgazer.showVideoPreview(false).showPredictionPoints(false).applyKalmanFilter(true);

    elements.statusText.textContent = 'Ready to Calibrate';
}

// --- Gaze Handling & Smoothing ---
function handleGaze(x, y) {
    state.lastGaze = { x, y };

    // Apply Exponential Moving Average (EMA) Smoothing
    // This reduces jitter from micro-saccades
    state.smoothedGaze.x = (CONFIG.SMOOTHING_FACTOR * x) + ((1 - CONFIG.SMOOTHING_FACTOR) * state.smoothedGaze.x);
    state.smoothedGaze.y = (CONFIG.SMOOTHING_FACTOR * y) + ((1 - CONFIG.SMOOTHING_FACTOR) * state.smoothedGaze.y);

    if (state.isCalibrating) {
        // Calibration logic is handled by the calibration loop
        return;
    }

    if (state.isCalibrated) {
        if (state.isPaused) {
            elements.dwellIndicator.style.display = 'none';
            return;
        }

        let targetX = state.smoothedGaze.x;
        let targetY = state.smoothedGaze.y;

        // --- Magnetic Snapping & Breakaway Logic ---
        if (!state.isLocked) {
            const nearest = findNearestClickable(targetX, targetY);
            if (nearest) {
                state.isLocked = true;
                state.lockedTarget = nearest;
                elements.statusText.textContent = 'MAGNETIC LOCK: ON';
                elements.statusText.classList.add('locked');
                console.log('Magnetic Snap Engaged');
            }
        } else {
            // Calculate "Breakaway Force"
            // Use RAW gaze (x, y) vs SNAP target to see if user is pulling away
            const rawDist = Math.sqrt(Math.pow(x - state.lockedTarget.x, 2) + Math.pow(y - state.lockedTarget.y, 2));

            if (rawDist > CONFIG.BREAKAWAY_DISTANCE) {
                state.isLocked = false;
                state.lockedTarget = null;
                elements.statusText.textContent = 'System Active - Search for Targets...';
                elements.statusText.classList.remove('locked');
                console.log('Breakaway: Lock Released');

                // Visual feedback for breakaway (blue flash)
                elements.dwellIndicator.style.borderColor = '#3498db';
                setTimeout(() => {
                    elements.dwellIndicator.style.borderColor = 'rgba(46, 213, 115, 0.5)';
                }, 400);
            } else {
                // Stay locked to the target
                targetX = state.lockedTarget.x;
                targetY = state.lockedTarget.y;
            }
        }

        // Send coordinates to backend
        state.socket.emit('move_mouse', {
            x: targetX,
            y: targetY,
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            timestamp: Date.now()
        });

        // Handle Dwell-to-Click (using the snapped/target coordinates)
        handleDwell(targetX, targetY);
    }
}

// --- Magnetic Snapping ---
function findNearestClickable(x, y) {
    if (state.isPaused) return null;

    // Find clickable elements: buttons, links, inputs, and our test buttons
    const clickables = document.querySelectorAll('button:not(#pause-btn), a, input[type="button"], input[type="submit"], [role="button"], .test-button');
    let nearest = null;
    let minSourceDist = CONFIG.MAGNETIC_RADIUS;

    clickables.forEach(el => {
        // Special check: ignore invisible elements or the pause button itself
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;

        const rect = el.getBoundingClientRect();
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        const dist = Math.sqrt(Math.pow(x - centerX, 2) + Math.pow(y - centerY, 2));

        if (dist < minSourceDist) {
            minSourceDist = dist;
            nearest = { x: centerX, y: centerY, element: el };
        }
    });

    return nearest;
}

// --- Pause Logic ---
function togglePause() {
    state.isPaused = !state.isPaused;

    if (state.isPaused) {
        elements.pauseBtn.textContent = 'Resume Gaze Control';
        elements.pauseBtn.classList.add('paused');
        elements.statusText.textContent = 'PAUSED - Free Mouse Use';
        elements.statusText.classList.remove('locked');
        state.isLocked = false;
        state.lockedTarget = null;
    } else {
        elements.pauseBtn.textContent = 'Pause Gaze Control';
        elements.pauseBtn.classList.remove('paused');
        elements.statusText.textContent = 'System Active - OS Control Enabled';
    }
}

// --- Dwell-to-Click Logic ---
function handleDwell(x, y) {
    const now = Date.now();

    // Check click cooldown strictly
    if (now - state.lastClickTime < CONFIG.COOLDOWN_TIME || state.isPaused) {
        elements.dwellIndicator.style.display = 'none';
        state.dwellStart = null; // Clear dwell if in cooldown
        return;
    }

    elements.dwellIndicator.style.display = 'block';
    elements.dwellIndicator.style.left = `${x}px`;
    elements.dwellIndicator.style.top = `${y}px`;

    const dist = Math.sqrt(Math.pow(x - state.dwellPoint.x, 2) + Math.pow(y - state.dwellPoint.y, 2));

    if (dist < CONFIG.DWELL_RADIUS) {
        if (!state.dwellStart) {
            state.dwellStart = now;
            state.dwellPoint = { x, y };
        }

        const elapsed = now - state.dwellStart;
        const progress = Math.min((elapsed / CONFIG.DWELL_TIME) * 100, 100);
        elements.dwellProgress.style.clipPath = `inset(${100 - progress}% 0 0 0)`;

        if (elapsed >= CONFIG.DWELL_TIME && (now - state.lastClickTime > CONFIG.COOLDOWN_TIME)) {
            triggerClick();
        }
    } else {
        state.dwellStart = null;
        state.dwellPoint = { x, y };
        elements.dwellProgress.style.clipPath = `inset(100% 0 0 0)`;
    }
}

function triggerClick() {
    state.lastClickTime = Date.now(); // Set immediately to prevent multiple triggers
    state.dwellStart = null;
    elements.dwellProgress.style.clipPath = `inset(100% 0 0 0)`;

    state.socket.emit('trigger_click', {
        x: state.smoothedGaze.x,
        y: state.smoothedGaze.y,
        timestamp: Date.now()
    });

    // Visual feedback for click (white flash)
    elements.dwellIndicator.style.borderColor = '#ffffff';
    elements.dwellIndicator.style.boxShadow = '0 0 20px rgba(255, 255, 255, 0.8)';
    console.log('OS-Level Click Triggered');

    setTimeout(() => {
        elements.dwellIndicator.style.borderColor = 'rgba(46, 213, 115, 0.5)';
        elements.dwellIndicator.style.boxShadow = 'none';
    }, 200);
}

// --- Calibration Logic ---
async function startCalibration() {
    state.isCalibrating = true;
    elements.overlay.style.display = 'none';
    elements.dot.style.display = 'block';
    elements.statusText.textContent = 'Calibrating...';

    for (let i = 0; i < state.calibrationPoints.length; i++) {
        const point = state.calibrationPoints[i];
        await calibratePoint(point.x, point.y);
    }

    state.isCalibrating = false;
    state.isCalibrated = true;
    elements.dot.style.display = 'none';
    elements.statusText.textContent = 'System Active - OS Control Enabled';
    elements.dwellIndicator.style.display = 'block';

    // Show test controls after calibration
    elements.testArea.style.display = 'grid';
    elements.pauseBtn.style.display = 'block';
    elements.debugMeshBtn.style.display = 'block';
}

function calibratePoint(pctX, pctY) {
    return new Promise((resolve) => {
        const x = (pctX / 100) * window.innerWidth;
        const y = (pctY / 100) * window.innerHeight;

        elements.dot.style.left = `${x}px`;
        elements.dot.style.top = `${y}px`;
        elements.dot.classList.remove('filling');

        // Wait for user to look at the dot
        setTimeout(() => {
            elements.dot.classList.add('filling');

            // Record position multiple times during dwell to train WebGazer
            const interval = setInterval(() => {
                webgazer.recordScreenPosition(x, y);
            }, 100);

            setTimeout(() => {
                clearInterval(interval);
                resolve();
            }, CONFIG.CALIBRATION_DWELL);
        }, 500);
    });
}

// Start the application
window.onload = init;
window.onbeforeunload = () => {
    webgazer.end();
};
