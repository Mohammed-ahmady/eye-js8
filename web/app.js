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
    RAW_DIRECT_MODE: false,
    ENABLE_IMPLICIT_LEARNING: false,
    PERSIST_MODEL_ACROSS_SESSIONS: false,
    SHOW_WEBGAZER_DEBUG_OVERLAY: false,

    SMOOTHING: 0.03,
    DWELL_TIME: 1300,       
    DWELL_RADIUS: 50,       
    DWELL_SOFT_ABORT_MS: 300,
    EMIT_INTERVAL: 16,
    
    // Raw mode bypasses these filters, but keep defaults for easy fallback.
    ALPHA_STILL: 0.015,
    ALPHA_MOVE:  0.08,
    MOVE_THRESHOLD: 80,
    STABILITY_DEADZONE: 18,
    EDGE_BOOST: 0.0,

    CALIBRATION_STABILITY_WINDOW_MS: 220,
    CALIBRATION_STABILITY_PX: 22,
    CALIBRATION_MAX_WAIT_MS: 700,
    CALIBRATION_SAMPLES_PER_POINT: 2,
    CALIBRATION_SAMPLE_GAP_MS: 80,
    
    CLICK_COOLDOWN: 1000,   // ms before another dwell-click is allowed
    
    RADIAL_MENU_DWELL: 800,        // ms to select a radial menu slice
    RADIAL_MENU_DEADZONE: 52,
    WEBGAZER_RETRY_MS: 2500,

    BAR_MENU_OPEN_MS: 700,
    BAR_MENU_SELECT_MS: 3000,
    BAR_MENU_POST_FREEZE_MS: 1000,
    BAR_MENU_PAGE_SIZE: 8,
    BAR_MENU_STALE_MS: 2200,
    BAR_MENU_MAX_ITEMS: 40,
    BAR_MENU_DEADZONE: 60,
    BAR_MENU_ANGLE_SMOOTHING: 0.35,

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
    webgazerStarted: false,
    webgazerStarting: false,
    pendingCalibrationDone: false,
    webgazerRetryTimer: null,
    webgazerRetryCount: 0,
    
    radialMenu: { active: false, x: 0, y: 0, hoverSlice: null, dwellStart: null, targetX: 0, targetY: 0, openedAt: 0 },
    barContext: { active: false, barType: '', barLabel: '', items: [], pageSize: CONFIG.BAR_MENU_PAGE_SIZE, ts: 0 },
    barMenu: { active: false, x: 0, y: 0, hoverSlice: null, dwellStart: null, openedAt: 0, items: [], page: 0, pages: 1, barLabel: '', barType: '', hoverStart: null, freezeUntil: 0, angle: null },
    gazeHistory: [],
};

const RADIAL_MENU_ACTIONS = ['Scroll', 'Cancel', 'Right', 'Double', 'Left'];


function getScreenTransform() {
    const dpr = window.devicePixelRatio || 1;
    const rawX = typeof window.screenX === 'number' ? window.screenX : (window.screenLeft || 0);
    const rawY = typeof window.screenY === 'number' ? window.screenY : (window.screenTop || 0);
    // Ignore offscreen placement (e.g., -32000, -32000) so coords stay stable.
    const useOffset = rawX > -1000 && rawY > -1000;
    return {
        dpr,
        offsetX: useOffset ? rawX : 0,
        offsetY: useOffset ? rawY : 0,
    };
}


function isGazeStableWindow() {
    const samples = state.gazeHistory;
    if (samples.length < 3) return false;
    let sumX = 0;
    let sumY = 0;
    for (const s of samples) {
        sumX += s.x;
        sumY += s.y;
    }
    const meanX = sumX / samples.length;
    const meanY = sumY / samples.length;
    let maxDist = 0;
    for (const s of samples) {
        const dx = s.x - meanX;
        const dy = s.y - meanY;
        const d = Math.sqrt((dx * dx) + (dy * dy));
        if (d > maxDist) maxDist = d;
    }
    return maxDist <= CONFIG.CALIBRATION_STABILITY_PX;
}


