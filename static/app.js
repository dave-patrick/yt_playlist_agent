let activeTab = 'dashboard';
let logInterval = null;
let statusInterval = null;
let wasRunning = false;

// Playlist Detail view state variables
let currentPlaylist = null;
let currentVideos = [];
let selectedVideoUrls = new Set();
let allPlaylists = [];

// Sort state for playlist video table
let sortColumn = null;   // 'index' | 'title' | 'channel' | 'published'
let sortDirection = 'asc'; // 'asc' | 'desc'

// Tab switcher
function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    
    // Find the button and content to activate
    const activeBtn = Array.from(document.querySelectorAll('.tab-btn')).find(btn => 
        btn.getAttribute('onclick').includes(tabId)
    );
    if (activeBtn) activeBtn.classList.add('active');
    
    const activeContent = document.getElementById(`tab-${tabId}`);
    if (activeContent) activeContent.classList.add('active');
    
    activeTab = tabId;

    if (tabId === 'maintenance') {
        loadMaintenanceQueue();
    } else if (tabId === 'rules') {
        loadRules();
    } else if (tabId === 'playlists') {
        // Reset playlist detail view when switching to playlists tab
        currentPlaylist = null;
        currentVideos = [];
        selectedVideoUrls.clear();
        document.getElementById('playlist-detail-container').style.display = 'none';
        document.getElementById('playlists-container').style.display = 'grid';
        loadPlaylists();
    }
}

function getJobSeconds(jobName, pendingActions) {
    if (!jobName) return 0;
    
    const nameLower = jobName.toLowerCase();
    const isApiMode = (currentUser && currentUser.logged_in);
    
    // Determine the average seconds per item for this job type
    let secPerItem = isApiMode ? 1.0 : 14; // Default for moves, deletes, and single action maintenance
    if (nameLower.includes('generate')) {
        secPerItem = 0.5; // very fast local file analysis
    } else if (nameLower.includes('dupe') || nameLower.includes('clean') || nameLower.includes('duplicate')) {
        secPerItem = isApiMode ? 1.5 : 25; // duplicate resolution involves remove + re-add (two operations)
    }
    
    // Check if it's a progress-tracked active job, e.g. "Batch Move (3/10)"
    const match = jobName.match(/\((\d+)\/(\d+)\)/);
    if (match) {
        const current = parseInt(match[1]);
        const total = parseInt(match[2]);
        const remaining = total - current;
        if (remaining <= 0) return 0;
        return Math.ceil(remaining * secPerItem);
    }
    
    // Check if it's a queued job with item count, e.g. "Batch Move (10 items)"
    const itemMatch = jobName.match(/\((\d+)\s+items?\)/);
    if (itemMatch) {
        const count = parseInt(itemMatch[1]);
        return Math.ceil(count * secPerItem);
    }
    
    if (nameLower.includes('scan')) {
        const statVideosEl = document.getElementById('stat-videos');
        if (statVideosEl && isApiMode) {
            const count = parseInt(statVideosEl.textContent.replace(/,/g, '')) || 0;
            if (count > 0) {
                return Math.ceil(count / 150) + 10;
            }
        }
        return isApiMode ? 45 : 90; // Default fallback to 45s for API mode if no cached value
    }
    if (nameLower.includes('sort')) {
        return isApiMode ? 15 : 45; // API sort takes ~15s, Browser sort takes ~45s
    }
    if (nameLower.includes('generate_maintenance')) {
        return 5; // Est: ~5s
    }
    if (nameLower.includes('apply_maintenance')) {
        const count = pendingActions || 0;
        if (count === 0) return 5;
        return count * (isApiMode ? 1.0 : 14);
    }
    
    return 10;
}

function getJobEstimateText(activeJob, pendingActions, queuedJobs = []) {
    if (!activeJob) return '';
    
    let totalSecs = getJobSeconds(activeJob, pendingActions);
    
    if (queuedJobs && queuedJobs.length > 0) {
        for (const qJob of queuedJobs) {
            totalSecs += getJobSeconds(qJob, 0);
        }
    }
    
    if (totalSecs <= 0) return ' (finishing...)';
    
    if (totalSecs < 60) {
        return ` (Est: ~${totalSecs}s remaining)`;
    } else {
        const mins = Math.floor(totalSecs / 60);
        const secs = totalSecs % 60;
        return ` (Est: ~${mins}m ${secs}s remaining)`;
    }
}

// Fetch stats and engine status
async function loadStatus() {
    try {
        const response = await fetch('/api/status');
        if (!response.ok) throw new Error("Status API fail");
        const data = await response.json();
        
        // Update stats
        document.getElementById('stat-playlists').textContent = data.total_playlists || '-';
        document.getElementById('stat-videos').textContent = data.total_videos || '-';
        document.getElementById('stat-actions').textContent = data.pending_actions || '0';
        document.getElementById('stat-ai-cache').textContent = data.ai_total !== undefined ? data.ai_total : (data.ai_cache_hits || '0');
        const detailsEl = document.getElementById('stat-ai-details');
        if (detailsEl) {
            if (data.ai_total !== undefined) {
                detailsEl.textContent = `${data.ai_pending} pending / ${data.ai_reviewed} reviewed`;
            } else {
                detailsEl.textContent = '0 pending / 0 reviewed';
            }
        }
        
        // Update engine status dot
        const dot = document.getElementById('engine-status-dot');
        const text = document.getElementById('engine-status-text');
        const stopBtn = document.getElementById('btn-stop-engine');
        
        const isRunningNow = (data.engine_status === 'running');
        dot.className = 'status-dot';
        if (isRunningNow) {
            dot.classList.add('busy');
            const estimate = getJobEstimateText(data.active_job, data.pending_actions, data.queued_jobs);
            let statusText = `Running: ${data.active_job || 'Task'}${estimate}`;
            if (data.queued_jobs && data.queued_jobs.length > 0) {
                statusText += ` (Queued: ${data.queued_jobs.join(', ')})`;
            }
            text.textContent = statusText;
            if (stopBtn) stopBtn.style.display = 'inline-block';
            // If running, double check if we should query logs more actively
            if (!logInterval) startLogPolling();
        } else {
            dot.classList.add('idle');
            text.textContent = 'Idle';
            if (stopBtn) stopBtn.style.display = 'none';
            // Stop log polling when idle, but do one final check
            if (logInterval && !data.active_job) {
                setTimeout(fetchLogs, 1000);
                clearInterval(logInterval);
                logInterval = null;
            }
        }

        if (wasRunning && !isRunningNow) {
            if (activeTab === 'maintenance') {
                loadMaintenanceQueue();
            } else if (activeTab === 'playlists') {
                loadPlaylists();
            }
        }
        wasRunning = isRunningNow;
        
        // Update last run
        if (data.last_run) {
            document.getElementById('last-run-text').textContent = data.last_run;
        }

        // Update queue badge on tab
        const badge = document.getElementById('queue-badge');
        if (data.pending_actions > 0) {
            badge.textContent = data.pending_actions;
            badge.style.display = 'inline-block';
        } else {
            badge.style.display = 'none';
        }
    } catch (err) {
        console.error("Error loading status:", err);
    }
}

// Log Polling
function startLogPolling() {
    if (logInterval) clearInterval(logInterval);
    fetchLogs();
    logInterval = setInterval(fetchLogs, 2000);
}

let lastLogLinesCount = 0;
async function fetchLogs() {
    try {
        const response = await fetch('/api/logs');
        if (!response.ok) return;
        const data = await response.json();
        
        const consoleEl = document.getElementById('console-logs');
        if (data.logs && data.logs.trim() !== "") {
            consoleEl.textContent = data.logs;
            // Auto scroll to bottom
            consoleEl.scrollTop = consoleEl.scrollHeight;
        } else {
            consoleEl.textContent = "No log output recorded yet.";
        }
    } catch (err) {
        console.error("Error fetching logs:", err);
    }
}

function copyLogs() {
    const logsText = document.getElementById('console-logs').textContent;
    navigator.clipboard.writeText(logsText)
        .then(() => {
            addConsoleLog("[Client] Logs copied to clipboard.");
        })
        .catch(err => {
            console.error("Failed to copy logs: ", err);
            alert("Failed to copy logs to clipboard.");
        });
}

function downloadLogs() {
    const logsText = document.getElementById('console-logs').textContent;
    const blob = new Blob([logsText], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    link.href = url;
    link.download = `yt_playlist_agent_logs_${timestamp}.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    addConsoleLog("[Client] Logs downloaded successfully.");
}

function clearLogs() {
    fetch('/api/logs/clear', { method: 'POST' })
        .then(() => {
            document.getElementById('console-logs').textContent = "Logs cleared.";
        });
}

// Trigger tasks
async function triggerTask(taskName) {
    try {
        addConsoleLog(`[Client] Requesting background task: ${taskName}...`);
        const response = await fetch(`/api/run-${taskName}`, { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Task '${taskName}' successfully spawned in background.`);
            loadStatus();
            startLogPolling();
        } else {
            addConsoleLog(`[Error] Failed to spawn task: ${data.detail || data.message}`);
        }
    } catch (err) {
        addConsoleLog(`[Error] Connection failed: ${err.message}`);
    }
}

async function triggerApplyMaintenance(force = false) {
    try {
        addConsoleLog(`[Client] Requesting maintenance execution (force=${force})...`);
        const response = await fetch('/api/maintenance/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ force: force })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Maintenance runner started.`);
            loadStatus();
            startLogPolling();
        } else {
            addConsoleLog(`[Error] Failed to start maintenance: ${data.detail || data.message}`);
        }
    } catch (err) {
        addConsoleLog(`[Error] Connection failed: ${err.message}`);
    }
}

