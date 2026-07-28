const form = document.getElementById('uploadForm');
const downloadForm = document.getElementById('downloadForm');
const mergeForm = document.getElementById('mergeForm');
const splitForm = document.getElementById('splitForm');
const dramaForm = document.getElementById('dramaForm');
const taskList = document.getElementById('taskList');
const taskSummary = document.getElementById('taskSummary');
const taskPagination = document.getElementById('taskPagination');
const runtimeCard = document.getElementById('runtimeCard');
const chooseOutputDirButton = document.getElementById('chooseOutputDir');
const chooseDramaOutputDirButton = document.getElementById('chooseDramaOutputDir');
const dramaRootMode = document.getElementById('dramaRootMode');
const dramaRootModeFields = document.getElementById('dramaRootModeFields');
const dramaManualModeFields = document.getElementById('dramaManualModeFields');
const chooseDramaSourceRootButton = document.getElementById('chooseDramaSourceRoot');
const chooseDramaRootOutputDirButton = document.getElementById('chooseDramaRootOutputDir');
const scanDramaRootButton = document.getElementById('scanDramaRootButton');
const chooseMergeOutputDirButton = document.getElementById('chooseMergeOutputDir');
const chooseSplitOutputDirButton = document.getElementById('chooseSplitOutputDir');
const tasks = new Map();
const menuItems = document.querySelectorAll('.menu-item');
const toolViews = document.querySelectorAll('[data-view-panel]');
let isProcessing = false;
let isPollingTasks = false;
const UPLOAD_CHUNK_SIZE = 20;
const TASKS_PER_PAGE = 8;
let taskPage = 1;

function replaceTasks(serverTasks) {
  tasks.clear();
  (serverTasks || []).forEach(task => tasks.set(task.task_id, task));
}

const dramaAiModelSelect = document.getElementById('dramaAiModel');
const dramaCustomAiModelInput = document.getElementById('dramaCustomAiModel');
const dramaWorkflowSelect = document.getElementById('dramaWorkflow');
const baiduCloudForm = document.getElementById('baiduCloudForm');
const baiduAuthorizeButton = document.getElementById('baiduAuthorizeButton');
const baiduSubmitCodeButton = document.getElementById('baiduSubmitCodeButton');
const baiduDisconnectButton = document.getElementById('baiduDisconnectButton');


function syncDramaWorkflow() {
  if (!dramaWorkflowSelect) return;
  const analyzerMode = dramaWorkflowSelect.value === 'analyzer';
  document.querySelectorAll('.drama-analyzer-only').forEach(element => {
    element.hidden = !analyzerMode;
  });
  const modeHint = document.getElementById('dramaModeHint');
  const footerHint = document.getElementById('dramaFooterHint');
  const submitButton = document.getElementById('dramaSubmitButton');
  const versionsLabel = document.getElementById('dramaVersionsLabel');
  if (modeHint) {
    modeHint.textContent = analyzerMode
      ? '读取字幕并分析剧情爆点，只抽取高分片段，可继续生成多版本。'
      : '先对整集生成视觉版本，再按 28–30 秒连续切完；尾段会自动均匀分配，不留下几秒碎片。';
  }
  if (footerHint) {
    footerHint.textContent = analyzerMode
      ? '有同名 JSON/SRT/VTT 字幕会优先使用；没有字幕时尝试 Whisper，并保留本地分析兜底。'
      : '输出按片段和版本编号保存；合并、切分中间文件会在任务结束后自动删除。';
  }
  if (submitButton) submitButton.textContent = analyzerMode ? '开始爆点分析' : '开始批量生成 Reel';
  if (versionsLabel?.firstChild) versionsLabel.firstChild.nodeValue = analyzerMode ? '每条处理版本数\n                ' : '每集处理版本数\n                ';
}

if (dramaWorkflowSelect) {
  dramaWorkflowSelect.addEventListener('change', syncDramaWorkflow);
  syncDramaWorkflow();
}


function activateView(viewId) {
  menuItems.forEach(item => item.classList.toggle('active', item.dataset.view === viewId));
  toolViews.forEach(view => view.classList.toggle('active', view.dataset.viewPanel === viewId));
}

function showBaiduStatus(data) {
  const target = document.getElementById('baiduCloudStatus');
  if (!target) return;
  const configured = data.configured ? '配置已保存' : '尚未配置';
  const authorized = data.authorized ? '已授权' : '未授权';
  const enabled = data.enabled ? '自动上传已开启' : '自动上传未开启';
  target.textContent = `${configured} · ${authorized} · ${enabled}`;
  target.classList.toggle('error', Boolean(data.configured && !data.authorized));
}

async function loadBaiduStatus() {
  if (!baiduCloudForm) return;
  try {
    const res = await fetch('/api/cloud/baidu/status');
    if (!res.ok) throw new Error(await readError(res));
    const data = await res.json();
    document.getElementById('baiduAppKey').value = data.app_key || '';
    document.getElementById('baiduRemoteDir').value = data.remote_dir || '';
    document.getElementById('baiduRedirectUri').value = data.redirect_uri || 'oob';
    document.getElementById('baiduAutoUpload').checked = Boolean(data.enabled);
    showBaiduStatus(data);
  } catch (error) {
    document.getElementById('baiduCloudStatus').textContent = `读取失败：${error.message || error}`;
  }
}