function recordCalibrationSamples(cx, cy) {
    const count = Math.max(1, CONFIG.CALIBRATION_SAMPLES_PER_POINT || 1);
    const gapMs = Math.max(0, CONFIG.CALIBRATION_SAMPLE_GAP_MS || 0);
    for (let i = 0; i < count; i += 1) {
        setTimeout(() => {
            try {
                webgazer.recordScreenPosition(cx, cy, 'click');
            } catch (err) {
                if (shouldIgnoreWebGazerDomError(err)) {
                    debugLog('calibration_point_ignored_dom_error', {
                        x: Math.round(cx),
                        y: Math.round(cy),
                        message: String(err && err.message ? err.message : err),
                    });
                    return;
                }
                debugLog('calibration_point_error', {
                    x: Math.round(cx),
                    y: Math.round(cy),
                    message: String(err && err.message ? err.message : err),
                });
            }
        }, i * gapMs);
    }
}


function scheduleCalibrationSample(x, y) {
    const { dpr, offsetX, offsetY } = getScreenTransform();
    const cx = (x - offsetX) / dpr;
    const cy = (y - offsetY) / dpr;
    const start = Date.now();

    const attempt = () => {
        const waitedMs = Date.now() - start;
        if (isBlinking()) {
            if (waitedMs >= CONFIG.CALIBRATION_MAX_WAIT_MS) {
                recordCalibrationSamples(cx, cy);
                return;
            }
            setTimeout(attempt, 40);
            return;
        }

        if (isGazeStableWindow() || waitedMs >= CONFIG.CALIBRATION_MAX_WAIT_MS) {
            recordCalibrationSamples(cx, cy);
            return;
        }

        setTimeout(attempt, 40);
    };

    attempt();
}


function activateGazeControl() {
    console.log('[calib] calibration complete — gaze streaming active');
    state.isReady = true;
    debugLog('calibration_done');
    updateStatus('Gaze control active');

    // Save only when persistence mode is explicitly enabled.
    if (CONFIG.PERSIST_MODEL_ACROSS_SESSIONS && webgazer.saveDataAcrossSessions) {
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
}


function shouldIgnoreWebGazerDomError(err) {
    const msg = String((err && err.message) || err || '');
    const m = msg.toLowerCase();
    if (!m.includes('removechild')) return false;
    return (
        m.includes('notfounderror') ||
        m.includes('failed to execute') ||
        m.includes('parameter 1 is not of type')
    );
}


async function probeCameraAccess() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        return { ok: false, reason: 'getUserMedia_unavailable' };
    }

    const constraintsPrimary = {
        video: {
            facingMode: 'user',
            width: { ideal: 640 },
            height: { ideal: 480 },
        },
        audio: false,
    };

    const constraintsFallback = { video: true, audio: false };

    const tryProbe = async (constraints, tag) => {
        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        stream.getTracks().forEach((t) => t.stop());
        return { ok: true, tag };
    };

    try {
        return await tryProbe(constraintsPrimary, 'primary');
    } catch (e1) {
        try {
            return await tryProbe(constraintsFallback, 'fallback');
        } catch (e2) {
            const err = e2 || e1;
            return {
                ok: false,
                reason: (err && err.message) ? err.message : String(err),
            };
        }
    }
}


function hardResetWebGazerCameraState() {
    try {
        if (webgazer.end) {
            webgazer.end();
        }
    } catch (_) {
        // Ignore reset failures; retries will still proceed.
    }

    try {
        const video = document.getElementById('webgazerVideoFeed');
        if (video && video.srcObject && video.srcObject.getTracks) {
            video.srcObject.getTracks().forEach((t) => t.stop());
            video.srcObject = null;
        }
    } catch (_) {
        // Best-effort cleanup only.
    }
}


function scheduleWebGazerRetry(reason) {
    if (state.webgazerStarted || state.webgazerStarting || state.webgazerRetryTimer) {
        return;
    }

    state.webgazerRetryCount += 1;
    const delayMs = CONFIG.WEBGAZER_RETRY_MS;
    console.warn(`[webgazer] retrying in ${delayMs}ms (attempt ${state.webgazerRetryCount}) — reason: ${reason}`);
    debugLog('webgazer_retry_scheduled', {
        attempt: state.webgazerRetryCount,
        delayMs,
        reason: String(reason || 'unknown'),
    });
    updateStatus(`Camera unavailable — retrying (${state.webgazerRetryCount}) ...`);

    state.webgazerRetryTimer = setTimeout(() => {
        state.webgazerRetryTimer = null;
        startWebGazerRuntime();
    }, delayMs);
}