async function triggerStopTask() {
    if (!confirm("Are you sure you want to stop the currently running background task?")) return;
    try {
        addConsoleLog(`[Client] Requesting task termination...`);
        const response = await fetch('/api/tasks/stop', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] ${data.message || 'Task successfully stopped.'}`);
            loadStatus();
        } else {
            addConsoleLog(`[Error] Failed to stop task: ${data.detail || data.message}`);
        }
    } catch (err) {
        addConsoleLog(`[Error] Connection failed: ${err.message}`);
    }
}

function addConsoleLog(message) {
    const consoleEl = document.getElementById('console-logs');
    const timestamp = new Date().toLocaleTimeString();
    consoleEl.textContent += `\n[${timestamp}] ${message}`;
    consoleEl.scrollTop = consoleEl.scrollHeight;
}

let maintSelectedVids = new Set();

let allMaintenanceActions = [];
let currentMaintFilter = 'all';
let currentMaintPlaylistFilter = '';

// Load Maintenance actions queue
async function loadMaintenanceQueue() {
    const listEl = document.getElementById('maintenance-list');
    const toolbarEl = document.getElementById('maint-batch-toolbar');
    
    listEl.innerHTML = '<div style="text-align: center; padding: 2rem; color: var(--text-secondary);">Loading queue...</div>';
    if (toolbarEl) toolbarEl.style.display = 'none';
    maintSelectedVids.clear();
    updateMaintSelectedCount();
    
    try {
        const response = await fetch('/api/maintenance');
        if (!response.ok) throw new Error("Failed to load queue");
        allMaintenanceActions = await response.json();
        
        // Ensure allPlaylists is loaded so we have dropdown categories
        if (allPlaylists.length === 0) {
            const plistResp = await fetch('/api/playlists');
            if (plistResp.ok) {
                allPlaylists = await plistResp.json();
            }
        }
        
        // Populate the maintenance playlist filter dropdown
        const filterSelect = document.getElementById('maint-playlist-filter');
        if (filterSelect) {
            filterSelect.innerHTML = '<option value="">-- All Playlists --</option>';
            const sortedPlaylists = [...allPlaylists].sort((a, b) => a.name.localeCompare(b.name));
            sortedPlaylists.forEach(p => {
                if (p.name.toLowerCase() !== "watch later") {
                    const opt = document.createElement('option');
                    opt.value = p.name;
                    opt.textContent = p.name;
                    filterSelect.appendChild(opt);
                }
            });
        }
        
        filterMaintenanceQueue(currentMaintFilter);
    } catch (err) {
        listEl.innerHTML = `<div style="text-align: center; padding: 2rem; color: var(--danger);">Failed to load actions queue: ${err.message}</div>`;
    }
}

// Filter the maintenance queue locally
function filterMaintenanceQueue(filterType) {
    currentMaintFilter = filterType;
    
    // 1. Filter by playlist first (if selected)
    let playlistFiltered = allMaintenanceActions || [];
    if (currentMaintPlaylistFilter) {
        playlistFiltered = allMaintenanceActions.filter(a => {
            if (a.type === 'MISPLACED') {
                const fromList = a.from || [];
                return fromList.some(p => p.toLowerCase() === currentMaintPlaylistFilter.toLowerCase());
            } else if (a.type && a.type.startsWith('DUPLICATE')) {
                const keepPl = a.keep || '';
                const removeList = a.remove || [];
                return (keepPl.toLowerCase() === currentMaintPlaylistFilter.toLowerCase()) ||
                       removeList.some(p => p.toLowerCase() === currentMaintPlaylistFilter.toLowerCase());
            }
            return true;
        });
    }

    // 2. Calculate counts dynamically based on the playlist-filtered subset
    const totalCount = playlistFiltered.length;
    const duplicateCount = playlistFiltered.filter(a => a.type && a.type.startsWith('DUPLICATE')).length;
    const misplacedCount = playlistFiltered.filter(a => a.type === 'MISPLACED').length;
    
    // Update button text with counts
    const btnAll = document.getElementById('maint-filter-all');
    if (btnAll) btnAll.textContent = `All (${totalCount})`;
    
    const btnDup = document.getElementById('maint-filter-duplicate');
    if (btnDup) btnDup.textContent = `Duplicates (${duplicateCount})`;
    
    const btnMis = document.getElementById('maint-filter-misplaced');
    if (btnMis) btnMis.textContent = `Misplaced (${misplacedCount})`;

    // Update active tab buttons
    document.querySelectorAll('[id^="maint-filter-"]').forEach(btn => btn.classList.remove('active'));
    const activeBtn = document.getElementById(`maint-filter-${filterType}`);
    if (activeBtn) activeBtn.classList.add('active');
    
    // 3. Filter by tab type (All / Duplicate / Misplaced)
    let finalFiltered = playlistFiltered;
    if (filterType === 'duplicate') {
        finalFiltered = playlistFiltered.filter(a => a.type && a.type.startsWith('DUPLICATE'));
    } else if (filterType === 'misplaced') {
        finalFiltered = playlistFiltered.filter(a => a.type === 'MISPLACED');
    }
    
    renderMaintenanceQueue(finalFiltered);
}

function filterMaintByPlaylist(playlistName) {
    currentMaintPlaylistFilter = playlistName;
    filterMaintenanceQueue(currentMaintFilter);
}

// Render the filtered maintenance actions list
function renderMaintenanceQueue(actions) {
    const listEl = document.getElementById('maintenance-list');
    const toolbarEl = document.getElementById('maint-batch-toolbar');
    
    if (!actions || actions.length === 0) {
        listEl.innerHTML = `
            <div style="text-align: center; padding: 4rem 2rem; color: var(--text-secondary);">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom: 1rem; color: var(--success);"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                <p style="font-size: 1.1rem; font-weight: 500; color: var(--text-primary);">No Maintenance Actions Found</p>
                <p style="font-size: 0.9rem; margin-top: 4px;">There are no actions matching the selected filter.</p>
            </div>
        `;
        if (toolbarEl) toolbarEl.style.display = 'none';
        return;
    }
    
    // Show toolbar and reset Select All checkbox
    if (toolbarEl) {
        toolbarEl.style.display = 'flex';
        const masterCb = document.getElementById('maint-select-all');
        if (masterCb) masterCb.checked = false;
    }
    
    listEl.innerHTML = '';
    actions.forEach((a, idx) => {
        const item = document.createElement('div');
        item.className = 'action-item';
        item.id = `maint-item-${a.vid}`;
        
        const isDup = a.type && a.type.startsWith('DUPLICATE');
        const badgeClass = isDup ? 'badge-duplicate' : 'badge-misplaced';
        const badgeText = isDup ? 'Duplicate' : 'Misplaced';
        
        let desc = '';
        if (isDup) {
            desc = `Keep in <strong>${a.keep}</strong>, remove duplicate from <strong>${a.remove.join(', ')}</strong>`;
        } else {
            let categoryOptions = '';
            const targetTo = a.to || '';
            allPlaylists.forEach(p => {
                const selected = p.name.toLowerCase() === targetTo.toLowerCase() ? 'selected' : '';
                categoryOptions += `<option value="${escapeHtml(p.name)}" ${selected}>${escapeHtml(p.name)}</option>`;
            });
            desc = `Move from <strong>${a.from ? a.from.join(', ') : ''}</strong> to 
                <select class="form-input inline-move-select" style="padding: 2px 6px; font-size: 0.85rem; width: 150px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); display: inline-block; margin-left: 4px;" onchange="updateMaintTarget('${a.vid}', this.value)">
                    ${categoryOptions}
                </select>`;
        }
        
        const aiBadge = a.is_ai ? `<span class="action-badge badge-ai" style="background: linear-gradient(135deg, #a855f7, #6366f1); color: white; margin-left: 6px;">AI Suggested</span>` : '';
        const pinBtn = (a.is_ai && a.channel && (a.to || a.keep)) ? 
            `<button class="action-btn" style="padding: 6px 12px; font-size: 0.8rem; background: rgba(168, 85, 247, 0.15); border: 1px solid rgba(168, 85, 247, 0.4); color: #c084fc; margin-right: 6px;" onclick="pinRuleFromDropdown(this, '${a.channel.replace(/'/g, "\\'")}', '${a.vid}', '${isDup ? a.keep.replace(/'/g, "\\'") : ''}')">Pin Rule</button>` : '';

        item.innerHTML = `
            <div style="display: flex; align-items: center; gap: 12px; margin-right: 8px;">
                <input type="checkbox" class="maint-checkbox" data-vid="${a.vid}" ${maintSelectedVids.has(a.vid) ? 'checked' : ''} onclick="handleMaintCheckboxClick(this, '${a.vid}', event)">
            </div>
            <div class="action-info" style="flex-grow: 1;">
                <div style="display: flex; gap: 6px; align-items: center; margin-bottom: 4px;">
                    <span class="action-badge ${badgeClass}">${badgeText}</span>
                    ${aiBadge}
                </div>
                <a class="action-title" href="https://www.youtube.com/watch?v=${a.vid}" target="_blank" title="Watch on YouTube" style="text-decoration: none; color: inherit; transition: color 0.2s ease;">${a.title}</a>
                <span class="action-desc">${desc}</span>
            </div>
            <div class="action-buttons">
                ${pinBtn}
                <button class="action-btn btn-success" style="padding: 6px 12px; font-size: 0.8rem;" onclick="applySingleAction('${a.vid}', ${idx})">Apply</button>
                <button class="action-btn btn-danger" style="padding: 6px 12px; font-size: 0.8rem; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: var(--text-secondary);" onclick="discardAction('${a.vid}', ${idx})">Skip</button>
                <button class="action-btn btn-danger" style="padding: 6px 12px; font-size: 0.8rem; background: var(--danger); border: 1px solid var(--danger); color: white;" onclick="deleteVideoAction('${a.vid}', ${idx})">Delete Video</button>
            </div>
        `;
        listEl.appendChild(item);
    });
}

let lastMaintCheckedIndex = null;

function handleMaintCheckboxClick(checkbox, vid, event) {
    const checkboxes = Array.from(document.querySelectorAll('#maintenance-list .maint-checkbox'));
    const clickedIdx = checkboxes.indexOf(checkbox);
    
    if (event && event.shiftKey && lastMaintCheckedIndex !== null && lastMaintCheckedIndex < checkboxes.length) {
        const start = Math.min(lastMaintCheckedIndex, clickedIdx);
        const end = Math.max(lastMaintCheckedIndex, clickedIdx);
        const targetCheckedState = checkbox.checked;
        
        for (let i = start; i <= end; i++) {
            const cb = checkboxes[i];
            cb.checked = targetCheckedState;
            const itemVid = cb.getAttribute('data-vid');
            if (targetCheckedState) {
                maintSelectedVids.add(itemVid);
            } else {
                maintSelectedVids.delete(itemVid);
            }
        }
    } else {
        if (checkbox.checked) {
            maintSelectedVids.add(vid);
        } else {
            maintSelectedVids.delete(vid);
        }
    }
    
    lastMaintCheckedIndex = clickedIdx;
    updateMaintSelectedCount();
}

function toggleSelectAllMaint(masterCheckbox) {
    const isChecked = masterCheckbox.checked;
    const checkboxes = document.querySelectorAll('.maint-checkbox');
    
    checkboxes.forEach(cb => {
        cb.checked = isChecked;
        const vid = cb.getAttribute('data-vid');
        if (isChecked) {
            maintSelectedVids.add(vid);
        } else {
            maintSelectedVids.delete(vid);
        }
    });
    
    updateMaintSelectedCount();
}

function updateMaintSelectedCount() {
    const count = maintSelectedVids.size;
    const badge = document.getElementById('maint-selected-count');
    if (badge) {
        badge.textContent = `${count} Selected`;
    }
}

