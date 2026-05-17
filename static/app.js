const form = document.getElementById('uploadForm');
const taskList = document.getElementById('taskList');
const runtimeCard = document.getElementById('runtimeCard');
const tasks = new Map();
let isProcessing = false;

function setSubmitLocked(locked, text = '') {
  const button = form.querySelector('button');
  isProcessing = locked;
  button.disabled = locked;
  button.textContent = text || (locked ? '正在生成，请稍等...' : '开始生成视觉版本');
}

async function checkRuntime() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    const ok = data.runtime && data.runtime.ok;
    runtimeCard.classList.toggle('bad', !ok);
    runtimeCard.querySelector('strong').textContent = ok ? '已就绪' : '缺 FFmpeg';
    runtimeCard.querySelector('small').textContent = ok ? data.runtime.ffmpeg : (data.runtime.error || '请查看 README');
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
    ['effect_background', 'effect_zoom', 'effect_color', 'effect_texture', 'effect_speed', 'effect_vignette'].forEach(name => boolField(data, name));
    const res = await fetch('/api/upload-batch', { method: 'POST', body: data });
    if (!res.ok) throw new Error(await res.text());
    const payload = await res.json();
    payload.tasks.forEach(task => tasks.set(task.task_id, { task_id: task.task_id, status_url: task.status_url, message: '已创建合并处理任务' }));
    renderTasks();
  } catch (error) {
    alert('上传失败：' + (error.message || error));
    setSubmitLocked(false);
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
    const title = task.original_filename || task.task_id;
    const sourceCount = task.source_filenames?.length || 0;
    const sourceText = sourceCount > 1 ? `<p>已合并 ${sourceCount} 个源视频：${escapeHtml(task.source_filenames.join(' / '))}</p>` : '';
    const versionText = task.output_count > 1 ? `<p>生成版本：${task.variant_paths?.length || 0}/${task.output_count}，完成后会自动合并为一个总视频。</p>` : '';
    const timingText = buildTimingText(task);
    const download = task.download_url && status === 'completed' ? `<a href="${task.download_url}">下载 ${escapeHtml(task.output_path?.split('/').pop() || 'MP4')}</a>` : '';
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
      ${download}${error}
    </article>`;
  }).join('');
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
  const activeTasks = [...tasks.values()].filter(task => !['completed', 'failed'].includes(task.status));
  if (activeTasks.length) {
    setSubmitLocked(true, '正在生成，请稍等...');
  } else if (isProcessing) {
    setSubmitLocked(false);
  }
}

function escapeHtml(text) {
  return String(text || '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

checkRuntime();
checkVersion();
setInterval(pollTasks, 1800);