async function startWebGazerRuntime() {
    if (state.webgazerStarted || state.webgazerStarting) {
        return;
    }

    state.webgazerStarting = true;

    // Force explicit camera constraints so WebGazer does not reuse stale settings.
    webgazer.params.camConstraints = {
        video: {
            facingMode: 'user',
            width: { ideal: 640 },
            height: { ideal: 480 },
        },
        audio: false,
    };

    const probe = await probeCameraAccess();
    if (!probe.ok) {
        state.webgazerStarting = false;
        state.webgazerStarted = false;
        hardResetWebGazerCameraState();
        debugLog('webgazer_camera_probe_failed', { reason: probe.reason || 'unknown' });
        scheduleWebGazerRetry(probe.reason || 'camera_probe_failed');
        return;
    }

    debugLog('webgazer_camera_probe_ok', { mode: probe.tag || 'unknown' });

    webgazer
        .setGazeListener((data) => {
            if (!data) return;
            onGaze(data.x, data.y);
        })
        .showVideoPreview(CONFIG.SHOW_WEBGAZER_DEBUG_OVERLAY)
        .showPredictionPoints(CONFIG.SHOW_WEBGAZER_DEBUG_OVERLAY)
        .applyKalmanFilter(!CONFIG.RAW_DIRECT_MODE)
        .begin()
        .then(() => {
            state.webgazerStarting = false;
            state.webgazerStarted = true;
            state.webgazerRetryCount = 0;
            console.log('[webgazer] started — notifying server');
            debugLog('webgazer_started', {
                kalman: !CONFIG.RAW_DIRECT_MODE,
                rawMode: CONFIG.RAW_DIRECT_MODE,
            });
            updateStatus('WebGazer ready — awaiting calibration ...');
            state.socket.emit('webgazer_ready', {});

            if (state.pendingCalibrationDone) {
                state.pendingCalibrationDone = false;
                activateGazeControl();
            }
        })
        .catch(err => {
            state.webgazerStarting = false;
            state.webgazerStarted = false;
            const message = (err && err.message) ? err.message : String(err);
            hardResetWebGazerCameraState();
            console.error('[webgazer] FAILED:', err);
            debugLog('webgazer_start_failed', { message });
            scheduleWebGazerRetry(message);
        });
}


function debugLog(event, details = {}) {
    try {
        if (!state.socket || !state.socket.connected) return;
        state.socket.emit('debug_log', {
            source: 'web_app',
            event,
            details,
            timestamp: Date.now(),
        });
    } catch (e) {
        // Never fail gaze runtime because debug telemetry failed.
    }
}