async function triggerMaintBatchApply() {
    const count = maintSelectedVids.size;
    if (count === 0) return;
    
    if (!confirm(`Are you sure you want to apply the selected ${count} maintenance actions?`)) return;
    
    try {
        addConsoleLog(`[Client] Spawning batch execution of ${count} maintenance actions...`);
        const response = await fetch('/api/maintenance/batch-apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vids: Array.from(maintSelectedVids) })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Batch apply started successfully in background.`);
            loadStatus();
            startLogPolling();
        } else {
            alert(`Error: ${data.detail || data.message}`);
        }
    } catch (err) {
        console.error(err);
        alert(`Failed to apply batch: ${err.message}`);
    }
}

async function triggerMaintBatchDiscard() {
    const count = maintSelectedVids.size;
    if (count === 0) return;
    
    if (!confirm(`Are you sure you want to skip/discard the selected ${count} maintenance actions?`)) return;
    
    try {
        addConsoleLog(`[Client] Discarding ${count} selected maintenance actions...`);
        const response = await fetch('/api/maintenance/batch-discard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vids: Array.from(maintSelectedVids) })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Discarded ${count} actions.`);
            loadMaintenanceQueue();
            loadStatus();
        } else {
            alert(`Error: ${data.detail || data.message}`);
        }
    } catch (err) {
        console.error(err);
        alert(`Failed to discard batch: ${err.message}`);
    }
}

async function triggerMaintBatchDelete() {
    const count = maintSelectedVids.size;
    if (count === 0) return;
    
    if (!confirm(`Are you sure you want to delete the selected ${count} videos from all their playlists? This will remove them completely.`)) return;
    
    try {
        addConsoleLog(`[Client] Spawning batch deletion of ${count} videos from maintenance...`);
        const response = await fetch('/api/maintenance/batch-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vids: Array.from(maintSelectedVids) })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Batch deletion started successfully in background.`);
            maintSelectedVids.clear();
            updateMaintSelectedCount();
            loadMaintenanceQueue();
            loadStatus();
            startLogPolling();
        } else {
            alert(`Error: ${data.detail || data.message}`);
        }
    } catch (err) {
        console.error(err);
        alert(`Failed to delete batch: ${err.message}`);
    }
}

async function pinRule(channel, category) {
    if (!channel || !category) return;
    try {
        const response = await fetch('/api/rules/add-channel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: channel, category: category })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Learned channel rule: ${channel} -> ${category}`);
            alert(`Added rule mapping '${channel}' to '${category}'!`);
            loadRules();
            loadMaintenanceQueue();
        } else {
            alert(`Failed to add rule: ${data.detail}`);
        }
    } catch(err) {
        console.error(err);
    }
}

async function pinRuleFromDropdown(btnEl, channel, vid, defaultCategory) {
    let category = defaultCategory;
    if (!category) {
        const row = btnEl.closest('.action-item');
        if (row) {
            const select = row.querySelector('.inline-move-select');
            if (select) {
                category = select.value;
            }
        }
    }
    if (!category) {
        alert("Please select a target playlist.");
        return;
    }
    await pinRule(channel, category);
}

async function updateMaintTarget(vid, targetVal) {
    try {
        const response = await fetch('/api/maintenance/update-target', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vid: vid, target: targetVal })
        });
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to update target playlist.");
        }
        
        const action = allMaintenanceActions.find(a => a.vid === vid);
        if (action) {
            action.to = targetVal;
        }
        
        addConsoleLog(`[Client] Updated target playlist for video ${vid} to '${targetVal}'.`);
    } catch (err) {
        console.error(err);
        alert(`Error: ${err.message}`);
    }
}


function removeMaintItemLocally(vid) {
    // Remove the video ID from allMaintenanceActions
    allMaintenanceActions = allMaintenanceActions.filter(a => a.vid !== vid);
    
    // Delete from checked set
    maintSelectedVids.delete(vid);
    updateMaintSelectedCount();
    
    // Fade out and remove element from DOM
    const el = document.getElementById(`maint-item-${vid}`);
    if (el) {
        el.style.transition = 'all 0.3s ease';
        el.style.opacity = '0';
        el.style.transform = 'translateY(10px)';
        setTimeout(() => {
            el.remove();
            // Re-apply filter to handle empty state correctly if all matching items are gone
            filterMaintenanceQueue(currentMaintFilter);
        }, 300);
    } else {
        filterMaintenanceQueue(currentMaintFilter);
    }
}

async function applySingleAction(vid, index) {
    try {
        const response = await fetch(`/api/maintenance/apply-single`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vid: vid })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Applied single action for video ID: ${vid}`);
            removeMaintItemLocally(vid);
            loadStatus();
        } else {
            alert(`Error: ${data.detail || data.message}`);
        }
    } catch (err) {
        console.error(err);
    }
}

async function discardAction(vid, index) {
    try {
        const response = await fetch(`/api/maintenance/discard`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vid: vid })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Discarded action for video ID: ${vid}`);
            removeMaintItemLocally(vid);
            loadStatus();
        } else {
            alert(`Error: ${data.detail || data.message}`);
        }
    } catch (err) {
        console.error(err);
    }
}

async function deleteVideoAction(vid, index) {
    if (!confirm("Are you sure you want to delete this video from all its playlists? This will remove it completely.")) {
        return;
    }
    try {
        const response = await fetch(`/api/maintenance/delete-single`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vid: vid })
        });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Deleted video from playlists for ID: ${vid}`);
            removeMaintItemLocally(vid);
            loadStatus();
        } else {
            alert(`Error: ${data.detail || data.message}`);
        }
    } catch (err) {
        console.error(err);
    }
}

async function generateMaintenanceList() {
    addConsoleLog("[Client] Requesting dry run regeneration of maintenance queue...");
    try {
        const response = await fetch('/api/maintenance/generate', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            addConsoleLog(`[Client] Maintenance queue regeneration started in background...`);
            // Poll or wait a bit, then load
            setTimeout(() => {
                loadMaintenanceQueue();
                loadStatus();
            }, 3000);
        } else {
            addConsoleLog(`[Error] Failed to generate: ${data.detail || data.message}`);
        }
    } catch (err) {
        addConsoleLog(`[Error] Connection failed: ${err.message}`);
    }
}

// Rules & Mappings
async function loadRules() {
    try {
        const response = await fetch('/api/rules');
        if (!response.ok) throw new Error("Rules load failed");
        const data = await response.json();
        
        document.getElementById('rules-md').value = data.rules_md || '';
        document.getElementById('rules-channels').value = data.channels_txt || '';
    } catch (err) {
        console.error("Error loading rules:", err);
    }
}

async function saveRules() {
    const rulesMd = document.getElementById('rules-md').value;
    const channelsTxt = document.getElementById('rules-channels').value;
    
    try {
        const response = await fetch('/api/rules', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                rules_md: rulesMd,
                channels_txt: channelsTxt
            })
        });
        const data = await response.json();
        if (data.success) {
            alert("Rules saved successfully!");
        } else {
            alert(`Save failed: ${data.detail || data.message}`);
        }
    } catch (err) {
        console.error(err);
    }
}

// Load Playlists
async function loadPlaylists() {
    const container = document.getElementById('playlists-container');
    container.innerHTML = '<div style="text-align: center; padding: 2rem; color: var(--text-secondary); grid-column: span 3;">Loading playlists...</div>';
    
    try {
        const response = await fetch('/api/playlists');
        if (!response.ok) throw new Error("Playlists fetch failed");
        const data = await response.json();
        
        allPlaylists = data; // Save globally
        
        if (!data || data.length === 0) {
            container.innerHTML = '<div style="text-align: center; padding: 2rem; color: var(--text-secondary); grid-column: span 3;">No playlists scanned. Run a scan from the dashboard!</div>';
            return;
        }
        
        container.innerHTML = '';
        data.forEach(p => {
            const card = document.createElement('div');
            card.className = 'playlist-card glass';
            
            card.innerHTML = `
                <div class="playlist-name">${p.name}</div>
                <div class="playlist-meta">
                    <span>${p.video_count || 0} Videos</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <button class="icon-btn rescan-single-btn" onclick="event.stopPropagation(); rescanSinglePlaylist('${p.name.replace(/'/g, "\\'")}', '${p.url}', this)" title="Rescan playlist" style="background: transparent; border: none; color: var(--text-secondary); cursor: pointer; padding: 4px; display: inline-flex; align-items: center; justify-content: center; transition: color 0.2s;">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"></path></svg>
                        </button>
                        <a href="${p.url}" target="_blank" style="color: var(--primary); text-decoration: none; font-size: 0.85rem;" onclick="event.stopPropagation()">View on YT ↗</a>
                    </div>
                </div>
            `;
            
            card.onclick = () => showPlaylistVideos(p);
            container.appendChild(card);
        });
    } catch (err) {
        container.innerHTML = `<div style="text-align: center; padding: 2rem; color: var(--danger); grid-column: span 3;">Error loading playlists: ${err.message}</div>`;
    }
}

async function rescanSinglePlaylist(name, url, buttonEl) {
    if (buttonEl.disabled) return;
    
    // Disable button and add spinning animation
    buttonEl.disabled = true;
    const svg = buttonEl.querySelector('svg');
    if (svg) svg.style.animation = 'spin 1s linear infinite';
    
    addConsoleLog(`[Client] Triggering single rescan for playlist: ${name}...`);
    
    try {
        const response = await fetch(`/api/playlists/videos?playlist_url=${encodeURIComponent(url)}&refresh=true`);
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to rescan playlist");
        }
        
        const data = await response.json();
        const videosCount = data.videos ? data.videos.length : 0;
        addConsoleLog(`[Client] Rescan successful for '${name}'. Found ${videosCount} videos.`);
        
        // Reload the playlists grid and update status
        loadPlaylists();
        loadStatus();
    } catch (err) {
        addConsoleLog(`[Error] Rescan failed for '${name}': ${err.message}`);
        alert(`Rescan failed: ${err.message}`);
    } finally {
        buttonEl.disabled = false;
        if (svg) svg.style.animation = '';
    }
}

async function showPlaylistVideos(playlist) {
    currentPlaylist = playlist;
    selectedVideoUrls.clear();
    updateSelectedCount();
    
    // Reset sort state for each new playlist
    sortColumn = null;
    sortDirection = 'asc';
    ['index', 'title', 'channel', 'published'].forEach(col => {
        const th = document.getElementById(`th-${col}`);
        if (th) th.classList.remove('sort-asc', 'sort-desc');
    });
    
    // Hide grid, show detail view
    document.getElementById('playlists-container').style.display = 'none';
    document.getElementById('playlist-detail-container').style.display = 'block';
    
    // Set headers
    document.getElementById('current-playlist-title').textContent = playlist.name;
    document.getElementById('current-playlist-meta').textContent = `${playlist.video_count || 0} videos cached`;
    
    // Populate target select dropdown (excluding current playlist)
    populateTargetPlaylists(playlist.name);
    
    // Setup search
    document.getElementById('video-search').value = '';
    
    // Load videos
    const tbody = document.getElementById('video-table-body');
    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 3rem; color: var(--text-secondary);">Loading videos...</td></tr>';
    
    try {
        const url = `/api/playlists/videos?playlist_url=${encodeURIComponent(playlist.url)}`;
        const response = await fetch(url);
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to load videos");
        }
        const data = await response.json();
        currentVideos = data.videos || [];
        renderVideoTable(currentVideos);
        document.getElementById('current-playlist-meta').textContent = `${currentVideos.length} videos cached`;
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 3rem; color: var(--danger);">Failed to load videos: ${err.message}</td></tr>`;
    }
}

