/**
 * Gaze Mouse System — Frontend Logic (Xvfb / Native Calibration Edition)
 * =========================================================================
 * This page runs HIDDEN inside a headless Chromium on a virtual display.
 * There is NO visible calibration UI here. Calibration is driven entirely
 * by the native GTK overlay (calibration.py) on the real display.
 *
 * Flow:
 *   1. Page loads → WebGazer initialises silently (camera opens)
 *   2. Socket connects → register as 'browser'
 *   3. Server sends 'native_calibration_point' for each dot the user clicks
 *      in the GTK window → fed directly into WebGazer's regression
 *   4. Server sends 'native_calibration_done' → gaze streaming starts
 *   5. Gaze coords (in real screen space) → emitted to server → pyautogui
 */

// Windows note: Chrome must be launched with --use-angle=d3d11 (not gl).
// This is handled automatically by launcher_win.py.
// Linux: --use-angle=gl | Windows: --use-angle=d3d11
const CONFIG = {
    SERVER_URL: 'http://localhost:5000',
    SMOOTHING: 0.03,   // Ultra-heavy damping
    DWELL_TIME: 1300,       
    DWELL_RADIUS: 50,       
    DWELL_SOFT_ABORT_MS: 300,
    EMIT_INTERVAL: 33,     
    
    // --- Ultra-Stability Precision Smoothing ---
    ALPHA_STILL: 0.02,      // MAXIMUM STABILITY — prevents all jitter
    ALPHA_MOVE:  0.12,      // BALANCED SLOW MOVEMENT
    MOVE_THRESHOLD: 60,     
    STABILITY_DEADZONE: 10, // px — cursor is frozen unless glance > 10px
    EDGE_BOOST: 0.0,        // NO edge attraction
    
    CLICK_COOLDOWN: 1000,   // ms before another dwell-click is allowed
    
    RADIAL_MENU_DWELL: 800,        // ms to select a radial menu slice
    RADIAL_MENU_CANCEL_DIST: 250,  // px distance to auto-cancel menu

};

// ── State ──────────────────────────────────────────────────────────────────────
const state = {
    isReady: false,   // true once calibration_done received
    smoothed: { x: 0, y: 0 },
    dwellStart: null,
    dwellAnchor: { x: 0, y: 0 },
    dwellExitTime: null,       // timestamp when gaze last left dwell radius
    lastClickTime: 0,
    lastEmitTime: 0,
    isLocked: false,
    isPaused: false,
    socket: null,
    calibSamplesLeft: 0,       // samples still expected from GTK
    poseOffsetX: 0,            // head pose correction offset (set by FIX-7)
    poseOffsetY: 0,
    
    radialMenu: { active: false, x: 0, y: 0, hoverSlice: null, dwellStart: null },
};


// ── Socket.IO ──────────────────────────────────────────────────────────────────
function initSocket() {
    state.socket = io(CONFIG.SERVER_URL, { transports: ['websocket'] });

    state.socket.on('connect', () => {
        console.log('[socket] connected — registering as browser');
        state.socket.emit('register', { type: 'browser' });
        updateStatus('Waiting for native calibration ...');
    });

    state.socket.on('disconnect', () => {
        console.warn('[socket] disconnected');
        updateStatus('Server disconnected');
    });

    // ── Calibration relay from server ─────────────────────────────────────────
    state.socket.on('native_calibration_point', (data) => {
        const { x, y } = data;
        // Guard against blink frames corrupting calibration data
        if (isBlinking()) {
            console.log(`[calib] blink detected — skipping sample at (${x}, ${y})`);
            return;
        }
        webgazer.recordScreenPosition(x, y, 'click');
        updateStatus(`Calibrating ... (${x}, ${y})`);
    });

    state.socket.on('native_calibration_done', () => {
        console.log('[calib] calibration complete — gaze streaming active');
        state.isReady = true;
        updateStatus('Gaze control active');

        // Explicitly save the freshly calibrated model to IndexedDB
        if (webgazer.saveDataAcrossSessions) {
            webgazer.saveDataAcrossSessions(true);
            console.log('[model] calibration saved to IndexedDB for next session');
        }

        // Request Wake Lock to prevent the hidden page from being throttled
        if ('wakeLock' in navigator) {
            navigator.wakeLock.request('screen').then(() => {
                console.log('[wakeLock] acquired');
            }).catch(err => {
                console.warn('[wakeLock] not available:', err.message);
            });
        }
    });

    // ── Snap / release feedback ───────────────────────────────────────────────
    state.socket.on('snapped', (data) => {
        state.isLocked = true;
        console.log(`[snap] locked → ${data.name}`);
    });

    state.socket.on('released', () => {
        state.isLocked = false;
        console.log('[snap] released');
    });

    // ── Head pose drift compensation ──────────────────────────────────────────
    // server.py computes how far the user's head has moved since calibration
    // and sends XY correction deltas. We accumulate them into state.poseOffset.
    state.socket.on('pose_correction', (data) => {
        state.poseOffsetX = data.dx || 0;
        state.poseOffsetY = data.dy || 0;
    });

    // --- Implicit Calibration (Self-Learning) ---
    state.socket.on('click_learned', (data) => {
        if (isBlinking()) {
            console.log('[learning] skipped — blink detected, protecting model');
            return;
        }
        console.log(`[learning] training on successful click at (${data.x}, ${data.y})`);
        webgazer.recordScreenPosition(data.x, data.y, 'click');
    });
}


