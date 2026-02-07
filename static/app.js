// Config management
const CONFIG_KEY = 'aigpic.config_name';
let availableConfigs = [];
let defaultConfigName = '';

let currentPage = 1;
const pageSize = 16;
let ws = null;
let isGenerating = false;
let taskTimer = null;
let previewImages = [];
let previewIndex = -1;
const NOTICE_DURATION = 3000;

// Video modal state
let selectedImageId = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    loadImages();
    connectWebSocket();
    loadConfigOptions();
});

// Event Listeners
function setupEventListeners() {
    document.getElementById('configSelect').addEventListener('change', (event) => {
        persistConfigSelection(event.target.value);
    });

    // Generate button
    document.getElementById('generateBtn').addEventListener('click', generateImages);

    // Preview navigation
    document.getElementById('previewPrev').addEventListener('click', (event) => {
        event.stopPropagation();
        showPrevImage();
    });
    document.getElementById('previewNext').addEventListener('click', (event) => {
        event.stopPropagation();
        showNextImage();
    });

    // Pagination
    document.getElementById('prevPage').addEventListener('click', () => changePage(-1));
    document.getElementById('nextPage').addEventListener('click', () => changePage(1));

    // Modal close buttons
    document.querySelectorAll('.close').forEach(btn => {
        btn.addEventListener('click', closeModals);
    });

    // Close modal when clicking outside
    window.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal')) {
            closeModals();
        }
    });

    // Video config modal
    document.getElementById('generateVideoBtn').addEventListener('click', generateVideo);
}

// Config functions
async function loadConfigOptions() {
    try {
        const response = await fetch('/api/configs');
        if (!response.ok) {
            throw new Error('Failed to load config list');
        }

        const data = await response.json();
        availableConfigs = Array.isArray(data.configs) ? data.configs : [];
        defaultConfigName = data.default || (availableConfigs[0] ? availableConfigs[0].name : '');

        const storedName = getStoredConfigName();
        const selectedName = availableConfigs.some(config => config.name === storedName)
            ? storedName
            : defaultConfigName;

        setConfigOptions(availableConfigs, selectedName);
    } catch (error) {
        console.error('Failed to load configs:', error);
        setConfigOptions([], '');
    }
}

function setConfigOptions(configs, selectedName) {
    const select = document.getElementById('configSelect');
    select.innerHTML = configs.length
        ? configs.map(config => `<option value="${config.name}">${config.name}</option>`).join('')
        : '<option value="">No configs available</option>';

    if (selectedName) {
        select.value = selectedName;
        persistConfigSelection(selectedName);
    }
}

function getStoredConfigName() {
    return localStorage.getItem(CONFIG_KEY);
}

function getSelectedConfigName() {
    const select = document.getElementById('configSelect');
    if (select && select.value) {
        return select.value;
    }
    return getStoredConfigName() || '';
}

function persistConfigSelection(name) {
    if (name) {
        localStorage.setItem(CONFIG_KEY, name);
    }
}

function getSelectedRatio() {
    const selected = document.querySelector('input[name="ratio"]:checked');
    return selected ? selected.value : 'default';
}

function showNotice(message, options = {}) {
    const container = document.getElementById('noticeContainer');
    if (!container) {
        return;
    }

    const notice = document.createElement('div');
    notice.className = 'notice';

    const text = document.createElement('span');
    text.className = 'notice-text';
    text.textContent = message;

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'notice-close';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', () => removeNotice(notice));

    notice.appendChild(text);
    notice.appendChild(closeBtn);
    container.appendChild(notice);

    if (options.autoClose !== false) {
        const duration = Number.isFinite(options.duration) ? options.duration : NOTICE_DURATION;
        if (duration > 0) {
            notice._timeoutId = setTimeout(() => removeNotice(notice), duration);
        }
    }
}

