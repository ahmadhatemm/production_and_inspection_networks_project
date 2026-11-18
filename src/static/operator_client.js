/**
 * MCT 431 Industrial Networks - Milestone 1
 * Operator Communication Layer (Person 3)
 * 
 * Implements:
 *  - HTTP Command Interface with unique cmd_id, timeouts & bounded retries (Req VII, VIII)
 *  - Strict 3-Event Lifecycle Tracking: (1) Delivered, (2) Accepted, (3) Completed
 *  - WebSocket Client for live telemetry, status streaming, and heartbeat/stale detection (Req IX)
 *  - Error handling for MALFORMED (400), INVALID_STATE (409), and DEVICE_NOT_READY (503)
 */

class OperatorClient {
    /**
     * @param {Object} options
     * @param {string} options.host - Gateway IP/hostname (default: window.location.hostname || 'localhost')
     * @param {number} options.port - Gateway port (default: 8081)
     * @param {number} options.timeoutMs - Timeout for HTTP ACK in ms (default: 500)
     * @param {number} options.maxRetries - Number of retries on timeout/failure (default: 3)
     * @param {number} options.heartbeatTimeoutMs - Threshold for stale data detection (default: 3000)
     */
    constructor(options = {}) {
        this.host = options.host || (typeof window !== 'undefined' && window.location.hostname ? window.location.hostname : 'localhost');
        this.port = options.port || 8081;
        this.timeoutMs = options.timeoutMs || 500;
        this.maxRetries = options.maxRetries !== undefined ? options.maxRetries : 3;
        this.heartbeatTimeoutMs = options.heartbeatTimeoutMs || 3000;

        this.baseUrl = `http://${this.host}:${this.port}`;
        this.wsUrl = `ws://${this.host}:${this.port}/ws`;

        // 32-bit unsigned command ID generator (1 to 2^32 - 1)
        this._nextCmdId = Math.floor(Math.random() * 1000) + 1;

        // Active command tracker: Map<cmd_id, CommandRecord>
        // CommandRecord: { cmd_id, cmd, body, stage: 'created'|'delivered'|'accepted'|'completed'|'rejected'|'failed', ... }
        this.commands = new Map();

        // WebSocket state
        this.ws = null;
        this.wsConnected = false;
        this.lastHeartbeatTs = 0;
        this.heartbeatTimer = null;
        this.reconnectTimer = null;
        this.shouldReconnect = true;

        // Latest cached gateway status
        this.latestStatus = null;

        // Event callbacks registry: Map<eventName, Set<Function>>
        this.listeners = new Map();

        // Start heartbeat watchdog
        this._startHeartbeatWatchdog();
    }

    // ==========================================
    // Event System
    // ==========================================

    on(event, callback) {
        if (!this.listeners.has(event)) {
            this.listeners.set(event, new Set());
        }
        this.listeners.get(event).add(callback);
        return () => this.off(event, callback);
    }

    off(event, callback) {
        if (this.listeners.has(event)) {
            this.listeners.get(event).delete(callback);
        }
    }

    emit(event, ...args) {
        if (this.listeners.has(event)) {
            for (const cb of this.listeners.get(event)) {
                try {
                    cb(...args);
                } catch (err) {
                    console.error(`[OperatorClient] Error in listener for event '${event}':`, err);
                }
            }
        }
    }

    // ==========================================
    // Command ID & 3-Event Lifecycle Management
    // ==========================================

    /**
     * Generates a unique 32-bit unsigned integer ID.
     */
    generateCmdId() {
        const id = this._nextCmdId;
        this._nextCmdId = (this._nextCmdId + 1) >>> 0;
        if (this._nextCmdId === 0) this._nextCmdId = 1;
        return id;
    }