// ── WebGazer ───────────────────────────────────────────────────────────────────
function initWebGazer() {
    // Point WebGazer at locally-served models so TF.js never needs the internet.
    // Without this, headless Chromium silently fails to fetch from tfhub.dev
    // and WebGazer produces no gaze output at all.
    webgazer.params.blazefaceModelURL = 'http://localhost:8000/models/blazeface/model.json';
    webgazer.params.facemeshModelURL = 'http://localhost:8000/models/facemesh/model.json';

    // Enable IndexedDB model persistence — model survives page reloads and restarts.
    // WebGazer will automatically load the saved regression weights on next session.
    webgazer.saveDataAcrossSessions(true);
    console.log('[model] IndexedDB persistence enabled');
    
    // --- WebGazer Tuning ---
    webgazer.params.imgWidth = 240;  // Default is 128. Increase for higher fidelity.
    webgazer.params.imgHeight = 240;
    webgazer.params.sampleRate = 30; // Maximize prediction frequency

    console.log('[webgazer] using local models');

    // WebGL check — logs to stderr (visible in terminal via --enable-logging)
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    if (!gl) {
        console.error('[FATAL] No WebGL — TF.js cannot run. Check --use-gl=angle flag.');
        updateStatus('FATAL: No WebGL');
        return;
    }
    console.log('[webgl] OK —', gl.getParameter(gl.RENDERER));

    webgazer
        .setGazeListener((data) => {
            if (!data) return;
            onGaze(data.x, data.y);
        })
        .showVideoPreview(false)
        .showPredictionPoints(false)
        .applyKalmanFilter(true)
        .begin()
        .then(() => {
            console.log('[webgazer] started — notifying server');
            updateStatus('WebGazer ready — awaiting calibration ...');
            state.socket.emit('webgazer_ready', {});
        })
        .catch(err => {
            console.error('[webgazer] FAILED:', err);
            updateStatus('ERROR: ' + err.message);
        });

    console.log('[webgazer] initialising ...');
    updateStatus('WebGazer initialising ...');
}


// ── Center-bias correction ─────────────────────────────────────────────────────
// WebGazer's regression pulls all predictions toward the screen center.
// This cubic correction layer pushes predictions outward proportional to
// how far they already are from center. Tune CONFIG.EDGE_BOOST (0.15–0.30).
function applyCenterBiasCorrection(x, y) {
    const W = window.innerWidth;
    const H = window.innerHeight;

    // Normalise to [-1, 1] range
    const nx = (x / W) * 2 - 1;
    const ny = (y / H) * 2 - 1;

    const k = CONFIG.EDGE_BOOST;

    // Cubic push: points near center unaffected, edges pushed out strongly
    const cx = nx + k * Math.sign(nx) * (nx * nx);
    const cy = ny + k * Math.sign(ny) * (ny * ny);

    // Clamp to avoid overshooting past screen edges
    const clamp = (v) => Math.max(-1, Math.min(1, v));

    return {
        x: ((clamp(cx) + 1) / 2) * W,
        y: ((clamp(cy) + 1) / 2) * H,
    };
}