// ── Socket.IO ──────────────────────────────────────────────────────────────────
function initSocket() {
    state.socket = io(CONFIG.SERVER_URL, { transports: ['websocket'] });

    state.socket.on('connect', () => {
        console.log('[socket] connected — registering as browser');
        state.socket.emit('register', { type: 'browser' });
        debugLog('socket_connect', { rawMode: CONFIG.RAW_DIRECT_MODE });
        updateStatus('Waiting for native calibration ...');
    });

    state.socket.on('disconnect', () => {
        console.warn('[socket] disconnected');
        debugLog('socket_disconnect');
        updateStatus('Server disconnected');
    });

    // ── Calibration relay from server ─────────────────────────────────────────
    state.socket.on('native_calibration_point', (data) => {
        const { x, y } = data;

        // Any new calibration point means we are in an active calibration phase.
        if (state.isReady || state.radialMenu.active || state.barMenu.active) {
            state.isReady = false;
            state.dwellStart = null;
            state.dwellExitTime = null;
            updateDwellRing(null, null, 0);
            if (state.radialMenu.active) {
                state.radialMenu.active = false;
                state.socket.emit('radial_menu_state', { active: false, menu_type: 'default' });
            }
            if (state.barMenu.active) {
                closeBarMenu('calibration');
            }
        }

        // Guard against blink frames corrupting calibration data
        if (isBlinking()) {
            console.log(`[calib] blink detected — skipping sample at (${x}, ${y})`);
            return;
        }
        scheduleCalibrationSample(x, y);
        debugLog('calibration_point', { x: Math.round(x), y: Math.round(y) });
        updateStatus(`Calibrating ... (${x}, ${y})`);
    });

    state.socket.on('native_calibration_done', () => {
        if (!state.webgazerStarted) {
            state.pendingCalibrationDone = true;
            debugLog('calibration_done_deferred', { reason: 'webgazer_not_started' });
            return;
        }

        activateGazeControl();
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
        if (CONFIG.RAW_DIRECT_MODE) return;
        state.poseOffsetX = data.dx || 0;
        state.poseOffsetY = data.dy || 0;
    });

    state.socket.on('bar_context', (data) => {
        if (!data || typeof data !== 'object') return;
        const items = Array.isArray(data.items) ? data.items : [];

        // Once menu is open, ignore inactive context refreshes so selection stays stable.
        if (state.barMenu.active && !data.active) {
            return;
        }

        state.barContext.active = !!data.active;
        state.barContext.barType = data.bar_type || '';
        state.barContext.barLabel = data.bar_label || '';
        state.barContext.pageSize = data.page_size || CONFIG.BAR_MENU_PAGE_SIZE;
        state.barContext.items = items.slice(0, CONFIG.BAR_MENU_MAX_ITEMS);
        state.barContext.ts = Date.now();

        if (state.barMenu.active) {
            syncBarMenuItems();
        }
    });

    // --- Implicit Calibration (Self-Learning) ---
    state.socket.on('click_learned', (data) => {
        if (!CONFIG.ENABLE_IMPLICIT_LEARNING) return;
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

    if (webgazer.saveDataAcrossSessions) {
        webgazer.saveDataAcrossSessions(CONFIG.PERSIST_MODEL_ACROSS_SESSIONS);
    }

    if (CONFIG.RAW_DIRECT_MODE && webgazer.clearData) {
        webgazer.clearData();
        console.log('[model] cleared persisted calibration for clean raw session');
            debugLog('model_cleared_for_raw_mode');
    }
    
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

    console.log('[webgazer] initialising ...');
    updateStatus('WebGazer initialising ...');
    startWebGazerRuntime();
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
    if (!isFinite(rawX) || !isFinite(rawY)) return;

    const now = Date.now();
    state.gazeHistory.push({ x: rawX, y: rawY, ts: now });
    const cutoff = now - CONFIG.CALIBRATION_STABILITY_WINDOW_MS;
    while (state.gazeHistory.length && state.gazeHistory[0].ts < cutoff) {
        state.gazeHistory.shift();
    }

    if (!state.isReady || state.isPaused) return;

    // Fully freeze normal gaze/cursor flow while bar menu is open.
    // Only directional selection logic is allowed during this mode.
    if (state.barMenu.active) {
        handleBarMenuGaze(rawX, rawY);
        return;
    }

    if (!state.barMenu.active && isBarMenuFrozen()) {
        return;
    }

    let x = rawX;
    let y = rawY;

    if (!CONFIG.RAW_DIRECT_MODE) {
        // --- Stability Deadzone ---
        const sdx = rawX - state.smoothed.x;
        const sdy = rawY - state.smoothed.y;
        const dist = Math.sqrt(sdx * sdx + sdy * sdy);
        if (dist < CONFIG.STABILITY_DEADZONE) {
            return;
        }

        // --- Dynamic Smoothing (Adaptive EMA) ---
        let a = CONFIG.ALPHA_STILL;
        if (dist > CONFIG.MOVE_THRESHOLD) {
            a = CONFIG.ALPHA_MOVE;
        }

        state.smoothed.x = a * rawX + (1 - a) * state.smoothed.x;
        state.smoothed.y = a * rawY + (1 - a) * state.smoothed.y;

        // Apply head pose correction, then center-bias correction.
        const poseX = state.smoothed.x - (state.poseOffsetX || 0);
        const poseY = state.smoothed.y - (state.poseOffsetY || 0);
        const corrected = applyCenterBiasCorrection(poseX, poseY);
        x = corrected.x;
        y = corrected.y;
    }

    state.smoothed.x = x;
    state.smoothed.y = y;

    if (state.radialMenu.active) {
        handleRadialMenuGaze(x, y);
    } else if (isBarContextActive()) {
        handleBarHover(x, y);
    } else {
        handleDwell(x, y);
    }

    // Throttle emissions to ~30 fps
    const emitNow = Date.now();
    if (!state.barMenu.active && !isBarMenuFrozen() && emitNow - state.lastEmitTime >= CONFIG.EMIT_INTERVAL) {
        // Convert CSS pixels to physical pixels before sending to the server.
        const { dpr, offsetX, offsetY } = getScreenTransform();
        const outX = (x * dpr) + offsetX;
        const outY = (y * dpr) + offsetY;
        state.socket.emit('move_mouse', {
            x: outX,
            y: outY,
            screen_left: window.screenX || 0,
            screen_top: window.screenY || 0,
            device_pixel_ratio: window.devicePixelRatio || 1,
            timestamp: emitNow,
        });
        state.lastEmitTime = emitNow;
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
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;
    state.radialMenu.active = true;
    state.radialMenu.x = cx;
    state.radialMenu.y = cy;
    state.radialMenu.targetX = x;
    state.radialMenu.targetY = y;
    state.radialMenu.hoverSlice = null;
    state.radialMenu.dwellStart = null;
    state.radialMenu.openedAt = Date.now();
    
    state.socket.emit('radial_menu_state', {
        active: true,
        x: cx,
        y: cy,
        slice: null,
        progress: 0,
        target_x: x,
        target_y: y,
        menu_type: 'default',
    });
    console.log('[radial] menu opened at', cx, cy, 'target', x, y);
    debugLog('radial_opened', { x: Math.round(cx), y: Math.round(cy), target_x: Math.round(x), target_y: Math.round(y) });
    updateDwellRing(null, null, 0);
}

function closeRadialMenu(triggerActionName = null) {
    state.radialMenu.active = false;
    state.radialMenu.hoverSlice = null;
    state.radialMenu.openedAt = 0;
    state.socket.emit('radial_menu_state', { active: false, menu_type: 'default' });
    
    // Reset regular dwell to prevent immediate re-triggering
    state.dwellStart = null;
    state.dwellAnchor = { x: state.smoothed.x, y: state.smoothed.y };
    state.lastClickTime = Date.now();
    
    if (!triggerActionName || triggerActionName === 'Cancel') {
        console.log('[radial] menu cancelled');
        debugLog('radial_cancelled');
    }
}

function fireRadialAction(actionName) {
    if (!actionName || actionName === 'Cancel') return;
    let type = 'left';
    if (actionName === 'Right') type = 'right';
    if (actionName === 'Double') type = 'double';

    if (actionName === 'Scroll') {
        state.socket.emit('trigger_scroll', { x: state.radialMenu.x, y: state.radialMenu.y, direction: 'down', amount: 3 });
        console.log('[radial] scroll triggered');
        debugLog('radial_action', { action: 'Scroll' });
    } else {
        state.socket.emit('trigger_action', { x: state.radialMenu.x, y: state.radialMenu.y, type: type });
        console.log(`[radial] ${type} click triggered`);
        debugLog('radial_action', { action: actionName, type: type });
    }
}

function handleRadialMenuGaze(x, y) {
    const rm = state.radialMenu;
    const dx = x - rm.x;
    const dy = y - rm.y;
    const dist = Math.sqrt(dx * dx + dy * dy);

    // Deadzone check
    if (dist < CONFIG.RADIAL_MENU_DEADZONE) {
        updateRadialMenuHover(null);
        return;
    }

    // Determine slice (0 to N-1)
    // angle from top (-pi/2) clockwise
    let angle = Math.atan2(dy, dx); // -pi to pi
    angle += Math.PI / 2;
    if (angle < 0) angle += 2 * Math.PI; // 0 to 2pi
    
    const sliceAngle = (2 * Math.PI) / RADIAL_MENU_ACTIONS.length;
    angle = (angle + (sliceAngle / 2)) % (2 * Math.PI);
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
            active: true,
            x: rm.x,
            y: rm.y,
            slice: rm.hoverSlice,
            progress: 0,
            target_x: rm.targetX,
            target_y: rm.targetY,
            menu_type: 'default',
        });
    } else if (sliceIndex !== null) {
        const elapsed = now - rm.dwellStart;
        const progress = Math.min(elapsed / CONFIG.RADIAL_MENU_DWELL, 1.0);
        
        state.socket.emit('radial_menu_state', {
            active: true,
            x: rm.x,
            y: rm.y,
            slice: rm.hoverSlice,
            progress: progress,
            target_x: rm.targetX,
            target_y: rm.targetY,
            menu_type: 'default',
        });
        
        if (progress >= 1.0) {
            const action = RADIAL_MENU_ACTIONS[sliceIndex];
            if (action === 'Cancel') {
                closeRadialMenu('Cancel');
                return;
            }
            fireRadialAction(action);
            rm.hoverSlice = null;
            rm.dwellStart = null;
            state.socket.emit('radial_menu_state', {
                active: true,
                x: rm.x,
                y: rm.y,
                slice: null,
                progress: 0,
                target_x: rm.targetX,
                target_y: rm.targetY,
                menu_type: 'default',
            });
        }
    }
}