function populateTargetPlaylists(excludeName) {
    const select = document.getElementById('target-playlist-select');
    if (!select) return;
    select.innerHTML = '';
    
    // Default empty/select option
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = '-- Select Playlist --';
    select.appendChild(defaultOpt);
    
    allPlaylists.forEach(p => {
        if (p.name.toLowerCase() !== excludeName.toLowerCase()) {
            const opt = document.createElement('option');
            opt.value = p.name;
            opt.textContent = p.name;
            select.appendChild(opt);
        }
    });
}

function goBackToPlaylists() {
    currentPlaylist = null;
    currentVideos = [];
    selectedVideoUrls.clear();
    sortColumn = null;
    sortDirection = 'asc';
    ['index', 'title', 'channel', 'published'].forEach(col => {
        const th = document.getElementById(`th-${col}`);
        if (th) th.classList.remove('sort-asc', 'sort-desc');
    });
    
    // Hide details, show grid
    document.getElementById('playlist-detail-container').style.display = 'none';
    document.getElementById('playlists-container').style.display = 'grid';
    
    // Reload playlists to ensure counts etc are up-to-date
    loadPlaylists();
}

// ─── Column Sorting ────────────────────────────────────────────────────────

// Map of published relative strings to a numeric rank for sorting
function publishedRank(str) {
    if (!str || str === 'Unknown') return Number.MAX_SAFE_INTEGER;
    const s = str.toLowerCase();
    const n = parseInt(s) || 1;
    if (s.includes('second')) return n;
    if (s.includes('minute')) return n * 60;
    if (s.includes('hour'))   return n * 3600;
    if (s.includes('day') || s.includes('yesterday')) return n * 86400;
    if (s.includes('week'))   return n * 604800;
    if (s.includes('month'))  return n * 2592000;
    if (s.includes('year'))   return n * 31536000;
    return Number.MAX_SAFE_INTEGER;
}

function durationToSeconds(str) {
    if (!str || str === 'Unknown' || str === '--:--') return 0;
    const parts = str.split(':').map(Number);
    if (parts.some(isNaN)) return 0;
    if (parts.length === 2) {
        return parts[0] * 60 + parts[1];
    } else if (parts.length === 3) {
        return parts[0] * 3600 + parts[1] * 60 + parts[2];
    }
    return 0;
}