// ── Gaze handler ───────────────────────────────────────────────────────────────
function onGaze(rawX, rawY) {
    if (!state.isReady || state.isPaused) return;
    if (!isFinite(rawX) || !isFinite(rawY)) return;

    // --- Stability Deadzone ---
    const sdx = rawX - state.smoothed.x;
    const sdy = rawY - state.smoothed.y;
    const dist = Math.sqrt(sdx*sdx + sdy*sdy);
    if (dist < CONFIG.STABILITY_DEADZONE) {
        return;
    }

    // --- Dynamic Smoothing (Adaptive EMA) ---
    // If we jumped far, we prioritze speed over stability (less lag)
    // If we are mostly still, we prioritize stability (less jitter)
    let a = CONFIG.ALPHA_STILL;
    if (dist > CONFIG.MOVE_THRESHOLD) {
        a = CONFIG.ALPHA_MOVE;
    }

    state.smoothed.x = a * rawX + (1 - a) * state.smoothed.x;
    state.smoothed.y = a * rawY + (1 - a) * state.smoothed.y;

    // Apply head pose correction, then center-bias correction
    const poseX = state.smoothed.x - (state.poseOffsetX || 0);
    const poseY = state.smoothed.y - (state.poseOffsetY || 0);
    const corrected = applyCenterBiasCorrection(poseX, poseY);
    const { x, y } = corrected;
    const W = window.innerWidth;
    const H = window.innerHeight;

    if (state.radialMenu.active) {
        handleRadialMenuGaze(x, y);
    } else {
        handleDwell(x, y);
    }

    // Throttle emissions to ~30 fps
    const now = Date.now();
    if (now - state.lastEmitTime >= CONFIG.EMIT_INTERVAL) {
        state.socket.emit('move_mouse', {
            x, y,
            screen_left: window.screenX || 0,
            screen_top: window.screenY || 0,
            device_pixel_ratio: window.devicePixelRatio || 1,
            timestamp: now,
        });
        state.lastEmitTime = now;
    }
}




// ── Blink detection ────────────────────────────────────────────────────────────
// Returns true if the user is currently blinking, based on Eye Aspect Ratio (EAR).
// During blinks, MediaPipe iris landmarks collapse vertically — this catches them
// before they corrupt the WebGazer regression model.
function isBlinking() {
    try {
        const eyeFeatures = webgazer.getEyeFeats ? webgazer.getEyeFeats() : null;
        if (!eyeFeatures) return false;

        // Check left eye
        if (eyeFeatures.left && eyeFeatures.left.height !== undefined) {
            const h = eyeFeatures.left.height || 0;
            const w = eyeFeatures.left.width  || 1;
            if ((h / w) < 0.15) return true;
        }
        // Check right eye
        if (eyeFeatures.right && eyeFeatures.right.height !== undefined) {
            const h = eyeFeatures.right.height || 0;
            const w = eyeFeatures.right.width  || 1;
            if ((h / w) < 0.15) return true;
        }
        return false;
    } catch (e) {
        return false; // If we can't tell, assume no blink — safe default
    }
}

// ── Dwell-to-click ─────────────────────────────────────────────────────────────
function handleDwell(x, y) {
    const now = Date.now();
    if (now - state.lastClickTime < CONFIG.CLICK_COOLDOWN) {
        updateDwellRing(null, null, 0);
        return;
    }

    const dx = x - state.dwellAnchor.x;
    const dy = y - state.dwellAnchor.y;
    const dist = Math.sqrt(dx * dx + dy * dy);

    if (dist < CONFIG.DWELL_RADIUS) {
        // Gaze is inside the dwell radius
        state.dwellExitTime = null; // clear any soft-abort timer

        if (!state.dwellStart) {
            state.dwellStart = now;
            state.dwellAnchor = { x, y };
        }
        const elapsed = now - state.dwellStart;
        const progress = Math.min(elapsed / CONFIG.DWELL_TIME, 1.0);

        updateDwellRing(state.dwellAnchor.x, state.dwellAnchor.y, progress);

        if (progress >= 1.0) {
            openRadialMenu(state.dwellAnchor.x, state.dwellAnchor.y);
        }
    } else {
        // Gaze left the dwell radius
        if (state.dwellStart !== null) {
            // We were dwelling — start the soft-abort grace period
            if (state.dwellExitTime === null) {
                state.dwellExitTime = now;
            }
            const exitDuration = now - state.dwellExitTime;

            if (exitDuration < CONFIG.DWELL_SOFT_ABORT_MS) {
                // Within grace period — hold progress, don't reset yet
                const elapsed = now - state.dwellStart;
                const progress = Math.min(elapsed / CONFIG.DWELL_TIME, 1.0);
                updateDwellRing(state.dwellAnchor.x, state.dwellAnchor.y, progress * 0.85);
                return; // Do not reset
            }
        }

        // Grace period expired or never dwelling — full reset
        state.dwellStart = null;
        state.dwellExitTime = null;
        state.dwellAnchor = { x, y };
        updateDwellRing(null, null, 0);
    }
}

