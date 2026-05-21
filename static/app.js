const form = document.getElementById('uploadForm');
const mergeForm = document.getElementById('mergeForm');
const splitForm = document.getElementById('splitForm');
const taskList = document.getElementById('taskList');
const runtimeCard = document.getElementById('runtimeCard');
const tasks = new Map();
const menuItems = document.querySelectorAll('.menu-item');
const toolViews = document.querySelectorAll('[data-view-panel]');
let isProcessing = false;


function activateView(viewId) {
  menuItems.forEach(item => item.classList.toggle('active', item.dataset.view === viewId));
  toolViews.forEach(view => view.classList.toggle('active', view.dataset.viewPanel === viewId));
}

menuItems.forEach(item => {
  item.addEventListener('click', () => activateView(item.dataset.view));
});

function setSubmitLocked(locked, text = '') {
  const button = form.querySelector('button');
  isProcessing = locked;
  button.disabled = locked;
  button.textContent = text || (locked ? '正在生成，请稍等...' : '开始生成视觉版本');
}

function setFormLocked(targetForm, locked, textWhenLocked, textWhenReady) {
  const button = targetForm.querySelector('button[type="submit"]');
  button.disabled = locked;
  button.textContent = locked ? textWhenLocked : textWhenReady;
}

function addCreatedTask(payload, message) {
  tasks.set(payload.task_id, { task_id: payload.task_id, status_url: payload.status_url, message });
  renderTasks();
}

async function checkRuntime() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    const ok = data.runtime && data.runtime.ok;
    runtimeCard.classList.toggle('bad', !ok);
    runtimeCard.querySelector('strong').textContent = ok ? '已就绪' : '缺 FFmpeg';
    runtimeCard.querySelector('small').textContent = ok
      ? `${data.runtime.ffmpeg} · 运行 ${data.active_jobs || 0}/${data.max_parallel_jobs || 1} · 待处理 ${data.pending_jobs || 0}`
      : (data.runtime.error || '请查看 README');
  } catch (error) {
    runtimeCard.classList.add('bad');
    runtimeCard.querySelector('strong').textContent = '未启动';
    runtimeCard.querySelector('small').textContent = String(error.message || error);
  }
}

async function checkVersion() {
  const target = document.getElementById('versionInfo');
  if (!target) return;
  try {
    const res = await fetch('/api/version');
    const data = await res.json();
    const time = data.committed_at ? data.committed_at.replace(' +0800', '') : 'local build';
    target.textContent = `Version ${data.version || 'local'} · ${time}`;
    if (data.subject) target.title = data.subject;
  } catch (error) {
    target.textContent = 'Version local';
  }
}

function boolField(formData, name) {
  formData.set(name, form.elements[name].checked ? 'true' : 'false');
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (isProcessing) return;
  const files = document.getElementById('videoFiles').files;
  if (!files.length) {
    alert('请先选择视频文件');
    return;
  }
  setSubmitLocked(true, '正在上传并创建任务...');
  try {
    const data = new FormData();
    [...files].forEach(file => data.append('files', file));
    data.set('intensity', document.getElementById('intensity').value);
    data.set('output_count', document.getElementById('outputCount').value || '1');
    data.set('worker_count', document.getElementById('workerCount').value || '3');
    ['effect_background', 'effect_zoom', 'effect_color', 'effect_texture', 'effect_speed', 'effect_vignette', 'effect_center_scratch', 'effect_light_sweep', 'effect_film_grain'].forEach(name => boolField(data, name));
    const res = await fetch('/api/upload-batch', { method: 'POST', body: data });
    if (!res.ok) throw new Error(await res.text());
    const payload = await res.json();
    payload.tasks.forEach(task => tasks.set(task.task_id, { task_id: task.task_id, status_url: task.status_url, message: '已创建独立处理任务' }));
    renderTasks();
    setSubmitLocked(false);
  } catch (error) {
    alert('上传失败：' + (error.message || error));
    setSubmitLocked(false);
  }
});


mergeForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const files = document.getElementById('mergeFiles').files;
  if (files.length < 2) {
    alert('请至少选择两个视频进行合并');
    return;
  }
  setFormLocked(mergeForm, true, '正在上传合并任务...', '合并视频');
  try {
    const data = new FormData();
    [...files].forEach(file => data.append('files', file));
    const res = await fetch('/api/merge', { method: 'POST', body: data });
    if (!res.ok) throw new Error(await res.text());
    addCreatedTask(await res.json(), '已创建合并任务');
  } catch (error) {
    alert('合并任务创建失败：' + (error.message || error));
  } finally {
    setFormLocked(mergeForm, false, '正在上传合并任务...', '合并视频');
  }
});

splitForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = document.getElementById('splitFile').files[0];
  if (!file) {
    alert('请先选择要切分的视频');
    return;
  }
  setFormLocked(splitForm, true, '正在上传切分任务...', '切分视频');
  try {
    const data = new FormData();
    data.append('file', file);
    data.set('segment_range', document.getElementById('segmentRange').value || '50-56');
    const res = await fetch('/api/split', { method: 'POST', body: data });
    if (!res.ok) throw new Error(await res.text());
    addCreatedTask(await res.json(), '已创建切分任务');
  } catch (error) {
    alert('切分任务创建失败：' + (error.message || error));
  } finally {
    setFormLocked(splitForm, false, '正在上传切分任务...', '切分视频');
  }
});