function sortVideoTable(column) {
    // Toggle direction if clicking same column, else default to asc
    if (sortColumn === column) {
        sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        sortColumn = column;
        sortDirection = 'asc';
    }

    // Update header classes
    ['index', 'title', 'channel', 'length', 'published'].forEach(col => {
        const th = document.getElementById(`th-${col}`);
        if (!th) return;
        th.classList.remove('sort-asc', 'sort-desc');
        if (col === sortColumn) {
            th.classList.add(sortDirection === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    });

    // Re-render using current (possibly filtered) rows
    filterVideoTable();
}

function getSortedVideos(videos) {
    if (!sortColumn) return videos;
    const sorted = [...videos];
    sorted.sort((a, b) => {
        let va, vb;
        if (sortColumn === 'index') {
            // Sort by original position — use index in currentVideos
            va = currentVideos.indexOf(a);
            vb = currentVideos.indexOf(b);
        } else if (sortColumn === 'title') {
            va = (a.title || '').toLowerCase();
            vb = (b.title || '').toLowerCase();
        } else if (sortColumn === 'channel') {
            va = (a.channel || '').toLowerCase();
            vb = (b.channel || '').toLowerCase();
        } else if (sortColumn === 'length') {
            va = durationToSeconds(a.duration);
            vb = durationToSeconds(b.duration);
        } else if (sortColumn === 'published') {
            va = publishedRank(a.published);
            vb = publishedRank(b.published);
        }
        if (va < vb) return sortDirection === 'asc' ? -1 : 1;
        if (va > vb) return sortDirection === 'asc' ? 1 : -1;
        return 0;
    });
    return sorted;
}

function escapeHtml(text) {
    if (!text) return '';
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

async function moveVideoInline(selectEl, index, videoUrl) {
    const targetPlaylist = selectEl.value;
    if (!targetPlaylist) return;
    
    const sourcePlaylist = currentPlaylist ? currentPlaylist.name : '';
    if (!sourcePlaylist) {
        alert("Source playlist not found.");
        return;
    }
    
    if (!confirm(`Are you sure you want to move this video to '${targetPlaylist}'?`)) {
        selectEl.value = '';
        return;
    }
    
    // Replace dropdown with a spinner
    const container = selectEl.parentElement;
    const originalContent = container.innerHTML;
    container.innerHTML = `<span style="font-size: 0.85rem; color: var(--text-secondary);"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="animation: spin 1s linear infinite; margin-right: 6px; vertical-align: middle; display: inline-block;"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg>Moving...</span>`;
    
    try {
        const response = await fetch('/api/playlists/move-single', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                video_url: videoUrl,
                source_playlist: sourcePlaylist,
                target_playlist: targetPlaylist
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to move video.");
        }
        
        const data = await response.json();
        
        // Find the tr element
        const tr = document.getElementById(`video-row-${index}`);
        if (tr) {
            tr.classList.add('fade-out');
            setTimeout(() => {
                tr.remove();
                // Update cached count
                currentVideos = currentVideos.filter(v => v.url !== videoUrl);
                if (currentPlaylist) {
                    currentPlaylist.video_count = currentVideos.length;
                    document.getElementById('current-playlist-meta').textContent = `${currentVideos.length} videos cached`;
                }
            }, 500);
        }
        
        addConsoleLog(`[Client] Moved video inline to '${targetPlaylist}': ${videoUrl}`);
    } catch (err) {
        container.innerHTML = originalContent;
        addConsoleLog(`[Error] Inline move failed: ${err.message}`);
        alert(`Failed to move video: ${err.message}`);
    }
}

// ─── Video Table Rendering ───────────────────────────────────────────────────

function renderVideoTable(videos) {
    const tbody = document.getElementById('video-table-body');
    tbody.innerHTML = '';
    
    document.getElementById('select-all-videos').checked = false;
    
    if (!videos || videos.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; padding: 3rem; color: var(--text-secondary);">No videos in this playlist.</td></tr>';
        return;
    }
    
    const sourcePlaylistName = currentPlaylist ? currentPlaylist.name : '';
    let optionsHtml = '<option value="">Move to...</option>';
    allPlaylists.forEach(p => {
        if (p.name.toLowerCase() !== sourcePlaylistName.toLowerCase()) {
            optionsHtml += `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`;
        }
    });

    videos.forEach((v, index) => {
        const tr = document.createElement('tr');
        tr.className = 'video-row';
        tr.id = `video-row-${index}`;
        
        // Check if selected
        const isChecked = selectedVideoUrls.has(v.url);
        if (isChecked) {
            tr.classList.add('selected');
        }
        
        tr.innerHTML = `
            <td style="text-align: center;">
                <input type="checkbox" class="video-checkbox" data-url="${v.url}" ${isChecked ? 'checked' : ''} onclick="handleRowCheckboxClick(this, ${index}, event)">
            </td>
            <td style="text-align: center; color: var(--text-secondary); font-family: 'JetBrains Mono', monospace;">${index + 1}</td>
            <td>
                <a href="${v.url}" target="_blank" class="video-title-link">${v.title}</a>
            </td>
            <td style="color: var(--text-secondary);">${v.channel || 'Unknown'}</td>
            <td style="color: var(--text-secondary); font-family: 'JetBrains Mono', monospace; font-size: 0.85rem;">${v.duration || '--:--'}</td>
            <td style="color: var(--text-secondary);">${v.published || 'Unknown'}</td>
            <td style="text-align: center;">
                <a href="${v.url}" target="_blank" class="btn" style="padding: 6px 10px; font-size: 0.8rem; width: max-content; margin: 0 auto;">Watch</a>
            </td>
            <td style="text-align: center;">
                <select class="form-input inline-move-select" style="padding: 4px 8px; font-size: 0.85rem; width: 140px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); display: inline-block;" onchange="moveVideoInline(this, ${index}, '${v.url}')">
                    ${optionsHtml}
                </select>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

let lastCheckedIndex = null;

function handleRowCheckboxClick(checkbox, index, event) {
    const checkboxes = Array.from(document.querySelectorAll('#video-table-body .video-checkbox'));
    const clickedIdx = checkboxes.indexOf(checkbox);
    
    if (event && event.shiftKey && lastCheckedIndex !== null && lastCheckedIndex < checkboxes.length) {
        const start = Math.min(lastCheckedIndex, clickedIdx);
        const end = Math.max(lastCheckedIndex, clickedIdx);
        const targetCheckedState = checkbox.checked;
        
        for (let i = start; i <= end; i++) {
            const cb = checkboxes[i];
            cb.checked = targetCheckedState;
            const url = cb.getAttribute('data-url');
            const row = cb.closest('tr');
            
            if (targetCheckedState) {
                selectedVideoUrls.add(url);
                if (row) row.classList.add('selected');
            } else {
                selectedVideoUrls.delete(url);
                if (row) row.classList.remove('selected');
            }
        }
    } else {
        const url = checkbox.getAttribute('data-url');
        const row = checkbox.closest('tr');
        if (checkbox.checked) {
            selectedVideoUrls.add(url);
            if (row) row.classList.add('selected');
        } else {
            selectedVideoUrls.delete(url);
            if (row) row.classList.remove('selected');
        }
    }
    
    lastCheckedIndex = clickedIdx;
    updateSelectedCount();
}

function toggleSelectAll(masterCheckbox) {
    const isChecked = masterCheckbox.checked;
    const checkboxes = document.querySelectorAll('.video-checkbox');
    
    checkboxes.forEach(cb => {
        const row = cb.closest('tr');
        // Only toggle visible checkboxes (in case search is active)
        if (row && row.style.display !== 'none') {
            cb.checked = isChecked;
            const url = cb.getAttribute('data-url');
            if (isChecked) {
                selectedVideoUrls.add(url);
                row.classList.add('selected');
            } else {
                selectedVideoUrls.delete(url);
                row.classList.remove('selected');
            }
        }
    });
    
    updateSelectedCount();
}

function updateSelectedCount() {
    const count = selectedVideoUrls.size;
    const tag = document.getElementById('selected-count-tag');
    
    if (count > 0) {
        tag.textContent = `${count} Selected`;
        tag.style.display = 'inline-block';
        
        // Only enable Move if target playlist is selected
        const targetPlaylist = document.getElementById('target-playlist-select').value;
        document.getElementById('btn-batch-move').disabled = !targetPlaylist;
        document.getElementById('btn-batch-delete').disabled = false;
    } else {
        tag.style.display = 'none';
        document.getElementById('btn-batch-move').disabled = true;
        document.getElementById('btn-batch-delete').disabled = true;
    }
}

function filterVideoTable() {
    const query = document.getElementById('video-search').value.toLowerCase().trim();
    
    // Filter videos by search query
    let filtered = currentVideos;
    if (query) {
        filtered = currentVideos.filter(v => {
            const title = (v.title || '').toLowerCase();
            const channel = (v.channel || '').toLowerCase();
            return title.includes(query) || channel.includes(query);
        });
    }
    
    // Apply sort on top of filter
    const sorted = getSortedVideos(filtered);
    
    // Re-render the table with filtered+sorted videos
    renderVideoTable(sorted);
}

async function refreshPlaylistVideosLive() {
    if (!currentPlaylist) return;
    
    const tbody = document.getElementById('video-table-body');
    tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; padding: 3rem; color: var(--text-secondary);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="animation: spin 1s linear infinite; margin-right: 8px; vertical-align: middle;"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg>Fetching live videos from YouTube... This may take a moment.</td></tr>';
    
    const btn = document.getElementById('btn-refresh-live');
    if (btn) btn.disabled = true;
    
    try {
        const url = `/api/playlists/videos?playlist_url=${encodeURIComponent(currentPlaylist.url)}&refresh=true`;
        addConsoleLog(`[Client] Triggering live fetch for '${currentPlaylist.name}'...`);
        const response = await fetch(url);
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to fetch live videos");
        }
        const data = await response.json();
        currentVideos = data.videos || [];
        selectedVideoUrls.clear();
        updateSelectedCount();
        renderVideoTable(currentVideos);
        document.getElementById('current-playlist-meta').textContent = `${currentVideos.length} videos fetched live`;
        addConsoleLog(`[Client] Live fetch successful. Found ${currentVideos.length} videos.`);
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 3rem; color: var(--danger);">Failed to live fetch: ${err.message}</td></tr>`;
        addConsoleLog(`[Error] Live fetch fail: ${err.message}`);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function scanAndGenerateQueueForPlaylist() {
    if (!currentPlaylist) return;
    
    const tbody = document.getElementById('video-table-body');
    tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; padding: 3rem; color: var(--text-secondary);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="animation: spin 1s linear infinite; margin-right: 8px; vertical-align: middle;"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg>Scanning playlist and refreshing live from YouTube...</td></tr>';
    
    const scanBtn = document.getElementById('btn-scan-generate');
    if (scanBtn) {
        scanBtn.disabled = true;
        scanBtn.textContent = 'Scanning...';
    }
    
    try {
        // Step 1: Refresh playlist videos live
        const url = `/api/playlists/videos?playlist_url=${encodeURIComponent(currentPlaylist.url)}&refresh=true`;
        addConsoleLog(`[Client] Scanning playlist '${currentPlaylist.name}' live...`);
        const response = await fetch(url);
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to fetch live videos");
        }
        const data = await response.json();
        currentVideos = data.videos || [];
        selectedVideoUrls.clear();
        updateSelectedCount();
        renderVideoTable(currentVideos);
        document.getElementById('current-playlist-meta').textContent = `${currentVideos.length} videos scanned live`;
        addConsoleLog(`[Client] Scan completed. Found ${currentVideos.length} videos. Now regenerating maintenance queue...`);
        
        // Step 2: Generate maintenance queue actions
        const genResponse = await fetch('/api/maintenance/generate', { method: 'POST' });
        const genData = await genResponse.json();
        if (genData.success) {
            addConsoleLog(`[Client] Maintenance queue regeneration successfully started in the background.`);
            tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; padding: 3rem; color: var(--success);">Scan complete! Regenerating maintenance queue in the background. Check progress on the Dashboard.</td></tr>';
            // Reload the table with videos after a brief delay
            setTimeout(() => {
                renderVideoTable(currentVideos);
            }, 3000);
        } else {
            throw new Error(genData.detail || "Failed to start queue generation");
        }
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 3rem; color: var(--danger);">Failed to scan & generate: ${err.message}</td></tr>`;
        addConsoleLog(`[Error] Scan & Generate fail: ${err.message}`);
        // Restore table on fail after delay
        setTimeout(() => {
            renderVideoTable(currentVideos);
        }, 5000);
    } finally {
        if (scanBtn) {
            scanBtn.disabled = false;
            scanBtn.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"></path></svg>
                Scan & Generate Queue
            `;
        }
    }
}

async function removePlaylistDuplicates() {
    if (!currentPlaylist) return;
    
    // Find duplicate URLs locally first
    const urls = currentVideos.map(v => v.url);
    const uniqueUrls = new Set(urls);
    const duplicatesCount = urls.length - uniqueUrls.size;
    
    if (duplicatesCount === 0) {
        alert("No duplicates found in this playlist.");
        return;
    }
    
    if (!confirm(`Found ${duplicatesCount} duplicate video(s) in this playlist. Would you like the agent to clean them up in the background?`)) return;
    
    const btn = document.getElementById('btn-remove-duplicates');
    if (btn) btn.disabled = true;
    
    addConsoleLog(`[Client] Requesting duplicate cleanup for playlist: '${currentPlaylist.name}'...`);
    
    try {
        const response = await fetch('/api/playlists/remove-duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ playlist_name: currentPlaylist.name })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to start duplicate cleanup");
        }
        
        addConsoleLog(`[Client] Duplicate cleanup successfully started in background.`);
        loadStatus();
        startLogPolling();
    } catch (err) {
        addConsoleLog(`[Error] Failed to clean duplicates: ${err.message}`);
        alert(`Failed to clean duplicates: ${err.message}`);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function triggerBatchMove() {
    if (!currentPlaylist || selectedVideoUrls.size === 0) return;
    const targetPlaylist = document.getElementById('target-playlist-select').value;
    if (!targetPlaylist) {
        alert("Please select a target playlist.");
        return;
    }
    
    const count = selectedVideoUrls.size;
    const confirmMsg = `Are you sure you want to move ${count} selected video(s) from '${currentPlaylist.name}' to '${targetPlaylist}'?`;
    if (!confirm(confirmMsg)) return;
    
    const payload = {
        video_urls: Array.from(selectedVideoUrls),
        source_playlist: currentPlaylist.name,
        target_playlist: targetPlaylist
    };
    
    addConsoleLog(`[Client] Sending request to batch move ${count} video(s)...`);
    
    try {
        const response = await fetch('/api/playlists/batch-move', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to start batch move");
        }
        
        const data = await response.json();
        addConsoleLog(`[Client] Batch move queued successfully.`);
        
        // Clear selection
        selectedVideoUrls.clear();
        updateSelectedCount();
        
        loadStatus();
        startLogPolling();
    } catch (err) {
        addConsoleLog(`[Error] Batch move request failed: ${err.message}`);
        alert(`Failed to start batch move: ${err.message}`);
    }
}

async function triggerBatchDelete() {
    if (!currentPlaylist || selectedVideoUrls.size === 0) return;
    
    const count = selectedVideoUrls.size;
    const confirmMsg = `Are you sure you want to delete ${count} selected video(s) from '${currentPlaylist.name}'? This action can be rolled back via logs.`;
    if (!confirm(confirmMsg)) return;
    
    const payload = {
        video_urls: Array.from(selectedVideoUrls),
        playlist: currentPlaylist.name
    };
    
    addConsoleLog(`[Client] Sending request to batch delete ${count} video(s)...`);
    
    try {
        const response = await fetch('/api/playlists/batch-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to start batch delete");
        }
        
        const data = await response.json();
        addConsoleLog(`[Client] Batch delete queued successfully.`);
        
        // Clear selection
        selectedVideoUrls.clear();
        updateSelectedCount();
        
        loadStatus();
        startLogPolling();
    } catch (err) {
        addConsoleLog(`[Error] Batch delete request failed: ${err.message}`);
        alert(`Failed to start batch delete: ${err.message}`);
    }
}

// Settings
async function loadSettings() {
    try {
        const response = await fetch('/api/settings');
        if (response.ok) {
            const data = await response.json();
            document.getElementById('gemini-key').value = data.gemini_api_key || '';
            document.getElementById('webhook-url').value = data.notification_webhook || '';
            
            // Also update localStorage so they stay in sync
            if (data.gemini_api_key) localStorage.setItem('gemini_api_key', data.gemini_api_key);
            if (data.notification_webhook) localStorage.setItem('notification_webhook', data.notification_webhook);
        } else {
            document.getElementById('gemini-key').value = localStorage.getItem('gemini_api_key') || '';
            document.getElementById('webhook-url').value = localStorage.getItem('notification_webhook') || '';
        }
    } catch (err) {
        console.error("Failed to load settings from server:", err);
        document.getElementById('gemini-key').value = localStorage.getItem('gemini_api_key') || '';
        document.getElementById('webhook-url').value = localStorage.getItem('notification_webhook') || '';
    }
}

async function saveSettings() {
    const geminiKey = document.getElementById('gemini-key').value.trim();
    const webhook = document.getElementById('webhook-url').value.trim();
    
    localStorage.setItem('gemini_api_key', geminiKey);
    localStorage.setItem('notification_webhook', webhook);
    
    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                gemini_api_key: geminiKey,
                notification_webhook: webhook
            })
        });
        const data = await response.json();
        if (data.success) {
            alert("Settings saved successfully and synced with server!");
        } else {
            alert("Failed to sync settings with server.");
        }
    } catch (err) {
        console.error(err);
    }
}

// Screenshot Viewer
function viewDebugScreenshot(filename) {
    const modal = document.getElementById('screenshot-modal');
    const img = document.getElementById('modal-img');
    const title = document.getElementById('modal-title');
    
    img.src = `/api/screenshots/${filename}?t=${new Date().getTime()}`;
    title.textContent = filename;
    modal.classList.add('active');
}

function closeScreenshotModal() {
    document.getElementById('screenshot-modal').classList.remove('active');
}

// AI Classification History Modal
let aiClassifications = [];
let aiFilter = 'all';
let sortAIColumn = null;
let sortAIDirection = 'asc';

let selectedAICategoryVids = new Set();

let currentAIPlaylistFilter = '';

function openAIClassificationsModal() {
    document.getElementById('ai-classifications-modal').classList.add('active');
    
    // Reset sort and selection state
    sortAIColumn = null;
    sortAIDirection = 'asc';
    const th = document.getElementById('th-ai-category');
    if (th) th.classList.remove('sort-asc', 'sort-desc');
    
    currentAIPlaylistFilter = '';
    const playlistFilter = document.getElementById('ai-playlist-filter');
    if (playlistFilter) playlistFilter.value = '';
    
    selectedAICategoryVids.clear();
    updateAISelectedCount();
    
    loadAIClassifications();
}

function closeAIClassificationsModal() {
    document.getElementById('ai-classifications-modal').classList.remove('active');
}

async function loadAIClassifications() {
    const tbody = document.getElementById('ai-classifications-table-body');
    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; padding: 2rem; color: var(--text-secondary);">Loading classifications...</td></tr>';
    
    try {
        const response = await fetch('/api/ai-classifications');
        if (!response.ok) throw new Error("Failed to load classifications");
        const rawClassifications = await response.json();
        
        // Filter out pending suggestions that already match the current playlist
        aiClassifications = rawClassifications.filter(c => {
            if (c.status !== 'pending') return true;
            if (!c.current_playlist || !c.category) return true;
            return c.current_playlist.toLowerCase() !== c.category.toLowerCase();
        });
        
        // Update counts on filter tabs
        const total = aiClassifications.length;
        const pending = aiClassifications.filter(c => c.status === 'pending').length;
        const reviewed = total - pending;
        
        document.getElementById('ai-filter-all').textContent = `All (${total})`;
        document.getElementById('ai-filter-pending').textContent = `Pending (${pending})`;
        document.getElementById('ai-filter-reviewed').textContent = `Reviewed (${reviewed})`;
        
        // Ensure allPlaylists is loaded so we have dropdown categories
        if (allPlaylists.length === 0) {
            const plistResp = await fetch('/api/playlists');
            if (plistResp.ok) {
                allPlaylists = await plistResp.json();
            }
        }

        // Populate batch correct select dropdown
        populateAIBatchCorrectSelect();
        populateAIPlaylistFilter();
        
        renderAIClassifications();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; padding: 2rem; color: var(--danger);">Error: ${err.message}</td></tr>`;
    }
}

function populateAIBatchCorrectSelect() {
    const select = document.getElementById('ai-batch-correct-select');
    if (!select) return;
    select.innerHTML = '';
    
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = 'Correct Selected to...';
    select.appendChild(defaultOpt);
    
    allPlaylists.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.name;
        opt.textContent = p.name;
        select.appendChild(opt);
    });
}

function filterAIClassifications(filterType) {
    aiFilter = filterType;
    
    // Toggle active classes on filter buttons
    document.querySelectorAll('#ai-classifications-modal .tabs-nav button').forEach(btn => {
        btn.classList.remove('active');
    });
    const activeBtn = document.getElementById(`ai-filter-${filterType}`);
    if (activeBtn) activeBtn.classList.add('active');
    
    // Reset selection when changing filters
    selectedAICategoryVids.clear();
    updateAISelectedCount();
    
    renderAIClassifications();
}

function sortAIClassificationsTable(column) {
    if (sortAIColumn === column) {
        sortAIDirection = sortAIDirection === 'asc' ? 'desc' : 'asc';
    } else {
        sortAIColumn = column;
        sortAIDirection = 'asc';
    }
    
    const th = document.getElementById('th-ai-category');
    if (th) {
        th.classList.remove('sort-asc', 'sort-desc');
        if (sortAIColumn === 'category') {
            th.classList.add(sortAIDirection === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    }
    
    renderAIClassifications();
}

function populateAIPlaylistFilter() {
    const select = document.getElementById('ai-playlist-filter');
    if (!select) return;
    const currentVal = select.value;
    select.innerHTML = '<option value="">-- All Playlists --</option>';
    allPlaylists.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.name;
        opt.textContent = p.name;
        select.appendChild(opt);
    });
    select.value = currentVal || '';
}

function filterAIByPlaylist(val) {
    currentAIPlaylistFilter = val;
    renderAIClassifications();
}

function renderAIClassifications() {
    const tbody = document.getElementById('ai-classifications-table-body');
    tbody.innerHTML = '';
    
    // Reset Select All
    const masterCb = document.getElementById('select-all-ai');
    if (masterCb) masterCb.checked = false;
    
    let filtered = aiClassifications;
    if (aiFilter === 'pending') {
        filtered = aiClassifications.filter(c => c.status === 'pending');
    } else if (aiFilter === 'reviewed') {
        filtered = aiClassifications.filter(c => c.status === 'approved' || c.status === 'corrected');
    }
    
    if (currentAIPlaylistFilter) {
        filtered = filtered.filter(c => c.current_playlist && c.current_playlist.toLowerCase() === currentAIPlaylistFilter.toLowerCase());
    }
    
    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; padding: 3rem; color: var(--text-secondary);">No classifications found matching this filter.</td></tr>';
        return;
    }
    
    // Build options for playlist dropdown
    let categoryOptions = '<option value="">Correct to...</option>';
    allPlaylists.forEach(p => {
        categoryOptions += `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`;
    });
    
    // Sort classifications: category or timestamp
    let sorted = [...filtered];
    if (sortAIColumn === 'category') {
        sorted.sort((a, b) => {
            const catA = (a.category || '').toLowerCase();
            const catB = (b.category || '').toLowerCase();
            if (catA < catB) return sortAIDirection === 'asc' ? -1 : 1;
            if (catA > catB) return sortAIDirection === 'asc' ? 1 : -1;
            return 0;
        });
    } else {
        sorted.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
    }
    
    sorted.forEach((c, index) => {
        const tr = document.createElement('tr');
        tr.className = 'video-row';
        tr.id = `ai-row-${index}`;
        
        const isChecked = selectedAICategoryVids.has(c.vid);
        if (isChecked) {
            tr.classList.add('selected');
        }
        
        let statusBadge = '';
        if (c.status === 'pending') {
            statusBadge = `<span class="action-badge badge-misplaced" style="background: rgba(245, 158, 11, 0.15); color: var(--warning);">Pending</span>`;
        } else if (c.status === 'approved') {
            statusBadge = `<span class="action-badge badge-duplicate" style="background: rgba(16, 185, 129, 0.15); color: var(--success);">Approved</span>`;
        } else {
            statusBadge = `<span class="action-badge badge-duplicate" style="background: rgba(59, 130, 246, 0.15); color: var(--primary);">Corrected</span>`;
        }
        
        const vidUrl = `https://www.youtube.com/watch?v=${c.vid}`;
        const confidencePct = Math.round(c.confidence * 100) + '%';
        
        let controlsHtml = '';
        if (c.status === 'pending') {
            const escapedCategory = escapeHtml(c.category).replace(/'/g, "\\'");
            controlsHtml = `
                <div style="display: flex; gap: 6px; justify-content: center; align-items: center;">
                    <button class="btn btn-success" style="padding: 4px 10px; font-size: 0.8rem; margin: 0;" onclick="submitAIClassificationAction('${c.vid}', 'approve', '${escapedCategory}')">Approve</button>
                    <select class="form-input inline-move-select" style="padding: 4px 8px; font-size: 0.8rem; width: 110px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); margin: 0;" onchange="if(this.value) submitAIClassificationAction('${c.vid}', 'correct', this.value)">
                        ${categoryOptions}
                    </select>
                    <button class="btn" style="padding: 4px 10px; font-size: 0.8rem; margin: 0; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); color: var(--text-secondary);" onclick="submitAIClassificationAction('${c.vid}', 'skip')">Skip</button>
                    <button class="btn btn-danger" style="padding: 4px 10px; font-size: 0.8rem; margin: 0; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #f87171;" onclick="deleteAIClassification('${c.vid}')">Delete</button>
                </div>
            `;
        } else {
            controlsHtml = `
                <div style="display: flex; gap: 12px; justify-content: center; align-items: center;">
                    <span style="font-size: 0.8rem; color: var(--text-secondary);">Rule Pinned! Learned ✓</span>
                    <button class="btn" style="padding: 4px 10px; font-size: 0.8rem; margin: 0; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); color: var(--text-secondary);" onclick="submitAIClassificationAction('${c.vid}', 'skip')">Skip</button>
                    <button class="btn btn-danger" style="padding: 4px 10px; font-size: 0.8rem; margin: 0; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #f87171;" onclick="deleteAIClassification('${c.vid}')">Delete</button>
                </div>
            `;
        }
        
        tr.innerHTML = `
            <td style="text-align: center;">
                <input type="checkbox" class="ai-checkbox" data-vid="${c.vid}" ${isChecked ? 'checked' : ''} onclick="handleAIRowCheckboxClick(this, ${index}, event)">
            </td>
            <td>
                <div style="font-weight: 500; margin-bottom: 2px;">
                    <a href="${vidUrl}" target="_blank" class="video-title-link">${escapeHtml(c.title)}</a>
                </div>
                <div style="font-size: 0.8rem; color: var(--text-secondary);">${escapeHtml(c.channel)}</div>
                <div style="font-size: 0.8rem; color: var(--primary); margin-top: 2px;">Playlist: <strong>${escapeHtml(c.current_playlist || 'Unknown')}</strong></div>
            </td>
            <td style="font-weight: 500; color: var(--text-primary); font-size: 0.9rem;">${escapeHtml(c.category)}</td>
            <td style="text-align: center; font-family: monospace; font-size: 0.9rem; color: var(--text-secondary);">${confidencePct}</td>
            <td>${statusBadge}</td>
            <td style="text-align: center;">${controlsHtml}</td>
        `;
        tbody.appendChild(tr);
    });
}

async function deleteAIClassification(vid) {
    if (!confirm("Are you sure you want to delete this video from its playlist?")) return;
    try {
        const response = await fetch('/api/ai-classifications/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vids: [vid] })
        });
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to delete video");
        }
        addConsoleLog(`[Client] Queued deletion for video: ${vid}`);
        loadAIClassifications();
        loadStatus();
        startLogPolling();
    } catch (err) {
        console.error(err);
        alert(`Error: ${err.message}`);
    }
}

async function triggerAIBatchDelete() {
    const count = selectedAICategoryVids.size;
    if (count === 0) return;
    if (!confirm(`Are you sure you want to delete the ${count} selected video(s) from their playlists?`)) return;
    try {
        const response = await fetch('/api/ai-classifications/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vids: Array.from(selectedAICategoryVids) })
        });
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to batch delete videos");
        }
        addConsoleLog(`[Client] Queued batch deletion for ${count} videos.`);
        selectedAICategoryVids.clear();
        updateAISelectedCount();
        loadAIClassifications();
        loadStatus();
        startLogPolling();
    } catch (err) {
        console.error(err);
        alert(`Error: ${err.message}`);
    }
}