    /**
     * Internal command execution with retries and 3-event lifecycle
     * @param {string} endpoint - Relative path (e.g., '/api/v1/batch/start')
     * @param {string} cmdName - Friendly name (e.g., 'batch/start')
     * @param {Object} payload - JSON body arguments
     * @param {number} [customCmdId] - Optional existing cmd_id (for explicit retry)
     * @returns {Promise<Object>} Resolves on 'accepted' or 'completed' depending on call
     */
    async sendCommand(endpoint, cmdName, payload = {}, customCmdId = null) {
        const cmd_id = customCmdId !== null ? customCmdId : this.generateCmdId();
        const requestBody = { ...payload, cmd_id };

        const record = {
            cmd_id,
            cmd: cmdName,
            endpoint,
            payload: requestBody,
            stage: 'pending',
            retriesLeft: this.maxRetries,
            attempts: 0,
            createdAt: Date.now(),
            deliveredAt: null,
            acceptedAt: null,
            completedAt: null,
            error: null,
            duplicate: false,
            // Promise resolvers for lifecycle stages
            deferredAccepted: null,
            deferredCompleted: null
        };

        const acceptedPromise = new Promise((resolve, reject) => {
            record.deferredAccepted = { resolve, reject };
        });

        const completedPromise = new Promise((resolve, reject) => {
            record.deferredCompleted = { resolve, reject };
        });

        record.promises = {
            accepted: acceptedPromise,
            completed: completedPromise
        };

        this.commands.set(cmd_id, record);
        this.emit('cmd_created', { cmd_id, cmd: cmdName, payload: requestBody });

        // Trigger network execution loop
        this._executeWithRetry(record);

        return record;
    }