function removeNotice(notice) {
    if (!notice) {
        return;
    }
    if (notice._timeoutId) {
        clearTimeout(notice._timeoutId);
    }
    notice.remove();
}

function ensureTaskTimer() {
    if (taskTimer) {
        return;
    }
    taskTimer = setInterval(updateTaskTimers, 1000);
}

function updateTaskTimers() {
    const items = document.querySelectorAll('.task-item');
    items.forEach(item => {
        const status = item.dataset.status;
        const statusEl = item.querySelector('.task-status');
        if (!statusEl) {
            return;
        }

        const startedAt = item.dataset.startedAt || item.dataset.createdAt;
        const finishedAt = item.dataset.finishedAt;
        const duration = getDurationSeconds(startedAt, status === 'running' ? null : finishedAt);
        const durationText = duration !== null ? `${duration}s` : '';

        if (status === 'running') {
            statusEl.textContent = durationText ? `Generating (${durationText})` : 'Generating';
        } else if (status === 'succeeded') {
            statusEl.textContent = durationText ? `Completed (${durationText})` : 'Completed';
        } else if (status === 'failed') {
            statusEl.textContent = durationText ? `Failed (${durationText})` : 'Failed';
        } else if (status === 'queued') {
            statusEl.textContent = 'Queued';
        }
    });
}

function getDurationSeconds(startedAt, finishedAt) {
    if (!startedAt) {
        return null;
    }
    const startTime = Date.parse(startedAt);
    if (Number.isNaN(startTime)) {
        return null;
    }
    const endTime = finishedAt ? Date.parse(finishedAt) : Date.now();
    if (Number.isNaN(endTime)) {
        return null;
    }
    return Math.max(0, Math.floor((endTime - startTime) / 1000));
}

function closeModals() {
    document.querySelectorAll('.modal').forEach(modal => {
        modal.style.display = 'none';
    });
}