async function saveBaiduSettings() {
  const payload = {
    app_key: document.getElementById('baiduAppKey').value.trim(),
    secret_key: document.getElementById('baiduSecretKey').value.trim(),
    remote_dir: document.getElementById('baiduRemoteDir').value.trim(),
    redirect_uri: document.getElementById('baiduRedirectUri').value.trim() || 'oob',
    enabled: document.getElementById('baiduAutoUpload').checked,
  };
  const res = await fetch('/api/cloud/baidu/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readError(res));
  const data = await res.json();
  document.getElementById('baiduSecretKey').value = '';
  showBaiduStatus(data);
  return data;
}

if (baiduCloudForm) {
  baiduCloudForm.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await saveBaiduSettings();
      alert('百度网盘设置已保存。');
    } catch (error) {
      alert('保存失败：' + (error.message || error));
    }
  });
}

if (baiduAuthorizeButton) {
  baiduAuthorizeButton.addEventListener('click', async () => {
    try {
      await saveBaiduSettings();
      const res = await fetch('/api/cloud/baidu/auth-url');
      if (!res.ok) throw new Error(await readError(res));
      const data = await res.json();
      window.open(data.url, '_blank', 'noopener');
    } catch (error) {
      alert('打开授权失败：' + (error.message || error));
    }
  });
}

if (baiduSubmitCodeButton) {
  baiduSubmitCodeButton.addEventListener('click', async () => {
    const code = document.getElementById('baiduAuthCode').value.trim();
    try {
      const res = await fetch('/api/cloud/baidu/authorize', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
      });
      if (!res.ok) throw new Error(await readError(res));
      showBaiduStatus(await res.json());
      document.getElementById('baiduAuthCode').value = '';
      alert('百度网盘授权成功。');
    } catch (error) {
      alert('授权失败：' + (error.message || error));
    }
  });
}

if (baiduDisconnectButton) {
  baiduDisconnectButton.addEventListener('click', async () => {
    if (!confirm('确定解除本机保存的百度网盘授权？')) return;
    const res = await fetch('/api/cloud/baidu/disconnect', { method: 'POST' });
    showBaiduStatus(await res.json());
  });
}

menuItems.forEach(item => {
  item.addEventListener('click', () => activateView(item.dataset.view));
});

if (dramaAiModelSelect && dramaCustomAiModelInput) {
  dramaAiModelSelect.addEventListener('change', () => {
    const custom = dramaAiModelSelect.value === 'custom';
    dramaCustomAiModelInput.disabled = !custom;
    if (custom) dramaCustomAiModelInput.focus();
  });
}

if (chooseOutputDirButton) {
  chooseOutputDirButton.addEventListener('click', async () => {
    chooseOutputDirButton.disabled = true;
    chooseOutputDirButton.textContent = '选择中...';
    try {
      const res = await fetch('/api/select-output-dir', { method: 'POST' });
      if (!res.ok) throw new Error(await readError(res));
      const payload = await res.json();
      if (payload.path) document.getElementById('variantOutputDir').value = payload.path;
    } catch (error) {
      alert('选择文件夹失败：' + (error.message || error));
    } finally {
      chooseOutputDirButton.disabled = false;
      chooseOutputDirButton.textContent = '选择';
    }
  });
}

if (chooseDramaOutputDirButton) {
  chooseDramaOutputDirButton.addEventListener('click', async () => {
    chooseDramaOutputDirButton.disabled = true;
    chooseDramaOutputDirButton.textContent = '选择中...';
    try {
      const res = await fetch('/api/select-output-dir', { method: 'POST' });
      if (!res.ok) throw new Error(await readError(res));
      const payload = await res.json();
      if (payload.path) document.getElementById('dramaOutputDir').value = payload.path;
    } catch (error) {
      alert('选择文件夹失败：' + (error.message || error));
    } finally {
      chooseDramaOutputDirButton.disabled = false;
      chooseDramaOutputDirButton.textContent = '选择';
    }
  });
}

async function chooseToolOutputDir(button, inputId) {
  button.disabled = true;
  button.textContent = '选择中...';
  try {
    const res = await fetch('/api/select-output-dir', { method: 'POST' });
    if (!res.ok) throw new Error(await readError(res));
    const payload = await res.json();
    if (payload.path) document.getElementById(inputId).value = payload.path;
  } catch (error) {
    alert('选择文件夹失败：' + (error.message || error));
  } finally {
    button.disabled = false;
    button.textContent = '选择';
  }
}

chooseMergeOutputDirButton?.addEventListener('click', () => chooseToolOutputDir(chooseMergeOutputDirButton, 'mergeOutputDir'));
chooseSplitOutputDirButton?.addEventListener('click', () => chooseToolOutputDir(chooseSplitOutputDirButton, 'splitOutputDir'));
chooseDramaSourceRootButton?.addEventListener('click', () => chooseToolOutputDir(chooseDramaSourceRootButton, 'dramaSourceRoot'));
chooseDramaRootOutputDirButton?.addEventListener('click', () => chooseToolOutputDir(chooseDramaRootOutputDirButton, 'dramaRootOutputDir'));