// ── Bar Menu Logic ───────────────────────────────────────────────────────────
function isBarContextActive() {
    if (!state.barContext.active) return false;
    const age = Date.now() - (state.barContext.ts || 0);
    return age >= 0 && age <= CONFIG.BAR_MENU_STALE_MS;
}

function isBarMenuFrozen() {
    return Date.now() < (state.barMenu.freezeUntil || 0);
}

function normalizeAngle(angle) {
    let a = angle;
    while (a <= -Math.PI) a += Math.PI * 2;
    while (a > Math.PI) a -= Math.PI * 2;
    return a;
}

function smoothAngle(prev, next, factor) {
    if (prev === null || prev === undefined) return next;
    const delta = normalizeAngle(next - prev);
    return prev + (delta * factor);
}

function syncBarMenuItems() {
    const items = Array.isArray(state.barContext.items) ? state.barContext.items : [];
    state.barMenu.items = items.slice(0, CONFIG.BAR_MENU_MAX_ITEMS);
    state.barMenu.barLabel = state.barContext.barLabel || state.barMenu.barLabel;
    state.barMenu.barType = state.barContext.barType || state.barMenu.barType;
    const totalPages = Math.max(1, Math.ceil(state.barMenu.items.length / (state.barContext.pageSize || CONFIG.BAR_MENU_PAGE_SIZE)));
    state.barMenu.pages = totalPages;
    if (state.barMenu.page >= totalPages) {
        state.barMenu.page = totalPages - 1;
    }
    if (state.barMenu.active) {
        emitBarMenuState(state.barMenu.hoverSlice, 0);
    }
}