// WebSocket connection
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/tasks`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('WebSocket connected');
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === 'initial_tasks') {
            renderTasks(data.tasks);
        } else if (data.type === 'task_update') {
            updateTask(data.task);
        }
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected, reconnecting...');
        setTimeout(connectWebSocket, 3000);
    };
}

async function updateTask(task) {
    // Fetch latest tasks from server
    try {
        const response = await fetch('/api/tasks');
        if (response.ok) {
            const tasks = await response.json();
            renderTasks(tasks);
        }
    } catch (error) {
        console.error('Failed to fetch tasks:', error);
    }

    // If task succeeded, refresh images
    if (task.status === 'succeeded') {
        loadImages();
    }
}

// Generate images
async function generateImages() {
    if (isGenerating) return;

    const prompt = document.getElementById('prompt').value.trim();
    const count = parseInt(document.getElementById('count').value);
    const configName = getSelectedConfigName();
    const ratio = getSelectedRatio();

    // Validation
    if (!prompt) {
        showNotice('Please enter a prompt');
        return;
    }

    if (!configName) {
        showNotice('Please select a config');
        const select = document.getElementById('configSelect');
        if (select) {
            select.focus();
        }
        return;
    }

    if (count < 1 || count > 10) {
        showNotice('Image count must be between 1 and 10');
        return;
    }

    // Set button to loading state
    const btn = document.getElementById('generateBtn');
    const originalContent = btn.innerHTML;
    btn.innerHTML = '<span class="btn-label">Loading...</span>';
    btn.disabled = true;
    isGenerating = true;

    const promptWithRatio = ratio === 'default' ? prompt : `${prompt} Aspect Ratio ${ratio}`;

    try {
        const response = await fetch('/api/tasks', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                prompt: promptWithRatio,
                n: count,
                config_name: configName
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create task');
        }

        const data = await response.json();
        console.log(`Task created: ${data.task_id}`);

        // Don't clear prompt - keep it for user

    } catch (error) {
        showNotice(`Error: ${error.message}`);
    } finally {
        // Restore button state
        btn.innerHTML = originalContent;
        btn.disabled = false;
        isGenerating = false;
    }
}

// Task management
function renderTasks(tasks) {
    const container = document.getElementById('tasksList');

    if (!tasks || tasks.length === 0) {
        container.innerHTML = '<p style="color: #999;">No tasks</p>';
        return;
    }

    // Keep only last 10 tasks, with running tasks first
    const sortedTasks = tasks.slice(0, 10);

    container.innerHTML = sortedTasks.map(task => {
        const statusText = getTaskStatusText(task);

        // Limit prompt to 10 characters
        const displayPrompt = formatTaskPrompt(task.prompt);
        const shortPrompt = displayPrompt.length > 10
            ? displayPrompt.substring(0, 10) + '...'
            : displayPrompt;

        const startedAt = task.started_at || '';
        const finishedAt = task.finished_at || '';
        const createdAt = task.created_at || '';

        return `
            <div class="task-item ${task.status}" data-status="${task.status}" data-started-at="${startedAt}" data-finished-at="${finishedAt}" data-created-at="${createdAt}">
                <span class="task-prompt">${shortPrompt}</span>
                <span class="task-status">${statusText}</span>
            </div>
        `;
    }).join('');

    ensureTaskTimer();
    updateTaskTimers();
}

function getTaskStatusText(task) {
    const duration = getDurationSeconds(
        task.started_at || task.created_at,
        task.status === 'running' ? null : task.finished_at
    );
    const durationText = duration !== null ? `${duration}s` : '';

    if (task.status === 'running') {
        return durationText ? `Generating (${durationText})` : 'Generating';
    }
    if (task.status === 'succeeded') {
        return durationText ? `Completed (${durationText})` : 'Completed';
    }
    if (task.status === 'failed') {
        return durationText ? `Failed (${durationText})` : 'Failed';
    }
    if (task.status === 'queued') {
        return 'Queued';
    }
    return task.status || '';
}

function formatTaskPrompt(prompt) {
    if (!prompt) {
        return '';
    }
    return prompt.replace(/\s+Aspect Ratio (9:16|16:9|4:3|1:1)$/, '');
}

// Image management
async function loadImages() {
    try {
        const response = await fetch(`/api/images?page=${currentPage}&page_size=${pageSize}`);
        if (!response.ok) return;

        const data = await response.json();
        renderImages(data);
    } catch (error) {
        console.error('Failed to load images:', error);
    }
}

function renderImages(data) {
    const grid = document.getElementById('imagesGrid');
    const pageInfo = document.getElementById('pageInfo');

    if (data.items.length === 0) {
        grid.innerHTML = '<p style="color: #999; grid-column: 1/-1; text-align: center;">No images</p>';
        pageInfo.textContent = '0 / 0';
        previewImages = [];
        previewIndex = -1;
        return;
    }

    // Filter only image files for preview navigation
    previewImages = data.items.filter(img => !isVideoFile(img.filename)).map(img => img.url);

    grid.innerHTML = data.items.map(img => {
        if (isVideoFile(img.filename)) {
            // Video item
            return `
                <div class="video-item">
                    <span class="video-badge">VIDEO</span>
                    <video src="${img.url}" controls muted></video>
                    <div class="image-actions">
                        <button class="action-btn" onclick="event.stopPropagation(); showPrompt(${img.id})" title="View Prompt">
                            ℹ️
                        </button>
                        <button class="action-btn" onclick="event.stopPropagation(); deleteImage(${img.id})" title="Delete">
                            🗑️
                        </button>
                    </div>
                </div>
            `;
        } else {
            // Image item with video conversion button
            return `
                <div class="image-item" onclick="previewImage('${img.url}')">
                    <img src="${img.url}" alt="Generated image">
                    <div class="image-actions">
                        <button class="action-btn-video" onclick="event.stopPropagation(); showVideoConfig(${img.id})" title="Convert to Video">
                            🎬
                        </button>
                        <button class="action-btn" onclick="event.stopPropagation(); showPrompt(${img.id})" title="View Prompt">
                            ℹ️
                        </button>
                        <button class="action-btn" onclick="event.stopPropagation(); deleteImage(${img.id})" title="Delete">
                            🗑️
                        </button>
                    </div>
                </div>
            `;
        }
    }).join('');

    const totalPages = Math.ceil(data.total / pageSize);
    pageInfo.textContent = `${currentPage} / ${totalPages}`;

    document.getElementById('prevPage').disabled = currentPage === 1;
    document.getElementById('nextPage').disabled = currentPage >= totalPages;
}

function changePage(delta) {
    currentPage += delta;
    if (currentPage < 1) currentPage = 1;
    loadImages();
}

function previewImage(url) {
    document.getElementById('previewImage').src = url;
    document.getElementById('previewModal').style.display = 'block';
    previewIndex = previewImages.indexOf(url);
}

function showPrevImage() {
    if (previewIndex <= 0) {
        showNotice('No more images');
        return;
    }
    previewIndex -= 1;
    document.getElementById('previewImage').src = previewImages[previewIndex];
}

function showNextImage() {
    if (previewIndex === -1 || previewIndex >= previewImages.length - 1) {
        showNotice('No more images');
        return;
    }
    previewIndex += 1;
    document.getElementById('previewImage').src = previewImages[previewIndex];
}

async function showPrompt(imageId) {
    try {
        const response = await fetch(`/api/images/${imageId}/prompt`);
        if (!response.ok) throw new Error('Failed to get prompt');

        const data = await response.json();
        document.getElementById('promptText').textContent = data.prompt;
        document.getElementById('promptModal').style.display = 'block';
    } catch (error) {
        showNotice(`Error: ${error.message}`);
    }
}

async function deleteImage(imageId) {
    if (!confirm('Are you sure you want to delete this image?')) return;

    try {
        const response = await fetch(`/api/images/${imageId}`, {
            method: 'DELETE'
        });

        if (!response.ok) throw new Error('Failed to delete');

        loadImages();
    } catch (error) {
        showNotice(`Error: ${error.message}`);
    }
}

// Video functions
function isVideoFile(filename) {
    if (!filename) return false;
    const ext = filename.split('.').pop().toLowerCase();
    return ['mp4', 'webm', 'mov', 'avi'].includes(ext);
}

function showVideoConfig(imageId) {
    selectedImageId = imageId;
    document.getElementById('videoPrompt').value = '';
    document.getElementById('videoDuration').value = '5';
    document.getElementById('videoConfigModal').style.display = 'block';
}

async function generateVideo() {
    if (!selectedImageId) {
        showNotice('No image selected');
        return;
    }

    const prompt = document.getElementById('videoPrompt').value.trim();
    const duration = parseInt(document.getElementById('videoDuration').value);
    const ratioEl = document.querySelector('input[name="videoRatio"]:checked');
    const aspectRatio = ratioEl ? ratioEl.value : '16:9';

    const btn = document.getElementById('generateVideoBtn');
    const originalContent = btn.innerHTML;
    btn.innerHTML = '<span class="btn-label">Generating...</span>';
    btn.disabled = true;

    try {
        const response = await fetch(`/api/images/${selectedImageId}/to-video`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                prompt: prompt || 'Animate this image',
                video_config: {
                    duration: duration,
                    aspect_ratio: aspectRatio
                },
                config_name: 'grok-video'
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create video task');
        }

        const data = await response.json();
        console.log(`Video task created: ${data.task_id}`);

        closeModals();
        showNotice('Video generation task created. Check progress in task list.');

    } catch (error) {
        showNotice(`Error: ${error.message}`);
    } finally {
        btn.innerHTML = originalContent;
        btn.disabled = false;
        selectedImageId = null;
    }
}