function syncDramaRootMode() {
  const enabled = Boolean(dramaRootMode?.checked);
  if (dramaRootModeFields) dramaRootModeFields.hidden = !enabled;
  if (dramaManualModeFields) dramaManualModeFields.hidden = enabled;
}

dramaRootMode?.addEventListener('change', syncDramaRootMode);
syncDramaRootMode();

async function scanDramaRoot() {
  const result = document.getElementById('dramaRootScanResult');
  const sourceRoot = document.getElementById('dramaSourceRoot')?.value.trim();
  if (!sourceRoot) {
    alert('请先选择剧集源根目录。');
    return;
  }
  scanDramaRootButton.disabled = true;
  scanDramaRootButton.textContent = '扫描中...';
  if (result) result.textContent = '正在扫描一级子文件夹...';
  try {
    const res = await fetch('/api/drama-reels/root-scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_root: sourceRoot }),
    });
    if (!res.ok) throw new Error(await readError(res));
    const data = await res.json();
    const names = (data.groups || []).slice(0, 12).map(group => `${group.name}（${group.video_count} 个视频，${group.text_count} 个 TXT）`);
    const more = data.group_count > names.length ? ` 等 ${data.group_count} 组` : '';
    if (result) result.textContent = `识别到 ${data.group_count} 组、共 ${data.video_count} 个视频：${names.join('；')}${more}`;
  } catch (error) {
    if (result) result.textContent = `扫描失败：${error.message || error}`;
  } finally {
    scanDramaRootButton.disabled = false;
    scanDramaRootButton.textContent = '扫描目录';
  }
}

scanDramaRootButton?.addEventListener('click', scanDramaRoot);

function batchGroupTemplate(kind) {
  const isDrama = kind === 'drama';
  const accept = isDrama
    ? 'video/mp4,video/quicktime,video/x-matroska,video/x-msvideo,video/webm,.m4v,.mkv'
    : 'video/mp4,video/quicktime,video/x-msvideo,video/webm,.m4v';
  return `<article class="batch-group" data-batch-kind="${kind}">
    <div class="batch-group-header"><strong>附加目录组</strong><button class="danger-button remove-batch-group" type="button">删除本组</button></div>
    <label class="drop-zone compact-drop"><span>${isDrama ? '选择剧集文件夹' : '选择视频文件夹'}</span><input class="batch-folder-input" type="file" multiple webkitdirectory accept="${accept}" /></label>
    <label>视频介绍<textarea class="batch-intro" rows="3" placeholder="本组独立介绍，输出为 介绍.txt"></textarea></label>
    <label>输出文件夹路径<span class="path-picker"><input class="batch-output-dir" type="text" value="data/outputs" placeholder="选择本组输出目录" /><button class="secondary compact choose-batch-output" type="button">选择</button></span></label>
  </article>`;
}

document.getElementById('addVariantGroup')?.addEventListener('click', () => {
  document.getElementById('variantGroupList').insertAdjacentHTML('beforeend', batchGroupTemplate('variant'));
});

document.getElementById('addDramaGroup')?.addEventListener('click', () => {
  document.getElementById('dramaGroupList').insertAdjacentHTML('beforeend', batchGroupTemplate('drama'));
});

document.addEventListener('click', async event => {
  const removeButton = event.target.closest('.remove-batch-group');
  if (removeButton) {
    removeButton.closest('.batch-group')?.remove();
    return;
  }
  const chooseButton = event.target.closest('.choose-batch-output');
  if (!chooseButton) return;
  chooseButton.disabled = true;
  chooseButton.textContent = '选择中...';
  try {
    const res = await fetch('/api/select-output-dir', { method: 'POST' });
    if (!res.ok) throw new Error(await readError(res));
    const payload = await res.json();
    if (payload.path) chooseButton.closest('.path-picker')?.querySelector('.batch-output-dir')?.setAttribute('value', payload.path);
    const input = chooseButton.closest('.path-picker')?.querySelector('.batch-output-dir');
    if (input && payload.path) input.value = payload.path;
  } catch (error) {
    alert('选择文件夹失败：' + (error.message || error));
  } finally {
    chooseButton.disabled = false;
    chooseButton.textContent = '选择';
  }
});