let lastAICheckedIndex = null;

function handleAIRowCheckboxClick(checkbox, index, event) {
    const checkboxes = Array.from(document.querySelectorAll('#ai-classifications-table-body .ai-checkbox'));
    const clickedIdx = checkboxes.indexOf(checkbox);
    
    if (event && event.shiftKey && lastAICheckedIndex !== null && lastAICheckedIndex < checkboxes.length) {
        const start = Math.min(lastAICheckedIndex, clickedIdx);
        const end = Math.max(lastAICheckedIndex, clickedIdx);
        const targetCheckedState = checkbox.checked;
        
        for (let i = start; i <= end; i++) {
            const cb = checkboxes[i];
            cb.checked = targetCheckedState;
            const vid = cb.getAttribute('data-vid');
            const row = cb.closest('tr');
            
            if (targetCheckedState) {
                selectedAICategoryVids.add(vid);
                if (row) row.classList.add('selected');
            } else {
                selectedAICategoryVids.delete(vid);
                if (row) row.classList.remove('selected');
            }
        }
    } else {
        const vid = checkbox.getAttribute('data-vid');
        const row = checkbox.closest('tr');
        if (checkbox.checked) {
            selectedAICategoryVids.add(vid);
            if (row) row.classList.add('selected');
        } else {
            selectedAICategoryVids.delete(vid);
            if (row) row.classList.remove('selected');
        }
    }
    
    lastAICheckedIndex = clickedIdx;
    updateAISelectedCount();
}

