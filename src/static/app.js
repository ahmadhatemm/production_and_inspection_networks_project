// Line 01 — Production & Inspection Control HMI Controller
// Binds UI controls and telemetry views to OperatorClient

document.addEventListener('DOMContentLoaded', () => {
    // Connection & Header Badges
    const inputGwHost = document.getElementById('input-gw-host');
    const inputGwPort = document.getElementById('input-gw-port');
    const btnConnect = document.getElementById('btn-connect');

    const elModeBadge = document.getElementById('mode-badge');
    const elWsBadge = document.getElementById('ws-status-badge');
    const elHeartbeatBadge = document.getElementById('heartbeat-badge');

    // Alert Banners & Report Drawer
    const elWatchdogBanner = document.getElementById('watchdog-banner');
    const elWatchdogText = document.getElementById('watchdog-text');
    const elFaultBanner = document.getElementById('fault-banner');
    const elFaultCode = document.getElementById('fault-code');
    const elFaultDevice = document.getElementById('fault-device');
    const elFaultMessage = document.getElementById('fault-message');
    const btnBannerClearFault = document.getElementById('btn-banner-clear-fault');

    const elReportPanel = document.getElementById('report-panel');
    const elReportSubtitle = document.getElementById('report-subtitle');
    const btnExportCsv = document.getElementById('btn-export-csv');
    const btnCloseReport = document.getElementById('btn-close-report');
    const btnFetchReport = document.getElementById('btn-fetch-report');

    // KPI Cards
    const elStateLed = document.getElementById('state-led');
    const elGatewayState = document.getElementById('gateway-state');
    const elStateHint = document.getElementById('state-hint');
    const elProduced = document.getElementById('count-produced');
    const elQueued = document.getElementById('count-queued');
    const elInspected = document.getElementById('count-inspected');
    const elTarget = document.getElementById('count-target');
    const elBatchBar = document.getElementById('batch-bar');

    const elAccepted = document.getElementById('count-accepted');
    const elRejected = document.getElementById('count-rejected');
    const elActiveRuleLabel = document.getElementById('active-rule-label');
    const elConfirmedTimings = document.getElementById('confirmed-timings');

    const elQueueDepth = document.getElementById('queue-depth');
    const elQueueCapLabel = document.getElementById('queue-cap-label');
    const elQueueBar = document.getElementById('queue-bar');
    const elQueueBlocked = document.getElementById('queue-blocked');

    // Field Device Nodes (Station A & Station B)
    const elEspALed = document.getElementById('esp-a-led');
    const elEspAStatus = document.getElementById('esp-a-status');
    const elEspAHb = document.getElementById('esp-a-hb');
    const elEspAHbBar = document.getElementById('esp-a-hb-bar');

    const elEspBLed = document.getElementById('esp-b-led');
    const elEspBStatus = document.getElementById('esp-b-status');
    const elEspBHb = document.getElementById('esp-b-hb');
    const elEspBHbBar = document.getElementById('esp-b-hb-bar');

    // Forms & Action Buttons
    const elValidationMsg = document.getElementById('validation-msg');
    const inputTargetParts = document.getElementById('input-target-parts');
    const inputCycleTime = document.getElementById('input-cycle-time');
    const inputInspectTime = document.getElementById('input-inspect-time');
    const inputRuleN = document.getElementById('input-rule-n');
    const inputRuleK = document.getElementById('input-rule-k');
    const elRulePreviewChips = document.getElementById('rule-preview-chips');

    const btnStart = document.getElementById('btn-start');
    const btnPause = document.getElementById('btn-pause');
    const btnResume = document.getElementById('btn-resume');
    const btnStop = document.getElementById('btn-stop');
    const btnClearFault = document.getElementById('btn-clear-fault');
    const btnSetConfig = document.getElementById('btn-set-config');
    const btnModeProd = document.getElementById('btn-mode-prod');
    const btnModeBench = document.getElementById('btn-mode-bench');

    // Tables & Console
    const tableCmdsBody = document.querySelector('#table-cmds tbody');
    const tablePartsBody = document.querySelector('#table-parts tbody');
    const logBox = document.getElementById('log-box');

    // Auto-populate Gateway IP/Port from current browser URL
    inputGwHost.value = window.location.hostname || 'localhost';
    inputGwPort.value = window.location.port ? parseInt(window.location.port, 10) : 8080;

    let client = null;
    const activeCommandRows = new Map();
    const activePartRows = new Map();
    const partHistory = [];

    const STATE_META = {
        idle: { hint: 'Ready for configuration or batch start', led: 'led-gray' },
        running: { hint: 'Production & inspection active', led: 'led-green' },
        paused: { hint: 'Batch paused — inspection rule editable', led: 'led-amber' },
        fault: { hint: 'System halted — clear fault to return to idle', led: 'led-red' }
    };

    function log(msg, color = '#86efac') {
        const timeStr = new Date().toISOString().substring(11, 23);
        const div = document.createElement('div');
        div.style.color = color;
        div.textContent = `[${timeStr}] ${msg}`;
        logBox.appendChild(div);
        logBox.scrollTop = logBox.scrollHeight;
    }

    function showValidationError(msg) {
        if (!msg) {
            elValidationMsg.classList.add('hidden');
            elValidationMsg.textContent = '';
            return;
        }
        elValidationMsg.textContent = msg;
        elValidationMsg.classList.remove('hidden');
    }

    // Live deterministic preview: FAIL when (part_id + K) mod N == 0
    function updateRulePreview() {
        const N = parseInt(inputRuleN.value, 10);
        const K = parseInt(inputRuleK.value, 10);
        elRulePreviewChips.innerHTML = '';

        if (isNaN(N) || N < 1 || isNaN(K) || K < 0) {
            elRulePreviewChips.innerHTML = '<span style="color:#ef4444;">Invalid N (&ge;1) or K (&ge;0)</span>';
            return;
        }

        for (let partId = 1; partId <= 8; partId++) {
            const isFail = ((partId + K) % N) === 0;
            const chip = document.createElement('span');
            chip.className = isFail ? 'chip-fail' : 'chip-pass';
            chip.textContent = `#${partId}:${isFail ? 'FAIL' : 'PASS'}`;
            elRulePreviewChips.appendChild(chip);
        }
    }

    inputRuleN.addEventListener('input', updateRulePreview);
    inputRuleK.addEventListener('input', updateRulePreview);
    updateRulePreview();

    // --- 3-Stage Command Table Updater ---
    function addOrUpdateCmdRow(cmd_id, cmdName, stage, details = '') {
        let row = activeCommandRows.get(cmd_id);
        if (!row) {
            row = document.createElement('tr');
            row.innerHTML = `
                <td class="mono"><strong>#${cmd_id}</strong></td>
                <td class="mono"><code>${cmdName}</code></td>
                <td class="col-delivered"><span class="tag-stage">SENDING</span></td>
                <td class="col-accepted">-</td>
                <td class="col-completed">-</td>
                <td class="col-details">${details}</td>
            `;
            tableCmdsBody.prepend(row);
            activeCommandRows.set(cmd_id, row);
        }

        const tdDelivered = row.querySelector('.col-delivered');
        const tdAccepted = row.querySelector('.col-accepted');
        const tdCompleted = row.querySelector('.col-completed');
        const tdDetails = row.querySelector('.col-details');

        if (stage === 'delivered') {
            tdDelivered.innerHTML = `<span class="tag-delivered">DELIVERED</span>`;
        } else if (stage === 'accepted') {
            tdDelivered.innerHTML = `<span class="tag-delivered">DELIVERED</span>`;
            tdAccepted.innerHTML = `<span class="tag-accepted">ACCEPTED</span>`;
        } else if (stage === 'completed') {
            tdDelivered.innerHTML = `<span class="tag-delivered">DELIVERED</span>`;
            tdAccepted.innerHTML = `<span class="tag-accepted">ACCEPTED</span>`;
            tdCompleted.innerHTML = `<span class="tag-completed">COMPLETED</span>`;
        } else if (stage === 'rejected') {
            tdAccepted.innerHTML = `<span class="tag-rejected">REJECTED</span>`;
        } else if (stage === 'failed') {
            tdDelivered.innerHTML = `<span class="tag-rejected">FAILED</span>`;
        }

        if (details) {
            tdDetails.textContent = details;
        }
    }

    // --- 5-Stage Part & Inspection Pipeline Updater ---
    function updatePartRow(msg) {
        let row = activePartRows.get(msg.part_id);
        if (!row) {
            row = document.createElement('tr');
            row.innerHTML = `
                <td class="mono"><strong>#${msg.part_id}</strong></td>
                <td class="col-job mono">-</td>
                <td class="col-stage">-</td>
                <td class="col-verdict"><span style="color:var(--text-muted);">PENDING</span></td>
                <td class="col-ts mono">-</td>
            `;
            tablePartsBody.prepend(row);
            activePartRows.set(msg.part_id, row);

            if (tablePartsBody.children.length > 100) {
                tablePartsBody.removeChild(tablePartsBody.lastChild);
            }
        }

        if (msg.job_id !== undefined && msg.job_id !== null) {
            row.querySelector('.col-job').textContent = `#${msg.job_id}`;
        }

        const stageClass = msg.stage === 'completed' ? 'tag-completed' : 'tag-stage';
        row.querySelector('.col-stage').innerHTML = `<span class="${stageClass}">${msg.stage}</span>`;

        if (msg.stage === 'completed' && msg.verdict) {
            const isPass = msg.verdict === 'PASS';
            row.querySelector('.col-verdict').innerHTML = `<span class="${isPass ? 'verdict-pass' : 'verdict-fail'}">${msg.verdict}</span>`;
            partHistory.push({
                part_id: msg.part_id,
                job_id: msg.job_id,
                verdict: msg.verdict,
                ts: msg.ts
            });
        }

        row.querySelector('.col-ts').textContent = new Date(msg.ts || Date.now()).toLocaleTimeString();
    }

    // --- Device Connectivity & Heartbeat Age Updater ---
    function updateDeviceCard(dev, elLed, elStatus, elHb, elBar) {
        if (!dev) return;
        const connected = Boolean(dev.connected);
        const ageMs = dev.heartbeat_age_ms !== undefined ? dev.heartbeat_age_ms : 0;

        elLed.className = `led-indicator ${connected ? 'led-green' : 'led-red'}`;
        elStatus.textContent = connected ? 'CONNECTED' : 'DISCONNECTED';
        elStatus.className = `node-badge ${connected ? 'badge-online' : 'badge-offline'}`;
        elHb.textContent = `${ageMs} ms`;

        const pct = Math.min(100, (ageMs / 3000) * 100);
        elBar.style.width = `${pct}%`;
        if (!connected || ageMs >= 2500) {
            elBar.style.background = '#ef4444';
        } else if (ageMs >= 1500) {
            elBar.style.background = '#f59e0b';
        } else {
            elBar.style.background = '#22c55e';
        }
    }

    // --- Batch Completion Summary Drawer ---
    function renderBatchReport(report, sourceLabel = 'Live Batch Completion Event') {
        if (!report) return;
        const target = report.target || 0;
        const inspected = report.inspected || 0;
        const accepted = report.accepted || 0;
        const rejected = report.rejected || 0;
        const yieldPct = inspected > 0 ? ((accepted / inspected) * 100).toFixed(1) : '0.0';

        document.getElementById('rep-target').textContent = target;
        document.getElementById('rep-produced').textContent = report.produced || 0;
        document.getElementById('rep-inspected').textContent = inspected;
        document.getElementById('rep-accepted').textContent = accepted;
        document.getElementById('rep-rejected').textContent = rejected;
        document.getElementById('rep-yield').textContent = `${yieldPct}%`;
        document.getElementById('rep-duration').textContent = report.duration_ms !== undefined ? `${report.duration_ms} ms` : 'N/A';

        elReportSubtitle.textContent = `${sourceLabel} • ${new Date(report.ts || Date.now()).toLocaleTimeString()}`;
        elReportPanel.classList.remove('hidden');
    }

    // --- Initialize & Bind OperatorClient ---
    function initClient() {
        if (client) {
            client.disconnectWebSocket();
        }

        const host = inputGwHost.value.trim() || 'localhost';
        const port = parseInt(inputGwPort.value, 10) || 8080;

        client = new OperatorClient({
            host,
            port,
            timeoutMs: 500,
            maxRetries: 3,
            heartbeatTimeoutMs: 3000
        });

        // 3-Stage Command Events
        client.on('cmd_created', ({ cmd_id, cmd }) => {
            addOrUpdateCmdRow(cmd_id, cmd, 'pending', 'Transmitting HTTP POST...');
        });

        client.on('cmd_delivered', ({ cmd_id, cmd, status, attempt }) => {
            addOrUpdateCmdRow(cmd_id, cmd, 'delivered', `HTTP ${status} (attempt ${attempt})`);
            log(`[1-DELIVERED] cmd_id=${cmd_id} (${cmd}) HTTP ${status}`, '#38bdf8');
        });

        client.on('cmd_accepted', ({ cmd_id, cmd, duplicate }) => {
            const dupTag = duplicate ? ' [DUPLICATE SUPPRESSED]' : '';
            addOrUpdateCmdRow(cmd_id, cmd, 'accepted', `Gateway validated${dupTag}`);
            log(`[2-ACCEPTED] cmd_id=${cmd_id} (${cmd})${dupTag}`, '#22c55e');
        });

        client.on('cmd_completed', ({ cmd_id, cmd }) => {
            addOrUpdateCmdRow(cmd_id, cmd, 'completed', 'Confirmed downstream');
            log(`[3-COMPLETED] cmd_id=${cmd_id} (${cmd}) completed via WS`, '#d8b4fe');
        });

        client.on('cmd_rejected', ({ cmd_id, cmd, error }) => {
            addOrUpdateCmdRow(cmd_id, cmd, 'rejected', `${error.code}: ${error.message}`);
            log(`[REJECTED] cmd_id=${cmd_id} (${cmd}) -> ${error.code}: ${error.message}`, '#ef4444');
        });

        client.on('cmd_retry', ({ cmd_id, cmd, attempt, nextAttempt }) => {
            addOrUpdateCmdRow(cmd_id, cmd, 'pending', `Timeout — retrying (${nextAttempt}/${client.maxRetries + 1})...`);
            log(`[RETRY] cmd_id=${cmd_id} (${cmd}) attempt ${attempt} timed out -> try ${nextAttempt}`, '#f59e0b');
        });

        client.on('cmd_failed', ({ cmd_id, cmd, error, reason }) => {
            const errMsg = error ? error.message : (reason || 'Command failed');
            addOrUpdateCmdRow(cmd_id, cmd, 'failed', errMsg);
            log(`[FAILED] cmd_id=${cmd_id} (${cmd}) -> ${errMsg}`, '#ef4444');
        });

        // WebSocket & Watchdog Events
        client.on('ws_connected', async () => {
            elWsBadge.textContent = `WS: CONNECTED (${host}:${port})`;
            elWsBadge.className = 'status-pill pill-ok';
            elWatchdogBanner.classList.add('hidden');
            log(`WebSocket connected to ws://${host}:${port}/ws`, '#22c55e');

            try {
                await client.getStatus();
            } catch (_) { /* Initial WS status frame handles hydration */ }
        });

        client.on('ws_disconnected', () => {
            elWsBadge.textContent = 'WS: DISCONNECTED';
            elWsBadge.className = 'status-pill pill-err';
            log('WebSocket disconnected — attempting auto-reconnect...', '#ef4444');
        });

        client.on('heartbeat_timeout', ({ timeSinceLastMs }) => {
            elHeartbeatBadge.textContent = `TELEMETRY STALE (${timeSinceLastMs} ms)`;
            elHeartbeatBadge.className = 'status-pill pill-err';
            elWatchdogText.textContent = `No telemetry received from Gateway for ${timeSinceLastMs} ms (> 3000 ms limit).`;
            elWatchdogBanner.classList.remove('hidden');
            log(`[WATCHDOG] Telemetry stale (${timeSinceLastMs} ms > 3000 ms)`, '#f59e0b');
        });

        // Live Telemetry Stream
        client.on('status', (status) => {
            elHeartbeatBadge.textContent = 'TELEMETRY: ACTIVE';
            elHeartbeatBadge.className = 'status-pill pill-ok';
            elWatchdogBanner.classList.add('hidden');

            const state = status.state || 'idle';
            const mode = status.mode || 'production';
            const meta = STATE_META[state] || STATE_META.idle;

            elGatewayState.textContent = state.toUpperCase();
            elGatewayState.className = `state-badge state-${state}`;
            elStateLed.className = `led-indicator ${meta.led}`;
            elStateHint.textContent = meta.hint;

            elModeBadge.textContent = `MODE: ${mode.toUpperCase()}`;
            btnModeProd.classList.toggle('mode-active', mode === 'production');
            btnModeBench.classList.toggle('mode-active', mode === 'benchmark');

            if (state !== 'fault') {
                elFaultBanner.classList.add('hidden');
            }

            if (status.batch) {
                const { target, produced, queued, inspected, accepted, rejected } = status.batch;
                elProduced.textContent = produced;
                elQueued.textContent = queued;
                elInspected.textContent = inspected;
                elTarget.textContent = `/ ${target} INSPECTED`;
                elAccepted.textContent = accepted;
                elRejected.textContent = rejected;

                const batchPct = target > 0 ? Math.min(100, (inspected / target) * 100) : 0;
                elBatchBar.style.width = `${batchPct}%`;
            }

            if (status.queue) {
                const { depth, capacity, blocked } = status.queue;
                elQueueDepth.textContent = `${depth} / ${capacity}`;
                elQueueCapLabel.textContent = `Cap: ${capacity}`;
                const qPct = capacity > 0 ? Math.min(100, (depth / capacity) * 100) : 0;
                elQueueBar.style.width = `${qPct}%`;

                if (blocked) {
                    elQueueBlocked.style.display = 'inline-block';
                    elQueueBar.style.background = '#ef4444';
                } else {
                    elQueueBlocked.style.display = 'none';
                    elQueueBar.style.background = '#38bdf8';
                }
            }

            if (status.params) {
                const { cycle_time_ms, inspect_time_ms, N, K } = status.params;
                elConfirmedTimings.textContent = `Cycle: ${cycle_time_ms} ms | Inspect: ${inspect_time_ms} ms`;
                elActiveRuleLabel.textContent = `(part_id + ${K}) mod ${N} == 0`;
            }

            if (status.devices) {
                updateDeviceCard(status.devices.esp32_a, elEspALed, elEspAStatus, elEspAHb, elEspAHbBar);
                updateDeviceCard(status.devices.esp32_b, elEspBLed, elEspBStatus, elEspBHb, elEspBHbBar);
            }

            // State-Machine Button Interlocks
            btnStart.disabled = (state !== 'idle');
            btnPause.disabled = (state !== 'running');
            btnResume.disabled = (state !== 'paused');
            btnStop.disabled = (state !== 'running' && state !== 'paused');
            btnClearFault.disabled = (state !== 'fault');
            btnSetConfig.disabled = (state !== 'idle' && state !== 'paused');
            btnModeProd.disabled = (state !== 'idle');
            btnModeBench.disabled = (state !== 'idle');
        });

        client.on('part_event', (msg) => {
            updatePartRow(msg);
        });

        client.on('batch_complete', (summary) => {
            log(`=== BATCH COMPLETE === Target: ${summary.target}, Accepted: ${summary.accepted}, Rejected: ${summary.rejected}, Duration: ${summary.duration_ms} ms`, '#d8b4fe');
            renderBatchReport(summary, 'Live Batch Completion Event');
        });

        client.on('fault', (fault) => {
            elFaultCode.textContent = fault.code || 'FAULT';
            elFaultDevice.textContent = fault.device || 'gateway';
            elFaultMessage.textContent = fault.message || 'Fault condition triggered';
            elFaultBanner.classList.remove('hidden');
            log(`[SYSTEM FAULT] ${fault.code} (${fault.device}): ${fault.message}`, '#ef4444');
        });

        client.connectWebSocket();
    }

    // --- Operator Command Button Handlers ---
    btnConnect.addEventListener('click', () => initClient());

    btnStart.addEventListener('click', async () => {
        showValidationError('');
        const target_parts = parseInt(inputTargetParts.value, 10);
        const cycle_time_ms = parseInt(inputCycleTime.value, 10);
        const inspect_time_ms = parseInt(inputInspectTime.value, 10);

        if (isNaN(target_parts) || target_parts < 1 || target_parts > 10000) {
            showValidationError('target_parts must be between 1 and 10,000.');
            return;
        }
        if (isNaN(cycle_time_ms) || cycle_time_ms < 10 || cycle_time_ms > 60000) {
            showValidationError('cycle_time_ms must be between 10 and 60,000 ms.');
            return;
        }
        if (isNaN(inspect_time_ms) || inspect_time_ms < 10 || inspect_time_ms > 60000) {
            showValidationError('inspect_time_ms must be between 10 and 60,000 ms.');
            return;
        }

        elReportPanel.classList.add('hidden');
        await client.startBatch({ target_parts, cycle_time_ms, inspect_time_ms });
    });

    btnPause.addEventListener('click', async () => {
        await client.pauseBatch();
    });

    btnResume.addEventListener('click', async () => {
        await client.resumeBatch();
    });

    btnStop.addEventListener('click', async () => {
        await client.stopBatch();
    });

    btnClearFault.addEventListener('click', async () => {
        await client.clearFault();
    });

    btnBannerClearFault.addEventListener('click', async () => {
        await client.clearFault();
    });

    btnSetConfig.addEventListener('click', async () => {
        showValidationError('');
        const N = parseInt(inputRuleN.value, 10);
        const K = parseInt(inputRuleK.value, 10);

        if (isNaN(N) || N < 1) {
            showValidationError('Modulus N must be an integer >= 1.');
            return;
        }
        if (isNaN(K) || K < 0) {
            showValidationError('Offset K must be an integer >= 0.');
            return;
        }

        await client.setInspectionConfig({ N, K });
    });

    btnModeProd.addEventListener('click', async () => {
        await client.setMode({ mode: 'production' });
    });

    btnModeBench.addEventListener('click', async () => {
        await client.setMode({ mode: 'benchmark' });
    });

    btnFetchReport.addEventListener('click', async () => {
        try {
            const report = await client.getBatchReport();
            renderBatchReport(report, 'Fetched via GET /api/v1/batch/report');
        } catch (err) {
            log(`Failed to fetch batch report: ${err.message}`, '#ef4444');
        }
    });

    btnCloseReport.addEventListener('click', () => {
        elReportPanel.classList.add('hidden');
    });

    btnExportCsv.addEventListener('click', () => {
        if (partHistory.length === 0) {
            log('No completed part verdicts in current session to export.', '#f59e0b');
            return;
        }
        const header = 'part_id,job_id,verdict,timestamp_iso\n';
        const rows = partHistory.map(p => `${p.part_id},${p.job_id},${p.verdict},${new Date(p.ts).toISOString()}`).join('\n');
        const blob = new Blob([header + rows], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `line01_batch_verdicts_${Date.now()}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    });

    document.getElementById('btn-clear-cmds').addEventListener('click', () => {
        tableCmdsBody.innerHTML = '';
        activeCommandRows.clear();
    });

    document.getElementById('btn-clear-parts').addEventListener('click', () => {
        tablePartsBody.innerHTML = '';
        activePartRows.clear();
        partHistory.length = 0;
    });

    document.getElementById('btn-clear-log').addEventListener('click', () => {
        logBox.innerHTML = '';
    });

    // Boot initial connection
    initClient();
});