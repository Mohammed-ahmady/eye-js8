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
    COOLDOWN_TIME: 1000, // ms after click
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
    currentPointIndex: 0
};

// --- UI Elements ---
const elements = {
    overlay: document.getElementById('calibration-overlay'),
    dot: document.getElementById('calibration-dot'),
    startBtn: document.getElementById('start-btn'),
    statusText: document.getElementById('status-text'),
    serverStatus: document.getElementById('server-status'),
    dwellIndicator: document.getElementById('dwell-indicator'),
    dwellProgress: document.getElementById('dwell-progress')
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

    // Initialize WebGazer
    webgazer.setGazeListener((data, elapsedTime) => {
        if (data == null) return;
        handleGaze(data.x, data.y);
    }).begin();

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
        // Send smoothed coordinates to backend for OS-level mouse control
        // Include full viewport dimensions for accurate screen mapping
        state.socket.emit('move_mouse', {
            x: state.smoothedGaze.x,
            y: state.smoothedGaze.y,
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            timestamp: Date.now()
        });

        // Handle Dwell-to-Click for OS-level clicks
        handleDwell(state.smoothedGaze.x, state.smoothedGaze.y);
    }
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

// --- Dwell-to-Click Logic ---
function handleDwell(x, y) {
    const now = Date.now();
    
    // Check cooldown to prevent accidental double-clicks
    if (now - state.lastClickTime < CONFIG.COOLDOWN_TIME) {
        elements.dwellIndicator.style.display = 'none';
        return;
    }

    // Update indicator position to follow gaze
    elements.dwellIndicator.style.display = 'block';
    elements.dwellIndicator.style.left = `${x}px`;
    elements.dwellIndicator.style.top = `${y}px`;

    // Check if gaze is within radius of the dwell point
    const dist = Math.sqrt(Math.pow(x - state.dwellPoint.x, 2) + Math.pow(y - state.dwellPoint.y, 2));

    if (dist < CONFIG.DWELL_RADIUS) {
        if (!state.dwellStart) {
            state.dwellStart = now;
            state.dwellPoint = { x, y };
        }

        const elapsed = now - state.dwellStart;
        const progress = Math.min((elapsed / CONFIG.DWELL_TIME) * 100, 100);
        
        // Update visual progress (filling from bottom up)
        elements.dwellProgress.style.clipPath = `inset(${100 - progress}% 0 0 0)`;

        if (elapsed >= CONFIG.DWELL_TIME) {
            triggerClick();
        }
    } else {
        // Reset dwell if gaze moves too far
        state.dwellStart = null;
        state.dwellPoint = { x, y };
        elements.dwellProgress.style.clipPath = `inset(100% 0 0 0)`;
    }
}

function triggerClick() {
    // Send click command to backend for OS-level execution
    state.socket.emit('trigger_click', {
        x: state.smoothedGaze.x,
        y: state.smoothedGaze.y,
        timestamp: Date.now()
    });
    
    state.lastClickTime = Date.now();
    state.dwellStart = null;
    elements.dwellProgress.style.clipPath = `inset(100% 0 0 0)`;
    
    // Visual feedback for click (white flash)
    elements.dwellIndicator.style.borderColor = '#ffffff';
    elements.dwellIndicator.style.boxShadow = '0 0 20px rgba(255, 255, 255, 0.8)';
    
    console.log('OS-Level Click Triggered');
    
    setTimeout(() => {
        elements.dwellIndicator.style.borderColor = 'rgba(46, 213, 115, 0.5)';
        elements.dwellIndicator.style.boxShadow = 'none';
    }, 200);
}

// Start the application
window.onload = init;
window.onbeforeunload = () => {
    webgazer.end();
};