function toggleSelectAllAI(masterCheckbox) {
    const isChecked = masterCheckbox.checked;
    const checkboxes = document.querySelectorAll('.ai-checkbox');
    
    checkboxes.forEach(cb => {
        const row = cb.closest('tr');
        if (row && row.style.display !== 'none') {
            cb.checked = isChecked;
            const vid = cb.getAttribute('data-vid');
            if (isChecked) {
                selectedAICategoryVids.add(vid);
                row.classList.add('selected');
            } else {
                selectedAICategoryVids.delete(vid);
                row.classList.remove('selected');
            }
        }
    });
    
    updateAISelectedCount();
}

function updateAISelectedCount() {
    const count = selectedAICategoryVids.size;
    const tag = document.getElementById('ai-selected-count-tag');
    const actionsGroup = document.getElementById('ai-batch-actions-group');
    
    if (count > 0) {
        if (tag) {
            tag.textContent = `${count} Selected`;
        }
        if (actionsGroup) {
            actionsGroup.style.display = 'flex';
        }
    } else {
        if (actionsGroup) {
            actionsGroup.style.display = 'none';
        }
    }
}

async function submitAIClassificationAction(vid, action, category = '') {
    if (!vid || !action || (!category && action !== 'skip')) return;
    
    try {
        const response = await fetch('/api/ai-classifications/action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                vid: vid,
                action: action,
                category: category
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to process action");
        }
        
        if (action === "skip") {
            aiClassifications = aiClassifications.filter(c => c.vid !== vid);
        } else {
            const item = aiClassifications.find(c => c.vid === vid);
            if (item) {
                if (action === "approve") {
                    item.status = "approved";
                } else if (action === "correct") {
                    item.status = "corrected";
                    item.category = category;
                }
            }
        }
        
        selectedAICategoryVids.delete(vid);
        updateAISelectedCount();
        
        renderAIClassifications();
        loadStatus();
    } catch (err) {
        alert(`Error: ${err.message}`);
    }
}

async function submitAIClassificationBatch(action, category = '') {
    const count = selectedAICategoryVids.size;
    if (count === 0) return;
    
    let confirmMsg = '';
    if (action === 'approve') {
        confirmMsg = `Are you sure you want to approve ${count} selected AI classifications?`;
    } else if (action === 'correct') {
        confirmMsg = `Are you sure you want to correct ${count} selected AI classifications to '${category}'?`;
    } else if (action === 'skip') {
        confirmMsg = `Are you sure you want to skip/remove ${count} selected AI classifications?`;
    }
        
    if (!confirm(confirmMsg)) {
        if (action === 'correct') {
            document.getElementById('ai-batch-correct-select').value = '';
        }
        return;
    }
    
    try {
        const response = await fetch('/api/ai-classifications/batch-action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                vids: Array.from(selectedAICategoryVids),
                action: action,
                category: category
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to process batch action");
        }
        
        selectedAICategoryVids.clear();
        updateAISelectedCount();
        
        if (action === 'correct') {
            document.getElementById('ai-batch-correct-select').value = '';
        }
        
        await loadAIClassifications();
        loadStatus();
    } catch (err) {
        alert(`Error: ${err.message}`);
    }
}

// Tracked Videos Modal functionality
let allTrackedVideos = [];
let selectedTrackedUids = new Set(); // Stores "url|||playlist"
let sortTrackedColumn = null;
let sortTrackedDirection = 'asc';

async function openTrackedVideosModal() {
    const modal = document.getElementById('tracked-videos-modal');
    if (!modal) return;
    
    modal.classList.add('active');
    
    // Clear search input and selection state
    document.getElementById('tracked-video-search').value = '';
    selectedTrackedUids.clear();
    updateTrackedSelectedCount();
    
    // Reset sort state
    sortTrackedColumn = null;
    sortTrackedDirection = 'asc';
    ['index', 'title', 'channel', 'playlist', 'published'].forEach(col => {
        const th = document.getElementById(`th-tracked-${col}`);
        if (th) th.classList.remove('sort-asc', 'sort-desc');
    });
    
    // Show loading state
    const tbody = document.getElementById('tracked-videos-table-body');
    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 3rem; color: var(--text-secondary);">Loading tracked videos...</td></tr>';
    
    try {
        // Fetch latest playlists to extract videos
        const response = await fetch('/api/playlists');
        if (!response.ok) throw new Error("Playlists fetch failed");
        allPlaylists = await response.json();
        
        // Flatten videos across all playlists
        allTrackedVideos = [];
        allPlaylists.forEach(p => {
            const playlistName = p.name;
            const videos = p.videos || [];
            videos.forEach(v => {
                allTrackedVideos.push({
                    title: v.title || 'Unknown Video',
                    url: v.url,
                    channel: v.channel || 'Unknown',
                    published: v.published || 'Unknown',
                    playlist: playlistName
                });
            });
        });
        
        // Sort alphabetically by title by default
        allTrackedVideos.sort((a, b) => a.title.localeCompare(b.title));
        
        // Populate batch target dropdown
        populateTrackedTargetPlaylists();
        
        renderTrackedVideosTable(allTrackedVideos);
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 3rem; color: var(--danger);">Failed to load tracked videos: ${err.message}</td></tr>`;
    }
}

function populateTrackedTargetPlaylists() {
    const select = document.getElementById('tracked-target-select');
    if (!select) return;
    select.innerHTML = '';
    
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = '-- Select Playlist --';
    select.appendChild(defaultOpt);
    
    allPlaylists.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.name;
        opt.textContent = p.name;
        select.appendChild(opt);
    });
}

function closeTrackedVideosModal() {
    const modal = document.getElementById('tracked-videos-modal');
    if (modal) {
        modal.classList.remove('active');
    }
}