function updateDwellRing(x, y, progress) {
    const ring = document.getElementById('dwell-ring');
    if (!ring) return;

    if (progress <= 0 || x === null) {
        ring.classList.remove('active');
        return;
    }

    ring.classList.add('active');
    ring.style.left = `${x}px`;
    ring.style.top = `${y}px`;
    
    // Shrink from scale(1.0) to scale(0.1)
    const scale = 1.0 - (progress * 0.9);
    ring.style.transform = `translate(-50%, -50%) scale(${scale})`;
    ring.style.opacity = 0.3 + (progress * 0.7);
}

// ── Radial Menu Logic ─────────────────────────────────────────────────────────

function openRadialMenu(x, y) {
    state.radialMenu.active = true;
    state.radialMenu.x = x;
    state.radialMenu.y = y;
    state.radialMenu.hoverSlice = null;
    state.radialMenu.dwellStart = null;
    
    state.socket.emit('radial_menu_state', { active: true, x, y, slice: null, progress: 0 });
    console.log('[radial] menu opened at', x, y);
    updateDwellRing(null, null, 0);
}

function closeRadialMenu(triggerActionName = null) {
    state.radialMenu.active = false;
    state.radialMenu.hoverSlice = null;
    state.socket.emit('radial_menu_state', { active: false });
    
    // Reset regular dwell to prevent immediate re-triggering
    state.dwellStart = null;
    state.dwellAnchor = { x: state.smoothed.x, y: state.smoothed.y };
    state.lastClickTime = Date.now();
    
    if (triggerActionName && triggerActionName !== 'Cancel') {
        let type = 'left';
        if (triggerActionName === 'Right') type = 'right';
        if (triggerActionName === 'Double') type = 'double';
        
        if (triggerActionName === 'Scroll') {
            state.socket.emit('trigger_scroll', { x: state.radialMenu.x, y: state.radialMenu.y, direction: 'down', amount: 3 });
            console.log('[radial] scroll triggered');
        } else {
            state.socket.emit('trigger_action', { x: state.radialMenu.x, y: state.radialMenu.y, type: type });
            console.log(`[radial] ${type} click triggered`);
        }
    } else {
        console.log('[radial] menu cancelled');
    }
}

function handleRadialMenuGaze(x, y) {
    const rm = state.radialMenu;
    const dx = x - rm.x;
    const dy = y - rm.y;
    const dist = Math.sqrt(dx * dx + dy * dy);

    // Auto cancel if look too far away
    if (dist > CONFIG.RADIAL_MENU_CANCEL_DIST) {
        closeRadialMenu('Cancel');
        return;
    }

    // Deadzone check
    if (dist < 40) {
        updateRadialMenuHover(null);
        return;
    }

    // Determine slice (0 to 4)
    // angle from top (-pi/2) clockwise
    let angle = Math.atan2(dy, dx); // -pi to pi
    angle += Math.PI / 2;
    if (angle < 0) angle += 2 * Math.PI; // 0 to 2pi
    
    const sliceAngle = (2 * Math.PI) / 5;
    const sliceIndex = Math.floor(angle / sliceAngle);
    
    updateRadialMenuHover(sliceIndex);
}

function updateRadialMenuHover(sliceIndex) {
    const rm = state.radialMenu;
    const now = Date.now();
    
    if (sliceIndex !== rm.hoverSlice) {
        rm.hoverSlice = sliceIndex;
        rm.dwellStart = sliceIndex !== null ? now : null;
        state.socket.emit('radial_menu_state', {
            active: true, x: rm.x, y: rm.y, slice: rm.hoverSlice, progress: 0
        });
    } else if (sliceIndex !== null) {
        const elapsed = now - rm.dwellStart;
        const progress = Math.min(elapsed / CONFIG.RADIAL_MENU_DWELL, 1.0);
        
        state.socket.emit('radial_menu_state', {
            active: true, x: rm.x, y: rm.y, slice: rm.hoverSlice, progress: progress
        });
        
        if (progress >= 1.0) {
            const actions = ['Double', 'Left', 'Scroll', 'Right', 'Cancel'];
            closeRadialMenu(actions[sliceIndex]);
        }
    }
}


// ── Helpers ────────────────────────────────────────────────────────────────────
function updateStatus(msg) {
    const el = document.getElementById('status');
    if (el) el.textContent = msg;
    console.log(`[status] ${msg}`);
}


// ── Boot ───────────────────────────────────────────────────────────────────────
window.onload = () => {
    initSocket();
    initWebGazer();
};

window.onbeforeunload = () => {
    webgazer.end();
};