function openBarMenu() {
    if (!isBarContextActive()) return;
    if (!state.barContext.items || !state.barContext.items.length) return;
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;
    state.barMenu.active = true;
    state.barMenu.x = cx;
    state.barMenu.y = cy;
    state.barMenu.hoverSlice = null;
    state.barMenu.dwellStart = null;
    state.barMenu.openedAt = Date.now();
    state.barMenu.page = 0;
    state.barMenu.angle = null;
    state.barMenu.freezeUntil = 0;
    syncBarMenuItems();

    if (state.radialMenu.active) {
        state.radialMenu.active = false;
        state.socket.emit('radial_menu_state', { active: false, menu_type: 'default' });
    }

    emitBarMenuState(null, 0);
    updateDwellRing(null, null, 0);
}

function closeBarMenu(reason = null, freezeMs = 0) {
    state.barMenu.active = false;
    state.barMenu.hoverSlice = null;
    state.barMenu.dwellStart = null;
    state.barMenu.openedAt = 0;
    state.barMenu.hoverStart = null;
    state.barMenu.angle = null;
    if (freezeMs > 0) {
        state.barMenu.freezeUntil = Date.now() + freezeMs;
    }
    state.socket.emit('radial_menu_state', { active: false, menu_type: 'bar' });

    // Reset regular dwell to prevent immediate re-triggering
    state.dwellStart = null;
    state.dwellAnchor = { x: state.smoothed.x, y: state.smoothed.y };
    state.lastClickTime = Date.now();

    if (!reason || reason === 'cancel') {
        console.log('[bar] menu closed');
        debugLog('bar_menu_closed', { reason: reason || 'cancel' });
    }
}

function buildBarMenuEntries() {
    const pageSize = state.barContext.pageSize || CONFIG.BAR_MENU_PAGE_SIZE;
    const totalPages = Math.max(1, Math.ceil(state.barMenu.items.length / pageSize));
    const page = Math.max(0, Math.min(state.barMenu.page, totalPages - 1));
    const start = page * pageSize;
    const sliceItems = state.barMenu.items.slice(start, start + pageSize);
    const entries = sliceItems.map((item, idx) => ({
        type: 'item',
        id: item.id,
        label: item.label || `Item ${start + idx + 1}`,
    }));

    if (totalPages > 1) {
        entries.push({ type: 'action', action: 'prev', label: 'Prev' });
        entries.push({ type: 'action', action: 'next', label: 'Next' });
    }

    entries.push({ type: 'action', action: 'cancel', label: 'Cancel' });
    return { entries, totalPages, page };
}