    async _executeWithRetry(record) {
        const url = `${this.baseUrl}${record.endpoint}`;

        while (record.attempts <= this.maxRetries) {
            record.attempts++;
            const attemptNum = record.attempts;
            
            try {
                this.emit('log', {
                    level: 'info',
                    message: `Sending ${record.cmd} (cmd_id: ${record.cmd_id}, attempt: ${attemptNum}/${this.maxRetries + 1})`
                });

                // AbortController for strict timeout handling (Ack timeout = 500 ms)
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), this.timeoutMs);

                const response = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    },
                    body: JSON.stringify(record.payload),
                    signal: controller.signal
                });

                clearTimeout(timeoutId);

                // --- STAGE 1: DELIVERED ---
                // Bytes arrived at Gateway and connection was answered
                record.deliveredAt = Date.now();
                record.stage = 'delivered';
                this.emit('cmd_delivered', {
                    cmd_id: record.cmd_id,
                    cmd: record.cmd,
                    attempt: attemptNum,
                    status: response.status
                });

                const data = await response.json();

                if (response.ok && data.ok) {
                    // --- STAGE 2: ACCEPTED ---
                    // Gateway parsed JSON, validated parameters & state, agreed to act
                    record.acceptedAt = Date.now();
                    record.stage = 'accepted';
                    record.duplicate = Boolean(data.duplicate);

                    this.emit('cmd_accepted', {
                        cmd_id: record.cmd_id,
                        cmd: record.cmd,
                        data,
                        duplicate: record.duplicate
                    });

                    if (record.deferredAccepted) {
                        record.deferredAccepted.resolve(data);
                    }

                    // Note: If this command is one that completes immediately at the gateway (e.g. mode, clear/fault),
                    // or if it completes downstream (ESP32 B config or batch completion),
                    // completion will be signaled via WebSocket cmd_event or batch_complete!
                    return;

                } else {
                    // Gateway explicitly rejected the command (HTTP 400, 409, 503)
                    record.stage = 'rejected';
                    const errorObj = data.error || { code: `HTTP_${response.status}`, message: data.message || 'Rejected' };
                    record.error = errorObj;

                    this.emit('cmd_rejected', {
                        cmd_id: record.cmd_id,
                        cmd: record.cmd,
                        status: response.status,
                        error: errorObj
                    });

                    if (record.deferredAccepted) {
                        record.deferredAccepted.reject(new Error(`${errorObj.code}: ${errorObj.message}`));
                    }
                    if (record.deferredCompleted) {
                        record.deferredCompleted.reject(new Error(`${errorObj.code}: ${errorObj.message}`));
                    }
                    return; // Rejections (e.g. 409 INVALID_STATE) are final and must not be retried!
                }

            } catch (err) {
                // Timeout or network error
                const isTimeout = err.name === 'AbortError';
                this.emit('log', {
                    level: 'warn',
                    message: `Attempt ${attemptNum} failed for cmd_id ${record.cmd_id}: ${isTimeout ? 'ACK Timeout (' + this.timeoutMs + 'ms)' : err.message}`
                });

                if (attemptNum <= this.maxRetries) {
                    // Bounded retry with SAME cmd_id and SAME payload
                    this.emit('cmd_retry', {
                        cmd_id: record.cmd_id,
                        cmd: record.cmd,
                        attempt: attemptNum,
                        nextAttempt: attemptNum + 1,
                        reason: isTimeout ? 'TIMEOUT' : 'NETWORK_ERROR'
                    });
                    // Short pause before retry (e.g. 50ms)
                    await new Promise(r => setTimeout(r, 50));
                } else {
                    // Retries exhausted
                    record.stage = 'failed';
                    record.error = { code: 'RETRIES_EXHAUSTED', message: `No ACK received after ${this.maxRetries + 1} attempts` };
                    
                    this.emit('cmd_failed', {
                        cmd_id: record.cmd_id,
                        cmd: record.cmd,
                        error: record.error
                    });

                    if (record.deferredAccepted) {
                        record.deferredAccepted.reject(new Error(record.error.message));
                    }
                    if (record.deferredCompleted) {
                        record.deferredCompleted.reject(new Error(record.error.message));
                    }
                }
            }
        }
    }

    // ==========================================
    // High-Level HTTP Command APIs
    // ==========================================

    /**
     * Start a production batch (POST /api/v1/batch/start)
     */
    async startBatch({ target_parts = 20, cycle_time_ms = 1000, inspect_time_ms = 500, cmd_id = null } = {}) {
        return this.sendCommand('/api/v1/batch/start', 'batch/start', {
            target_parts,
            cycle_time_ms,
            inspect_time_ms
        }, cmd_id);
    }

    /**
     * Pause a running batch (POST /api/v1/batch/pause)
     */
    async pauseBatch(cmd_id = null) {
        return this.sendCommand('/api/v1/batch/pause', 'batch/pause', {}, cmd_id);
    }

    /**
     * Resume a paused batch (POST /api/v1/batch/resume)
     */
    async resumeBatch(cmd_id = null) {
        return this.sendCommand('/api/v1/batch/resume', 'batch/resume', {}, cmd_id);
    }

    /**
     * Stop a running or paused batch and return to idle (POST /api/v1/batch/stop)
     */
    async stopBatch(cmd_id = null) {
        return this.sendCommand('/api/v1/batch/stop', 'batch/stop', {}, cmd_id);
    }

    /**
     * Set examiner's inspection rule parameters N and K (POST /api/v1/config/inspection)
     */
    async setInspectionConfig({ N = 5, K = 2, cmd_id = null } = {}) {
        return this.sendCommand('/api/v1/config/inspection', 'config/inspection', {
            N,
            K
        }, cmd_id);
    }

    /**
     * Clear fault and return to idle (POST /api/v1/fault/clear)
     */
    async clearFault(cmd_id = null) {
        return this.sendCommand('/api/v1/fault/clear', 'fault/clear', {}, cmd_id);
    }

    /**
     * Set operating mode (POST /api/v1/mode)
     */
    async setMode({ mode = 'production', cmd_id = null } = {}) {
        return this.sendCommand('/api/v1/mode', 'mode', { mode }, cmd_id);
    }

    /**
     * Get instantaneous status snapshot (GET /api/v1/status)
     */
    async getStatus() {
        const res = await fetch(`${this.baseUrl}/api/v1/status`);
        if (!res.ok) throw new Error(`HTTP ${res.status} fetching status`);
        const data = await res.json();
        this.latestStatus = data;
        return data;
    }

    /**
     * Get final batch report (GET /api/v1/batch/report)
     */
    async getBatchReport() {
        const res = await fetch(`${this.baseUrl}/api/v1/batch/report`);
        if (!res.ok) throw new Error(`HTTP ${res.status} fetching report`);
        return await res.json();
    }

    // ==========================================
    // WebSocket Client & Live Status Streaming
    // ==========================================

    connectWebSocket() {
        this.shouldReconnect = true;
        if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
            return;
        }

        try {
            this.emit('ws_connecting', { url: this.wsUrl });
            this.ws = new WebSocket(this.wsUrl);

            this.ws.onopen = () => {
                this.wsConnected = true;
                this.lastHeartbeatTs = Date.now();
                this.emit('ws_connected', { url: this.wsUrl });
                this.emit('log', { level: 'info', message: `Connected to WebSocket: ${this.wsUrl}` });
            };

            this.ws.onmessage = (event) => {
                this._handleWsMessage(event.data);
            };

            this.ws.onerror = (err) => {
                this.emit('ws_error', err);
            };

            this.ws.onclose = (event) => {
                this.wsConnected = false;
                this.emit('ws_disconnected', { code: event.code, reason: event.reason });
                this.emit('log', { level: 'warn', message: `WebSocket disconnected (code: ${event.code})` });

                if (this.shouldReconnect) {
                    this._scheduleReconnect();
                }
            };

        } catch (err) {
            this.emit('ws_error', err);
            if (this.shouldReconnect) {
                this._scheduleReconnect();
            }
        }
    }

    disconnectWebSocket() {
        this.shouldReconnect = false;
        if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.wsConnected = false;
    }

    _scheduleReconnect() {
        if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => {
            if (this.shouldReconnect) {
                this.emit('log', { level: 'info', message: 'Reconnecting WebSocket...' });
                this.connectWebSocket();
            }
        }, 1500);
    }

    _handleWsMessage(rawJson) {
        try {
            const msg = JSON.parse(rawJson);
            if (!msg || msg.v !== 1) return;

            // Any message received acts as alive evidence
            this.lastHeartbeatTs = Date.now();

            switch (msg.type) {
                case 'status':
                    this.latestStatus = msg;
                    this.emit('status', msg);
                    break;

                case 'cmd_event':
                    // --- STAGE 3: COMPLETED (or REJECTED/FAILED downstream) ---
                    this._handleCmdEvent(msg);
                    this.emit('cmd_event', msg);
                    break;

                case 'part_event':
                    this.emit('part_event', msg);
                    break;

                case 'batch_complete':
                    this.emit('batch_complete', msg);
                    break;

                case 'fault':
                    this.emit('fault', msg);
                    break;

                default:
                    this.emit('unknown_message', msg);
            }
        } catch (err) {
            this.emit('log', { level: 'error', message: `Failed to parse WS JSON: ${err.message}` });
        }
    }

    _handleCmdEvent(msg) {
        const { cmd_id, cmd, stage, reason } = msg;
        const record = this.commands.get(cmd_id);

        if (record) {
            record.stage = stage;
            if (stage === 'completed') {
                record.completedAt = msg.ts || Date.now();
                this.emit('cmd_completed', {
                    cmd_id,
                    cmd: record.cmd,
                    ts: record.completedAt
                });
                if (record.deferredCompleted) {
                    record.deferredCompleted.resolve(msg);
                }
            } else if (stage === 'rejected' || stage === 'failed') {
                record.error = { code: stage.toUpperCase(), message: reason || 'Command failed downstream' };
                this.emit('cmd_failed', {
                    cmd_id,
                    cmd: record.cmd,
                    stage,
                    reason
                });
                if (record.deferredCompleted) {
                    record.deferredCompleted.reject(new Error(record.error.message));
                }
            }
        }
    }

    // ==========================================
    // Heartbeat & Stale Data Watchdog (Req IX)
    // ==========================================

    _startHeartbeatWatchdog() {
        if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
        this.heartbeatTimer = setInterval(() => {
            if (!this.wsConnected) return;

            const timeSinceLast = Date.now() - this.lastHeartbeatTs;
            if (timeSinceLast > this.heartbeatTimeoutMs) {
                this.emit('heartbeat_timeout', {
                    timeSinceLastMs: timeSinceLast,
                    limitMs: this.heartbeatTimeoutMs,
                    message: `Gateway heartbeat lost (${timeSinceLast} ms > ${this.heartbeatTimeoutMs} ms limit)`
                });
                this.emit('log', {
                    level: 'error',
                    message: `[WATCHDOG] Stale telemetry: No message for ${timeSinceLast}ms`
                });
            }
        }, 1000);
    }
}

// Support both ES Module, CommonJS, and Browser global
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { OperatorClient };
} else if (typeof window !== 'undefined') {
    window.OperatorClient = OperatorClient;
}