function renderTrackedVideosTable(videos) {
    const tbody = document.getElementById('tracked-videos-table-body');
    tbody.innerHTML = '';
    
    // Reset Select All
    const masterCb = document.getElementById('select-all-tracked');
    if (masterCb) masterCb.checked = false;
    
    // Update badge count
    const badge = document.getElementById('tracked-videos-count-badge');
    if (badge) {
        badge.textContent = `${videos.length} Videos`;
    }
    
    if (!videos || videos.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; padding: 3rem; color: var(--text-secondary);">No videos found.</td></tr>';
        return;
    }
    
    videos.forEach((v, index) => {
        const tr = document.createElement('tr');
        tr.className = 'video-row';
        tr.id = `tracked-row-${index}`;
        
        const uid = `${v.url}|||${v.playlist}`;
        const isChecked = selectedTrackedUids.has(uid);
        if (isChecked) {
            tr.classList.add('selected');
        }
        
        let optionsHtml = `<option value="">Move from ${escapeHtml(v.playlist)} to...</option>`;
        allPlaylists.forEach(p => {
            if (p.name.toLowerCase() !== v.playlist.toLowerCase()) {
                optionsHtml += `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`;
            }
        });
        
        tr.innerHTML = `
            <td style="text-align: center;">
                <input type="checkbox" class="tracked-video-checkbox" data-uid="${escapeHtml(uid)}" ${isChecked ? 'checked' : ''} onclick="handleTrackedRowCheckboxClick(this, ${index}, event)">
            </td>
            <td style="text-align: center; color: var(--text-secondary); font-family: 'JetBrains Mono', monospace;">${index + 1}</td>
            <td>
                <a href="${v.url}" target="_blank" class="video-title-link">${escapeHtml(v.title)}</a>
            </td>
            <td style="color: var(--text-secondary);">${escapeHtml(v.channel)}</td>
            <td style="color: var(--text-secondary); font-family: 'JetBrains Mono', monospace; font-size: 0.85rem;">${escapeHtml(v.duration || '--:--')}</td>
            <td>
                <div class="inline-move-container">
                    <select class="form-input inline-move-select" style="padding: 4px 8px; font-size: 0.85rem; width: 160px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); display: inline-block;" onchange="moveTrackedVideoInline(this, '${v.url}', '${escapeHtml(v.playlist)}', ${index})">
                        ${optionsHtml}
                    </select>
                </div>
            </td>
            <td style="color: var(--text-secondary);">${escapeHtml(v.published)}</td>
            <td style="text-align: center;">
                <a href="${v.url}" target="_blank" class="btn" style="padding: 6px 10px; font-size: 0.8rem; width: max-content; margin: 0 auto;">Watch</a>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

let lastTrackedCheckedIndex = null;

function handleTrackedRowCheckboxClick(checkbox, index, event) {
    const checkboxes = Array.from(document.querySelectorAll('#tracked-videos-table-body .tracked-video-checkbox'));
    const clickedIdx = checkboxes.indexOf(checkbox);
    
    if (event && event.shiftKey && lastTrackedCheckedIndex !== null && lastTrackedCheckedIndex < checkboxes.length) {
        const start = Math.min(lastTrackedCheckedIndex, clickedIdx);
        const end = Math.max(lastTrackedCheckedIndex, clickedIdx);
        const targetCheckedState = checkbox.checked;
        
        for (let i = start; i <= end; i++) {
            const cb = checkboxes[i];
            cb.checked = targetCheckedState;
            const uid = cb.getAttribute('data-uid');
            const row = cb.closest('tr');
            
            if (targetCheckedState) {
                selectedTrackedUids.add(uid);
                if (row) row.classList.add('selected');
            } else {
                selectedTrackedUids.delete(uid);
                if (row) row.classList.remove('selected');
            }
        }
    } else {
        const uid = checkbox.getAttribute('data-uid');
        const row = checkbox.closest('tr');
        if (checkbox.checked) {
            selectedTrackedUids.add(uid);
            if (row) row.classList.add('selected');
        } else {
            selectedTrackedUids.delete(uid);
            if (row) row.classList.remove('selected');
        }
    }
    
    lastTrackedCheckedIndex = clickedIdx;
    updateTrackedSelectedCount();
}

function toggleSelectAllTracked(masterCheckbox) {
    const isChecked = masterCheckbox.checked;
    const checkboxes = document.querySelectorAll('.tracked-video-checkbox');
    
    checkboxes.forEach(cb => {
        const row = cb.closest('tr');
        if (row && row.style.display !== 'none') {
            cb.checked = isChecked;
            const uid = cb.getAttribute('data-uid');
            if (isChecked) {
                selectedTrackedUids.add(uid);
                row.classList.add('selected');
            } else {
                selectedTrackedUids.delete(uid);
                row.classList.remove('selected');
            }
        }
    });
    
    updateTrackedSelectedCount();
}

function updateTrackedSelectedCount() {
    const count = selectedTrackedUids.size;
    const tag = document.getElementById('tracked-selected-count-tag');
    const actionsGroup = document.getElementById('tracked-batch-actions-group');
    
    if (count > 0) {
        if (tag) {
            tag.textContent = `${count} Selected`;
            tag.style.display = 'inline-block';
        }
        if (actionsGroup) {
            actionsGroup.style.display = 'flex';
        }
        
        const targetPlaylist = document.getElementById('tracked-target-select').value;
        const moveBtn = document.getElementById('btn-tracked-batch-move');
        if (moveBtn) moveBtn.disabled = !targetPlaylist;
    } else {
        if (tag) tag.style.display = 'none';
        if (actionsGroup) actionsGroup.style.display = 'none';
    }
}

async function triggerTrackedBatchMove() {
    const count = selectedTrackedUids.size;
    if (count === 0) return;
    
    const targetPlaylist = document.getElementById('tracked-target-select').value;
    if (!targetPlaylist) {
        alert("Please select a target playlist.");
        return;
    }
    
    if (!confirm(`Are you sure you want to move the selected ${count} video(s) to '${targetPlaylist}'?`)) return;
    
    const items = [];
    selectedTrackedUids.forEach(uid => {
        const parts = uid.split('|||');
        items.push({
            video_url: parts[0],
            source_playlist: parts[1]
        });
    });
    
    addConsoleLog(`[Client] Sending request to multi-source batch move ${count} video(s) to '${targetPlaylist}'...`);
    
    try {
        const response = await fetch('/api/playlists/batch-move-multi-source', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                items: items,
                target_playlist: targetPlaylist
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to start multi-source batch move");
        }
        
        closeTrackedVideosModal();
        switchTab('dashboard');
        loadStatus();
        startLogPolling();
    } catch (err) {
        alert(`Failed to move batch: ${err.message}`);
    }
}

async function triggerTrackedBatchDelete() {
    const count = selectedTrackedUids.size;
    if (count === 0) return;
    
    if (!confirm(`Are you sure you want to delete the selected ${count} video(s) from their playlists?`)) return;
    
    const items = [];
    selectedTrackedUids.forEach(uid => {
        const parts = uid.split('|||');
        items.push({
            video_url: parts[0],
            source_playlist: parts[1]
        });
    });
    
    addConsoleLog(`[Client] Sending request to multi-source batch delete ${count} video(s)...`);
    
    try {
        const response = await fetch('/api/playlists/batch-delete-multi-source', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                items: items
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to start multi-source batch delete");
        }
        
        closeTrackedVideosModal();
        switchTab('dashboard');
        loadStatus();
        startLogPolling();
    } catch (err) {
        alert(`Failed to delete batch: ${err.message}`);
    }
}

async function moveTrackedVideoInline(selectEl, videoUrl, sourcePlaylist, index) {
    const targetPlaylist = selectEl.value;
    if (!targetPlaylist) return;
    
    if (!confirm(`Are you sure you want to move this video from '${sourcePlaylist}' to '${targetPlaylist}'?`)) {
        selectEl.value = '';
        return;
    }
    
    // Replace dropdown with a spinner
    const container = selectEl.parentElement;
    const originalContent = container.innerHTML;
    container.innerHTML = `<span style="font-size: 0.85rem; color: var(--text-secondary);"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="animation: spin 1s linear infinite; margin-right: 6px; vertical-align: middle; display: inline-block;"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg>Moving...</span>`;
    
    try {
        const response = await fetch('/api/playlists/move-single', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                video_url: videoUrl,
                source_playlist: sourcePlaylist,
                target_playlist: targetPlaylist
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || "Failed to move video.");
        }
        
        // Update local state in allTrackedVideos
        const videoItem = allTrackedVideos.find(v => v.url === videoUrl && v.playlist === sourcePlaylist);
        if (videoItem) {
            videoItem.playlist = targetPlaylist;
        }
        
        // Re-render the container dropdown with the new source and update options
        let optionsHtml = `<option value="">Move from ${escapeHtml(targetPlaylist)} to...</option>`;
        allPlaylists.forEach(p => {
            if (p.name.toLowerCase() !== targetPlaylist.toLowerCase()) {
                optionsHtml += `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`;
            }
        });
        
        container.innerHTML = `
            <select class="form-input inline-move-select" style="padding: 4px 8px; font-size: 0.85rem; width: 160px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); display: inline-block;" onchange="moveTrackedVideoInline(this, '${videoUrl}', '${escapeHtml(targetPlaylist)}', ${index})">
                ${optionsHtml}
            </select>
        `;
        
        addConsoleLog(`[Client] Moved video from '${sourcePlaylist}' to '${targetPlaylist}': ${videoUrl}`);
    } catch (err) {
        container.innerHTML = originalContent;
        addConsoleLog(`[Error] Inline move failed: ${err.message}`);
        alert(`Failed to move video: ${err.message}`);
    }
}

function sortTrackedTable(column) {
    if (sortTrackedColumn === column) {
        sortTrackedDirection = sortTrackedDirection === 'asc' ? 'desc' : 'asc';
    } else {
        sortTrackedColumn = column;
        sortTrackedDirection = 'asc';
    }

    // Update header classes
    ['index', 'title', 'channel', 'length', 'playlist', 'published'].forEach(col => {
        const th = document.getElementById(`th-tracked-${col}`);
        if (!th) return;
        th.classList.remove('sort-asc', 'sort-desc');
        if (col === sortTrackedColumn) {
            th.classList.add(sortTrackedDirection === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    });

    filterTrackedVideos();
}

function getSortedTrackedVideos(videos) {
    if (!sortTrackedColumn) return videos;
    const sorted = [...videos];
    sorted.sort((a, b) => {
        let va, vb;
        if (sortTrackedColumn === 'index') {
            va = allTrackedVideos.indexOf(a);
            vb = allTrackedVideos.indexOf(b);
        } else if (sortTrackedColumn === 'title') {
            va = (a.title || '').toLowerCase();
            vb = (b.title || '').toLowerCase();
        } else if (sortTrackedColumn === 'channel') {
            va = (a.channel || '').toLowerCase();
            vb = (b.channel || '').toLowerCase();
        } else if (sortTrackedColumn === 'length') {
            va = durationToSeconds(a.duration);
            vb = durationToSeconds(b.duration);
        } else if (sortTrackedColumn === 'playlist') {
            va = (a.playlist || '').toLowerCase();
            vb = (b.playlist || '').toLowerCase();
        } else if (sortTrackedColumn === 'published') {
            va = publishedRank(a.published);
            vb = publishedRank(b.published);
        }
        if (va < vb) return sortTrackedDirection === 'asc' ? -1 : 1;
        if (va > vb) return sortTrackedDirection === 'asc' ? 1 : -1;
        return 0;
    });
    return sorted;
}

function filterTrackedVideos() {
    const query = document.getElementById('tracked-video-search').value.toLowerCase().trim();
    let filtered = allTrackedVideos;
    if (query) {
        filtered = allTrackedVideos.filter(v => {
            return (v.title || '').toLowerCase().includes(query) ||
                   (v.channel || '').toLowerCase().includes(query) ||
                   (v.playlist || '').toLowerCase().includes(query);
        });
    }
    
    const sorted = getSortedTrackedVideos(filtered);
    renderTrackedVideosTable(sorted);
}

let currentUser = null;

async function checkSession() {
    try {
        const response = await fetch('/api/auth/session');
        if (response.status === 401) {
            document.getElementById('login-container').style.display = 'flex';
            document.getElementById('app-container').style.display = 'none';
            return false;
        }
        
        const data = await response.json();
        currentUser = data;
        
        if (data.logged_in || data.local_mode) {
            document.getElementById('login-container').style.display = 'none';
            document.getElementById('app-container').style.display = 'block';
            
            const profileDiv = document.getElementById('user-profile');
            if (data.email) {
                document.getElementById('user-name').innerText = data.name || 'User';
                document.getElementById('user-email').innerText = data.email;
                profileDiv.style.display = 'flex';
            } else {
                profileDiv.style.display = 'none';
            }
            return true;
        } else {
            document.getElementById('login-container').style.display = 'flex';
            document.getElementById('app-container').style.display = 'none';
            return false;
        }
    } catch (e) {
        console.error("Session check failed", e);
        return false;
    }
}

async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.reload();
    } catch (e) {
        console.error("Logout failed", e);
    }
}

// Initialize
window.addEventListener('load', async () => {
    const authenticated = await checkSession();
    if (authenticated) {
        loadStatus();
        loadSettings();
        statusInterval = setInterval(loadStatus, 4000);
        
        const select = document.getElementById('target-playlist-select');
        if (select) {
            select.addEventListener('change', updateSelectedCount);
        }
    }
});