async function pollTasks() {
  for (const [id, current] of tasks.entries()) {
    if (current.status === 'completed' || current.status === 'failed') continue;
    try {
      const res = await fetch(`/api/tasks/${id}`);
      const task = await res.json();
      tasks.set(id, task);
    } catch (error) {
      tasks.set(id, { ...current, status: 'failed', message: '查询任务失败', error: String(error.message || error) });
    }
  }
  renderTasks();
  refreshSubmitState();
}

async function loadExistingTasks() {
  try {
    const res = await fetch(`/api/tasks?_=${Date.now()}`);
    if (!res.ok) throw new Error(await res.text());
    const payload = await res.json();
    (payload.tasks || []).forEach(task => tasks.set(task.task_id, task));
    renderTasks();
    refreshSubmitState();
  } catch (error) {
    taskList.className = 'task-list empty';
    taskList.innerHTML = `<span class="error">无法连接本地服务：${escapeHtml(error.message || error)}</span>`;
    setSubmitLocked(false);
  }
}

function renderTasks() {
  const list = [...tasks.values()].reverse();
  if (!list.length) {
    taskList.className = 'task-list empty';
    taskList.textContent = '等待上传视频。';
    return;
  }
  taskList.className = 'task-list';
  taskList.innerHTML = list.map(task => {
    const progress = task.progress || 0;
    const status = task.status || 'queued';
    const title = buildTaskTitle(task);
    const sourceText = task.source_filenames?.length ? `<p>源视频：${escapeHtml(task.source_filenames.join(' / '))}</p>` : '';
    const versionText = buildVersionText(task);
    const workerText = task.worker_count ? `<p>本批线程数：${task.worker_count}</p>` : '';
    const timingText = buildTimingText(task);
    const download = buildDownloadLinks(task, status);
    const error = task.error ? `<p class="error">${escapeHtml(task.error)}</p>` : '';
    return `<article class="task">
      <div class="task-header"><div class="task-title">${escapeHtml(title)}</div><span class="badge">${escapeHtml(status)}</span></div>
      <div class="progress"><i style="width:${progress}%"></i></div>
      <div class="task-meta">
        <span>当前进度：${progress}%</span>
        <span>${timingText}</span>
      </div>
      <p>${escapeHtml(task.message || '等待处理')}</p>
      ${sourceText}
      ${versionText}
      ${workerText}
      ${download}${error}
    </article>`;
  }).join('');
}

function buildTaskTitle(task) {
  const base = task.original_filename || task.task_id;
  if (task.operation === 'merge') return `合并视频：${base}`;
  if (task.operation === 'split') return `切分视频：${base}`;
  return base;
}

function buildVersionText(task) {
  if (task.operation === 'split') {
    return `<p>切分片段：${task.variant_paths?.length || 0} 个，支持单独下载和整包下载。</p>`;
  }
  if (task.output_count > 1) {
    return `<p>生成版本：${task.variant_paths?.length || 0}/${task.output_count}，每个版本都会提供单独下载链接。</p>`;
  }
  return '';
}

function buildDownloadLinks(task, status) {
  if (status !== 'completed') return '';
  const packageLink = task.package_url ? `<a href="${task.package_url}">下载全部分段 ZIP</a>` : '';
  const urls = task.variant_download_urls || [];
  if (urls.length) {
    return `<div class="download-list">${packageLink}${urls.map((url, index) => {
      const path = task.variant_paths?.[index] || '';
      const name = path.split('/').pop() || `版本 ${index + 1}`;
      const label = task.operation === 'split' ? `下载片段 ${index + 1}` : `下载版本 ${index + 1}`;
      return `<a href="${url}">${label}：${escapeHtml(name)}</a>`;
    }).join('')}</div>`;
  }
  if (task.download_url) {
    return `<div class="download-list">${packageLink}<a href="${task.download_url}">下载 ${escapeHtml(task.output_path?.split('/').pop() || 'MP4')}</a></div>`;
  }
  return '';
}

function buildTimingText(task) {
  const elapsed = formatDuration(task.elapsed_seconds);
  if (task.status === 'completed') return `总处理时间：${elapsed}`;
  if (task.status === 'failed') return `已处理：${elapsed}`;
  const remaining = typeof task.remaining_seconds === 'number' ? formatDuration(task.remaining_seconds) : '计算中';
  return `已处理：${elapsed} · 预计剩余：${remaining}`;
}

function formatDuration(value) {
  const seconds = Math.max(0, Math.round(Number(value || 0)));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours} 小时 ${mins} 分` : `${hours} 小时`;
}

function refreshSubmitState() {
  if (isProcessing) setSubmitLocked(false);
}

function escapeHtml(text) {
  return String(text || '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

checkRuntime();
checkVersion();
loadExistingTasks();
setInterval(pollTasks, 1800);
