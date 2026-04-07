/**
 * video2text — 前端主逻辑
 * 功能：多任务并行、SSE 实时进度、Toast 错误提示、任务停止、片段预览、逐条续写分镜
 */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);

  // ── 全局状态 ───────────────────────────────────────────────────────────────
  const allTasks = {};   // task_id -> { meta, refs, storyboard, storyboardSig, lastOutputUrl }
  let currentTaskId = null;

  function getTask(id) {
    if (!allTasks[id]) {
      allTasks[id] = {
        meta: {},
        videoRefs: [],
        imageRefs: [],
        subjects: [],
        storyboard: null,
        storyboardSig: null,
        lastOutputUrl: null,
        saveShotTimer: null,
        sseSource: null,
        // 表单状态（每个任务独立保存）
        form: {
          themeText: '',
          themeStyle: '',
          themeModel: '',
          minShots: '8',
          maxShots: '24',
          videoUrl: '',
          analyzeStyle: '',
          segmentScenes: false,
          textOnly: false,
          genStyle: '',
          genResolution: '',
          maxSeg: '15',
          maxWorkers: '4',
          videoFile: null,       // File object
          videoPreviewReset: true,  // dropzone 是否需要重置
          inputVideoUrl: '',     // 历史任务的输入视频 URL（从服务端恢复）
        },
      };
    }
    return allTasks[id];
  }

  // ── Toast 通知 ──────────────────────────────────────────────────────────────
  function showToast(msg, type = 'error', duration = 5000) {
    const container = $('#toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<span class="toast-msg">${escapeHtml(msg)}</span><span class="toast-close">✕</span>`;
    toast.querySelector('.toast-close').onclick = () => toast.remove();
    container.prepend(toast);
    if (duration > 0) {
      setTimeout(() => { if (toast.parentNode) toast.remove(); }, duration);
    }
  }

  // ── API 包装 ────────────────────────────────────────────────────────────────
  function api(path, opts = {}) {
    return fetch(path, {
      headers: opts.json ? { 'Content-Type': 'application/json' } : undefined,
      body: opts.body ? (opts.json ? JSON.stringify(opts.body) : opts.body) : undefined,
      method: opts.method || 'GET',
    }).then(async (r) => {
      if (r.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录');
      }
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      if (!r.ok) {
        const msg = (data && data.error) || (typeof data === 'string' ? data : '') || r.statusText || `HTTP ${r.status}`;
        throw new Error(msg);
      }
      return data != null ? data : {};
    });
  }

  // ── 工具 ──────────────────────────────────────────────────────────────────
  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
  function escapeAttr(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  }
  function statusLabel(s) {
    const map = {
      created: '已创建', pending: '等待中', theme_running: '分镜生成中',
      analyze_running: '视频分析中', storyboard_ready: '分镜就绪',
      queued_generate: '排队生成', generating: '视频生成中',
      done: '已完成', failed: '失败', cancelled: '已取消',
    };
    return map[s] || s || '未知';
  }
  function statusClass(s) {
    if (s === 'done') return 'done';
    if (s === 'failed') return 'failed';
    if (s === 'cancelled') return 'cancelled';
    if (['theme_running', 'analyze_running', 'generating', 'queued_generate'].includes(s)) return 'running';
    return 'other';
  }
  function isRunning(s) {
    return ['theme_running', 'analyze_running', 'generating', 'queued_generate'].includes(s);
  }

  // ── Drawer ─────────────────────────────────────────────────────────────────
  function openDrawer(id) {
    $('#overlay-' + id).classList.add('open');
    $('#drawer-' + id).classList.add('open');
  }
  function closeDrawer(id) {
    $('#overlay-' + id).classList.remove('open');
    $('#drawer-' + id).classList.remove('open');
  }

  // ── 任务状态栏（底部多任务栏）────────────────────────────────────────────
  function renderStatusBar() {
    const bar = $('#task-statusbar');
    const newBtn = $('#statusbar-new-btn');
    // 清除旧 pill 和 hint span
    bar.querySelectorAll('.task-pill, .statusbar-hint').forEach(p => p.remove());

    const ids = Object.keys(allTasks);
    if (ids.length === 0) {
      const hint = createEl('span', { className: 'statusbar-hint', style: 'font-size:12px;color:var(--muted)' }, '暂无任务，点击右侧新建');
      bar.insertBefore(hint, newBtn);
      return;
    }

    ids.forEach(tid => {
      const t = allTasks[tid];
      const st = t.meta.status || 'other';
      const pill = createEl('div', { className: 'task-pill' + (tid === currentTaskId ? ' active' : '') });
      const dot = createEl('div', { className: `pill-dot ${statusClass(st)}` });
      const label = createEl('span', {}, `${tid.slice(0, 8)} · ${statusLabel(st)}`);
      if (t.meta.segments_total && t.meta.segments_done !== undefined) {
        label.textContent += ` (${t.meta.segments_done}/${t.meta.segments_total})`;
      }
      pill.appendChild(dot);
      pill.appendChild(label);

      // 运行中显示取消按钮
      if (isRunning(st) || t.meta.cancelling) {
        const cancelBtn = createEl('span', { className: 'pill-cancel', title: '取消任务' }, '⏹');
        cancelBtn.onclick = (e) => { e.stopPropagation(); cancelTask(tid); };
        pill.appendChild(cancelBtn);
      }

      // 关闭按钮（从标签栏移除，不删除服务端数据）
      const closeBtn = createEl('span', { className: 'pill-close', title: '关闭标签' }, '✕');
      closeBtn.onclick = (e) => {
        e.stopPropagation();
        closeTaskTab(tid);
      };
      pill.appendChild(closeBtn);

      pill.onclick = () => switchTask(tid);
      bar.insertBefore(pill, newBtn);
    });
  }

  function createEl(tag, attrs = {}, text = '') {
    const el = document.createElement(tag);
    Object.keys(attrs).forEach(k => {
      if (k === 'style') {
        el.setAttribute('style', attrs[k]);
      } else if (k === 'className') {
        el.className = attrs[k];
      } else {
        el[k] = attrs[k];
      }
    });
    if (text) el.textContent = text;
    return el;
  }

  // ── 表单状态保存 / 恢复 ────────────────────────────────────────────────────
  function saveFormState(taskId) {
    if (!taskId || !allTasks[taskId]) return;
    const f = allTasks[taskId].form;
    f.themeText = ($('#theme-text') || {}).value || '';
    f.themeStyle = ($('#theme-style') || {}).value || '';
    f.themeModel = ($('#theme-model') || {}).value || '';
    f.minShots = ($('#min-shots') || {}).value || '8';
    f.maxShots = ($('#max-shots') || {}).value || '24';
    f.videoUrl = ($('#video-url') || {}).value || '';
    f.analyzeStyle = ($('#analyze-style') || {}).value || '';
    f.segmentScenes = $('#segment-scenes') ? $('#segment-scenes').checked : false;
    f.textOnly = $('#text-only') ? $('#text-only').checked : false;
    f.genStyle = ($('#gen-style') || {}).value || '';
    f.genResolution = ($('#gen-resolution') || {}).value || '';
    f.maxSeg = ($('#max-seg') || {}).value || '15';
    f.maxWorkers = ($('#max-workers') || {}).value || '4';
    // 视频文件：保存 File 对象引用
    const vf = $('#video-file');
    if (vf && vf.files && vf.files[0]) {
      f.videoFile = vf.files[0];
      f.videoPreviewReset = false;
    }
  }

  function restoreFormState(taskId) {
    const f = (allTasks[taskId] || {}).form;
    if (!f) return;
    if ($('#theme-text')) $('#theme-text').value = f.themeText || '';
    if ($('#theme-style')) $('#theme-style').value = f.themeStyle || '';
    if ($('#theme-model')) $('#theme-model').value = f.themeModel || '';
    if ($('#min-shots')) $('#min-shots').value = f.minShots || '8';
    if ($('#max-shots')) $('#max-shots').value = f.maxShots || '24';
    if ($('#video-url')) $('#video-url').value = f.videoUrl || '';
    if ($('#analyze-style')) $('#analyze-style').value = f.analyzeStyle || '';
    if ($('#segment-scenes')) $('#segment-scenes').checked = !!f.segmentScenes;
    if ($('#text-only')) {
      $('#text-only').checked = !!f.textOnly;
      $('#ref-section').classList.toggle('hidden', !!f.textOnly);
      const tw = $('#textonly-subject-wrap');
      if (tw) tw.classList.toggle('hidden', !f.textOnly);
    }
    if ($('#gen-style')) $('#gen-style').value = f.genStyle || '';
    if ($('#gen-resolution')) $('#gen-resolution').value = f.genResolution || '';
    if ($('#max-seg')) $('#max-seg').value = f.maxSeg || '15';
    if ($('#max-workers')) $('#max-workers').value = f.maxWorkers || '4';
    // 视频文件 / dropzone
    restoreVideoDropzone(f);
  }

  function restoreVideoDropzone(f) {
    const videoDrop = $('#video-drop');
    const videoFile = $('#video-file');
    if (!videoDrop || !videoFile) return;
    if (f.videoFile && !f.videoPreviewReset) {
      showAnalyzeVideoPreview(f.videoFile);
    } else if (f.inputVideoUrl) {
      // 从服务端 URL 恢复视频预览（历史任务加载）
      videoDrop.innerHTML = '';
      videoDrop.style.padding = '0';
      videoDrop.style.cursor = 'default';
      videoDrop.style.borderStyle = 'solid';
      const vid = document.createElement('video');
      vid.src = f.inputVideoUrl;
      vid.controls = true;
      vid.style.cssText = 'width:100%;max-height:260px;display:block;border-radius:var(--radius);';
      const changeBtn = document.createElement('button');
      changeBtn.type = 'button';
      changeBtn.className = 'ghost sm';
      changeBtn.textContent = '重新选择';
      changeBtn.style.cssText = 'margin:8px;font-size:12px;';
      changeBtn.onclick = () => {
        videoFile.value = '';
        f.inputVideoUrl = '';
        f.videoFile = null;
        f.videoPreviewReset = true;
        videoDrop.innerHTML = '拖拽视频到此处或点击选择（.mp4 等）';
        videoDrop.style.padding = '';
        videoDrop.style.cursor = '';
        videoDrop.style.borderStyle = '';
        bindVideoDrop();
      };
      videoDrop.appendChild(vid);
      videoDrop.appendChild(changeBtn);
    } else {
      videoFile.value = '';
      videoDrop.innerHTML = '拖拽视频到此处或点击选择（.mp4 等）';
      videoDrop.style.padding = '';
      videoDrop.style.cursor = '';
      videoDrop.style.borderStyle = '';
      bindVideoDrop();
    }
  }

  function resetFormToDefaults() {
    if ($('#theme-text')) $('#theme-text').value = '';
    if ($('#theme-style')) $('#theme-style').value = '';
    if ($('#theme-model')) $('#theme-model').value = '';
    if ($('#min-shots')) $('#min-shots').value = '8';
    if ($('#max-shots')) $('#max-shots').value = '24';
    if ($('#video-url')) $('#video-url').value = '';
    if ($('#analyze-style')) $('#analyze-style').value = '';
    if ($('#segment-scenes')) $('#segment-scenes').checked = false;
    if ($('#text-only')) {
      $('#text-only').checked = false;
      $('#ref-section').classList.remove('hidden');
      const tw = $('#textonly-subject-wrap');
      if (tw) tw.classList.add('hidden');
    }
    if ($('#gen-style')) $('#gen-style').value = '';
    if ($('#gen-resolution')) $('#gen-resolution').value = '';
    if ($('#max-seg')) $('#max-seg').value = '15';
    if ($('#max-workers')) $('#max-workers').value = '4';
    // 重置视频 dropzone
    const videoDrop = $('#video-drop');
    const videoFile = $('#video-file');
    if (videoDrop) {
      videoDrop.innerHTML = '拖拽视频到此处或点击选择（.mp4 等）';
      videoDrop.style.padding = '';
      videoDrop.style.cursor = '';
      videoDrop.style.borderStyle = '';
    }
    if (videoFile) videoFile.value = '';
    bindVideoDrop();
  }

  // ── 关闭任务标签（从 UI 移除，不删除服务端数据）──────────────────────────
  function closeTaskTab(tid) {
    const t = allTasks[tid];
    if (t && isRunning(t.meta.status)) {
      if (!confirm('该任务正在运行中，关闭标签不会停止后台任务。确认关闭？')) return;
    }
    if (t && t.sseSource) { t.sseSource.close(); t.sseSource = null; }
    delete allTasks[tid];
    if (currentTaskId === tid) {
      const remaining = Object.keys(allTasks);
      if (remaining.length > 0) {
        currentTaskId = null; // 清掉防止 switchTask 里 saveFormState 保存到已删除的任务
        switchTask(remaining[remaining.length - 1]);
      } else {
        currentTaskId = null;
        resetFormToDefaults();
        $('#task-id-display').textContent = '（点击「新建任务」）';
        $('#task-hint').textContent = '';
        $('#shots-container').innerHTML = '';
        $('#shots-empty').classList.remove('hidden');
        $('#output-wrap').classList.add('hidden');
        $('#progress-log').innerHTML = '';
        $('#gen-progress-wrap').classList.add('hidden');
        $('#gen-progress-bar').style.width = '0%';
        $('#subjects-list').innerHTML = '';
        renderSegments([]);
      }
    }
    renderStatusBar();
  }

  // ── 切换当前任务 ──────────────────────────────────────────────────────────
  function switchTask(id) {
    // 先保存当前任务的表单状态
    if (currentTaskId && currentTaskId !== id) {
      saveFormState(currentTaskId);
    }

    currentTaskId = id;
    const t = getTask(id);

    // 更新 task bar
    $('#task-id-display').textContent = id;
    renderHint(t.meta);

    // 恢复该任务的表单状态
    restoreFormState(id);

    // 如果任务有已保存的统一风格，优先用它覆盖表单中的风格字段
    if (t.meta.style !== undefined) {
      const savedStyle = t.meta.style || '';
      if ($('#gen-style')) $('#gen-style').value = savedStyle;
      if ($('#theme-style')) $('#theme-style').value = savedStyle;
      if ($('#analyze-style')) $('#analyze-style').value = savedStyle;
    }

    // 主体卡片：先用缓存立即渲染，再后台拉新数据（避免空白等待）
    renderSubjects();
    loadSubjects(id);

    // 渲染参考列表
    renderRefLists();

    // 渲染分镜
    if (t.storyboard) {
      renderShots();
      $('#shots-empty').classList.add('hidden');
    } else {
      $('#shots-container').innerHTML = '';
      $('#shots-empty').classList.remove('hidden');
    }

    // 渲染进度（强制传入当前任务 ID，确保只渲染本任务日志）
    renderProgress(t.meta, id);

    // 渲染输出
    if (t.lastOutputUrl) {
      $('#output-wrap').classList.remove('hidden');
      $('#output-video').src = t.lastOutputUrl;
      $('#download-link').href = t.lastOutputUrl;
    } else {
      $('#output-wrap').classList.add('hidden');
    }

    // 更新片段预览
    renderSegments(t.meta.segments || []);

    // 更新状态栏高亮
    renderStatusBar();
  }

  // ── 新建任务 ────────────────────────────────────────────────────────────────
  async function createNewTask() {
    // 先保存当前任务的表单状态
    if (currentTaskId) saveFormState(currentTaskId);
    try {
      const { task_id } = await api('/api/task/create', { method: 'POST', json: true, body: {} });
      const t = getTask(task_id);
      t.meta = { task_id, status: 'created' };
      currentTaskId = task_id;
      connectSSE(task_id);
      // 重置所有表单到默认值
      resetFormToDefaults();
      // 清空任务相关 UI
      $('#task-id-display').textContent = task_id;
      renderHint(t.meta);
      renderSubjects();
      renderRefLists();
      $('#shots-container').innerHTML = '';
      $('#shots-empty').classList.remove('hidden');
      $('#output-wrap').classList.add('hidden');
      renderProgress(t.meta, task_id);
      renderSegments([]);
      renderStatusBar();
      return task_id;
    } catch (e) {
      showToast('新建任务失败：' + e.message);
      throw e;
    }
  }

  if ($('#btn-new-task')) $('#btn-new-task').onclick = () => createNewTask();
  if ($('#statusbar-new-btn')) $('#statusbar-new-btn').onclick = () => createNewTask();

  // ── SSE 连接 ────────────────────────────────────────────────────────────────
  function connectSSE(task_id) {
    const t = getTask(task_id);
    if (t.sseSource) { t.sseSource.close(); t.sseSource = null; }

    const es = new EventSource(`/api/task/stream/${task_id}`);
    t.sseSource = es;

    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }

      if (data.type === 'snapshot') {
        // 初始快照：只更新本任务的数据
        t.meta = { ...t.meta, ...data };
        delete t.meta.type;
        if (data.storyboard && JSON.stringify(data.storyboard) !== t.storyboardSig) {
          t.storyboard = data.storyboard;
          t.storyboardSig = JSON.stringify(data.storyboard);
          if (task_id === currentTaskId) renderShots();
        }
        if (data.output_url && data.output_url !== t.lastOutputUrl) {
          t.lastOutputUrl = data.output_url;
          if (task_id === currentTaskId) {
            $('#output-wrap').classList.remove('hidden');
            $('#output-video').src = data.output_url;
            $('#download-link').href = data.output_url;
          }
        }
        // 快照到达后刷新该任务的日志和状态（仅当正在查看该任务时）
        if (task_id === currentTaskId) {
          renderProgress(t.meta, task_id);
          renderHint(t.meta);
        }
      } else if (data.type === 'progress') {
        const prog = t.meta.progress || [];
        prog.push({ t: data.t, msg: data.msg });
        t.meta.progress = prog.slice(-500);
        renderProgress(t.meta, task_id);
      } else if (data.type === 'status') {
        t.meta.status = data.status;
        if (data.error) t.meta.error = data.error;
        if (data.shot_count !== undefined) t.meta.shot_count = data.shot_count;
        if (task_id === currentTaskId) renderHint(t.meta);
        renderStatusBar();

        // 分镜就绪时立即拉取最新 storyboard 刷新预览
        if (data.status === 'storyboard_ready') {
          // 把服务端返回的 style 回填到输入框（强制统一风格）
          if (data.style !== undefined) {
            const t2 = getTask(task_id);
            t2.meta.style = data.style;
            if (task_id === currentTaskId) {
              if ($('#gen-style')) $('#gen-style').value = data.style;
              if ($('#theme-style')) $('#theme-style').value = data.style;
              if ($('#analyze-style')) $('#analyze-style').value = data.style;
            }
          }
          refreshTaskOnce(task_id);
          if (task_id === currentTaskId) {
            showToast(`分镜已生成（${data.shot_count || '?'} 镜），请查看步骤 3 预览`, 'success', 4000);
            $('#step3').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }
          // 主体也可能很快就绪，延迟轮询兜底（防止 SSE subjects_ready 丢失）
          _pollSubjectsUntilReady(task_id);
        }

        const terminal = ['done', 'failed', 'cancelled'];
        if (terminal.includes(data.status)) {
          if (data.status === 'done') showToast(`任务 ${task_id.slice(0,8)} 已完成！`, 'success');
          if (data.status === 'failed') showToast(`任务 ${task_id.slice(0,8)} 失败：${data.error || ''}`, 'error', 8000);
          if (data.status === 'cancelled') showToast(`任务 ${task_id.slice(0,8)} 已取消`, 'warning');
          es.close();
          t.sseSource = null;
          // 拉一次最终状态补全 segments / output_url
          refreshTaskOnce(task_id);
        }
      } else if (data.type === 'subjects_ready') {
        // 主体描述生成完成，直接用 SSE 携带的数据填充，省去额外 HTTP 请求
        if (data.subjects && data.subjects.length) {
          const t = getTask(task_id);
          t.subjects = data.subjects;
          if (task_id === currentTaskId) renderSubjects();
        } else {
          loadSubjects(task_id);
        }
      }
    };

    let _reconnectTimer = null;
    es.onerror = () => {
      es.close();
      t.sseSource = null;
      // 若任务仍在运行，3s 后自动重连，并拉一次最新进度补全日志
      const cur = getTask(task_id);
      if (isRunning(cur.meta.status) || !cur.meta.status) {
        clearTimeout(_reconnectTimer);
        _reconnectTimer = setTimeout(() => {
          refreshTaskOnce(task_id);   // 补全日志
          connectSSE(task_id);        // 重新订阅
        }, 3000);
      }
    };
  }

  async function refreshTaskOnce(task_id) {
    try {
      const st = await api('/api/task/' + task_id);
      const t = getTask(task_id);
      t.meta = { ...t.meta, ...st };
      delete t.meta.storyboard;
      // 回填统一风格到输入框
      if (st.style !== undefined && task_id === currentTaskId) {
        if ($('#gen-style')) $('#gen-style').value = st.style || '';
        if ($('#theme-style')) $('#theme-style').value = st.style || '';
        if ($('#analyze-style')) $('#analyze-style').value = st.style || '';
      }

      if (st.storyboard && JSON.stringify(st.storyboard) !== t.storyboardSig) {
        t.storyboard = st.storyboard;
        t.storyboardSig = JSON.stringify(st.storyboard);
        if (task_id === currentTaskId) { renderShots(); $('#shots-empty').classList.add('hidden'); }
      }
      if (st.output_url && st.output_url !== t.lastOutputUrl) {
        t.lastOutputUrl = st.output_url;
        if (task_id === currentTaskId) {
          $('#output-wrap').classList.remove('hidden');
          $('#output-video').src = st.output_url;
          $('#download-link').href = st.output_url;
        }
      }
      if (task_id === currentTaskId) {
        renderProgress(t.meta, task_id);
        renderHint(t.meta);
        renderSegments(st.segments || []);
      }
      renderStatusBar();
    } catch (_) {}
  }

  // ── 取消任务 ────────────────────────────────────────────────────────────────
  async function cancelTask(task_id) {
    try {
      await api(`/api/task/cancel/${task_id}`, { method: 'POST', json: true, body: {} });
      showToast('取消信号已发送，等待当前段完成后停止', 'warning', 4000);
      const t = getTask(task_id);
      t.meta.cancelling = true;
      renderStatusBar();
    } catch (e) {
      showToast('取消失败：' + e.message);
    }
  }

  // ── 时间格式化（UTC → 本地时区，HH:MM:SS） ──────────────────────────────────
  function formatLogTime(isoStr) {
    if (!isoStr) return '';
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return isoStr.slice(11, 19) || '';
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      return `${hh}:${mm}:${ss}`;
    } catch (_) {
      return isoStr.slice(11, 19) || '';
    }
  }

  function formatDateTime(isoStr) {
    if (!isoStr) return '';
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return isoStr.slice(0, 19);
      return d.toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-');
    } catch (_) {
      return isoStr.slice(0, 19);
    }
  }

  // ── 进度渲染 ────────────────────────────────────────────────────────────────
  function renderProgress(meta, taskId) {
    // 安全校验：只渲染当前任务的日志，防止后台任务的回调污染当前视图
    const tid = taskId || meta.task_id || currentTaskId;
    if (tid && tid !== currentTaskId) return;

    const log = $('#progress-log');
    const lines = (meta.progress || []).map(p =>
      `<div>[${formatLogTime(p.t)}] ${escapeHtml(p.msg)}</div>`
    );
    if (meta.error && meta.status === 'failed') {
      lines.push(`<div class="log-error">✗ ${escapeHtml(meta.error)}</div>`);
    }
    if (['theme_running', 'analyze_running'].includes(meta.status)) {
      lines.push(`<div class="log-waiting" id="log-waiting-dot">⏳ Waiting for LLM response<span class="dot-anim">…</span></div>`);
    }
    log.innerHTML = lines.join('');
    log.scrollTop = log.scrollHeight;

    // 进度条
    const bar = $('#gen-progress-bar');
    const wrap = $('#gen-progress-wrap');
    if (meta.segments_total && meta.segments_done !== undefined) {
      wrap.classList.remove('hidden');
      bar.style.width = `${Math.round((meta.segments_done / meta.segments_total) * 100)}%`;
    } else if (meta.status === 'done') {
      wrap.classList.remove('hidden');
      bar.style.width = '100%';
    } else {
      wrap.classList.add('hidden');
      bar.style.width = '0%';
    }
  }

  function renderHint(meta) {
    const hint = $('#task-hint');
    if (!hint) return;
    hint.className = '';
    const st = meta.status || '';
    if (st === 'theme_running') hint.textContent = '主题分镜生成中…';
    else if (st === 'analyze_running') hint.textContent = '视频分析中…';
    else if (st === 'generating' || st === 'queued_generate') {
      hint.textContent = meta.cancelling ? '取消中…' : '视频生成中…';
    }
    else if (st === 'storyboard_ready') hint.textContent = `分镜就绪（${meta.shot_count || '?'} 镜），可编辑第 3 步`;
    else if (st === 'failed') {
      hint.textContent = (meta.error || '失败').slice(0, 120);
      hint.classList.add('hint-failed');
    }
    else if (st === 'cancelled') {
      hint.textContent = '已取消，已生成的片段缓存保留，可继续生成';
      hint.className = 'hint-cancelled';
    }
    else if (st === 'done') { hint.textContent = '已完成 ✓'; hint.classList.add('hint-done'); }
    else hint.textContent = '';

    // 停止按钮显示
    const stopBtn = $('#btn-stop');
    if (stopBtn) {
      stopBtn.classList.toggle('hidden', !isRunning(st) && !meta.cancelling);
    }
  }

  // ── 片段预览 ────────────────────────────────────────────────────────────────
  function renderSegments(segments) {
    const grid = $('#segments-grid');
    if (!grid) return;
    if (!segments || segments.length === 0) {
      grid.innerHTML = '';
      grid.parentElement && grid.parentElement.classList.add('hidden');
      return;
    }
    grid.parentElement && grid.parentElement.classList.remove('hidden');
    grid.innerHTML = '';
    segments.forEach(seg => {
      const wrap = document.createElement('div');
      wrap.className = 'segment-thumb';
      wrap.innerHTML = `<video src="${escapeAttr(seg.url)}" muted playsinline preload="metadata"></video>
        <div class="seg-label">${escapeHtml(seg.name)}</div>`;
      wrap.querySelector('video').onmouseenter = function() { this.play(); };
      wrap.querySelector('video').onmouseleave = function() { this.pause(); this.currentTime = 0; };
      grid.appendChild(wrap);
    });
  }

  // ── 主体描述（subjects）────────────────────────────────────────────────────

  // 分镜就绪后轮询，直到主体卡片数据出现（最多等 3 分钟，每 8s 一次）
  // 主要用于兜底：防止 SSE subjects_ready 因断线等原因丢失
  let _pollSubjectsTimer = null;
  function _pollSubjectsUntilReady(task_id) {
    clearTimeout(_pollSubjectsTimer);
    let attempts = 0;
    async function poll() {
      const t = getTask(task_id);
      // 已经有数据或任务已终止就停
      if ((t.subjects && t.subjects.length > 0) || isRunning(t.meta.status) === false && t.meta.status !== 'storyboard_ready') return;
      try {
        const r = await api('/api/task/subjects/' + task_id);
        const fetched = r.subjects || [];
        if (fetched.length > 0) {
          t.subjects = fetched;
          if (task_id === currentTaskId) renderSubjects();
          return; // 已拿到，停止轮询
        }
      } catch (_) {}
      attempts++;
      if (attempts < 22) { // 最多轮询约 3 分钟
        _pollSubjectsTimer = setTimeout(poll, 8000);
      }
    }
    // 稍等一下再开始，给 LLM 一点启动时间
    _pollSubjectsTimer = setTimeout(poll, 5000);
  }

  async function loadSubjects(task_id) {
    const t = getTask(task_id);
    const alreadyHas = t.subjects && t.subjects.length > 0;
    // 已有缓存数据时直接渲染，不显示骨架（避免闪烁）
    if (!alreadyHas && task_id === currentTaskId) {
      const list = $('#subjects-list');
      if (list) list.innerHTML = '<div class="subjects-loading"><span class="loading-spinner"></span> 加载主体描述中…</div>';
    }
    try {
      const r = await api('/api/task/subjects/' + task_id);
      const fetched = r.subjects || [];
      // 仅当服务端有数据，或内存中本来就没有时才更新（防止覆盖 SSE 已推来的最新数据）
      if (fetched.length > 0 || !alreadyHas) {
        t.subjects = fetched;
      }
      if (task_id === currentTaskId) renderSubjects();
    } catch (_) {
      if (task_id === currentTaskId) renderSubjects();
    }
  }

  async function saveSubjects() {
    if (!currentTaskId) return;
    const t = getTask(currentTaskId);
    try {
      await api('/api/task/subjects/' + currentTaskId, {
        method: 'PUT', json: true, body: { subjects: t.subjects }
      });
    } catch (e) {
      showToast('保存主体失败：' + e.message, 'error', 3000);
    }
  }

  // 将主体名字列表提取出来（用于 @chip 匹配）
  function getSubjectNames(task_id) {
    const t = allTasks[task_id];
    if (!t) return [];
    return (t.subjects || []).map(s => s.name).filter(Boolean);
  }

  // 将文本中的 @Name 替换为 chip span
  function renderMentionChips(text, names) {
    if (!text || !names.length) return escapeHtml(text);
    // 按名字长度从长到短排序，避免短名截断长名
    const sorted = [...names].sort((a, b) => b.length - a.length);
    const escaped = escapeHtml(text);
    // 对每个名字做替换（在 HTML 转义后的文本里匹配）
    let result = escaped;
    sorted.forEach(name => {
      const eName = escapeHtml(name);
      const re = new RegExp('@' + eName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g');
      result = result.replace(re, `<span class="mention-chip">@${eName}</span>`);
    });
    return result;
  }

  let _translateTimers = {};

  async function translateField(task_id, idx, fromField, toField) {
    const t = getTask(task_id);
    const text = (t.subjects[idx] || {})[fromField] || '';
    if (!text.trim()) return;
    const target = toField === 'description_en' ? 'en' : 'zh';
    try {
      const r = await api('/api/translate', {
        method: 'POST', json: true,
        body: { text, target }
      });
      if (r.result && t.subjects[idx]) {
        t.subjects[idx][toField] = r.result;
        // 更新对应的 textarea（不触发再次翻译）
        const card = $('#subjects-list').querySelector(`[data-sidx="${idx}"]`);
        if (card) {
          const ta = card.querySelector(`[data-sfield="${toField}"]`);
          if (ta && document.activeElement !== ta) ta.value = r.result;
        }
        saveSubjects();
      }
    } catch (_) {}
  }

  function renderSubjects() {
    const t = getTask(currentTaskId);
    const list = $('#subjects-list');
    if (!list) return;
    list.innerHTML = '';

    if (!t.subjects || t.subjects.length === 0) {
      // 任务还在运行（分镜生成中）或刚完成分镜还没写完主体 → 显示生成中
      const generatingSubjects = isRunning(t.meta.status) || t.meta.status === 'storyboard_ready';
      if (generatingSubjects) {
        list.innerHTML = '<div class="subjects-loading"><span class="loading-spinner"></span> 主体描述生成中，请稍候…</div>';
      } else if (t.meta.status && t.meta.status !== 'created') {
        list.innerHTML = '<div class="subjects-empty">暂无主体，点击「+ 手动添加主体」创建</div>';
      }
      return;
    }

    (t.subjects || []).forEach((subj, idx) => {
      const card = document.createElement('div');
      card.className = 'subject-card';
      card.dataset.sidx = idx;
      card.innerHTML = `
        <div class="subject-card-header">
          <div class="subject-names">
            <input class="subject-name-en" data-sidx="${idx}" data-sfield="name"
              value="${escapeAttr(subj.name || '')}" placeholder="Name (EN)" />
            <span class="subject-name-sep">/</span>
            <input class="subject-name-zh" data-sidx="${idx}" data-sfield="name_zh"
              value="${escapeAttr(subj.name_zh || '')}" placeholder="中文名" />
          </div>
          <button class="ghost sm subject-del" data-sidx="${idx}" title="删除">✕</button>
        </div>
        <div class="subject-fields">
          <div class="subject-field">
            <label class="subject-field-label">EN Description <span class="field-tag">→ prompt</span></label>
            <textarea data-sidx="${idx}" data-sfield="description_en" rows="3"
              placeholder="Detailed English description for video prompt injection…">${escapeHtml(subj.description_en || '')}</textarea>
          </div>
          <div class="subject-field">
            <label class="subject-field-label">中文描述 <span class="field-tag">↔ 自动互译</span></label>
            <textarea data-sidx="${idx}" data-sfield="description_zh" rows="3"
              placeholder="中文描述，失焦后自动翻译为英文…">${escapeHtml(subj.description_zh || '')}</textarea>
          </div>
        </div>`;
      list.appendChild(card);
    });

    // 绑定事件
    list.querySelectorAll('input[data-sfield], textarea[data-sfield]').forEach(el => {
      const idx = +el.dataset.sidx;
      const field = el.dataset.sfield;
      el.oninput = () => {
        const t = getTask(currentTaskId);
        if (t.subjects[idx]) t.subjects[idx][field] = el.value;
      };
      el.onblur = () => {
        const t = getTask(currentTaskId);
        if (t.subjects[idx]) t.subjects[idx][field] = el.value;
        saveSubjects();
        // 自动互译
        if (field === 'description_zh' && el.value.trim()) {
          clearTimeout(_translateTimers[`${idx}_zh`]);
          _translateTimers[`${idx}_zh`] = setTimeout(
            () => translateField(currentTaskId, idx, 'description_zh', 'description_en'), 200
          );
        } else if (field === 'description_en' && el.value.trim()) {
          clearTimeout(_translateTimers[`${idx}_en`]);
          _translateTimers[`${idx}_en`] = setTimeout(
            () => translateField(currentTaskId, idx, 'description_en', 'description_zh'), 200
          );
        }
      };
    });

    list.querySelectorAll('.subject-del').forEach(btn => {
      btn.onclick = () => {
        const t = getTask(currentTaskId);
        t.subjects.splice(+btn.dataset.sidx, 1);
        saveSubjects();
        renderSubjects();
        renderShots(); // 刷新 chip 高亮
      };
    });
  }

  // 新增一个空主体
  if ($('#btn-add-subject')) {
    $('#btn-add-subject').onclick = () => {
      if (!currentTaskId) { showToast('请先新建任务'); return; }
      const t = getTask(currentTaskId);
      t.subjects.push({ name: '', name_zh: '', description_en: '', description_zh: '' });
      renderSubjects();
      // 聚焦到最新 name 输入
      const cards = $('#subjects-list').querySelectorAll('.subject-card');
      if (cards.length) {
        const last = cards[cards.length - 1];
        const inp = last.querySelector('.subject-name-en');
        if (inp) inp.focus();
      }
    };
  }

  // ── 分镜翻译缓存（idx -> {scene_zh, dialogue_zh, prompt_zh}）────────────────
  let _shotTransCache = {};

  async function translateShotField(idx, field, text) {
    const cacheKey = `${currentTaskId}_${idx}_${field}`;
    // 避免重复请求
    if (_shotTransCache[cacheKey] === text) return;
    const cardEl = $('#shots-container').querySelector(`.shot-card[data-shot-idx="${idx}"]`);
    if (!cardEl) return;
    const zhEl = cardEl.querySelector(`[data-zh="${field}"]`);
    if (!zhEl) return;
    zhEl.textContent = '翻译中…';
    try {
      const r = await api('/api/translate', {
        method: 'POST', json: true, body: { text, target: 'zh' },
      });
      const result = r.result || '';
      zhEl.textContent = result;
      _shotTransCache[cacheKey] = text;
    } catch (_) {
      zhEl.textContent = '（翻译失败）';
    }
  }

  // ── 分镜编辑 ────────────────────────────────────────────────────────────────
  function renderShots() {
    const t = getTask(currentTaskId);
    const sb = t.storyboard;
    if (!sb || !sb.shots) return;
    const names = getSubjectNames(currentTaskId);
    const c = $('#shots-container');
    c.innerHTML = '';
    sb.shots.forEach((shot, idx) => {
      const card = document.createElement('div');
      card.className = 'shot-card';
      card.dataset.shotIdx = idx;
      const hasPrompt = (shot.generation_prompt || '').trim().length > 0;

      // 检测本镜中出现的角色
      const shotText = [shot.dialogue, shot.character_action, shot.scene_description].join(' ');
      const mentioned = names.filter(n => shotText.includes(n) || shotText.includes('@' + n));
      const chipsHtml = mentioned.map(n => `<span class="mention-chip">@${escapeHtml(n)}</span>`).join('');

      // scene_description 里的 @Name 高亮
      const sceneHtml = renderMentionChips((shot.scene_description || '').slice(0, 200), names);

      // 找出本镜头出现的角色及其英文描述
      const mentionedSubjects = (getTask(currentTaskId).subjects || [])
        .filter(s => s.name && mentioned.includes(s.name) && s.description_en);

      card.innerHTML = `
        <header>
          <span>Shot ${shot.shot_id} · ${shot.duration}s · ${escapeHtml(shot.shot_type || '')}</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <div class="shot-mentions">${chipsHtml}</div>
            <button class="btn-shot-translate ghost sm" data-sidx="${idx}" title="翻译本镜场景/对白/prompt 为中文" style="font-size:11px;padding:2px 7px;opacity:0.7;">🌐 译</button>
          </div>
        </header>
        <div class="scene-desc">${sceneHtml}</div>
        <div class="shot-zh-block hidden" data-zh-block="${idx}">
          <div class="shot-zh-row">
            <span class="shot-zh-label">场景（中）</span>
            <span class="shot-zh-text" data-zh="scene">${escapeHtml(shot.scene_zh || '')}</span>
          </div>
          <div class="shot-zh-row">
            <span class="shot-zh-label">对白（中）</span>
            <span class="shot-zh-text" data-zh="dialogue">${escapeHtml(shot.dialogue_zh || '')}</span>
          </div>
          <div class="shot-zh-row">
            <span class="shot-zh-label">画面指令（中）</span>
            <span class="shot-zh-text" data-zh="prompt">${escapeHtml(shot.prompt_zh || '')}</span>
          </div>
        </div>
        <label>对白 dialogue（英文，标明说话人，如 Alex: "Are you okay?"）</label>
        <textarea data-field="dialogue" data-idx="${idx}">${escapeHtml(shot.dialogue || '')}</textarea>
        <div class="gp-label-row">
          <label>generation_prompt（英文，视频画面指令）</label>
          ${mentionedSubjects.length ? `<button class="btn-inject-subject ghost sm" data-idx="${idx}" title="将本镜头角色的详细描述前置注入到 generation_prompt，提升跨镜一致性">⚡ 注入角色描述</button>` : ''}
        </div>
        <textarea data-field="generation_prompt" data-idx="${idx}">${escapeHtml(shot.generation_prompt || '')}</textarea>
        ${!hasPrompt ? '<div class="prompt-warn">⚠ 未填写，将使用场景描述自动生成</div>' : ''}`;
      c.appendChild(card);
    });

    // 翻译按钮：展开/翻译中文块
    c.querySelectorAll('.btn-shot-translate').forEach(btn => {
      btn.onclick = async () => {
        const idx = +btn.dataset.sidx;
        const t2 = getTask(currentTaskId);
        const shot = t2.storyboard && t2.storyboard.shots[idx];
        if (!shot) return;
        const block = c.querySelector(`[data-zh-block="${idx}"]`);
        if (!block) return;
        const isHidden = block.classList.contains('hidden');
        if (isHidden) {
          block.classList.remove('hidden');
          btn.textContent = '🌐 收起';
          // 触发翻译（各字段独立，不阻塞）
          if (shot.scene_description && !block.querySelector('[data-zh="scene"]').textContent.trim()) {
            translateShotField(idx, 'scene', shot.scene_description);
          }
          if (shot.dialogue && !block.querySelector('[data-zh="dialogue"]').textContent.trim()) {
            translateShotField(idx, 'dialogue', shot.dialogue);
          }
          const gpText = shot.generation_prompt || shot.scene_description || '';
          if (gpText && !block.querySelector('[data-zh="prompt"]').textContent.trim()) {
            translateShotField(idx, 'prompt', gpText);
          }
        } else {
          block.classList.add('hidden');
          btn.textContent = '🌐 译';
        }
      };
    });

    c.querySelectorAll('textarea').forEach(ta => {
      ta.onblur = scheduleSaveStoryboard;
      ta.oninput = () => {
        const card = ta.closest('.shot-card');
        const warn = card && card.querySelector('.prompt-warn');
        if (warn && ta.dataset.field === 'generation_prompt') {
          warn.classList.toggle('hidden', ta.value.trim().length > 0);
        }
      };
    });

    // 注入角色描述按钮
    c.querySelectorAll('.btn-inject-subject').forEach(btn => {
      btn.onclick = () => {
        const idx = +btn.dataset.idx;
        const t = getTask(currentTaskId);
        const shot = t.storyboard && t.storyboard.shots[idx];
        if (!shot) return;
        const names = getSubjectNames(currentTaskId);
        const shotText = [shot.dialogue, shot.character_action, shot.scene_description, shot.generation_prompt].join(' ');
        const relevant = (t.subjects || []).filter(s => s.name && names.includes(s.name) &&
          (shotText.includes(s.name) || shotText.includes('@' + s.name)) && s.description_en);
        if (!relevant.length) { showToast('未找到本镜头相关角色描述', 'warning', 2000); return; }
        // 构建前缀：[Character descriptions] Name: desc; Name2: desc2.
        const prefix = '[Character descriptions] ' +
          relevant.map(s => `${s.name}: ${s.description_en.trim()}`).join('; ') + '.';
        const ta = btn.closest('.shot-card').querySelector('textarea[data-field="generation_prompt"]');
        const existing = ta.value.trim();
        // 若已有同样的前缀则不重复注入
        if (existing.startsWith('[Character descriptions]')) {
          // 替换旧前缀
          const rest = existing.replace(/^\[Character descriptions\][^.]*\.\s*/, '');
          ta.value = `${prefix} ${rest}`.trim();
        } else {
          ta.value = existing ? `${prefix} ${existing}` : prefix;
        }
        // 同步到 storyboard 并触发保存
        if (t.storyboard.shots[idx]) t.storyboard.shots[idx].generation_prompt = ta.value;
        scheduleSaveStoryboard();
        // 隐藏警告
        const warn = btn.closest('.shot-card').querySelector('.prompt-warn');
        if (warn) warn.classList.add('hidden');
        btn.textContent = '✓ 已注入';
        setTimeout(() => { btn.textContent = '⚡ 注入角色描述'; }, 2000);
      };
    });
  }

  function scheduleSaveStoryboard() {
    const t = getTask(currentTaskId);
    clearTimeout(t.saveShotTimer);
    t.saveShotTimer = setTimeout(saveStoryboardNow, 600);
  }

  async function saveStoryboardNow() {
    const t = getTask(currentTaskId);
    if (!currentTaskId || !t.storyboard) return;
    const tas = $('#shots-container').querySelectorAll('textarea');
    tas.forEach(ta => {
      const idx = +ta.dataset.idx;
      const field = ta.dataset.field;
      if (t.storyboard.shots[idx]) t.storyboard.shots[idx][field] = ta.value;
    });
    try {
      await api('/api/storyboard/' + currentTaskId, { method: 'PUT', json: true, body: t.storyboard });
      t.storyboardSig = JSON.stringify(t.storyboard);
    } catch (e) {
      showToast('保存分镜失败：' + e.message, 'error', 4000);
    }
  }

  // ── 参考媒体列表 ─────────────────────────────────────────────────────────────

  let _saveRefsTimer = null;
  function scheduleSaveRefs() {
    clearTimeout(_saveRefsTimer);
    _saveRefsTimer = setTimeout(saveRefsNow, 800);
  }

  async function saveRefsNow() {
    if (!currentTaskId) return;
    const t = getTask(currentTaskId);
    const refs = [];
    (t.videoRefs || []).forEach(v => {
      refs.push({ path: v.path, name: v.name || '', kind: 'video', desc: v.desc || '' });
    });
    (t.imageRefs || []).forEach(im => {
      refs.push({ path: im.path, name: im.name || '', kind: 'image', desc: im.subject || '' });
    });
    try {
      await api('/api/task/references/' + currentTaskId, {
        method: 'PUT', json: true, body: { reference_files: refs },
      });
    } catch (_) {}
  }

  function renderRefLists() {
    const t = getTask(currentTaskId);
    const lv = $('#list-videos');
    lv.innerHTML = '';
    (t.videoRefs || []).forEach((v, i) => {
      const div = document.createElement('div');
      div.className = 'ref-item';
      const thumbHtml = v.url
        ? `<video class="ref-thumb-video" src="${escapeAttr(v.url)}" muted playsinline preload="metadata"></video>`
        : `<div class="thumb">视频${i+1}</div>`;
      div.innerHTML = `${thumbHtml}
        <div>
          <strong>视频 ${i + 1}</strong>
          <label>人物/内容说明（用于 prompt 主体声明，如 "男主角，黑色外套"）</label>
          <input type="text" data-vi="${i}" class="video-desc-inp" value="${escapeAttr(v.desc)}" placeholder="e.g.: female lead, short black hair, dark jacket" />
        </div>
        <button class="ghost sm" data-vi="${i}" data-rm="v" title="移除">✕</button>`;
      lv.appendChild(div);
    });
    lv.querySelectorAll('.video-desc-inp').forEach(inp => {
      inp.oninput = () => { t.videoRefs[+inp.dataset.vi].desc = inp.value; scheduleSaveRefs(); };
    });
    lv.querySelectorAll('[data-rm="v"]').forEach(btn => {
      btn.onclick = () => {
        t.videoRefs.splice(+btn.dataset.vi, 1);
        renderRefLists();
        scheduleSaveRefs();
      };
    });

    const li = $('#list-images');
    li.innerHTML = '';
    (t.imageRefs || []).forEach((im, j) => {
      const div = document.createElement('div');
      div.className = 'ref-item';
      const thumbHtml = im.url
        ? `<img class="ref-thumb-img" src="${escapeAttr(im.url)}" />`
        : `<div class="thumb">图${j+1}</div>`;
      div.innerHTML = `${thumbHtml}
        <div>
          <strong>图 ${j + 1}</strong>
          <label>主体描述（图${j + 1} 对应哪个人物）</label>
          <input type="text" data-ii="${j}" class="img-sub-inp" value="${escapeAttr(im.subject)}" placeholder="e.g.: female lead, short black hair, dark jacket" />
        </div>
        <button class="ghost sm" data-ii="${j}" data-rm="i" title="移除">✕</button>`;
      li.appendChild(div);
    });
    li.querySelectorAll('.img-sub-inp').forEach(inp => {
      inp.oninput = () => { t.imageRefs[+inp.dataset.ii].subject = inp.value; scheduleSaveRefs(); };
    });
    li.querySelectorAll('[data-rm="i"]').forEach(btn => {
      btn.onclick = () => {
        t.imageRefs.splice(+btn.dataset.ii, 1);
        renderRefLists();
        scheduleSaveRefs();
      };
    });
  }

  // ── 上传参考 ────────────────────────────────────────────────────────────────
  async function handleFiles(kind, files) {
    if (!currentTaskId) { showToast('请先「新建任务」'); return; }
    if (!files || !files.length) return;
    const fd = new FormData();
    fd.append('task_id', currentTaskId);
    for (const f of files) fd.append('files', f);
    try {
      const r = await fetch('/api/upload/reference', { method: 'POST', body: fd });
      if (r.status === 401) { window.location.href = '/login'; return; }
      const j = await r.json();
      if (!r.ok) { showToast(j.error || '上传失败'); return; }
      const t = getTask(currentTaskId);
      for (const file of j.files) {
        if (file.kind === 'video') t.videoRefs.push({ path: file.path, name: file.name, desc: '' });
        else t.imageRefs.push({ path: file.path, name: file.name, subject: '' });
      }
      renderRefLists();
      showToast(`已上传 ${j.files.length} 个文件`, 'success', 2500);
    } catch (e) {
      showToast('上传失败：' + e.message);
    }
  }

  function setupDrop(dropId, inputId, kind) {
    const dz = $('#' + dropId);
    const inp = $('#' + inputId);
    if (!dz || !inp) return;
    dz.onclick = () => inp.click();
    dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('drag'); };
    dz.ondragleave = () => dz.classList.remove('drag');
    dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove('drag'); handleFiles(kind, e.dataTransfer.files); };
    inp.onchange = () => handleFiles(kind, inp.files);
  }
  setupDrop('drop-videos', 'input-videos', 'video');
  setupDrop('drop-images', 'input-images', 'image');

  // 视频分析文件拖拽（选择后显示内嵌播放器）
  const videoDrop = $('#video-drop');
  const videoFile = $('#video-file');

  function showAnalyzeVideoPreview(file) {
    if (!file || !videoDrop) return;
    const url = URL.createObjectURL(file);
    videoDrop.innerHTML = '';
    videoDrop.style.padding = '0';
    videoDrop.style.cursor = 'default';
    videoDrop.style.borderStyle = 'solid';
    const vid = document.createElement('video');
    vid.src = url;
    vid.controls = true;
    vid.style.cssText = 'width:100%;max-height:260px;display:block;border-radius:var(--radius);';
    const changeBtn = document.createElement('button');
    changeBtn.type = 'button';
    changeBtn.className = 'ghost sm';
    changeBtn.textContent = '重新选择';
    changeBtn.style.cssText = 'margin:8px;font-size:12px;';
    changeBtn.onclick = () => {
      videoFile.value = '';
      videoDrop.innerHTML = '拖拽视频到此处或点击选择（.mp4 等）';
      videoDrop.style.padding = '';
      videoDrop.style.cursor = '';
      videoDrop.style.borderStyle = '';
      URL.revokeObjectURL(url);
      bindVideoDrop();
    };
    videoDrop.appendChild(vid);
    videoDrop.appendChild(changeBtn);
  }

  function bindVideoDrop() {
    if (!videoDrop || !videoFile) return;
    videoDrop.onclick = () => videoFile.click();
    videoDrop.ondragover = (e) => { e.preventDefault(); videoDrop.classList.add('drag'); };
    videoDrop.ondragleave = () => videoDrop.classList.remove('drag');
    videoDrop.ondrop = (e) => {
      e.preventDefault();
      videoDrop.classList.remove('drag');
      const files = e.dataTransfer.files;
      if (files && files[0]) {
        // 把文件赋给 input（DataTransfer 方式）
        const dt = new DataTransfer();
        dt.items.add(files[0]);
        videoFile.files = dt.files;
        showAnalyzeVideoPreview(files[0]);
      }
    };
    videoFile.onchange = function() {
      if (this.files[0]) showAnalyzeVideoPreview(this.files[0]);
    };
  }
  bindVideoDrop();

  // ── 生成参数构建 ─────────────────────────────────────────────────────────────
  function buildGeneratePayload() {
    const t = getTask(currentTaskId);
    const textOnly = $('#text-only').checked;
    const maxWorkers = parseInt($('#max-workers').value) || 4;

    const refVideos = (t.videoRefs || []).map(v => v.path);
    const refVideoDescs = refVideos.length
      ? (t.videoRefs || []).map(v => (v.desc && v.desc.trim()) || 'character appearance and actions as shown in the reference video')
      : [];

    // subject_lines: reference subject descriptions, format "video1: xxx" / "image1: xxx"
    const subjectLines = [];
    (t.videoRefs || []).forEach((v, i) => {
      subjectLines.push(`video${i + 1}: ${v.desc || 'subject appearance and actions as shown in reference video'}`);
    });
    (t.imageRefs || []).forEach((im, j) => {
      subjectLines.push(`image${j + 1}: ${im.subject || 'subject as shown in reference image'}`);
    });

    if (textOnly) {
      // 纯文生：从主体卡片构建 character1/character2 格式，方便后端做 per-chunk 筛选
      // 格式: "character1: Name — description_en"（Name 用于分镜文本关键词匹配）
      const subjectLines = (t.subjects || [])
        .filter(s => s.name && s.description_en)
        .map((s, i) => `character${i + 1}: ${s.name.trim()} — ${s.description_en.trim()}`);
      return {
        task_id: currentTaskId,
        text_only_video: true,
        subject_lines: subjectLines,
        style: $('#gen-style').value.trim(),
        resolution: $('#gen-resolution').value.trim() || null,
        max_segment_seconds: parseFloat($('#max-seg').value) || 15,
        max_workers: maxWorkers,
      };
    }
    return {
      task_id: currentTaskId,
      text_only_video: false,
      reference_images: (t.imageRefs || []).map(x => x.path),
      reference_videos: refVideos,
      reference_video_descriptions: refVideoDescs,
      subject_lines: subjectLines,
      style: $('#gen-style').value.trim(),
      resolution: $('#gen-resolution').value.trim() || null,
      max_segment_seconds: parseFloat($('#max-seg').value) || 15,
      max_workers: maxWorkers,
    };
  }

  // ── 文字开关 ─────────────────────────────────────────────────────────────────
  if ($('#text-only')) {
    $('#text-only').onchange = () => {
      const on = $('#text-only').checked;
      $('#ref-section').classList.toggle('hidden', on);
      const tw = $('#textonly-subject-wrap');
      if (tw) tw.classList.toggle('hidden', !on);
    };
  }

  // ── 标签切换 ─────────────────────────────────────────────────────────────────
  if ($('#source-tabs')) {
    $('#source-tabs').onclick = (e) => {
      const b = e.target.closest('button[data-tab]');
      if (!b) return;
      $('#source-tabs').querySelectorAll('button').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      const tab = b.dataset.tab;
      $('#panel-theme').classList.toggle('hidden', tab !== 'theme');
      $('#panel-analyze').classList.toggle('hidden', tab !== 'analyze');
    };
  }

  // ── preflight 检查后生成 ─────────────────────────────────────────────────────
  async function preflightAndGenerate(payload) {
    try {
      await api('/api/task/preflight', { method: 'POST', json: true, body: { task_id: currentTaskId } });
    } catch (e) {
      const issues = e.message || '配置问题';
      showToast('生成前检查失败：' + issues, 'error', 8000);
      return false;
    }
    await saveStoryboardNow();
    try {
      await api('/api/task/generate', { method: 'POST', json: true, body: payload });
      const t = getTask(currentTaskId);
      t.meta.cancelling = false;
      connectSSE(currentTaskId);
      $('#step4').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      renderStatusBar();
      return true;
    } catch (e) {
      showToast('提交生成失败：' + e.message);
      return false;
    }
  }

  // ── 按钮事件 ─────────────────────────────────────────────────────────────────

  // 一键生成主题创意
  if ($('#btn-gen-idea')) {
    $('#btn-gen-idea').onclick = async () => {
      const btn = $('#btn-gen-idea');
      btn.disabled = true;
      btn.textContent = '生成中…';
      try {
        const style = ($('#theme-style') || {}).value ? $('#theme-style').value.trim() : '';
        const result = await api('/api/task/theme/generate-idea', {
          method: 'POST', json: true, body: { style },
        });
        if (result.idea) {
          $('#theme-text').value = result.idea;
        }
      } catch (e) {
        showToast('生成创意失败：' + e.message, 'error', 5000);
      } finally {
        btn.disabled = false;
        btn.textContent = '✨ 生成创意';
      }
    };
  }

  // 生成分镜
  if ($('#btn-theme')) {
    $('#btn-theme').onclick = async () => {
      if (!currentTaskId) { showToast('请先新建任务'); return; }
      try {
        await api('/api/task/theme', {
          method: 'POST', json: true,
          body: {
            task_id: currentTaskId,
            theme: $('#theme-text').value,
            style: $('#theme-style').value,
            min_shots: +$('#min-shots').value,
            max_shots: +$('#max-shots').value,
            model: ($('#theme-model').value || '').trim() || null,
          },
        });
        connectSSE(currentTaskId);
        renderStatusBar();
        $('#step4').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } catch (e) {
        showToast('提交失败：' + e.message);
      }
    };
  }

  // 续写下一镜
  if ($('#btn-next-shot')) {
    $('#btn-next-shot').onclick = async () => {
      if (!currentTaskId) { showToast('请先新建任务'); return; }
      const btn = $('#btn-next-shot');
      btn.disabled = true;
      btn.textContent = '生成中…';
      try {
        const result = await api('/api/task/theme/next', {
          method: 'POST', json: true,
          body: {
            task_id: currentTaskId,
            theme: $('#theme-text').value,
            style: $('#theme-style').value,
            model: ($('#theme-model').value || '').trim() || null,
          },
        });
        const t = getTask(currentTaskId);
        // 重新拉取最新分镜
        const st = await api('/api/task/' + currentTaskId);
        if (st.storyboard) {
          t.storyboard = st.storyboard;
          t.storyboardSig = JSON.stringify(st.storyboard);
          t.meta.shot_count = result.shot_count;
          renderShots();
          $('#shots-empty').classList.add('hidden');
          // 滚动到底部
          const shots = $('#shots-container');
          shots.scrollTop = shots.scrollHeight;
        }
        renderHint(t.meta);
        showToast(`已添加镜头 ${result.shot_count}`, 'success', 2500);
      } catch (e) {
        showToast('续写失败：' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '续写下一镜';
      }
    };
  }

  // 视频分析
  if ($('#btn-analyze')) {
    $('#btn-analyze').onclick = async () => {
      if (!currentTaskId) { showToast('请先新建任务'); return; }
      const url = $('#video-url').value.trim();
      const file = ($('#video-file') || {}).files && $('#video-file').files[0];
      if (!url && !file) { showToast('请上传视频或填写 URL', 'warning'); return; }
      try {
        if (url) {
          await api('/api/task/analyze', { method: 'POST', json: true, body: { task_id: currentTaskId, video_url: url, style: $('#analyze-style').value, segment_scenes: $('#segment-scenes').checked } });
        } else {
          const fd = new FormData();
          fd.append('task_id', currentTaskId);
          fd.append('video', file);
          fd.append('style', $('#analyze-style').value);
          fd.append('segment_scenes', $('#segment-scenes').checked ? 'true' : 'false');
          const r = await fetch('/api/task/analyze', { method: 'POST', body: fd });
          if (r.status === 401) { window.location.href = '/login'; return; }
          const j = await r.json().catch(() => ({}));
          if (!r.ok) { showToast(j.error || '上传失败'); return; }
        }
        connectSSE(currentTaskId);
        renderStatusBar();
        $('#step4').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } catch (e) {
        showToast('提交失败：' + e.message);
      }
    };
  }

  // 开始/继续生成（含前置步骤检查）
  if ($('#btn-generate')) {
    $('#btn-generate').onclick = async () => {
      if (!currentTaskId) { showToast('请先新建任务并完成分镜'); return; }
      const t = getTask(currentTaskId);

      // ── 前置步骤检查 ──
      const issues = [];
      // 1. 分镜检查
      if (!t.storyboard || !(t.storyboard.shots || []).length) {
        issues.push('⚠ 尚未生成分镜（步骤 1）');
      }
      // 2. 主体检查（纯文生时至少需有一个含英文描述的主体；参考生不强制）
      const hasSubjectsWithDesc = (t.subjects || []).some(s => s.description_en && s.description_en.trim());
      if (!t.storyboard || !(t.storyboard.shots || []).length) {
        // 已在上面提示，不重复
      } else if (!hasSubjectsWithDesc) {
        // 有分镜但没有主体描述——警告但允许继续
        issues.push('💡 主体描述为空（步骤 2），生成时将无法注入角色描述，可能影响跨镜一致性');
      }

      if (issues.length) {
        // 区分阻塞性问题（无分镜）和警告（无主体描述）
        const blocking = issues.filter(s => s.startsWith('⚠'));
        if (blocking.length) {
          showToast(blocking.join('\n'), 'error', 7000);
          return;
        }
        // 仅警告：弹确认框
        if (!confirm(issues.join('\n') + '\n\n是否仍要继续生成？')) return;
      }

      // ── 参考模式检查 ──
      if (!$('#text-only').checked) {
        if (!t.imageRefs.length && !t.videoRefs.length) {
          if (!confirm('未上传参考图/视频，将使用纯文生模式。继续？')) return;
          $('#text-only').checked = true;
          $('#ref-section').classList.add('hidden');
          const tw = $('#textonly-subject-wrap');
          if (tw) tw.classList.remove('hidden');
        }
      }
      await preflightAndGenerate(buildGeneratePayload());
    };
  }

  // 停止任务
  if ($('#btn-stop')) {
    $('#btn-stop').onclick = async () => {
      if (!currentTaskId) return;
      if (!confirm('确认停止当前任务？已生成的片段缓存会保留。')) return;
      await cancelTask(currentTaskId);
    };
  }

  // 清除片段缓存
  if ($('#btn-clear-seg')) {
    $('#btn-clear-seg').onclick = async () => {
      if (!currentTaskId) return;
      if (!confirm('确定删除已缓存片段？将从头重新生成。')) return;
      try {
        await api('/api/workspace/clear-segments/' + currentTaskId, { method: 'POST', json: true, body: {} });
        showToast('片段缓存已清除', 'success', 2500);
        renderSegments([]);
      } catch (e) {
        showToast('清除失败：' + e.message);
      }
    };
  }

  // 风格同步：将 gen-style 的值同步到 task 元数据，并回填到 theme-style / analyze-style
  if ($('#btn-sync-style')) {
    $('#btn-sync-style').onclick = async () => {
      if (!currentTaskId) { showToast('请先新建或选择任务', 'warning'); return; }
      const styleVal = ($('#gen-style') || {}).value ? $('#gen-style').value.trim() : '';
      try {
        await api('/api/task/style/' + currentTaskId, {
          method: 'PUT', json: true, body: { style: styleVal },
        });
        // 同步到分镜风格输入框
        if ($('#theme-style')) $('#theme-style').value = styleVal;
        if ($('#analyze-style')) $('#analyze-style').value = styleVal;
        showToast('风格已同步保存', 'success', 2500);
      } catch (e) {
        showToast('风格同步失败：' + e.message, 'error', 4000);
      }
    };
  }

  // 一步运行（主题+生成）
  if ($('#btn-run-theme')) {
    $('#btn-run-theme').onclick = async () => {
      if (!currentTaskId) { showToast('请先新建任务'); return; }
      try {
        const gen = buildGeneratePayload();
        await api('/api/task/run', {
          method: 'POST', json: true,
          body: {
            ...gen,
            theme: $('#theme-text').value,
            style: ($('#theme-style').value.trim()) || gen.style || '',
            min_shots: +$('#min-shots').value,
            max_shots: +$('#max-shots').value,
            model: ($('#theme-model').value || '').trim() || null,
          },
        });
        connectSSE(currentTaskId);
        renderStatusBar();
        $('#step4').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } catch (e) {
        showToast('提交失败：' + e.message);
      }
    };
  }

  // 一步运行（分析+生成）
  if ($('#btn-run-analyze')) {
    $('#btn-run-analyze').onclick = async () => {
      if (!currentTaskId) { showToast('请先新建任务'); return; }
      const url = $('#video-url').value.trim();
      const file = ($('#video-file') || {}).files && $('#video-file').files[0];
      if (!url && !file) { showToast('请上传视频或填 URL', 'warning'); return; }
      try {
        if (file && !url) {
          const fd = new FormData();
          fd.append('task_id', currentTaskId);
          fd.append('video', file);
          fd.append('style', $('#analyze-style').value);
          fd.append('segment_scenes', $('#segment-scenes').checked ? 'true' : 'false');
          const r = await fetch('/api/task/analyze', { method: 'POST', body: fd });
          if (r.status === 401) { window.location.href = '/login'; return; }
          const j = await r.json().catch(() => ({}));
          if (!r.ok) { showToast(j.error || '失败'); return; }
          connectSSE(currentTaskId);
          // 等待分镜就绪
          await waitStoryboardReady();
          await api('/api/task/generate', { method: 'POST', json: true, body: buildGeneratePayload() });
          connectSSE(currentTaskId);
        } else {
          await api('/api/task/run', {
            method: 'POST', json: true,
            body: { task_id: currentTaskId, video_url: url, style: $('#analyze-style').value, segment_scenes: $('#segment-scenes').checked, ...buildGeneratePayload() },
          });
          connectSSE(currentTaskId);
        }
        renderStatusBar();
        $('#step4').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } catch (e) {
        showToast('提交失败：' + e.message);
      }
    };
  }

  async function waitStoryboardReady() {
    for (let i = 0; i < 600; i++) {
      const t = getTask(currentTaskId);
      if (t.meta.status === 'storyboard_ready') return;
      if (t.meta.status === 'failed') throw new Error(t.meta.error || '分析失败');
      await new Promise(r => setTimeout(r, 2000));
    }
    throw new Error('分析超时');
  }

  // ── 设置面板 ─────────────────────────────────────────────────────────────────
  async function loadConfigForm() {
    try {
      const c = await api('/api/config');
      const set = (id, v) => { const el = $('#cfg-' + id); if (el) el.value = v ?? ''; };
      set('base-url', c.base_url);
      set('api-base', c.dashscope_api_base);
      set('vision', c.vision_model);
      set('theme-model', c.theme_story_model);
      set('idea-model', c.theme_idea_model);
      set('gen', c.video_gen_model);
      set('ref', c.video_ref_model);
      set('res', c.default_resolution);
      set('b64', c.max_video_base64_mb);
      set('thresh', c.scene_detect_threshold);
      set('fps', c.analysis_fps);
      set('maxseg', c.max_segment_seconds);
      set('ttl', c.task_ttl_days ?? 7);
      $('#cfg-key').value = c.dashscope_api_key || '';
      $('#cfg-req-ref').checked = !!c.require_reference;
      const crf = $('#cfg-chunk-ref-filter');
      if (crf) crf.checked = c.per_chunk_reference_filter !== false;
    } catch (e) {
      showToast('加载配置失败：' + e.message, 'warning', 4000);
    }
  }

  if ($('#btn-settings')) {
    $('#btn-settings').onclick = () => { loadConfigForm(); openDrawer('settings'); };
    $('#overlay-settings').onclick = () => closeDrawer('settings');
    $('#btn-close-settings').onclick = () => closeDrawer('settings');
  }

  if ($('#btn-save-config')) {
    $('#btn-save-config').onclick = async () => {
      const body = {
        base_url: $('#cfg-base-url').value.trim(),
        dashscope_api_base: $('#cfg-api-base').value.trim(),
        vision_model: $('#cfg-vision').value.trim(),
        theme_story_model: $('#cfg-theme-model').value.trim(),
        theme_idea_model: ($('#cfg-idea-model') || {}).value ? $('#cfg-idea-model').value.trim() : '',
        video_gen_model: $('#cfg-gen').value.trim(),
        video_ref_model: $('#cfg-ref').value.trim(),
        default_resolution: $('#cfg-res').value.trim(),
        max_video_base64_mb: parseFloat($('#cfg-b64').value) || 7,
        scene_detect_threshold: parseFloat($('#cfg-thresh').value) || 27,
        analysis_fps: parseFloat($('#cfg-fps').value) || 2,
        max_segment_seconds: parseFloat($('#cfg-maxseg').value) || 15,
        require_reference: $('#cfg-req-ref').checked,
        per_chunk_reference_filter: $('#cfg-chunk-ref-filter').checked,
      };
      const k = $('#cfg-key').value.trim();
      if (k) body.dashscope_api_key = k;
      try {
        await api('/api/config', { method: 'POST', json: true, body });
        showToast('配置已保存', 'success', 2500);
        closeDrawer('settings');
      } catch (e) {
        showToast('保存失败：' + e.message);
      }
    };
  }

  // ── 历史任务面板 ──────────────────────────────────────────────────────────────
  if ($('#btn-history')) {
    $('#btn-history').onclick = () => { refreshHistory(); openDrawer('history'); };
    $('#overlay-history').onclick = () => closeDrawer('history');
    $('#btn-close-history').onclick = () => closeDrawer('history');
  }
  if ($('#btn-resume-ws')) {
    $('#btn-resume-ws').onclick = () => { if ($('#btn-history')) $('#btn-history').click(); };
  }

  async function refreshHistory() {
    const el = $('#history-list');
    el.innerHTML = '<p class="hint">加载中…</p>';
    try {
      const { tasks } = await api('/api/workspace/list');
      el.innerHTML = tasks.length ? '' : '<p class="hint">暂无记录</p>';
      tasks.forEach(t => {
        const div = document.createElement('div');
        div.className = 'history-item';
        const sc = statusClass(t.status);
        div.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong style="font-size:13px;font-family:var(--mono)">${t.task_id}</strong>
            <span class="hi-status ${sc}">${statusLabel(t.status)}</span>
          </div>
          <span class="hint" style="font-size:12px;">${formatDateTime(t.updated)} · ${t.shot_count ? t.shot_count + ' 镜' : ''}</span>
          <div class="flex" style="margin-top:6px;">
            <button class="ghost sm hi-load">加载</button>
            <button class="ghost sm danger hi-delete" style="font-size:11px;">删除</button>
          </div>`;
        div.querySelector('.hi-load').onclick = () => {
          loadHistoryTask(t.task_id);
          closeDrawer('history');
        };
        div.querySelector('.hi-delete').onclick = async (e) => {
          e.stopPropagation();
          if (!confirm(`确认删除任务 ${t.task_id}？`)) return;
          try {
            await api('/api/workspace/delete/' + t.task_id, { method: 'DELETE' });
            div.remove();
            delete allTasks[t.task_id];
            if (currentTaskId === t.task_id) { currentTaskId = null; }
            renderStatusBar();
            showToast('已删除', 'success', 2000);
          } catch (ex) { showToast('删除失败：' + ex.message); }
        };
        el.appendChild(div);
      });
    } catch (e) {
      el.innerHTML = `<p class="hint" style="color:var(--danger)">加载失败：${escapeHtml(e.message)}</p>`;
    }
  }

  async function loadHistoryTask(task_id) {
    if (currentTaskId) saveFormState(currentTaskId);
    const t = getTask(task_id);
    try {
      const [st] = await Promise.all([
        api('/api/task/' + task_id),
        loadSubjects(task_id),
      ]);
      t.meta = { ...st };
      delete t.meta.storyboard;
      if (st.storyboard) {
        t.storyboard = st.storyboard;
        t.storyboardSig = JSON.stringify(st.storyboard);
      }
      if (st.output_url) t.lastOutputUrl = st.output_url;

      // 恢复参考文件列表
      t.videoRefs = [];
      t.imageRefs = [];
      if (st.reference_files && st.reference_files.length) {
        st.reference_files.forEach(rf => {
          if (rf.kind === 'video') {
            t.videoRefs.push({ path: rf.path, name: rf.name || '', url: rf.url || '', desc: rf.desc || '' });
          } else {
            t.imageRefs.push({ path: rf.path, name: rf.name || '', url: rf.url || '', subject: rf.desc || '' });
          }
        });
      }

      // 恢复表单参数（从 task params 中还原）
      const params = st.params || {};
      const f = t.form;
      f.themeText = params.theme || '';
      f.themeStyle = params.style || st.style || '';
      f.themeModel = params.model || '';
      f.minShots = String(params.min_shots || 8);
      f.maxShots = String(params.max_shots || 24);
      f.videoUrl = params.video_url || '';
      f.analyzeStyle = params.style || st.style || '';
      f.segmentScenes = !!params.segment_scenes;
      f.genStyle = st.style || '';
      // 分析模式下的输入视频预览
      if (st.input_video_url) {
        f.inputVideoUrl = st.input_video_url;
      }
      // text_only 和 generation 参数
      const genParams = st.params_generate || st.params_run || {};
      f.textOnly = !!genParams.text_only_video;
      f.genResolution = genParams.resolution || '';
      f.maxSeg = String(genParams.max_segment_seconds || 15);
      f.maxWorkers = String(genParams.max_workers || 4);

      connectSSE(task_id);
      switchTask(task_id);
      renderStatusBar();
    } catch (e) {
      showToast('加载任务失败：' + e.message);
    }
  }

  // ── 用户认证 UI ──────────────────────────────────────────────────────────────
  let _currentRole = 'user';

  async function loadCurrentUser() {
    try {
      const r = await api('/auth/me');
      if (r.username && $('#user-display')) {
        $('#user-display').textContent = r.username;
      }
      _currentRole = r.role || 'user';
      // 管理员显示用户管理按钮
      const usersBtn = $('#btn-users');
      if (usersBtn) usersBtn.classList.toggle('hidden', _currentRole !== 'admin');
    } catch (_) {}
  }

  if ($('#btn-logout')) {
    $('#btn-logout').onclick = async () => {
      if (!confirm('确认登出？')) return;
      try {
        await fetch('/auth/logout', { method: 'POST' });
      } catch (_) {}
      window.location.href = '/login';
    };
  }

  if ($('#btn-chpwd')) {
    $('#btn-chpwd').onclick = () => openDrawer('chpwd');
    $('#overlay-chpwd').onclick = () => closeDrawer('chpwd');
    $('#btn-close-chpwd').onclick = () => closeDrawer('chpwd');
  }

  if ($('#btn-save-chpwd')) {
    $('#btn-save-chpwd').onclick = async () => {
      const oldPw = ($('#chpwd-old') || {}).value || '';
      const newPw = ($('#chpwd-new') || {}).value || '';
      const confirmPw = ($('#chpwd-confirm') || {}).value || '';
      if (!oldPw || !newPw) { showToast('请填写旧密码和新密码', 'warning'); return; }
      if (newPw !== confirmPw) { showToast('两次输入的新密码不一致', 'warning'); return; }
      if (newPw.length < 6) { showToast('新密码至少 6 位', 'warning'); return; }
      try {
        await api('/auth/change-password', {
          method: 'POST', json: true,
          body: { old_password: oldPw, new_password: newPw },
        });
        showToast('密码修改成功', 'success', 3000);
        closeDrawer('chpwd');
        $('#chpwd-old').value = '';
        $('#chpwd-new').value = '';
        $('#chpwd-confirm').value = '';
      } catch (e) {
        showToast('修改失败：' + e.message, 'error', 5000);
      }
    };
  }

  // ── 用户管理（管理员） ───────────────────────────────────────────────────────
  if ($('#btn-users')) {
    $('#btn-users').onclick = () => { refreshUserList(); openDrawer('users'); };
    $('#overlay-users').onclick = () => closeDrawer('users');
    $('#btn-close-users').onclick = () => closeDrawer('users');
  }

  async function refreshUserList() {
    const el = $('#users-list');
    if (!el) return;
    el.innerHTML = '<p class="hint">加载中…</p>';
    try {
      const { users } = await api('/auth/users');
      el.innerHTML = '';
      if (!users || !users.length) {
        el.innerHTML = '<p class="hint">暂无用户</p>';
        return;
      }
      users.forEach(u => {
        const row = document.createElement('div');
        row.className = 'user-row';
        const isAdmin = u.role === 'admin';
        row.innerHTML = `
          <div class="user-row-info">
            <span class="user-row-name">${escapeHtml(u.username)}</span>
            <span class="user-row-role ${isAdmin ? 'role-admin' : 'role-user'}">${isAdmin ? '管理员' : '用户'}</span>
          </div>
          <div class="user-row-actions">
            <button class="ghost sm ur-toggle-role" data-u="${escapeAttr(u.username)}" data-role="${u.role}" title="${isAdmin ? '降为普通用户' : '升为管理员'}">${isAdmin ? '降级' : '升级'}</button>
            <button class="ghost sm ur-reset-pwd" data-u="${escapeAttr(u.username)}" title="重置密码">重置密码</button>
            <button class="ghost sm danger ur-delete" data-u="${escapeAttr(u.username)}" title="删除用户">删除</button>
          </div>`;
        el.appendChild(row);
      });

      // 切换角色
      el.querySelectorAll('.ur-toggle-role').forEach(btn => {
        btn.onclick = async () => {
          const name = btn.dataset.u;
          const newRole = btn.dataset.role === 'admin' ? 'user' : 'admin';
          const label = newRole === 'admin' ? '管理员' : '普通用户';
          if (!confirm(`确认将 ${name} 设为${label}？`)) return;
          try {
            await api(`/auth/users/${encodeURIComponent(name)}/role`, {
              method: 'PUT', json: true, body: { role: newRole },
            });
            showToast(`已将 ${name} 设为${label}`, 'success', 2500);
            refreshUserList();
          } catch (e) { showToast(e.message, 'error', 5000); }
        };
      });

      // 重置密码
      el.querySelectorAll('.ur-reset-pwd').forEach(btn => {
        btn.onclick = async () => {
          const name = btn.dataset.u;
          const newPwd = prompt(`为 ${name} 设置新密码（至少 6 位）：`);
          if (!newPwd) return;
          if (newPwd.length < 6) { showToast('密码至少 6 位', 'warning'); return; }
          try {
            await api(`/auth/users/${encodeURIComponent(name)}/reset-password`, {
              method: 'POST', json: true, body: { new_password: newPwd },
            });
            showToast(`已重置 ${name} 的密码`, 'success', 2500);
          } catch (e) { showToast(e.message, 'error', 5000); }
        };
      });

      // 删除用户
      el.querySelectorAll('.ur-delete').forEach(btn => {
        btn.onclick = async () => {
          const name = btn.dataset.u;
          if (!confirm(`确认删除用户 ${name}？此操作不可撤销。`)) return;
          try {
            await api(`/auth/users/${encodeURIComponent(name)}`, { method: 'DELETE' });
            showToast(`已删除 ${name}`, 'success', 2500);
            refreshUserList();
          } catch (e) { showToast(e.message, 'error', 5000); }
        };
      });
    } catch (e) {
      el.innerHTML = `<p class="hint" style="color:var(--danger)">加载失败：${escapeHtml(e.message)}</p>`;
    }
  }

  // 创建用户
  if ($('#btn-create-user')) {
    $('#btn-create-user').onclick = async () => {
      const name = ($('#new-user-name') || {}).value ? $('#new-user-name').value.trim() : '';
      const pwd = ($('#new-user-pwd') || {}).value || '';
      const role = ($('#new-user-role') || {}).value || 'user';
      if (!name) { showToast('请输入用户名', 'warning'); return; }
      if (!pwd || pwd.length < 6) { showToast('密码至少 6 位', 'warning'); return; }
      try {
        await api('/auth/users', {
          method: 'POST', json: true,
          body: { username: name, password: pwd, role },
        });
        showToast(`用户 ${name} 创建成功`, 'success', 3000);
        $('#new-user-name').value = '';
        $('#new-user-pwd').value = '';
        $('#new-user-role').value = 'user';
        refreshUserList();
      } catch (e) {
        showToast('创建失败：' + e.message, 'error', 5000);
      }
    };
  }

  // ── 初始化 ───────────────────────────────────────────────────────────────────
  loadCurrentUser();
  loadConfigForm();
  renderStatusBar();
  // 初次打开自动新建任务
  createNewTask().catch(() => {});
})();