function emitBarMenuState(sliceIndex, progress) {
    if (!state.barMenu.active) return;
    const { entries, totalPages, page } = buildBarMenuEntries();
    const labels = entries.map((e) => e.label);
    state.socket.emit('radial_menu_state', {
        active: true,
        x: state.barMenu.x,
        y: state.barMenu.y,
        slice: sliceIndex,
        progress: progress || 0,
        menu_type: 'bar',
        items: labels,
        title: state.barMenu.barLabel || state.barMenu.barType || 'Bar',
        page: page + 1,
        pages: totalPages,
    });
}

function handleBarHover() {
    if (!isBarContextActive()) {
        state.barMenu.hoverStart = null;
        return;
    }

    const now = Date.now();
    if (!state.barMenu.hoverStart) {
        state.barMenu.hoverStart = now;
    }

    const elapsed = now - state.barMenu.hoverStart;
    if (elapsed >= CONFIG.BAR_MENU_OPEN_MS) {
        state.barMenu.hoverStart = null;
        openBarMenu();
    }
}

function handleBarMenuGaze(rawX, rawY) {
    const bm = state.barMenu;
    const dx = rawX - bm.x;
    const dy = rawY - bm.y;
    const dist = Math.sqrt(dx * dx + dy * dy);

    if (dist < CONFIG.BAR_MENU_DEADZONE) {
        updateBarMenuHover(null);
        return;
    }

    const { entries } = buildBarMenuEntries();
    if (!entries.length) {
        updateBarMenuHover(null);
        return;
    }

    let angle = Math.atan2(dy, dx);
    angle = smoothAngle(bm.angle, angle, CONFIG.BAR_MENU_ANGLE_SMOOTHING);
    bm.angle = angle;

    angle += Math.PI / 2;
    if (angle < 0) angle += 2 * Math.PI;

    const sliceAngle = (2 * Math.PI) / entries.length;
    angle = (angle + (sliceAngle / 2)) % (2 * Math.PI);
    const sliceIndex = Math.floor(angle / sliceAngle);
    updateBarMenuHover(sliceIndex);
}

function updateBarMenuHover(sliceIndex) {
    const bm = state.barMenu;
    const now = Date.now();
    const { entries, totalPages } = buildBarMenuEntries();

    if (sliceIndex !== bm.hoverSlice) {
        bm.hoverSlice = sliceIndex;
        bm.dwellStart = sliceIndex !== null ? now : null;
        emitBarMenuState(sliceIndex, 0);
    } else if (sliceIndex !== null) {
        const elapsed = now - bm.dwellStart;
        const progress = Math.min(elapsed / CONFIG.BAR_MENU_SELECT_MS, 1.0);
        emitBarMenuState(sliceIndex, progress);

        if (progress >= 1.0) {
            const entry = entries[sliceIndex];
            if (entry) {
                fireBarMenuAction(entry, totalPages);
            }
            bm.hoverSlice = null;
            bm.dwellStart = null;
            emitBarMenuState(null, 0);
        }
    }
}

function fireBarMenuAction(entry, totalPages) {
    if (!entry) return;
    if (entry.type === 'action') {
        if (entry.action === 'cancel') {
            closeBarMenu('cancel');
            return;
        }
        if (entry.action === 'prev') {
            state.barMenu.page = (state.barMenu.page - 1 + totalPages) % totalPages;
            state.barMenu.hoverSlice = null;
            state.barMenu.dwellStart = null;
            emitBarMenuState(null, 0);
            return;
        }
        if (entry.action === 'next') {
            state.barMenu.page = (state.barMenu.page + 1) % totalPages;
            state.barMenu.hoverSlice = null;
            state.barMenu.dwellStart = null;
            emitBarMenuState(null, 0);
            return;
        }
        return;
    }

    if (entry.type === 'item' && entry.id) {
        state.socket.emit('bar_menu_select', { id: entry.id });
        debugLog('bar_menu_select', { id: entry.id, label: entry.label });
        closeBarMenu('select', CONFIG.BAR_MENU_POST_FREEZE_MS);
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
    // WebGazer occasionally throws a benign DOM NotFoundError while removing
    // internal calibration nodes. Suppress only this known signature.
    window.addEventListener('error', (event) => {
        const err = event && (event.error || event.message);
        if (shouldIgnoreWebGazerDomError(err)) {
            debugLog('ignored_global_dom_error', {
                message: String((err && err.message) || err),
            });
            event.preventDefault();
        }
    });

    initSocket();
    initWebGazer();
};

window.onbeforeunload = () => {
    webgazer.end();
};