function validVideoFiles(inputs) {
  const valid = /\.(mp4|mov|mkv|m4v|avi|webm)$/i;
  const seen = new Set();
  return inputs.flatMap(input => input?.files ? [...input.files] : []).filter(file => {
    if (!valid.test(file.name)) return false;
    const key = `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function outputFolderName(path, fallback) {
  const parts = String(path || '').replace(/\\/g, '/').split('/').filter(Boolean);
  return parts.pop() || fallback;
}

function collectVariantGroups() {
  const groups = [{
    files: validVideoFiles([document.getElementById('videoFiles'), document.getElementById('variantFolder')]),
    outputDir: document.getElementById('variantOutputDir').value || 'data/outputs',
    introText: document.getElementById('introText').value || '',
  }];
  document.querySelectorAll('#variantGroupList .batch-group').forEach(group => groups.push({
    files: validVideoFiles([group.querySelector('.batch-folder-input')]),
    outputDir: group.querySelector('.batch-output-dir').value || 'data/outputs',
    introText: group.querySelector('.batch-intro').value || '',
  }));
  return groups.filter(group => group.files.length);
}

function collectDramaGroups() {
  const groups = [{
    files: collectDramaFiles(),
    outputDir: document.getElementById('dramaOutputDir').value || 'data/outputs',
    introText: dramaForm.elements.namedItem('intro_text')?.value || '',
  }];
  document.querySelectorAll('#dramaGroupList .batch-group').forEach(group => groups.push({
    files: validVideoFiles([group.querySelector('.batch-folder-input')]),
    outputDir: group.querySelector('.batch-output-dir').value || 'data/outputs',
    introText: group.querySelector('.batch-intro').value || '',
  }));
  return groups.filter(group => group.files.length);
}

function collectDramaSettings() {
  const payload = {};
  ['versions_per_episode', 'worker_count', 'min_seconds', 'max_seconds', 'intensity',
    'hook_clip_seconds', 'hook_duration', 'hook_texts', 'subtitle_model'].forEach(name => {
    const field = dramaForm.elements.namedItem(name);
    if (field) payload[name] = field.value;
  });
  ['effect_background', 'effect_zoom', 'effect_color', 'effect_texture', 'effect_speed',
    'effect_vignette', 'effect_center_scratch', 'effect_light_sweep', 'effect_film_grain',
    'effect_frame_extract', 'effect_frame_interpolate', 'effect_md5', 'effect_border',
    'effect_random_transition', 'effect_remove_progress', 'effect_hook_clip',
    'effect_hook_caption', 'effect_english_subtitles'].forEach(name => {
    payload[name] = Boolean(dramaForm.elements.namedItem(name)?.checked);
  });
  payload.intro_text = dramaForm.elements.namedItem('intro_text')?.value || '';
  return payload;
}

async function submitDramaRootBatch() {
  const sourceRoot = document.getElementById('dramaSourceRoot')?.value.trim();
  const outputRoot = document.getElementById('dramaRootOutputDir')?.value.trim();
  if (!sourceRoot || !outputRoot) {
    alert('请选择剧集源根目录和输出目标根目录。');
    return;
  }
  const readyText = '开始流水线处理';
  setFormLocked(dramaForm, true, '正在扫描并创建目录任务...', readyText);
  try {
    const payload = {
      ...collectDramaSettings(),
      source_root: sourceRoot,
      output_root: outputRoot,
      group_parallelism: document.getElementById('dramaGroupParallelism')?.value || '1',
    };
    const res = await fetch('/api/drama-reels/root-batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res));
    const data = await res.json();
    (data.tasks || []).forEach((task, index) => addCreatedTask(task, `根目录组 ${index + 1}/${data.tasks.length} 已创建`));
    await pollTasks();
  } catch (error) {
    alert('根目录批量任务创建失败：' + (error.message || error));
  } finally {
    setFormLocked(dramaForm, false, '正在创建任务...', readyText);
  }
}

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
      ? `编码器 ${data.video_encoder || 'libx264'} · 运行 ${data.active_jobs || 0}/${data.max_parallel_jobs || 1} · 待处理 ${data.pending_jobs || 0}`
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
    const build = data.build ? ` · Build ${data.build}` : '';
    target.textContent = `Version ${data.version || 'local'}${build} · ${time}`;
    if (data.subject) target.title = data.subject;
  } catch (error) {
    target.textContent = 'Version local';
  }
}

function boolField(formData, name) {
  formData.set(name, form.elements[name].checked ? 'true' : 'false');
}

function createVariantFormData(fileChunk, allFiles, startIndex, batchId, group) {
  const data = new FormData();
  fileChunk.forEach(file => data.append('files', file));
  data.set('intensity', document.getElementById('intensity').value);
  data.set('output_count', document.getElementById('outputCount').value || '1');
  data.set('worker_count', document.getElementById('workerCount').value || '3');
  data.set('output_dir', group.outputDir);
  data.set('intro_text', group.introText);
  data.set('cloud_group_id', group.cloudGroupId);
  data.set('cloud_folder_name', outputFolderName(group.outputDir, '处理视频'));
  data.set('hook_texts', document.getElementById('hookTexts').value || '');
  data.set('hook_duration', document.getElementById('hookDuration').value || '3');
  data.set('hook_clip_seconds', document.getElementById('hookClipSeconds').value || '3');
  data.set('subtitle_model', document.getElementById('variantSubtitleModel').value || 'base');
  data.set('batch_id', batchId);
  data.set('batch_total', String(allFiles.length));
  data.set('batch_start', String(startIndex + 1));
  [
    'effect_background',
    'effect_zoom',
    'effect_color',
    'effect_texture',
    'effect_speed',
    'effect_vignette',
    'effect_center_scratch',
    'effect_light_sweep',
    'effect_film_grain',
    'effect_hook_clip',
    'effect_hook_caption',
    'effect_frame_extract',
    'effect_frame_interpolate',
    'effect_md5',
    'effect_mirror',
    'effect_border',
    'effect_random_transition',
    'effect_remove_progress',
    'effect_english_subtitles',
  ].forEach(name => boolField(data, name));
  return data;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (isProcessing) return;
  const groups = collectVariantGroups();
  const totalFiles = groups.reduce((sum, group) => sum + group.files.length, 0);
  if (!groups.length) {
    alert('请先选择一个或多个视频文件夹');
    return;
  }
  setSubmitLocked(true, `正在上传 0/${totalFiles} 个文件...`);
  try {
    let uploaded = 0;
    await Promise.all(groups.map(async (group, groupIndex) => {
      const batchId = `web_${Date.now()}_${groupIndex}_${Math.random().toString(16).slice(2)}`;
      group.cloudGroupId = batchId;
      for (let start = 0; start < group.files.length; start += UPLOAD_CHUNK_SIZE) {
        const chunk = group.files.slice(start, start + UPLOAD_CHUNK_SIZE);
        const data = createVariantFormData(chunk, group.files, start, batchId, group);
        const res = await fetch('/api/upload-batch', { method: 'POST', body: data });
        if (!res.ok) throw new Error(await readError(res));
        const payload = await res.json();
        payload.tasks.forEach(task => tasks.set(task.task_id, { task_id: task.task_id, status_url: task.status_url, message: `目录组 ${groupIndex + 1}：已创建处理任务` }));
        uploaded += chunk.length;
        setSubmitLocked(true, `正在上传 ${uploaded}/${totalFiles} 个文件...`);
        renderTasks();
      }
    }));
    renderTasks();
    setSubmitLocked(false);
  } catch (error) {
    alert('上传失败：' + (error.message || error));
    setSubmitLocked(false);
  }
});


if (downloadForm) {
  downloadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const url = document.getElementById('downloadUrl').value.trim();
    if (!url) {
      alert('请先粘贴视频分享链接');
      return;
    }
    setFormLocked(downloadForm, true, '正在解析并下载...', '下载视频');
    try {
      const cookiesBrowser = document.getElementById('downloadCookiesBrowser').value;
      const proxy = document.getElementById('downloadProxy').value.trim();
      const allowPlaylist = document.getElementById('downloadAllowPlaylist').checked;
      const maxDownloads = document.getElementById('downloadMaxDownloads').value || '30';
      const body = { url, allow_playlist: allowPlaylist, max_downloads: Number(maxDownloads) };
      if (cookiesBrowser) body.cookies_browser = cookiesBrowser;
      if (proxy) body.proxy = proxy;
      const res = await fetch('/api/download-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await readError(res));
      const payload = await res.json();
      tasks.set(payload.task_id, {
        task_id: payload.task_id,
        status_url: payload.status_url,
        status: 'queued',
        progress: 0,
        operation: 'download',
        original_filename: url,
        message: '已创建下载任务',
      });
      renderTasks();
      pollTasks();
    } catch (error) {
      alert('下载失败：' + (error.message || error));
    } finally {
      setFormLocked(downloadForm, false, '正在解析并下载...', '下载视频');
    }
  });
}


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
    data.set('output_dir', document.getElementById('mergeOutputDir').value || 'data/outputs');
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
    data.set('output_dir', document.getElementById('splitOutputDir').value || 'data/outputs');
    const res = await fetch('/api/split', { method: 'POST', body: data });
    if (!res.ok) throw new Error(await res.text());
    addCreatedTask(await res.json(), '已创建切分任务');
  } catch (error) {
    alert('切分任务创建失败：' + (error.message || error));
  } finally {
    setFormLocked(splitForm, false, '正在上传切分任务...', '切分视频');
  }
});

if (dramaForm) {
  dramaForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (dramaRootMode?.checked) {
      await submitDramaRootBatch();
      return;
    }
    const groups = collectDramaGroups();
    const totalFiles = groups.reduce((sum, group) => sum + group.files.length, 0);
    if (!groups.length) {
      alert('请先选择一个或多个剧集文件夹。');
      return;
    }
    const readyText = '开始流水线处理';
    setFormLocked(dramaForm, true, `正在上传 ${totalFiles} 集并创建多组流水线...`, readyText);
    try {
      await Promise.all(groups.map(async (group, groupIndex) => {
        const data = new FormData();
        group.files.forEach(file => data.append('files', file));
        data.set('output_dir', group.outputDir);
        data.set('intro_text', group.introText);
        data.set('cloud_group_id', `drama_${Date.now()}_${groupIndex}_${Math.random().toString(16).slice(2)}`);
        data.set('cloud_folder_name', outputFolderName(group.outputDir, 'ShortDrama'));
        data.set('max_episodes', '50');
        ['versions_per_episode', 'worker_count', 'min_seconds', 'max_seconds', 'intensity',
          'hook_clip_seconds', 'hook_duration', 'hook_texts', 'subtitle_model'].forEach(name => {
          const field = dramaForm.elements.namedItem(name);
          if (field) data.set(name, field.value);
        });
        ['effect_background', 'effect_zoom', 'effect_color', 'effect_texture', 'effect_speed',
          'effect_vignette', 'effect_center_scratch', 'effect_light_sweep', 'effect_film_grain',
          'effect_frame_extract', 'effect_frame_interpolate', 'effect_md5', 'effect_border',
          'effect_random_transition', 'effect_remove_progress', 'effect_hook_clip',
          'effect_hook_caption', 'effect_english_subtitles'].forEach(name => {
          const field = dramaForm.elements.namedItem(name);
          data.set(name, String(Boolean(field?.checked)));
          });
        const res = await fetch('/api/drama-reels/batch', { method: 'POST', body: data });
        if (!res.ok) throw new Error(await readError(res));
        addCreatedTask(await res.json(), `Short Drama 目录组 ${groupIndex + 1} 已创建`);
      }));
    } catch (error) {
      alert('剧集拆条失败：' + (error.message || error));
    } finally {
      setFormLocked(dramaForm, false, '正在创建任务...', readyText);
    }
  });
}

function selectedDramaAiModel() {
  const selected = document.getElementById('dramaAiModel')?.value || 'openai/gpt-4.1-mini';
  if (selected === 'custom') {
    return document.getElementById('dramaCustomAiModel')?.value.trim() || 'openai/gpt-4.1-mini';
  }
  return selected;
}

function collectDramaFiles() {
  const valid = /\.(mp4|mov|mkv|m4v|avi|webm)$/i;
  const fileInput = document.getElementById('dramaFile');
  const folderInput = document.getElementById('dramaFolder');
  const files = [
    ...(fileInput?.files ? [...fileInput.files] : []),
    ...(folderInput?.files ? [...folderInput.files] : []),
  ].filter(file => valid.test(file.name));
  const seen = new Set();
  return files.filter(file => {
    const key = `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function pollTasks() {
  if (document.hidden || isPollingTasks) return;
  isPollingTasks = true;
  try {
    const res = await fetch(`/api/tasks?_=${Date.now()}`);
    if (!res.ok) throw new Error(await res.text());
    const payload = await res.json();
    replaceTasks(payload.tasks);
    renderTasks();
    refreshSubmitState();
  } catch (error) {
    // A transient status-query failure must not turn hundreds of healthy jobs
    // into failed jobs. Keep the last known state and retry next interval.
    console.warn('查询任务状态失败：', error);
  } finally {
    isPollingTasks = false;
  }
}

async function loadExistingTasks() {
  try {
    const res = await fetch(`/api/tasks?_=${Date.now()}`);
    if (!res.ok) throw new Error(await res.text());
    const payload = await res.json();
    replaceTasks(payload.tasks);
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
    if (taskPagination) taskPagination.hidden = true;
    if (taskSummary) taskSummary.hidden = true;
    return;
  }
  renderTaskSummary(list);
  const totalPages = Math.max(1, Math.ceil(list.length / TASKS_PER_PAGE));
  taskPage = Math.min(Math.max(1, taskPage), totalPages);
  const pageList = list.slice((taskPage - 1) * TASKS_PER_PAGE, taskPage * TASKS_PER_PAGE);
  taskList.className = 'task-list';
  taskList.innerHTML = pageList.map(task => {
    const progress = task.progress || 0;
    const status = task.status || 'queued';
    const title = buildTaskTitle(task);
    const sourceNames = task.source_filenames || [];
    const sourcePreview = sourceNames.slice(0, 5).join(' / ');
    const sourceMore = sourceNames.length > 5 ? ` / …另 ${sourceNames.length - 5} 个` : '';
    const sourceText = sourceNames.length ? `<p>源视频：${escapeHtml(sourcePreview + sourceMore)}</p>` : '';
    const versionText = buildVersionText(task);
    const workerText = task.worker_count ? `<p>本批线程数：${task.worker_count}</p>` : '';
    const timingText = buildTimingText(task);
    const download = buildDownloadLinks(task, status);
    const error = task.error ? `<p class="error">${escapeHtml(task.error)}</p>` : '';
    const cloud = buildBaiduUploadText(task);
    const actions = buildTaskActions(task);
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
      ${actions}
      ${cloud}${download}${error}
    </article>`;
  }).join('');
  if (taskPagination) {
    taskPagination.hidden = totalPages <= 1;
    taskPagination.innerHTML = `<button class="secondary compact" type="button" data-task-page="${taskPage - 1}" ${taskPage <= 1 ? 'disabled' : ''}>上一页</button><span>第 ${taskPage} / ${totalPages} 页 · 共 ${list.length} 个任务</span><button class="secondary compact" type="button" data-task-page="${taskPage + 1}" ${taskPage >= totalPages ? 'disabled' : ''}>下一页</button>`;
  }
}

function renderTaskSummary(list) {
  if (!taskSummary) return;
  const groups = new Map();
  list.forEach(task => {
    const options = task.tool_options || {};
    const key = options.cloud_group_id || task.batch_id || task.task_id;
    const outputDir = options.output_dir || '';
    const name = options.cloud_folder_name || outputFolderName(outputDir, task.operation === 'drama_batch_reels' ? 'Short Drama' : '处理视频');
    if (!groups.has(key)) groups.set(key, { name, total: 0, completed: 0, processing: 0, queued: 0, failed: 0 });
    const group = groups.get(key);
    group.total += 1;
    if (task.status === 'completed') group.completed += 1;
    else if (task.status === 'failed' || task.status === 'cancelled') group.failed += 1;
    else if (task.status === 'processing') group.processing += 1;
    else group.queued += 1;
  });
  const completed = list.filter(task => task.status === 'completed').length;
  const failed = list.filter(task => task.status === 'failed' || task.status === 'cancelled').length;
  const processing = list.filter(task => task.status === 'processing').length;
  const queued = list.length - completed - failed - processing;
  taskSummary.hidden = false;
  taskSummary.innerHTML = `<div class="task-summary-total"><strong>全部任务</strong><span>总数 ${list.length} · 已完成 ${completed} · 处理中 ${processing} · 排队 ${queued}${failed ? ` · 失败/停止 ${failed}` : ''}</span></div><div class="task-summary-groups">${[...groups.values()].map(group => `<div class="task-summary-group"><strong>${escapeHtml(group.name)}</strong><span>共 ${group.total} 个任务 · 已完成 ${group.completed} · 处理中 ${group.processing} · 排队 ${group.queued}${group.failed ? ` · 失败/停止 ${group.failed}` : ''}</span></div>`).join('')}</div>`;
}

taskPagination?.addEventListener('click', event => {
  const button = event.target.closest('[data-task-page]');
  if (!button || button.disabled) return;
  taskPage = Number(button.dataset.taskPage || 1);
  renderTasks();
  taskList.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

function buildBaiduUploadText(task) {
  const upload = task.effects?.baidu_upload;
  if (!upload) return '';
  const statusMap = { queued: '等待上传', uploading: '正在上传', completed: '上传完成', failed: '上传失败' };
  const error = upload.error ? ` · ${escapeHtml(upload.error)}` : '';
  return `<p class="${upload.status === 'failed' ? 'error' : ''}">百度网盘：${statusMap[upload.status] || upload.status} · ${upload.uploaded_count || 0}/${upload.file_count || 0} 个文件 · ${escapeHtml(upload.remote_dir || '')}${error}</p>`;
}


function isTerminalStatus(status) {
  return ['completed', 'failed', 'cancelled'].includes(status);
}

function buildTaskActions(task) {
  if (isTerminalStatus(task.status)) return '';
  return `<div class="task-actions"><button class="danger-button" type="button" onclick="cancelTask('${escapeHtml(task.task_id)}')">停止任务</button></div>`;
}

async function cancelTask(taskId) {
  const current = tasks.get(taskId);
  if (current) {
    tasks.set(taskId, { ...current, cancel_requested: true, message: '正在停止任务...' });
    renderTasks();
  }
  try {
    const res = await fetch(`/api/tasks/${taskId}/cancel`, { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const payload = await res.json();
    if (payload.task) tasks.set(taskId, payload.task);
    renderTasks();
  } catch (error) {
    alert('停止任务失败：' + (error.message || error));
  }
}

function buildTaskTitle(task) {
  const base = task.original_filename || task.task_id;
  if (task.operation === 'download') return `下载视频：${base}`;
  if (task.operation === 'merge') return `合并视频：${base}`;
  if (task.operation === 'split') return `切分视频：${base}`;
  if (task.operation === 'drama_factory') return `剧集拆条：${base}`;
  if (task.operation === 'drama_batch_reels') return `整集批量 Reel：${base}`;
  if (task.operation === 'drama_reel_analyzer') return `Drama Reel Analyzer：${base}`;
  if (task.operation === 'drama_reel_generate') return `生成 Reel 视频：${base}`;
  return base;
}

function buildVersionText(task) {
  if (task.operation === 'download') {
    const title = task.effects?.title ? `标题：${escapeHtml(task.effects.title)}` : '已保存到本地素材目录。';
    const extractor = task.effects?.extractor ? ` · 来源：${escapeHtml(task.effects.extractor)}` : '';
    const duration = task.effects?.duration ? ` · 时长：${formatDuration(task.effects.duration)}` : '';
    const count = task.effects?.download_count ? ` · 文件数：${task.effects.download_count}` : '';
    return `<p>${title}${extractor}${duration}${count}</p>`;
  }
  if (task.operation === 'drama_factory') {
    const clipCount = task.effects?.clip_count || 0;
    const outputCount = task.variant_paths?.length || 0;
    const source = task.effects?.transcript_source || 'pending';
    return `<p>剧集拆条：已生成 ${outputCount}/${task.output_count || 0} 个视频，爆点片段 ${clipCount} 个，字幕来源：${escapeHtml(source)}。</p>`;
  }
  if (task.operation === 'drama_batch_reels') {
    const sourceCount = task.effects?.source_count || task.source_paths?.length || 0;
    const segmentCount = task.effects?.segment_count || 0;
    const versions = task.effects?.versions_per_segment || 1;
    const workers = task.effects?.worker_count || 3;
    const range = task.effects?.segment_range || [28, 30];
    const batchRoot = task.effects?.batch_root ? `；批次目录：${escapeHtml(task.effects.batch_root)}` : '';
    const subtitleText = task.effects?.english_subtitle_count ? `，英文字幕 ${task.effects.english_subtitle_count} 条` : '';
    return `<p>流水线：${sourceCount} 集已合并，切成 ${segmentCount} 段，每段 ${versions} 个版本，${workers} 线程处理，时长 ${range[0]}–${range[1]} 秒，共 ${task.variant_paths?.length || 0} 个最终视频${subtitleText}${batchRoot}。</p>`;
  }
  if (task.operation === 'drama_reel_analyzer') {
    const topCount = task.effects?.top20_count || 0;
    const highlightCount = task.effects?.highlight_count || 0;
    const episodeCount = task.effects?.episode_count || 0;
    const provider = task.effects?.ai_provider || 'local';
    const model = task.effects?.ai_model ? ` · 模型：${escapeHtml(task.effects.ai_model)}` : '';
    const pipeline = task.effects?.variant_pipeline;
    const pipelineText = pipeline?.enabled
      ? `<p>自动多版本：基础片段 ${pipeline.base_clip_count || 0} 条，每条 ${pipeline.versions_per_clip || 0} 版，已输出 ${pipeline.output_count || 0} 个；最终文件夹：${escapeHtml(pipeline.final_dir || '')}</p>`
      : '';
    const reports = task.status === 'completed' && !pipeline?.enabled ? `<div class="download-list compact-links">
      <a href="/api/drama-reels/${task.task_id}/reports/highlights">highlights.json</a>
      <a href="/api/drama-reels/${task.task_id}/reports/reel_plan">reel_plan.json</a>
      <a href="/api/drama-reels/${task.task_id}/reports/top20">top20_reels.json</a>
      <button class="secondary compact" type="button" onclick="loadDramaTop20('${escapeHtml(task.task_id)}')">查看 Top20</button>
      <button class="secondary compact" type="button" onclick="generateDramaTop20('${escapeHtml(task.task_id)}')">生成 Top20 视频</button>
    </div>` : '';
    return `<p>分析剧集：${episodeCount} 集，候选爆点 ${highlightCount} 条，Top ${topCount}，AI：${escapeHtml(provider)}${model}。</p>${pipelineText}${reports}`;
  }
  if (task.operation === 'drama_reel_generate') {
    return `<p>已生成 Reel 视频：${task.variant_paths?.length || 0} 个，文件已保存到设置的输出目录。</p>`;
  }
  if (task.operation === 'split') {
    return `<p>切分片段：${task.variant_paths?.length || 0} 个，支持单独下载和整包下载。</p>`;
  }
  if (task.output_count > 1) {
    const subtitleText = task.effects?.english_subtitle_count ? `，英文字幕 ${task.effects.english_subtitle_count} 条` : '';
    return `<p>生成版本：${task.variant_paths?.length || 0}/${task.output_count}${subtitleText}，文件已保存到设置的输出目录。</p>`;
  }
  return '';
}

function buildDownloadLinks(task, status) {
  if (status !== 'completed') return '';
  if (['variant', 'drama_factory', 'drama_batch_reels', 'drama_reel_analyzer', 'drama_reel_generate'].includes(task.operation)) return '';
  const packageLink = task.operation === 'split' && task.package_url ? `<a href="${task.package_url}">下载全部分段 ZIP</a>` : '';
  const urls = task.variant_download_urls || [];
  if (urls.length) {
    return `<div class="download-list">${packageLink}${urls.map((url, index) => {
      const path = task.variant_paths?.[index] || '';
      const name = path.split('/').pop() || `文件 ${index + 1}`;
      const label = task.operation === 'split' ? `下载片段 ${index + 1}` : `下载视频 ${index + 1}`;
      return `<a href="${url}">${label}：${escapeHtml(name)}</a>`;
    }).join('')}</div>`;
  }
  if (task.download_url) {
    return `<div class="download-list">${packageLink}<a href="${task.download_url}">下载 ${escapeHtml(task.output_path?.split('/').pop() || 'MP4')}</a></div>`;
  }
  return '';
}

async function loadDramaTop20(taskId) {
  const target = document.getElementById('dramaReelResults');
  if (!target) return;
  target.innerHTML = '<p class="hint">正在加载 Top20...</p>';
  activateView('dramaView');
  try {
    const res = await fetch(`/api/drama-reels/${taskId}/reports/top20`);
    if (!res.ok) throw new Error(await readError(res));
    const rows = await res.json();
    target.innerHTML = `<div class="reel-table-wrap"><table class="reel-table">
      <thead><tr><th>Reel ID</th><th>集数</th><th>开始</th><th>结束</th><th>时长</th><th>爆点类型</th><th>评分</th><th>Hook 文案</th></tr></thead>
      <tbody>${(rows || []).map(row => `<tr>
        <td>${escapeHtml(row.id)}</td>
        <td>${escapeHtml(row.episode)}</td>
        <td>${escapeHtml(row.start)}</td>
        <td>${escapeHtml(row.end)}</td>
        <td>${escapeHtml(row.duration)}s</td>
        <td>${escapeHtml((row.type || []).join(' / '))}</td>
        <td><strong>${escapeHtml(row.overall_score)}</strong></td>
        <td>${escapeHtml(row.hook_text)}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
  } catch (error) {
    target.innerHTML = `<p class="error">加载 Top20 失败：${escapeHtml(error.message || error)}</p>`;
  }
}

async function generateDramaTop20(taskId) {
  if (!confirm('按 top20_reels.json 生成 Reel 视频？这会调用 FFmpeg，可能需要一些时间。')) return;
  try {
    const res = await fetch(`/api/drama-reels/${taskId}/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ selected_ids: [] }),
    });
    if (!res.ok) throw new Error(await readError(res));
    addCreatedTask(await res.json(), 'Top20 Reel 生成任务已创建');
  } catch (error) {
    alert('生成 Reel 失败：' + (error.message || error));
  }
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

async function readError(res) {
  const text = await res.text();
  try {
    const data = JSON.parse(text);
    return data.detail || text;
  } catch (error) {
    return text;
  }
}

checkRuntime();
checkVersion();
loadBaiduStatus();
loadExistingTasks();
setInterval(pollTasks, 3000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) pollTasks();
});
