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
      // 清空输入
      if ($('#theme-text')) $('#theme-text').value = '';
      if ($('#video-url')) $('#video-url').value = '';
      if ($('#video-file')) $('#video-file').value = '';
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
            // 确保 step2/3/4 可见（分析模式也需展示分镜预览等步骤）
            ['step2', 'step3', 'step4'].forEach(id => {
              const el = document.getElementById(id);
              if (el) el.classList.remove('hidden');
            });
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

  /** 主题任务：主体卡片中 subject_N 参考图的预览 URL */
  function subjectRefPreviewUrl(taskId, subj) {
    const p = String(subj.reference_image_path || '').replace(/\\/g, '/');
    if (!p) return '';
    const fname = p.split('/').pop() || '';
    if (!fname.startsWith('subject_')) return '';
    return `/api/files/${taskId}/references/${encodeURIComponent(fname)}`;
  }

  async function uploadSubjectReference(index, file) {
    if (!currentTaskId || !file) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch(`/api/task/subjects/${currentTaskId}/upload/${index}`, { method: 'POST', body: fd });
      if (r.status === 401) { window.location.href = '/login'; return; }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { showToast(j.error || '上传失败'); return; }
      const t = getTask(currentTaskId);
      t.subjects = j.subjects || t.subjects;
      renderSubjects();
      showToast('角色参考图已上传', 'success', 2500);
    } catch (e) {
      showToast('上传失败：' + (e.message || e));
    }
  }

  async function deleteSubjectReference(index) {
    if (!currentTaskId) return;
    try {
      const j = await api(`/api/task/subjects/${currentTaskId}/reference/${index}`, { method: 'DELETE' });
      const t = getTask(currentTaskId);
      t.subjects = j.subjects || [];
      renderSubjects();
      showToast('已移除参考图', 'success', 2000);
    } catch (e) {
      showToast('移除失败：' + (e.message || e));
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

  // IP 模式：将 generation_prompt 中的「图N」反向替换为 @角色名 高亮预览
  function _renderCharRefPreview(prompt, charRefMap) {
    if (!charRefMap || !Object.keys(charRefMap).length) return '';
    if (!prompt.trim()) return '';
    const hasRef = Object.values(charRefMap).some(tag => prompt.includes(tag));
    if (!hasRef) return '';
    // 构建 图N → 角色名 的反向映射（tag → name）
    const tagToName = {};
    for (const [name, tag] of Object.entries(charRefMap)) {
      if (!tagToName[tag]) tagToName[tag] = name;
    }
    let html = escapeHtml(prompt);
    const tags = Object.keys(tagToName).sort((a, b) => b.length - a.length);
    tags.forEach(tag => {
      const eName = escapeHtml(tag);
      const charName = escapeHtml(tagToName[tag]);
      const re = new RegExp(eName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g');
      html = html.replace(re, `<span class="char-ref-chip" title="${eName}">@${charName}</span>`);
    });
    const legend = tags.map(tag => `<span class="char-ref-legend">${escapeHtml(tag)} = @${escapeHtml(tagToName[tag])}</span>`).join(' ');
    return `<div class="char-ref-preview"><div class="char-ref-legend-row">📎 ${legend}</div><div class="char-ref-text">${html}</div></div>`;
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
      const refUrl = currentTaskId ? subjectRefPreviewUrl(currentTaskId, subj) : '';
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
        </div>
        <div class="subject-ref-row">
          <label class="subject-field-label">角色参考图 <span class="field-tag">按角色 r2v</span></label>
          <div class="subject-ref-upload">
            ${refUrl ? `<img class="subject-ref-thumb" src="${escapeAttr(refUrl)}" alt="" />` : ''}
            <div class="subject-ref-drop" data-sidx="${idx}" title="点击或拖入图片">${refUrl ? '更换图片' : '点击 / 拖放上传'}</div>
            <input type="file" accept="image/*" class="hidden subject-ref-file" data-sidx="${idx}" />
            ${refUrl ? `<button type="button" class="ghost sm subject-ref-del" data-sidx="${idx}">移除</button>` : ''}
          </div>
          <p class="subject-ref-hint">分镜生成后上传，名称需与分镜对白/动作中的角色一致；生成时按镜头自动选用参考图（与 IP 模式相同策略）。</p>
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

    list.querySelectorAll('.subject-ref-drop').forEach((drop) => {
      const idx = +drop.dataset.sidx;
      const inp = list.querySelector(`.subject-ref-file[data-sidx="${idx}"]`);
      drop.onclick = (ev) => {
        if (ev.target.closest('.subject-ref-del')) return;
        if (inp) inp.click();
      };
      drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('drag'); };
      drop.ondragleave = () => drop.classList.remove('drag');
      drop.ondrop = (e) => {
        e.preventDefault();
        drop.classList.remove('drag');
        const f = e.dataTransfer.files && e.dataTransfer.files[0];
        if (f && f.type.startsWith('image/')) uploadSubjectReference(idx, f);
        else if (f) showToast('请上传图片文件');
      };
    });
    list.querySelectorAll('.subject-ref-file').forEach((inp) => {
      inp.onchange = () => {
        const f = inp.files && inp.files[0];
        if (f) uploadSubjectReference(+inp.dataset.sidx, f);
        inp.value = '';
      };
    });
    list.querySelectorAll('.subject-ref-del').forEach((btn) => {
      btn.onclick = () => deleteSubjectReference(+btn.dataset.sidx);
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

      // metadata badges
      const metaParts = [];
      if (shot.cut_rhythm) metaParts.push(`<span class="shot-cut-rhythm">${escapeHtml(shot.cut_rhythm)}</span>`);
      if (shot.camera_angle) metaParts.push(`<span class="meta-label">${escapeHtml(shot.camera_angle)}</span>`);
      if (shot.focal_character) metaParts.push(`<span class="shot-focal">${escapeHtml(shot.focal_character)}</span>`);
      const metaHtml = metaParts.length ? `<div class="shot-meta-row">${metaParts.join(' ')}</div>` : '';

      const anchorHtml = shot.continuity_anchor ? `<div class="shot-continuity-anchor"><span class="meta-label">anchor:</span> ${escapeHtml(shot.continuity_anchor)}</div>` : '';
      const contNoteHtml = shot.continuity_note ? `<div class="shot-continuity-note"><span class="meta-label">cut:</span> ${escapeHtml(shot.continuity_note)}</div>` : '';
      const negHtml = shot.negative_prompt_hint ? `<div class="shot-neg-hint">${escapeHtml(shot.negative_prompt_hint)}</div>` : '';

      card.innerHTML = `
        <header>
          <span>Shot ${shot.shot_id} · <input type="number" class="shot-duration-input" data-idx="${idx}" value="${shot.duration}" min="0.5" max="30" step="0.5" title="镜头时长（秒），可直接修改" />s · ${escapeHtml(shot.shot_type || '')}</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <div class="shot-mentions">${chipsHtml}</div>
            <button class="btn-shot-translate ghost sm" data-sidx="${idx}" title="翻译本镜场景/对白/prompt 为中文" style="font-size:11px;padding:2px 7px;opacity:0.7;">🌐 译</button>
          </div>
        </header>
        ${metaHtml}
        <div class="scene-desc">${sceneHtml}</div>
        ${anchorHtml}${contNoteHtml}${negHtml}
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
        ${!hasPrompt ? '<div class="prompt-warn">⚠ 未填写，将使用场景描述自动生成</div>' : ''}
        ${_renderCharRefPreview(shot.generation_prompt || '', sb.ip_char_ref_map)}`;
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

    c.querySelectorAll('.shot-duration-input').forEach(inp => {
      inp.oninput = () => {
        const idx = +inp.dataset.idx;
        const t2 = getTask(currentTaskId);
        const v = parseFloat(inp.value);
        if (t2.storyboard && t2.storyboard.shots[idx] && !isNaN(v) && v > 0) {
          t2.storyboard.shots[idx].duration = Math.round(v * 100) / 100;
        }
      };
      inp.onblur = () => {
        const idx = +inp.dataset.idx;
        const t2 = getTask(currentTaskId);
        let v = parseFloat(inp.value);
        if (isNaN(v) || v < 0.5) v = 0.5;
        if (v > 30) v = 30;
        v = Math.round(v * 100) / 100;
        inp.value = v;
        if (t2.storyboard && t2.storyboard.shots[idx]) {
          t2.storyboard.shots[idx].duration = v;
        }
        scheduleSaveStoryboard();
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
        // 构建前缀：[Character descriptions] Name: desc; Name2: desc2. [/descriptions]
        const prefix = '[Character descriptions] ' +
          relevant.map(s => `${s.name}: ${s.description_en.trim()}`).join('; ') + ' [/descriptions]';
        const ta = btn.closest('.shot-card').querySelector('textarea[data-field="generation_prompt"]');
        const existing = ta.value.trim();
        // 若已有同样的前缀则不重复注入
        if (existing.startsWith('[Character descriptions]')) {
          // 替换旧前缀（兼容新旧格式）
          const rest = existing.replace(/^\[Character descriptions\][\s\S]*?\[\/descriptions\]\s*/, '')
            || existing.replace(/^\[Character descriptions\][^.]*\.\s*/, '');
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
    $('#shots-container').querySelectorAll('.shot-duration-input').forEach(inp => {
      const idx = +inp.dataset.idx;
      const v = parseFloat(inp.value);
      if (t.storyboard.shots[idx] && !isNaN(v) && v > 0) {
        t.storyboard.shots[idx].duration = Math.round(v * 100) / 100;
      }
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

    const hasSubjectRefs = (t.subjects || []).some(
      s => s.reference_image_path && String(s.reference_image_path).trim()
    );
    if (hasSubjectRefs) {
      return {
        task_id: currentTaskId,
        text_only_video: false,
        use_subject_refs: true,
        reference_images: [],
        reference_videos: [],
        reference_video_descriptions: [],
        subject_lines: [],
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
      $('#panel-ip-mode').classList.toggle('hidden', tab !== 'ip-mode');
      $('#panel-analyze').classList.toggle('hidden', tab !== 'analyze');
      // step2/3/4 仅在主题模式下立即显示；分析模式在分析完成后才展示；IP 模式有自己的步骤
      const showSteps = (tab !== 'ip-mode' && tab !== 'analyze');
      ['step2', 'step3', 'step4'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('hidden', !showSteps);
      });
      if (tab === 'ip-mode') {
        loadIPList().then(() => {
          if (currentIPId && currentIPData) {
            showIPDetail(currentIPData);
          } else {
            ipSetStep(1);
          }
        });
      }
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
        // 分析任务提交后确保 step4（进度日志）可见
        ['step2', 'step3', 'step4'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.classList.remove('hidden');
        });
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
      const hasSubjectRefs = (t.subjects || []).some(
        s => s.reference_image_path && String(s.reference_image_path).trim()
      );
      if (!$('#text-only').checked && !hasSubjectRefs) {
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
        // 分析任务提交后确保步骤卡片可见
        ['step2', 'step3', 'step4'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.classList.remove('hidden');
        });
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
      set('img-model', c.image_gen_model);
      set('img-size', c.image_gen_size);
      set('ttl', c.task_ttl_days ?? 7);
      set('max-workers', c.max_workers ?? 4);
      $('#cfg-key').value = c.dashscope_api_key || '';
      const imgThink = $('#cfg-img-thinking');
      if (imgThink) imgThink.checked = c.image_gen_thinking_mode !== false;
      $('#cfg-req-ref').checked = !!c.require_reference;
      const crf = $('#cfg-chunk-ref-filter');
      if (crf) crf.checked = c.per_chunk_reference_filter !== false;
      const vwm = $('#cfg-video-watermark');
      if (vwm) vwm.checked = c.video_watermark !== false;
      const vpe = $('#cfg-video-prompt-extend');
      if (vpe) vpe.checked = c.video_prompt_extend !== false;
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
        max_video_base64_mb: parseFloat($('#cfg-b64').value) || 9,
        scene_detect_threshold: parseFloat($('#cfg-thresh').value) || 27,
        analysis_fps: parseFloat($('#cfg-fps').value) || 2,
        max_segment_seconds: parseFloat($('#cfg-maxseg').value) || 15,
        image_gen_model: ($('#cfg-img-model') || {}).value ? $('#cfg-img-model').value.trim() : 'wan2.7-image-pro',
        image_gen_thinking_mode: $('#cfg-img-thinking') ? $('#cfg-img-thinking').checked : true,
        image_gen_size: ($('#cfg-img-size') || {}).value ? $('#cfg-img-size').value.trim() : '2K',
        task_ttl_days: parseInt($('#cfg-ttl').value) || 7,
        max_workers: parseInt($('#cfg-max-workers').value) || 4,
        require_reference: $('#cfg-req-ref').checked,
        per_chunk_reference_filter: $('#cfg-chunk-ref-filter').checked,
        video_watermark: $('#cfg-video-watermark') ? $('#cfg-video-watermark').checked : true,
        video_prompt_extend: $('#cfg-video-prompt-extend') ? $('#cfg-video-prompt-extend').checked : true,
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
      const filtered = tasks.filter(t => t.type !== 'ip-theme');
      el.innerHTML = filtered.length ? '' : '<p class="hint">暂无记录</p>';
      filtered.forEach(t => {
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

      // 根据任务类型自动切换到对应的标签页
      const taskType = st.type || '';
      if (taskType === 'analyze' && $('#source-tabs')) {
        $('#source-tabs').querySelectorAll('button').forEach(x => x.classList.remove('active'));
        const analyzeBtn = $('#source-tabs').querySelector('[data-tab="analyze"]');
        if (analyzeBtn) analyzeBtn.classList.add('active');
        $('#panel-theme').classList.add('hidden');
        $('#panel-ip-mode').classList.add('hidden');
        $('#panel-analyze').classList.remove('hidden');
      } else if (taskType === 'theme' && $('#source-tabs')) {
        $('#source-tabs').querySelectorAll('button').forEach(x => x.classList.remove('active'));
        const themeBtn = $('#source-tabs').querySelector('[data-tab="theme"]');
        if (themeBtn) themeBtn.classList.add('active');
        $('#panel-theme').classList.remove('hidden');
        $('#panel-ip-mode').classList.add('hidden');
        $('#panel-analyze').classList.add('hidden');
      }
      // 非 IP 任务确保 step2/3/4 可见
      if (taskType !== 'ip-theme') {
        ['step2', 'step3', 'step4'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.classList.remove('hidden');
        });
      }

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

  // ── 缓存清理（管理员面板） ───────────────────────────────────────────────────
  if ($('#btn-disk-usage')) {
    $('#btn-disk-usage').onclick = async () => {
      const el = $('#disk-usage-info');
      el.textContent = '加载中...';
      try {
        const data = await api('/api/admin/disk-usage');
        if (!data.length) { el.textContent = '暂无数据'; return; }
        el.innerHTML = data.map(u =>
          `<div>${escapeHtml(u.user)}: ${u.tasks} 个任务, ${u.mb} MB</div>`
        ).join('');
      } catch (e) { el.textContent = '加载失败: ' + e.message; }
    };
  }
  if ($('#btn-cleanup-preview')) {
    $('#btn-cleanup-preview').onclick = async () => {
      const el = $('#cleanup-result');
      el.textContent = '预览中...';
      try {
        const data = await api('/api/admin/cleanup', {
          method: 'POST', json: true, body: { dry_run: true },
        });
        if (!data.deleted.length) {
          el.textContent = '没有可清理的过期任务。';
        } else {
          el.textContent = `可删除 ${data.deleted.length} 个任务，释放 ${data.freed_mb} MB\n` +
            data.deleted.join('\n');
        }
      } catch (e) { el.textContent = '预览失败: ' + e.message; }
    };
  }
  if ($('#btn-cleanup-run')) {
    $('#btn-cleanup-run').onclick = async () => {
      if (!confirm('确定要清理所有过期任务？此操作不可恢复。')) return;
      const el = $('#cleanup-result');
      el.textContent = '清理中...';
      try {
        const data = await api('/api/admin/cleanup', {
          method: 'POST', json: true, body: { dry_run: false },
        });
        el.textContent = `已删除 ${data.deleted.length} 个任务，释放 ${data.freed_mb} MB`;
        if (data.skipped_running.length) {
          el.textContent += `\n跳过运行中: ${data.skipped_running.join(', ')}`;
        }
      } catch (e) { el.textContent = '清理失败: ' + e.message; }
    };
  }

  // ── IP 模式（5 步依赖流）──────────────────────────────────────────────────
  let ipList = [];
  let currentIPId = '';
  let currentIPData = null;
  let ipProposal = null;
  let ipCurrentStep = 1;
  let ipStoryOutline = null;
  let ipImageTaskId = null;

  function ipSetStep(step) {
    ipCurrentStep = step;
    const bar = $('#ip-steps-bar');
    if (bar) {
      bar.querySelectorAll('.ip-step').forEach(el => {
        const s = parseInt(el.dataset.step);
        el.classList.remove('active', 'completed', 'disabled');
        if (s === step) el.classList.add('active');
        else if (s < step) el.classList.add('completed');
        else el.classList.add('disabled');
      });
    }
    // 累积展开：1~step 全部显示，step+1 以后隐藏
    for (let i = 1; i <= 5; i++) {
      const panel = $(`#ip-step-${i}`);
      if (!panel) continue;
      if (i <= step) {
        panel.classList.remove('hidden');
      } else {
        panel.classList.add('hidden');
      }
    }
    // 滚动到当前步骤
    const cur = $(`#ip-step-${step}`);
    if (cur && step > 1) {
      setTimeout(() => cur.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);
    }
  }

  // Step bar click — jump to step
  if ($('#ip-steps-bar')) {
    $('#ip-steps-bar').onclick = (e) => {
      const stepEl = e.target.closest('.ip-step');
      if (!stepEl || stepEl.classList.contains('disabled')) return;
      const s = parseInt(stepEl.dataset.step);
      if (s <= ipCurrentStep || stepEl.classList.contains('completed')) {
        ipSetStep(s);
      }
    };
  }


  async function loadIPList() {
    try {
      ipList = await api('/api/ips');
      const sel = $('#ip-select');
      sel.innerHTML = '<option value="">— 请选择或创建 IP —</option>';
      ipList.forEach(ip => {
        const opt = document.createElement('option');
        opt.value = ip.id;
        opt.textContent = `${ip.name} (${ip.name_en}) — ${ip.tagline}`;
        sel.appendChild(opt);
      });
      if (currentIPId) {
        sel.value = currentIPId;
      } else {
        $('#ip-detail-panel').classList.add('hidden');
        ipSetStep(1);
      }
    } catch (e) {
      showToast('加载 IP 列表失败: ' + e.message);
    }
  }

  function _renderGuidelinesPanel(ip) {
    let container = $('#ip-guidelines-panel');
    if (!container) {
      container = document.createElement('div');
      container.id = 'ip-guidelines-panel';
      const summary = $('#ip-settings-summary');
      if (summary && summary.parentNode) summary.parentNode.insertBefore(container, summary.nextSibling);
      else return;
    }
    const guidelines = ip.creative_guidelines || [];
    const feedbackCount = (ip.feedback_log || []).length;
    if (!guidelines.length && !feedbackCount) { container.innerHTML = ''; return; }
    container.innerHTML = `
      <details class="guidelines-section" style="margin:8px 0;">
        <summary style="cursor:pointer;font-weight:600;font-size:0.9em;color:var(--text-secondary);">
          创作指南 (${guidelines.length} 条) · 反馈记录 ${feedbackCount} 条
        </summary>
        <div class="guidelines-body" style="padding:8px 0;">
          ${guidelines.length ? `<ul class="guidelines-list" style="margin:0 0 8px 16px;font-size:0.85em;">${guidelines.map((g, i) => `<li>${escapeHtml(g)}</li>`).join('')}</ul>` : '<p style="font-size:0.85em;color:var(--text-secondary);">暂无创作指南</p>'}
          <div class="inline-actions" style="gap:6px;">
            <button type="button" class="ghost sm" id="btn-guidelines-edit">编辑指南</button>
            ${feedbackCount >= 3 ? '<button type="button" class="ghost sm" id="btn-guidelines-distill">提炼指南</button>' : ''}
          </div>
        </div>
      </details>
    `;
    const editBtn = container.querySelector('#btn-guidelines-edit');
    if (editBtn) editBtn.onclick = () => _showGuidelinesEditor(ip);
    const distillBtn = container.querySelector('#btn-guidelines-distill');
    if (distillBtn) distillBtn.onclick = async () => {
      distillBtn.disabled = true; distillBtn.textContent = '提炼中…';
      try {
        const data = await api(`/api/ip/${ip.id}/feedback/distill`, { method: 'POST', json: true, body: {} });
        showToast('创作指南已更新', 'success');
        ip.creative_guidelines = data.creative_guidelines;
        _renderGuidelinesPanel(ip);
      } catch (e) { showToast('提炼失败: ' + e.message); }
    };
  }

  function _showGuidelinesEditor(ip) {
    const existing = document.querySelector('.guidelines-editor-popup');
    if (existing) existing.remove();
    const popup = document.createElement('div');
    popup.className = 'guidelines-editor-popup';
    popup.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:200;width:min(500px,90vw);background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    const lines = (ip.creative_guidelines || []).join('\n');
    popup.innerHTML = `
      <h4 style="margin:0 0 12px;">编辑创作指南</h4>
      <p style="font-size:0.85em;color:var(--text-secondary);margin:0 0 8px;">每行一条指南规则</p>
      <textarea style="min-height:120px;">${escapeHtml(lines)}</textarea>
      <div class="flex" style="margin-top:12px;justify-content:flex-end;gap:8px;">
        <button type="button" class="primary sm" id="guidelines-save">保存</button>
        <button type="button" class="ghost sm" id="guidelines-cancel">取消</button>
      </div>
    `;
    document.body.appendChild(popup);
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:199;';
    document.body.appendChild(overlay);
    const close = () => { popup.remove(); overlay.remove(); };
    overlay.onclick = close;
    popup.querySelector('#guidelines-cancel').onclick = close;
    popup.querySelector('#guidelines-save').onclick = async () => {
      const text = popup.querySelector('textarea').value;
      const newGuidelines = text.split('\n').map(s => s.trim()).filter(Boolean);
      try {
        const data = await api(`/api/ip/${ip.id}/guidelines`, {
          method: 'PUT', json: true,
          body: { guidelines: newGuidelines },
        });
        ip.creative_guidelines = data.creative_guidelines;
        close();
        showToast('创作指南已保存', 'success');
        _renderGuidelinesPanel(ip);
      } catch (e) { showToast('保存失败: ' + e.message); }
    };
  }

  function renderIPSettingsSummary(ip) {
    const container = $('#ip-settings-summary');
    if (!container) return;
    const vd = ip.visual_dna || {};
    const sd = ip.story_dna || {};
    const wd = ip.world_dna || {};
    container.innerHTML = `
      <div class="ip-settings-grid">
        <div class="ip-setting-item"><b>视觉风格</b><span>${escapeHtml(vd.style_keywords || vd.style_keywords_en || '')}</span></div>
        <div class="ip-setting-item"><b>色调</b><span>${escapeHtml(vd.color_tone || '')}</span></div>
        <div class="ip-setting-item"><b>类型</b><span>${escapeHtml(sd.genre || '')}</span></div>
        <div class="ip-setting-item"><b>叙事模式</b><span>${escapeHtml(sd.narrative_pattern || '').substring(0, 80)}</span></div>
        <div class="ip-setting-item"><b>主场景</b><span>${escapeHtml(wd.primary_setting || '').substring(0, 80)}</span></div>
        <div class="ip-setting-item"><b>角色数</b><span>${(ip.characters || []).length} 个</span></div>
      </div>
      <div class="inline-actions">
        <button type="button" class="ghost sm" id="btn-ip-edit-settings">编辑设定</button>
        <button type="button" class="ghost sm" id="btn-ip-refine-settings">修改润色</button>
      </div>
    `;
    const editBtn = $('#btn-ip-edit-settings');
    if (editBtn) editBtn.onclick = () => { openDrawer('ip-manage'); showIPManage(ip); };
    const refineBtn = $('#btn-ip-refine-settings');
    if (refineBtn) refineBtn.onclick = () => showRefinePopup('visual_dna', ip.visual_dna, (result) => {
      api(`/api/ip/${ip.id}`, { method: 'PUT', json: true, body: { visual_dna: result } })
        .then(() => { showToast('设定已更新', 'success'); reloadCurrentIP(); })
        .catch(e => showToast('更新失败: ' + e.message));
    });
  }

  function showIPDetail(ip) {
    currentIPId = ip.id;
    currentIPData = ip;
    const panel = $('#ip-detail-panel');
    panel.classList.remove('hidden');
    $('#ip-detail-name').textContent = `${ip.name} (${ip.name_en})`;
    $('#ip-detail-tagline').textContent = ip.tagline;
    renderIPSettingsSummary(ip);

    const hasChars = (ip.characters || []).length > 0;
    const allHaveImages = hasChars && ip.characters.every(c => c.reference_image_path);

    const hasOutline = ip.last_story_outline && Object.keys(ip.last_story_outline).length > 0;

    const hasStoryboard = hasOutline && ip.last_story_outline._last_storyboard_task_id;

    // 确定最远可达步骤
    let reachable = 1;
    if (hasChars) reachable = 2;
    if (allHaveImages) reachable = 3;
    if (allHaveImages && hasOutline) reachable = 4;
    if (allHaveImages && hasOutline && hasStoryboard) reachable = 5;

    // 进入最远可达步骤
    ipSetStep(reachable);

    // 允许回看已完成步骤
    for (let s = 1; s < reachable; s++) {
      const el = $(`#ip-steps-bar [data-step="${s}"]`);
      if (el) { el.classList.remove('disabled'); el.classList.add('completed'); }
    }

    // 渲染角色卡片（step 2 面板内容，即使当前不在 step 2 也需要准备好）
    if (hasChars) renderCharCards(ip);

    // 恢复持久化的故事大纲
    if (hasOutline) {
      ipStoryOutline = ip.last_story_outline;
      renderIPStoryPreview(ip.last_story_outline);

      // 恢复分镜预览（无 task_id 时必须清空内存态，避免沿用上一条 IP 的续跑 ID）
      const sbTaskId = ip.last_story_outline._last_storyboard_task_id;
      if (sbTaskId) {
        _ipLastStoryboardTaskId = sbTaskId;
        _loadIPStoryboardPreview(sbTaskId);
      } else {
        _ipLastStoryboardTaskId = null;
        _ipStoryboardShots = [];
        const sbPrev = $('#ip-storyboard-preview');
        if (sbPrev) {
          sbPrev.innerHTML = '';
          sbPrev.classList.add('hidden');
        }
      }
    } else {
      _ipLastStoryboardTaskId = null;
      _ipStoryboardShots = [];
      const sbPrev = $('#ip-storyboard-preview');
      if (sbPrev) {
        sbPrev.innerHTML = '';
        sbPrev.classList.add('hidden');
      }
    }

    // 恢复视频生成记录
    _ipVideoRuns = [];
    if (ip.last_video_task_ids && ip.last_video_task_ids.length) {
      _restoreIPVideoRuns(ip.last_video_task_ids);
    } else {
      _renderIPVideoRuns();
    }

    // 渲染创作指南面板
    _renderGuidelinesPanel(ip);
  }

  async function reloadCurrentIP() {
    if (!currentIPId) return;
    try {
      const ip = await api(`/api/ip/${currentIPId}`);
      currentIPData = ip;
      showIPDetail(ip);
    } catch (e) { showToast('刷新 IP 失败: ' + e.message); }
  }

  if ($('#ip-select')) {
    $('#ip-select').onchange = async () => {
      const id = $('#ip-select').value;
      if (!id) {
        $('#ip-detail-panel').classList.add('hidden');
        currentIPId = '';
        currentIPData = null;
        ipStoryOutline = null;
        _ipLastStoryboardTaskId = null;
        _ipStoryboardShots = [];
        ipSetStep(1);
        return;
      }
      try {
        const ip = await api(`/api/ip/${id}`);
        showIPDetail(ip);
      } catch (e) { showToast('加载 IP 失败: ' + e.message); }
    };
  }

  if ($('#btn-refresh-ips')) $('#btn-refresh-ips').onclick = () => loadIPList();

  // ── Step 2: 角色资产卡片 ──
  let _voicePresets = null;
  async function loadVoicePresets() {
    if (_voicePresets) return _voicePresets;
    try { _voicePresets = await api('/api/voices'); } catch (e) { _voicePresets = []; }
    return _voicePresets;
  }

  function renderCharCards(ip) {
    const container = $('#ip-char-cards');
    if (!container) return;
    container.innerHTML = '';
    (ip.characters || []).forEach(c => {
      const card = document.createElement('div');
      card.className = 'ip-char-card';
      const hasImg = !!c.reference_image_path;
      const vp = c.voice_profile || {};
      const voiceLabel = vp.mode === 'preset' ? (vp.preset_name || vp.preset_id || '预置')
                       : vp.mode === 'clone' ? '克隆音色'
                       : '未设置';
      card.innerHTML = `
        <div class="ip-char-card-img">
          ${hasImg
            ? `<img src="/api/ip/${ip.id}/character/${c.id}/image?t=${Date.now()}" data-full-src="/api/ip/${ip.id}/character/${c.id}/image?t=${Date.now()}" onerror="this.parentNode.innerHTML='<div class=\\'char-img-placeholder\\'>图片加载失败</div>'" title="点击查看大图" />`
            : `<div class="char-img-placeholder">📷<span>暂无参考图</span></div>`
          }
        </div>
        <div class="ip-char-card-body">
          <div class="ip-char-card-name">${escapeHtml(c.name)} <span style="color:var(--muted);font-weight:400;font-size:12px;">(${escapeHtml(c.name_en)})</span></div>
          <span class="ip-char-card-role ${c.role === 'protagonist' ? 'protagonist' : 'supporting'}">${c.role === 'protagonist' ? '主角' : '配角'}</span>
          <div class="ip-char-card-desc">${escapeHtml((c.visual_description || '').substring(0, 120))}</div>
          <div class="ip-char-card-voice">
            <span class="voice-label" title="角色音色">🎙 ${escapeHtml(voiceLabel)}</span>
            <button type="button" class="ghost sm" data-action="voice" data-char-id="${c.id}">设置音色</button>
            ${vp.mode ? `<button type="button" class="ghost sm" data-action="preview-voice" data-char-id="${c.id}">试听</button>` : ''}
          </div>
          <div class="ip-char-card-actions">
            <button type="button" class="ghost sm" data-action="regen" data-char-id="${c.id}">重新生成</button>
            <button type="button" class="ghost sm" data-action="upload" data-char-id="${c.id}">上传替换</button>
          </div>
        </div>
      `;
      container.appendChild(card);
    });

    function _refreshSingleCharImage(charId) {
      const card = container.querySelector(`[data-action="regen"][data-char-id="${charId}"]`);
      if (!card) return;
      const cardEl = card.closest('.ip-char-card');
      if (!cardEl) return;
      const imgWrap = cardEl.querySelector('.ip-char-card-img');
      if (imgWrap) {
        imgWrap.innerHTML = `<img src="/api/ip/${ip.id}/character/${charId}/image?t=${Date.now()}" data-full-src="/api/ip/${ip.id}/character/${charId}/image?t=${Date.now()}" onerror="this.parentNode.innerHTML='<div class=\\'char-img-placeholder\\'>图片加载失败</div>'" title="点击查看大图" />`;
        imgWrap.querySelector('img').onclick = () => {
          const lb = document.createElement('div');
          lb.className = 'img-lightbox';
          lb.innerHTML = `<img src="/api/ip/${ip.id}/character/${charId}/image?t=${Date.now()}" />`;
          lb.onclick = () => lb.remove();
          document.body.appendChild(lb);
        };
      }
    }

    // Bind actions
    container.querySelectorAll('[data-action="regen"]').forEach(btn => {
      btn.onclick = () => _startCharRegen(btn, btn.dataset.charId, ip.id, {});
    });

    function _startCharRegen(btn, charId, ipId, extraBody) {
      const cardEl = btn.closest('.ip-char-card');
      _clearCardError(cardEl);
      _clearCardProgress(cardEl);
      btn.disabled = true; btn.textContent = '启动中…';

      api(`/api/ip/${ipId}/character/${charId}/regenerate`, {
        method: 'POST',
        json: Object.keys(extraBody).length > 0,
        body: Object.keys(extraBody).length > 0 ? extraBody : undefined,
      }).then(data => {
        if (data.task_id) {
          btn.textContent = '生成中…';
          _pollCharRegenTask(data.task_id, btn, cardEl, charId, ipId);
        }
      }).catch(e => {
        btn.textContent = '重新生成'; btn.disabled = false;
        showToast('启动失败: ' + e.message);
      });
    }

    function _pollCharRegenTask(taskId, btn, cardEl, charId, ipId) {
      const progressEl = _ensureCardProgress(cardEl);
      let lastLen = 0;
      const timer = setInterval(async () => {
        try {
          const task = await api(`/api/task/${taskId}`);
          const progress = task.progress || [];
          for (let i = lastLen; i < progress.length; i++) {
            const msg = typeof progress[i] === 'string' ? progress[i] : (progress[i].message || JSON.stringify(progress[i]));
            const line = document.createElement('div');
            line.textContent = msg;
            progressEl.appendChild(line);
            progressEl.scrollTop = progressEl.scrollHeight;
          }
          lastLen = progress.length;

          if (task.status === 'done' || task.status === 'failed') {
            clearInterval(timer);
            btn.textContent = '重新生成'; btn.disabled = false;

            if (task.status === 'done' && task.char_ok) {
              showToast(`角色图已重新生成`, 'success');
              if (task.ip) { currentIPData = task.ip; }
              _refreshSingleCharImage(charId);
              const descEl = cardEl.querySelector('.ip-char-card-desc');
              if (descEl && task.ip) {
                const uc = (task.ip.characters || []).find(x => x.id === charId);
                if (uc) descEl.textContent = (uc.visual_description || '').substring(0, 120);
              }
              setTimeout(() => _clearCardProgress(cardEl), 3000);
            } else if (task.status === 'failed') {
              const errType = task.error_type || '';
              const canFix = task.can_auto_fix || false;
              if (canFix && errType) {
                _showCardError(cardEl, charId, ipId, errType, task.error || '');
              } else {
                showToast('生成失败: ' + (task.error || '未知错误'));
              }
              setTimeout(() => _clearCardProgress(cardEl), 5000);
            } else {
              setTimeout(() => _clearCardProgress(cardEl), 3000);
            }
          }
        } catch (e) {
          clearInterval(timer);
          btn.textContent = '重新生成'; btn.disabled = false;
        }
      }, 1500);
    }

    function _ensureCardProgress(cardEl) {
      let el = cardEl.querySelector('.char-progress-log');
      if (!el) {
        el = document.createElement('div');
        el.className = 'char-progress-log';
        cardEl.appendChild(el);
      }
      el.innerHTML = '';
      return el;
    }

    function _clearCardProgress(cardEl) {
      if (!cardEl) return;
      const el = cardEl.querySelector('.char-progress-log');
      if (el) el.remove();
    }

    function _clearCardError(cardEl) {
      if (!cardEl) return;
      const old = cardEl.querySelector('.char-error-bar');
      if (old) old.remove();
    }

    function _showCardError(cardEl, charId, ipId, errorType, errMsg) {
      if (!cardEl) return;
      _clearCardError(cardEl);
      const errorLabels = {
        '版权侵权': { icon: '⚠️', label: '版权风险', hint: '角色名称或描述可能涉及已有版权' },
        '内容安全审核': { icon: '🛡️', label: '安全审核', hint: '内容未通过平台安全审核' },
        '内容过滤': { icon: '🚫', label: '内容过滤', hint: '内容被平台过滤' },
        '安全审核': { icon: '🛡️', label: '安全审核', hint: '内容未通过安全检测' },
        '政策违规': { icon: '📜', label: '政策违规', hint: '内容不符合平台政策' },
        '敏感内容': { icon: '⚡', label: '敏感内容', hint: '内容包含敏感信息' },
      };
      const info = errorLabels[errorType] || { icon: '❌', label: errorType, hint: errMsg };
      const bar = document.createElement('div');
      bar.className = 'char-error-bar';
      bar.innerHTML = `
        <div class="char-error-info">
          <span class="char-error-icon">${info.icon}</span>
          <span class="char-error-text"><strong>${info.label}</strong>：${escapeHtml(info.hint)}</span>
        </div>
        <button type="button" class="btn-auto-fix" data-char-id="${charId}">🔧 一键修正重试</button>
      `;
      cardEl.appendChild(bar);
      bar.querySelector('.btn-auto-fix').onclick = () => {
        _clearCardError(cardEl);
        const regenBtn = cardEl.querySelector('[data-action="regen"]');
        _startCharRegen(regenBtn || bar, charId, ipId, { auto_fix: true, error_reason: errorType });
      };
    }
    container.querySelectorAll('[data-action="upload"]').forEach(btn => {
      btn.onclick = () => {
        const charId = btn.dataset.charId;
        const input = document.createElement('input');
        input.type = 'file'; input.accept = 'image/*';
        input.onchange = async () => {
          if (!input.files[0]) return;
          const fd = new FormData();
          fd.append('file', input.files[0]);
          btn.disabled = true; btn.textContent = '上传中…';
          try {
            const data = await api(`/api/ip/${ip.id}/character/${charId}/upload`, { method: 'POST', body: fd });
            showToast('角色图已上传', 'success');
            if (data.ip) { currentIPData = data.ip; }
            _refreshSingleCharImage(charId);
            btn.textContent = '上传替换'; btn.disabled = false;
          } catch (e) {
            showToast('上传失败: ' + e.message);
            btn.textContent = '上传替换'; btn.disabled = false;
          }
        };
        input.click();
      };
    });

    // Click image to view full size
    container.querySelectorAll('.ip-char-card-img img[data-full-src]').forEach(img => {
      img.onclick = () => {
        const lb = document.createElement('div');
        lb.className = 'img-lightbox';
        lb.innerHTML = `<img src="${img.dataset.fullSrc}" />`;
        lb.onclick = () => lb.remove();
        document.body.appendChild(lb);
      };
    });

    // Voice actions
    container.querySelectorAll('[data-action="voice"]').forEach(btn => {
      btn.onclick = () => showVoiceSelector(ip, btn.dataset.charId);
    });
    container.querySelectorAll('[data-action="preview-voice"]').forEach(btn => {
      btn.onclick = async () => {
        const charId = btn.dataset.charId;
        btn.disabled = true; btn.textContent = '播放中…';
        try {
          const data = await api(`/api/ip/${ip.id}/character/${charId}/voice/preview`, {
            method: 'POST', json: true, body: { text: '' },
          });
          if (data.audio_base64) {
            const audio = new Audio('data:audio/wav;base64,' + data.audio_base64);
            audio.play();
            audio.onended = () => { btn.textContent = '试听'; btn.disabled = false; };
          }
        } catch (e) {
          showToast('试听失败: ' + e.message);
          btn.textContent = '试听'; btn.disabled = false;
        }
      };
    });

    // Check if all images ready → unlock step 3
    const allReady = (ip.characters || []).length > 0 && ip.characters.every(c => c.reference_image_path);
    if (allReady) {
      const step3El = $('#ip-steps-bar [data-step="3"]');
      if (step3El) { step3El.classList.remove('disabled'); }
    }
  }

  function _refreshCharVoiceLabel(charId, ipData) {
    const container = $('#ip-char-cards');
    if (!container) return;
    const c = (ipData.characters || []).find(x => x.id === charId);
    if (!c) return;
    const btn = container.querySelector(`[data-action="voice"][data-char-id="${charId}"]`);
    if (!btn) return;
    const cardEl = btn.closest('.ip-char-card');
    if (!cardEl) return;
    const voiceDiv = cardEl.querySelector('.ip-char-card-voice');
    if (!voiceDiv) return;
    const vp = c.voice_profile || {};
    const voiceLabel = vp.mode === 'preset' ? (vp.preset_name || vp.preset_id || '预置')
                     : vp.mode === 'clone' ? '克隆音色'
                     : '未设置';
    voiceDiv.innerHTML = `
      <span class="voice-label" title="角色音色">🎙 ${escapeHtml(voiceLabel)}</span>
      <button type="button" class="ghost sm" data-action="voice" data-char-id="${c.id}">设置音色</button>
      ${vp.mode ? `<button type="button" class="ghost sm" data-action="preview-voice" data-char-id="${c.id}">试听</button>` : ''}
    `;
    voiceDiv.querySelector('[data-action="voice"]').onclick = () => showVoiceSelector(ipData, charId);
    const previewBtn = voiceDiv.querySelector('[data-action="preview-voice"]');
    if (previewBtn) {
      previewBtn.onclick = async () => {
        previewBtn.disabled = true; previewBtn.textContent = '播放中…';
        try {
          const data = await api(`/api/ip/${ipData.id}/character/${charId}/voice/preview`, {
            method: 'POST', json: true, body: { text: '' },
          });
          if (data.audio_base64) {
            const audio = new Audio('data:audio/wav;base64,' + data.audio_base64);
            audio.play();
            audio.onended = () => { previewBtn.textContent = '试听'; previewBtn.disabled = false; };
          }
        } catch (e) {
          showToast('试听失败: ' + e.message);
          previewBtn.textContent = '试听'; previewBtn.disabled = false;
        }
      };
    }
  }

  function _closeVoiceDrawer() {
    const overlay = document.querySelector('.voice-drawer-overlay');
    const drawer = document.querySelector('.voice-drawer');
    if (overlay) overlay.remove();
    if (drawer) drawer.remove();
  }

  async function showVoiceSelector(ip, charId) {
    _closeVoiceDrawer();
    const presets = await loadVoicePresets();
    const char = (ip.characters || []).find(c => c.id === charId);
    if (!char) return;
    const vp = char.voice_profile || {};

    const overlay = document.createElement('div');
    overlay.className = 'voice-drawer-overlay';
    overlay.onclick = _closeVoiceDrawer;
    document.body.appendChild(overlay);

    const drawer = document.createElement('div');
    drawer.className = 'voice-drawer';

    let presetsHtml = '';
    (presets || []).forEach(group => {
      presetsHtml += `<div class="voice-group-label">${escapeHtml(group.category)}</div>`;
      presetsHtml += '<div class="voice-grid">';
      (group.voices || []).forEach(v => {
        const sel = vp.preset_id === v.id ? ' selected' : '';
        presetsHtml += `<div class="voice-chip${sel}" data-vid="${v.id}" data-vname="${escapeHtml(v.name_zh)}" title="${escapeHtml(v.description_zh)}">${escapeHtml(v.name_zh)}<br><small>${escapeHtml(v.description_zh)}</small></div>`;
      });
      presetsHtml += '</div>';
    });

    drawer.innerHTML = `
      <div class="voice-drawer-header">
        <h3>🎙 ${escapeHtml(char.name)} — 音色设置</h3>
        <button class="close-btn" id="voice-drawer-close">&times;</button>
      </div>
      <div class="voice-drawer-body">
        <div style="margin-bottom:16px;">
          <label style="cursor:pointer;"><input type="radio" name="vd-mode" value="preset" ${vp.mode !== 'clone' ? 'checked' : ''}> 预置音色</label>
          <label style="margin-left:16px;cursor:pointer;"><input type="radio" name="vd-mode" value="clone" ${vp.mode === 'clone' ? 'checked' : ''}> 上传克隆</label>
        </div>
        <div id="vd-preset-panel">${presetsHtml}</div>
        <div id="vd-clone-panel" class="${vp.mode === 'clone' ? '' : 'hidden'}" style="margin-top:12px;">
          <p style="color:var(--muted);font-size:13px;margin-bottom:8px;">上传 3~10 秒清晰语音用于声音克隆（支持 wav/mp3）。</p>
          <input type="file" id="vd-clone-file" accept="audio/*" style="font-size:13px;" />
          ${vp.reference_audio_path ? `<p style="font-size:12px;color:var(--accent);margin-top:6px;">已有参考音频</p>` : ''}
        </div>
      </div>
      <div class="voice-drawer-footer">
        <button class="ghost" id="vd-cancel">取消</button>
        <button class="primary" id="vd-save">保存</button>
      </div>
    `;
    document.body.appendChild(drawer);

    let selectedPresetId = vp.preset_id || '';
    let selectedPresetName = vp.preset_name || '';

    drawer.querySelectorAll('.voice-chip').forEach(chip => {
      chip.onclick = () => {
        drawer.querySelectorAll('.voice-chip').forEach(c => c.classList.remove('selected'));
        chip.classList.add('selected');
        selectedPresetId = chip.dataset.vid;
        selectedPresetName = chip.dataset.vname;
      };
    });

    drawer.querySelectorAll('[name="vd-mode"]').forEach(radio => {
      radio.onchange = () => {
        const presetPanel = drawer.querySelector('#vd-preset-panel');
        const clonePanel = drawer.querySelector('#vd-clone-panel');
        if (radio.value === 'preset') { presetPanel.classList.remove('hidden'); clonePanel.classList.add('hidden'); }
        else { presetPanel.classList.add('hidden'); clonePanel.classList.remove('hidden'); }
      };
    });

    drawer.querySelector('#voice-drawer-close').onclick = _closeVoiceDrawer;
    drawer.querySelector('#vd-cancel').onclick = _closeVoiceDrawer;

    drawer.querySelector('#vd-save').onclick = async () => {
      const mode = drawer.querySelector('[name="vd-mode"]:checked').value;
      if (mode === 'clone') {
        const fileInput = drawer.querySelector('#vd-clone-file');
        if (fileInput.files && fileInput.files[0]) {
          const fd = new FormData();
          fd.append('file', fileInput.files[0]);
          try {
            const data = await api(`/api/ip/${ip.id}/character/${charId}/voice/upload`, { method: 'POST', body: fd });
            showToast('参考音频已上传', 'success');
            if (data.ip) { currentIPData = data.ip; _refreshCharVoiceLabel(charId, data.ip); }
          } catch (e) { showToast('上传失败: ' + e.message); }
        } else if (!vp.reference_audio_path) {
          showToast('请选择音频文件');
          return;
        }
      } else {
        if (!selectedPresetId) { showToast('请选择一个预置音色'); return; }
        try {
          const data = await api(`/api/ip/${ip.id}/character/${charId}/voice`, {
            method: 'PUT', json: true,
            body: { mode: 'preset', preset_id: selectedPresetId, preset_name: selectedPresetName },
          });
          showToast('音色已设置', 'success');
          if (data.ip) { currentIPData = data.ip; _refreshCharVoiceLabel(charId, data.ip); }
        } catch (e) { showToast('设置失败: ' + e.message); }
      }
      _closeVoiceDrawer();
    };
  }

  // Generate all character images (async)
  if ($('#btn-gen-all-char-images')) {
    $('#btn-gen-all-char-images').onclick = async () => {
      if (!currentIPId) { showToast('请先选择 IP'); return; }
      const btn = $('#btn-gen-all-char-images');
      btn.disabled = true; btn.textContent = '生成中…';
      const logArea = $('#ip-char-log');
      const progressWrap = $('#ip-char-progress');
      progressWrap.classList.remove('hidden');
      logArea.innerHTML = '<div>正在启动角色图生成…</div>';
      try {
        const data = await api(`/api/ip/${currentIPId}/generate-images`, { method: 'POST', json: true, body: {} });
        ipImageTaskId = data.task_id;
        pollIPImageTask(data.task_id);
      } catch (e) {
        showToast('启动角色图生成失败: ' + e.message);
        btn.disabled = false; btn.textContent = '生成全部角色图';
      }
    };
  }

  function pollIPImageTask(taskId) {
    const logArea = $('#ip-char-log');
    const btn = $('#btn-gen-all-char-images');
    let lastLen = 0;
    const container = $('#ip-char-cards');

    function _applyPartialIPUpdate(partialIP) {
      if (!partialIP || !container) return;
      const ipId = partialIP.id;
      (partialIP.characters || []).forEach(c => {
        if (!c.reference_image_path) return;
        const regenBtn = container.querySelector(`[data-action="regen"][data-char-id="${c.id}"]`);
        if (!regenBtn) return;
        const cardEl = regenBtn.closest('.ip-char-card');
        if (!cardEl) return;
        const imgWrap = cardEl.querySelector('.ip-char-card-img');
        if (!imgWrap || imgWrap.querySelector('img')) return; // 已有图片则跳过
        imgWrap.innerHTML = `<img src="/api/ip/${ipId}/character/${c.id}/image?t=${Date.now()}" data-full-src="/api/ip/${ipId}/character/${c.id}/image?t=${Date.now()}" onerror="this.parentNode.innerHTML='<div class=\\'char-img-placeholder\\'>图片加载失败</div>'" title="点击查看大图" />`;
      });
    }

    const timer = setInterval(async () => {
      try {
        const task = await api(`/api/task/${taskId}`);
        const progress = task.progress || [];
        for (let i = lastLen; i < progress.length; i++) {
          const msg = typeof progress[i] === 'string' ? progress[i] : (progress[i].message || JSON.stringify(progress[i]));
          const div = document.createElement('div');
          div.textContent = msg;
          logArea.appendChild(div);
          logArea.scrollTop = logArea.scrollHeight;
        }
        lastLen = progress.length;
        // 每张图生成完后后端即推送局部 IP 数据，在此实时刷新对应卡片
        if (task.ip) _applyPartialIPUpdate(task.ip);
        if (task.status === 'done' || task.status === 'failed') {
          clearInterval(timer);
          btn.disabled = false; btn.textContent = '生成全部角色图';
          if (task.status === 'done') {
            showToast('角色图生成完成！', 'success');
            reloadCurrentIP();
          } else {
            showToast('角色图生成失败: ' + (task.error || '未知错误'));
          }
        }
      } catch (e) {
        clearInterval(timer);
        btn.disabled = false; btn.textContent = '生成全部角色图';
      }
    }, 2000);
  }

  // ── Step 3: 故事大纲生成 ──
  if ($('#btn-ip-gen-story')) {
    $('#btn-ip-gen-story').onclick = async () => {
      if (!currentIPId) return;
      const btn = $('#btn-ip-gen-story');
      const hint = ($('#ip-theme-hint') || {}).value || '';
      btn.disabled = true; btn.textContent = '生成中…';
      try {
        const data = await api(`/api/ip/${currentIPId}/story`, {
          method: 'POST', json: true,
          body: { theme_hint: hint, min_shots: 8, max_shots: 16 },
        });
        ipStoryOutline = data.outline;
        renderIPStoryPreview(data.outline);
        showToast('故事大纲已生成', 'success');
        const recShots = parseInt(data.outline.recommended_shot_count);
        if (recShots && recShots > 0) {
          const avgDur = parseFloat(($('#ip-avg-shot-dur') || {}).value) || 2.5;
          const totalSec = Math.round(recShots * avgDur);
          const m = Math.floor(totalSec / 60), s = totalSec % 60;
          if ($('#ip-target-min')) $('#ip-target-min').value = m;
          if ($('#ip-target-sec')) $('#ip-target-sec').value = s;
          _calcShotCount();
        }
        ipSetStep(4);
      } catch (e) { showToast('故事生成失败: ' + e.message); }
      btn.disabled = false; btn.textContent = '生成故事大纲';
    };
  }

  function _renderOutlineTranslation(container, tTitle, tSynopsis, tBeats) {
    let html = `<b>标题:</b> ${escapeHtml(tTitle)}<br/><b>概要:</b> ${escapeHtml(tSynopsis)}`;
    if (tBeats.length) {
      html += '<br/><b>叙事节拍:</b><ol style="margin:4px 0 0 16px;">';
      tBeats.forEach(b => { html += `<li>${escapeHtml(b)}</li>`; });
      html += '</ol>';
    }
    container.innerHTML = `<div class="translate-result">${html}</div>`;
  }

  function renderIPStoryPreview(outline) {
    const container = $('#ip-story-preview');
    if (!container) return;
    container.classList.remove('hidden');
    const title = outline.title || outline.episode_title || '';
    const synopsis = outline.synopsis || outline.logline || '';
    const beats = outline.narrative_beats || [];
    const characters = outline.characters_featured || outline.cast || [];

    let beatsHtml = '';
    if (beats.length) {
      beatsHtml = '<ul class="ip-story-beats">' +
        beats.map(b => {
          const desc = typeof b === 'string' ? b : (b.beat || b.action || b.description || JSON.stringify(b));
          return `<li>${escapeHtml(desc)}</li>`;
        }).join('') + '</ul>';
    }

    let castHtml = '';
    if (characters.length) {
      castHtml = characters.map(c => {
        const name = typeof c === 'string' ? c : (c.name || '');
        return `<span style="padding:3px 8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;font-size:12px;">${escapeHtml(name)}</span>`;
      }).join(' ');
    }

    const recShots = outline.recommended_shot_count;
    const recHtml = recShots ? `<div class="ip-story-field"><div class="ip-story-field-label">推荐镜头数</div><div class="ip-story-field-content" style="font-weight:600;color:var(--primary);">${recShots} 镜</div></div>` : '';

    container.innerHTML = `
      <div class="ip-story-card">
        <h4>${escapeHtml(title)}</h4>
        ${synopsis ? `<div class="ip-story-field"><div class="ip-story-field-label">故事概要</div><div class="ip-story-field-content">${escapeHtml(synopsis)}</div></div>` : ''}
        ${castHtml ? `<div class="ip-story-field"><div class="ip-story-field-label">出场角色</div><div style="display:flex;flex-wrap:wrap;gap:4px;">${castHtml}</div></div>` : ''}
        ${recHtml}
        ${beatsHtml ? `<div class="ip-story-field"><div class="ip-story-field-label">叙事节拍</div>${beatsHtml}</div>` : ''}
        <div class="inline-actions" style="margin-top:12px;">
          <button type="button" class="ghost sm" id="btn-ip-story-translate">翻译</button>
          <button type="button" class="ghost sm" id="btn-ip-story-refine">修改润色</button>
          <button type="button" class="ghost sm" id="btn-ip-story-regen">重新生成</button>
        </div>
        <div id="ip-story-translate-result" class="hidden"></div>
        <div id="ip-story-refine-area" class="hidden"></div>
      </div>
    `;

    // Translate button — use cached translation or call qwen-mt-flash
    const translateBtn = $('#btn-ip-story-translate');
    if (translateBtn) translateBtn.onclick = async () => {
      const resultDiv = $('#ip-story-translate-result');
      resultDiv.classList.remove('hidden');

      const cached = outline._translation_zh;
      if (cached && cached.title) {
        _renderOutlineTranslation(resultDiv, cached.title, cached.synopsis, cached.beats || []);
        return;
      }

      resultDiv.innerHTML = '<div class="translate-result">翻译中…</div>';
      try {
        const title = outline.title || outline.episode_title || '';
        const synopsis = outline.synopsis || outline.logline || '';
        const beats = (outline.narrative_beats || []).map(b => b.description || '').filter(Boolean);
        const allText = [title, synopsis, ...beats].join('\n[SEP]\n');
        const data = await api('/api/translate', {
          method: 'POST', json: true,
          body: { text: allText, target: 'zh' },
        });
        const parts = (data.result || '').split(/\[SEP\]/i).map(s => s.trim());
        const tTitle = parts[0] || '';
        const tSynopsis = parts[1] || '';
        const tBeats = parts.slice(2).filter(Boolean);
        _renderOutlineTranslation(resultDiv, tTitle, tSynopsis, tBeats);

        outline._translation_zh = { title: tTitle, synopsis: tSynopsis, beats: tBeats };
        ipStoryOutline = outline;
        api('/api/ip/' + currentIPId + '/outline', { method: 'PUT', json: true, body: { outline } }).catch(() => {});
      } catch (e) { resultDiv.innerHTML = `<div class="translate-result" style="color:var(--danger)">翻译失败: ${escapeHtml(e.message)}</div>`; }
    };

    if (outline._translation_zh && outline._translation_zh.title) {
      const resultDiv = $('#ip-story-translate-result');
      if (resultDiv) {
        resultDiv.classList.remove('hidden');
        _renderOutlineTranslation(resultDiv, outline._translation_zh.title, outline._translation_zh.synopsis, outline._translation_zh.beats || []);
      }
    }

    // AI refine button
    const refineBtn = $('#btn-ip-story-refine');
    if (refineBtn) refineBtn.onclick = () => {
      const area = $('#ip-story-refine-area');
      area.classList.toggle('hidden');
      if (!area.classList.contains('hidden') && !area.querySelector('textarea')) {
        area.innerHTML = `
          <div class="refine-popup">
            <textarea placeholder="输入修改意见，如：让故事更搞笑，加入一个反转…"></textarea>
            <div class="flex">
              <button type="button" class="primary sm" id="btn-ip-story-refine-go">润色</button>
              <button type="button" class="ghost sm" id="btn-ip-story-refine-cancel">取消</button>
            </div>
          </div>
        `;
        $('#btn-ip-story-refine-cancel').onclick = () => area.classList.add('hidden');
        $('#btn-ip-story-refine-go').onclick = async () => {
          const instruction = area.querySelector('textarea').value.trim();
          if (!instruction) { showToast('请输入修改意见'); return; }
          const goBtn = $('#btn-ip-story-refine-go');
          goBtn.disabled = true; goBtn.textContent = '润色中…';
          try {
            const data = await api('/api/ip/' + currentIPId + '/refine', {
              method: 'POST', json: true,
              body: { section: 'story_outline', instruction, current_content: ipStoryOutline },
            });
            if (typeof data.result === 'object') {
              const oldOutline = ipStoryOutline;
              ipStoryOutline = data.result;
              renderIPStoryPreview(data.result);
              showToast('故事已润色', 'success');
              _recordFeedback('story', 'story_outline', instruction, oldOutline, data.result);
              api('/api/ip/' + currentIPId + '/outline', { method: 'PUT', json: true, body: { outline: data.result } }).catch(() => {});
            }
          } catch (e) { showToast('润色失败: ' + e.message); }
          goBtn.disabled = false; goBtn.textContent = '润色';
        };
      }
    };

    // Regen button
    const regenBtn = $('#btn-ip-story-regen');
    if (regenBtn) regenBtn.onclick = () => {
      ipStoryOutline = null;
      container.classList.add('hidden');
      $('#btn-ip-gen-story').click();
    };
  }

  // ── Step 4: 分镜生成 ──
  let _ipLastStoryboardTaskId = null;
  let _ipVideoRuns = []; // { task_id, status, output_url, created }

  const _SHOT_TYPE_MAP = { ECU: '极特写', CU: '特写', MCU: '中近景', MS: '中景', MLS: '中远景', WS: '远景', EWS: '大远景' };
  let _ipStoryboardShots = [];
  let _ipStoryboardTranslated = false;

  async function _loadIPStoryboardPreview(taskId) {
    const container = $('#ip-storyboard-preview');
    if (!container) return;
    try {
      const st = await api(`/api/task/${taskId}`);
      const sb = st.storyboard;
      if (!sb || !sb.shots || !sb.shots.length) {
        container.innerHTML = '<p style="color:var(--text-secondary);">未获取到分镜数据</p>';
        container.classList.remove('hidden');
        return;
      }
      _ipStoryboardShots = sb.shots;
      _ipStoryboardTranslated = sb.shots.some(s => s._scene_zh || s._dialogue_zh);
      _renderStoryboardTable(container);
    } catch (e) {
      container.innerHTML = `<p style="color:var(--danger);">加载分镜失败: ${escapeHtml(e.message)}</p>`;
      container.classList.remove('hidden');
    }
  }

  function _renderStoryboardTable(container) {
    if (!container) container = $('#ip-storyboard-preview');
    if (!container) return;
    const shots = _ipStoryboardShots;
    if (!shots.length) return;

    const totalDur = shots.reduce((s, sh) => s + (parseFloat(sh.duration) || 0), 0);
    let cards = '';
    shots.forEach((shot, i) => {
      const id = shot.shot_id || i + 1;
      const dur = shot.duration || '?';
      const rawType = (shot.shot_type || '').toUpperCase().trim();
      const typeCn = _SHOT_TYPE_MAP[rawType] || rawType;
      const typeLabel = typeCn !== rawType ? `${typeCn}(${rawType})` : rawType;

      const chars = (shot.characters_in_shot || []);
      const charsHtml = chars.length
        ? chars.map(c => `<span class="mention-chip" style="font-size:0.75em;">@${escapeHtml(c)}</span>`).join(' ')
        : (shot.focal_character ? `<span class="mention-chip" style="font-size:0.75em;">@${escapeHtml(shot.focal_character)}</span>` : '<span style="opacity:0.5;">—</span>');

      const scene = escapeHtml(shot._scene_zh || shot.scene_description || '');
      const dlg = shot.dialogue ? escapeHtml(shot._dialogue_zh || shot.dialogue) : '<span style="opacity:0.4;">无对白</span>';
      const prompt = escapeHtml(shot._prompt_zh || shot.generation_prompt || '');
      const action = shot.character_action ? escapeHtml(shot._action_zh || shot.character_action) : '';
      const camera = shot.camera_movement ? escapeHtml(shot.camera_movement) : '';
      const mood = shot.mood ? escapeHtml(shot.mood) : '';

      cards += `<div class="ip-shot-card" data-shot-idx="${i}" style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;background:var(--surface);">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;">
          <span style="font-weight:700;font-size:1.1em;color:var(--primary);">#${id}</span>
          <span style="background:var(--bg);padding:2px 8px;border-radius:4px;font-size:0.8em;">${dur}s</span>
          <span style="background:var(--bg);padding:2px 8px;border-radius:4px;font-size:0.8em;" title="${rawType}: ${typeCn}">${typeLabel}</span>
          ${camera ? `<span style="font-size:0.8em;opacity:0.7;">${escapeHtml(camera)}</span>` : ''}
          <span style="flex:1;"></span>
          ${charsHtml}
          <button type="button" class="ghost sm ip-shot-refine-btn" data-idx="${i}" style="font-size:0.75em;padding:2px 8px;" title="修改润色此分镜">✏ 润色</button>
        </div>
        <div style="font-size:0.85em;line-height:1.5;">
          <div><b>场景：</b>${scene}</div>
          ${action ? `<div><b>动作：</b>${action}</div>` : ''}
          <div><b>对白：</b><span style="font-style:italic;color:var(--primary);">${dlg}</span></div>
          ${mood ? `<div><b>氛围：</b><span style="opacity:0.7;">${mood}</span></div>` : ''}
          <div style="margin-top:4px;opacity:0.6;font-size:0.85em;"><b>画面指令：</b>${prompt}</div>
        </div>
      </div>`;
    });

    container.innerHTML = `
      <details open>
        <summary style="cursor:pointer;font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span>分镜预览 · ${shots.length} 镜 · ${totalDur.toFixed(1)}s</span>
          <button type="button" class="ghost sm" id="ip-sb-translate-btn" style="font-size:0.75em;padding:2px 10px;">${_ipStoryboardTranslated ? '显示原文' : '翻译'}</button>
        </summary>
        ${cards}
      </details>`;
    container.classList.remove('hidden');

    // 翻译按钮
    const trBtn = container.querySelector('#ip-sb-translate-btn');
    if (trBtn) {
      trBtn.onclick = async (e) => {
        e.stopPropagation();
        if (_ipStoryboardTranslated) {
          _ipStoryboardTranslated = false;
          _renderStoryboardTable(container);
          return;
        }
        trBtn.disabled = true; trBtn.textContent = '翻译中…';
        try {
          const texts = shots.map(s =>
            [s.scene_description || '', s.dialogue || '', s.generation_prompt || '', s.character_action || ''].join('\n[FIELD]\n')
          );
          const joined = texts.join('\n[SHOT]\n');
          const resp = await api('/api/translate', { method: 'POST', json: true, body: { text: joined, source_lang: 'en', target_lang: 'zh' } });
          const translated = (resp.translated || '').split('\n[SHOT]\n');
          translated.forEach((block, i) => {
            if (i >= shots.length) return;
            const fields = block.split('\n[FIELD]\n');
            shots[i]._scene_zh = (fields[0] || '').trim();
            shots[i]._dialogue_zh = (fields[1] || '').trim();
            shots[i]._prompt_zh = (fields[2] || '').trim();
            shots[i]._action_zh = (fields[3] || '').trim();
          });
          _ipStoryboardTranslated = true;
          _renderStoryboardTable(container);
          // 持久化翻译到后端
          if (_ipLastStoryboardTaskId) {
            const updates = shots.map((s, idx) => ({ _index: idx, _scene_zh: s._scene_zh || '', _dialogue_zh: s._dialogue_zh || '', _prompt_zh: s._prompt_zh || '', _action_zh: s._action_zh || '' }));
            api(`/api/task/${_ipLastStoryboardTaskId}/storyboard`, { method: 'PUT', json: true, body: { shots: updates } }).catch(() => {});
          }
        } catch (err) {
          showToast('翻译失败: ' + err.message);
          trBtn.disabled = false; trBtn.textContent = '翻译';
        }
      };
    }

    // 润色按钮
    container.querySelectorAll('.ip-shot-refine-btn').forEach(btn => {
      btn.onclick = () => {
        const idx = parseInt(btn.dataset.idx);
        const shot = shots[idx];
        if (!shot) return;
        const _pick = (s) => s ? ({ shot_id: s.shot_id, scene_description: s.scene_description, character_action: s.character_action, dialogue: s.dialogue, mood: s.mood, generation_prompt: s.generation_prompt, duration: s.duration, shot_type: s.shot_type, characters_in_shot: s.characters_in_shot }) : null;
        const content = {
          target_shot: {
            shot_id: shot.shot_id,
            scene_description: shot.scene_description,
            character_action: shot.character_action,
            dialogue: shot.dialogue,
            mood: shot.mood,
            generation_prompt: shot.generation_prompt,
            duration: shot.duration,
            shot_type: shot.shot_type,
            camera_movement: shot.camera_movement,
            characters_in_shot: shot.characters_in_shot,
            focal_character: shot.focal_character,
          },
          prev_shot: _pick(shots[idx - 1]),
          next_shot: _pick(shots[idx + 1]),
          story_outline_synopsis: ipStoryOutline ? (ipStoryOutline.synopsis || '') : '',
          total_shots: shots.length,
          shot_position: `${idx + 1}/${shots.length}`,
        };
        showRefinePopup('storyboard_shot', content, (result) => {
          if (typeof result === 'object') {
            const updated = result.target_shot || result;
            Object.assign(shots[idx], updated);
            shots[idx]._scene_zh = ''; shots[idx]._dialogue_zh = '';
            shots[idx]._prompt_zh = ''; shots[idx]._action_zh = '';
            _ipStoryboardTranslated = false;
            _renderStoryboardTable(container);
            // 持久化润色结果
            if (_ipLastStoryboardTaskId) {
              api(`/api/task/${_ipLastStoryboardTaskId}/storyboard`, { method: 'PUT', json: true, body: { shots: [{ _index: idx, ...updated }] } }).catch(() => {});
            }
            showToast('分镜已更新', 'success');
          }
        }, 'storyboard');
      };
    });

    // 更新 Step 5 的预计视频时长
    const durEl = $('#ip-estimated-duration');
    if (durEl) {
      const mins = Math.floor(totalDur / 60);
      const secs = Math.round(totalDur % 60);
      const timeStr = mins > 0 ? `${mins}分${secs}秒` : `${secs}秒`;
      durEl.textContent = `预计视频时长：${timeStr}（${shots.length} 个镜头，总计 ${totalDur.toFixed(1)}s）`;
      durEl.classList.remove('hidden');
    }
  }

  const _RUN_STATUS_MAP = { done: '✅ 完成', failed: '❌ 失败', cancelled: '⏹ 已取消', generating: '⏳ 生成中', generating_video: '⏳ 视频生成中', generating_storyboard: '⏳ 分镜生成中', pending: '⏳ 等待中', loading: '⏳ 加载中', storyboard_ready: '⏳ 分镜就绪', unknown: '❓ 未知' };

  function _renderIPVideoRuns() {
    const container = $('#ip-video-runs');
    if (!container) return;
    if (!_ipVideoRuns.length) { container.innerHTML = ''; return; }
    let html = '<div style="margin-bottom:8px;font-weight:600;font-size:0.9em;">生成记录</div>';
    _ipVideoRuns.forEach((run, i) => {
      const statusText = _RUN_STATUS_MAP[run.status] || `⏳ ${run.status}`;
      const canResume = (run.status === 'failed' || run.status === 'cancelled') && run._has_segments;
      const canView = !['loading', 'pending'].includes(run.status);
      const canFeedback = run.status === 'done';

      const isActive = ['generating', 'generating_video', 'generating_storyboard', 'pending'].includes(run.status);

      let btns = '';
      if (canView && !isActive) btns += `<button type="button" class="ghost sm ip-run-toggle" data-idx="${i}" style="font-size:0.75em;padding:2px 8px;">📂 查看</button>`;
      if (canResume) btns += `<button type="button" class="ghost sm ip-run-resume" data-idx="${i}" style="font-size:0.75em;padding:2px 8px;">▶ 继续生成</button>`;
      if (isActive) btns += `<button type="button" class="ghost sm ip-run-stop" data-idx="${i}" style="font-size:0.75em;padding:2px 8px;color:var(--danger);">⏹ 终止</button>`;
      if (canFeedback) btns += `<button type="button" class="ghost sm ip-run-feedback" data-idx="${i}" style="font-size:0.75em;padding:2px 8px;">💬 反馈</button>`;

      html += `<div class="ip-run-block" data-idx="${i}" style="border-bottom:1px solid var(--border);padding:6px 0;">
        <div style="display:flex;align-items:center;gap:8px;font-size:0.85em;">
          <span style="font-weight:600;">#${i + 1}</span>
          <span>${statusText}</span>
          ${btns}
        </div>
        <div class="ip-run-detail" data-idx="${i}" style="display:none;margin-top:8px;padding:8px 12px;background:var(--surface);border-radius:6px;font-size:0.82em;"></div>
      </div>`;
    });
    container.innerHTML = html;

    container.querySelectorAll('.ip-run-toggle').forEach(btn => {
      btn.onclick = async () => {
        const idx = parseInt(btn.dataset.idx);
        const detail = container.querySelector(`.ip-run-detail[data-idx="${idx}"]`);
        if (!detail) return;
        if (detail.style.display !== 'none') {
          detail.style.display = 'none';
          btn.textContent = '📂 查看';
          return;
        }
        btn.textContent = '⏳ 加载…';
        btn.disabled = true;
        await _loadRunDetailInline(detail, _ipVideoRuns[idx], idx);
        detail.style.display = 'block';
        btn.textContent = '📂 收起';
        btn.disabled = false;
      };
    });

    container.querySelectorAll('.ip-run-resume').forEach(btn => {
      btn.onclick = () => {
        const idx = parseInt(btn.dataset.idx);
        _resumeIPVideoRun(idx);
      };
    });

    container.querySelectorAll('.ip-run-feedback').forEach(btn => {
      btn.onclick = () => {
        const idx = parseInt(btn.dataset.idx);
        const run = _ipVideoRuns[idx];
        if (run) _showVideoFeedbackPopup(run, idx);
      };
    });

    container.querySelectorAll('.ip-run-stop').forEach(btn => {
      btn.onclick = async () => {
        const idx = parseInt(btn.dataset.idx);
        const run = _ipVideoRuns[idx];
        if (!run) return;
        btn.disabled = true; btn.textContent = '终止中…';
        try {
          await api(`/api/task/cancel/${run.task_id}`, { method: 'POST', json: true, body: {} });
          showToast('已发送终止信号', 'success');
          _ipVideoAppendLog('⏹ 已发送终止信号，等待当前段完成后停止…');
        } catch (e) { showToast('终止失败: ' + e.message); btn.disabled = false; btn.textContent = '⏹ 终止'; }
      };
    });
  }

  async function _loadRunDetailInline(el, run, idx) {
    el.innerHTML = '<div style="opacity:0.6;">加载中…</div>';
    try {
      const st = await api(`/api/task/${run.task_id}`);
      let logHtml = '';

      const progress = st.progress || [];
      if (progress.length) {
        const lines = progress.map(p => {
          const t = p.t ? p.t.replace(/.*T/, '').replace(/\.\d+.*/, '') : '';
          return `<div>[${escapeHtml(t)}] ${escapeHtml(p.msg)}</div>`;
        });
        logHtml += `<div style="max-height:150px;overflow-y:auto;margin-bottom:8px;padding:4px;background:var(--bg);border-radius:4px;">${lines.join('')}</div>`;
      }
      if (st.error) logHtml += `<div style="color:var(--danger);margin-bottom:8px;">❌ ${escapeHtml(st.error)}</div>`;

      const segs = st.segments || [];
      if (segs.length) {
        logHtml += `<div style="margin-bottom:4px;font-weight:600;">片段（${segs.length}）</div>`;
        logHtml += '<div class="segments-grid" style="margin-bottom:8px;">';
        segs.forEach(seg => {
          logHtml += `<div class="segment-thumb"><video src="${escapeAttr(seg.url)}" muted playsinline preload="metadata"></video><div class="seg-label">${escapeHtml(seg.name)}</div></div>`;
        });
        logHtml += '</div>';
      }

      if (st.output_url) {
        logHtml += `<div style="margin-bottom:4px;font-weight:600;">成片</div>`;
        logHtml += `<video src="${escapeAttr(st.output_url)}" controls style="width:100%;max-height:300px;border-radius:6px;"></video>`;
        logHtml += `<div style="margin-top:4px;"><a class="btn primary sm" href="${escapeAttr(st.output_url)}" download>⬇ 下载 MP4</a></div>`;
      }

      if (!progress.length && !st.error && !segs.length && !st.output_url) {
        logHtml = '<div style="opacity:0.6;">（无数据）</div>';
      }

      el.innerHTML = logHtml;

      el.querySelectorAll('.segment-thumb video').forEach(v => {
        v.onmouseenter = function() { this.play(); };
        v.onmouseleave = function() { this.pause(); this.currentTime = 0; };
      });
    } catch (e) {
      el.innerHTML = `<div style="color:var(--danger);">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  function _showVideoFeedbackPopup(run, runIdx) {
    const existing = document.querySelector('.refine-popup-global');
    if (existing) existing.remove();
    const popup = document.createElement('div');
    popup.className = 'refine-popup-global';
    popup.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:200;width:min(500px,90vw);background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    popup.innerHTML = `
      <h4 style="margin:0 0 12px;">视频反馈 · #${runIdx + 1}</h4>
      <p style="font-size:0.85em;opacity:0.7;margin:0 0 8px;">观看视频后，提交您的评价和改进建议。这些反馈将用于优化后续生成。</p>
      <textarea placeholder="例如：节奏太快/画面切换太频繁/角色表情不够生动/对白配音不自然…" style="min-height:80px;"></textarea>
      <div class="flex" style="margin-top:12px;justify-content:flex-end;">
        <button type="button" class="primary sm" id="vfb-submit">提交反馈</button>
        <button type="button" class="ghost sm" id="vfb-cancel">取消</button>
      </div>
    `;
    document.body.appendChild(popup);
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:199;';
    document.body.appendChild(overlay);
    const close = () => { popup.remove(); overlay.remove(); };
    overlay.onclick = close;
    popup.querySelector('#vfb-cancel').onclick = close;
    popup.querySelector('#vfb-submit').onclick = () => {
      const feedback = popup.querySelector('textarea').value.trim();
      if (!feedback) { showToast('请输入反馈内容'); return; }
      _recordFeedback('video', `video_run_${runIdx + 1}`, feedback, `task_id: ${run.task_id}`, '');
      close();
    };
  }

  let _ipTaskSSE = null;
  let _ipVideoProgressLines = [];
  let _ipVideoSegmentPollTimer = null;
  let _ipActiveVideoTaskId = null;

  function _connectIPTaskSSE(taskId, statusDiv, btn, btnLabel, onDone, onFail, opts) {
    const terminalStates = (opts && opts.terminal) || ['done', 'failed', 'cancelled', 'storyboard_ready'];
    if (_ipTaskSSE) { _ipTaskSSE.close(); _ipTaskSSE = null; }
    const es = new EventSource(`/api/task/stream/${taskId}`);
    _ipTaskSSE = es;

    const logWrap = $('#ip-progress-log-wrap');
    const logEl = $('#ip-progress-log');
    const barWrap = $('#ip-gen-progress-wrap');
    const bar = $('#ip-gen-progress-bar');
    if (logWrap) logWrap.classList.remove('hidden');
    _ipVideoProgressLines = [];

    _startIPSegmentPoll(taskId);

    const _handleTerminal = (status, error) => {
      es.close(); _ipTaskSSE = null;
      _stopIPSegmentPoll();
      btn.disabled = false;
      if (status === 'done') {
        statusDiv.textContent = '生成完成';
        showToast('任务完成', 'success');
        _ipVideoAppendLog('✅ 任务完成');
        _refreshIPSegmentsAndOutput(taskId);
        if (onDone) onDone();
      } else if (status === 'storyboard_ready') {
        statusDiv.textContent = '分镜就绪';
        showToast('分镜生成完成', 'success');
        _ipVideoAppendLog('✅ 分镜就绪');
        if (barWrap) { barWrap.classList.remove('hidden'); bar.style.width = '100%'; }
        _refreshIPSegmentsAndOutput(taskId);
        if (onDone) onDone();
      } else if (status === 'failed') {
        statusDiv.textContent = `生成失败: ${error || '未知错误'}`;
        showToast('任务失败: ' + (error || ''), 'error');
        _ipVideoAppendLog(`❌ 失败: ${error || '未知错误'}`);
        _refreshIPSegmentsAndOutput(taskId);
        if (onFail) onFail(error);
      } else {
        statusDiv.textContent = '任务已取消';
        _ipVideoAppendLog('⏹ 任务已取消');
        _refreshIPSegmentsAndOutput(taskId);
        if (onFail) onFail('cancelled');
      }
      _updateIPGenButton();
    };
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === 'progress') {
          const msg = data.msg || '生成中…';
          statusDiv.textContent = msg;
          _ipVideoAppendLog(msg);
        } else if (data.type === 'snapshot' || data.type === 'status') {
          if (data.type === 'snapshot' && data.progress && data.progress.length) {
            data.progress.forEach(p => {
              _ipVideoAppendLog(p.msg || '');
            });
            const lastMsg = data.progress[data.progress.length - 1];
            if (lastMsg && lastMsg.msg) statusDiv.textContent = lastMsg.msg;
          }
          if (data.segments_total && data.segments_done !== undefined) {
            if (barWrap) {
              barWrap.classList.remove('hidden');
              bar.style.width = `${Math.round((data.segments_done / data.segments_total) * 100)}%`;
            }
          }
          if (terminalStates.includes(data.status)) {
            _handleTerminal(data.status, data.error);
          } else if (data.status) {
            const _st = _RUN_STATUS_MAP[data.status];
            if (_st) statusDiv.textContent = _st;
          }
        }
      } catch {}
    };
    es.onerror = () => {
      es.close(); _ipTaskSSE = null;
      _stopIPSegmentPoll();
      btn.disabled = false;
      _updateIPGenButton();
    };
  }

  let _ipLastLogMsg = '';
  function _ipVideoAppendLog(msg) {
    const logEl = $('#ip-progress-log');
    if (!logEl || !msg) return;
    if (msg === _ipLastLogMsg) return;
    _ipLastLogMsg = msg;
    const now = new Date();
    const ts = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}`;
    _ipVideoProgressLines.push(`[${ts}] ${msg}`);
    if (_ipVideoProgressLines.length > 200) _ipVideoProgressLines = _ipVideoProgressLines.slice(-150);
    logEl.innerHTML = _ipVideoProgressLines.map(l => `<div>${escapeHtml(l)}</div>`).join('');
    logEl.scrollTop = logEl.scrollHeight;
  }

  function _startIPSegmentPoll(taskId) {
    _stopIPSegmentPoll();
    _ipVideoSegmentPollTimer = setInterval(async () => {
      try {
        const st = await api(`/api/task/${taskId}`);
        _renderIPSegments(st.segments || []);
        if (st.output_url) _showIPOutputVideo(st.output_url);
        if (st.segments_total && st.segments_done !== undefined) {
          const bar = $('#ip-gen-progress-bar');
          const barWrap = $('#ip-gen-progress-wrap');
          if (barWrap) { barWrap.classList.remove('hidden'); bar.style.width = `${Math.round((st.segments_done / st.segments_total) * 100)}%`; }
        }
      } catch {}
    }, 8000);
  }
  function _stopIPSegmentPoll() {
    if (_ipVideoSegmentPollTimer) { clearInterval(_ipVideoSegmentPollTimer); _ipVideoSegmentPollTimer = null; }
  }

  function _renderIPSegments(segments) {
    const grid = $('#ip-segments-grid');
    const section = $('#ip-segments-section');
    if (!grid || !section) return;
    if (!segments || !segments.length) { section.classList.add('hidden'); return; }
    section.classList.remove('hidden');
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

  function _showIPOutputVideo(url) {
    const wrap = $('#ip-output-wrap');
    const vid = $('#ip-output-video');
    const dl = $('#ip-download-link');
    if (!wrap || !vid) return;
    wrap.classList.remove('hidden');
    if (vid.src !== url) vid.src = url;
    if (dl) dl.href = url;
  }

  async function _refreshIPSegmentsAndOutput(taskId) {
    try {
      const st = await api(`/api/task/${taskId}`);
      _renderIPSegments(st.segments || []);
      if (st.output_url) _showIPOutputVideo(st.output_url);
    } catch {}
  }

  async function _loadIPVideoRunDetail(run, runIdx) {
    const logWrap = $('#ip-progress-log-wrap');
    const logEl = $('#ip-progress-log');
    if (logWrap) logWrap.classList.remove('hidden');
    _ipVideoProgressLines = [`⏳ 正在加载 #${runIdx + 1} 的详情…`];
    if (logEl) logEl.innerHTML = `<div>${escapeHtml(_ipVideoProgressLines[0])}</div>`;

    try {
      const st = await api(`/api/task/${run.task_id}`);

      _ipVideoProgressLines = [];
      const progress = st.progress || [];
      progress.forEach(p => {
        const t = p.t ? p.t.replace(/.*T/, '').replace(/\.\d+.*/, '') : '';
        _ipVideoProgressLines.push(`[${t}] ${p.msg}`);
      });
      if (st.error) _ipVideoProgressLines.push(`❌ 错误: ${st.error}`);
      if (!progress.length && !st.error) _ipVideoProgressLines.push('（无进度记录）');

      if (logEl) {
        logEl.innerHTML = _ipVideoProgressLines.map(l => `<div>${escapeHtml(l)}</div>`).join('');
        logEl.scrollTop = logEl.scrollHeight;
      }

      _renderIPSegments(st.segments || []);
      if (st.output_url) {
        _showIPOutputVideo(st.output_url);
      } else {
        const outWrap = $('#ip-output-wrap');
        if (outWrap) outWrap.classList.add('hidden');
      }

      // 更新进度条
      const barWrap = $('#ip-gen-progress-wrap');
      const bar = $('#ip-gen-progress-bar');
      if (barWrap && st.segments_total) {
        barWrap.classList.remove('hidden');
        const pct = st.status === 'done' ? 100 : Math.round(((st.segments_done || 0) / st.segments_total) * 100);
        bar.style.width = `${pct}%`;
      } else if (barWrap) {
        barWrap.classList.add('hidden');
      }
    } catch (e) {
      _ipVideoProgressLines = [`❌ 加载失败: ${e.message}`];
      if (logEl) logEl.innerHTML = `<div>${escapeHtml(_ipVideoProgressLines[0])}</div>`;
    }
  }

  async function _persistIPVideoTaskIds() {
    if (!currentIPId || !_ipVideoRuns.length) return;
    const ids = _ipVideoRuns.map(r => r.task_id);
    try {
      await api(`/api/ip/${currentIPId}/video-tasks`, { method: 'PUT', json: true, body: { task_ids: ids } });
    } catch {}
  }

  function _updateIPGenButton() {
    const btn = $('#btn-ip-gen-video');
    if (!btn) return;
    btn.textContent = '开始生成';
  }

  async function _resumeIPVideoRun(runIdx) {
    const run = _ipVideoRuns[runIdx];
    if (!run) return;
    const btn = $('#btn-ip-gen-video');
    if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
    const statusDiv = $('#ip-video-status');
    if (statusDiv) { statusDiv.classList.remove('hidden'); statusDiv.textContent = `#${runIdx + 1} 续跑中…`; }

    _ipVideoProgressLines = [];
    const logWrap = $('#ip-progress-log-wrap');
    if (logWrap) logWrap.classList.remove('hidden');
    const logEl = $('#ip-progress-log');
    if (logEl) logEl.innerHTML = '';
    const barWrap = $('#ip-gen-progress-wrap');
    const bar = $('#ip-gen-progress-bar');
    if (barWrap) { barWrap.classList.add('hidden'); bar.style.width = '0%'; }

    try {
      const voiceMode = ($('#ip-voice-mode') || {}).value || 'native';
      const resolution = ($('#ip-gen-resolution') || {}).value || '720*1280';
      const data = await api('/api/task/ip-theme', {
        method: 'POST', json: true,
        body: {
          ip_id: currentIPId,
          theme_hint: ($('#ip-theme-hint') || {}).value || '',
          min_shots: parseInt(($('#ip-min-shots') || {}).value || '8'),
          max_shots: parseInt(($('#ip-max-shots') || {}).value || '16'),
          generate_video: true,
          story_outline: ipStoryOutline,
          voice_mode: voiceMode,
          resolution: resolution,
          resume_task_id: run.task_id,
        },
      });
      run.status = 'generating';
      run.output_url = '';
      _ipActiveVideoTaskId = data.task_id;
      _renderIPVideoRuns();
      _persistIPVideoTaskIds();
      if (statusDiv) statusDiv.textContent = `#${runIdx + 1} 正在续跑视频…`;

      _connectIPTaskSSE(data.task_id, statusDiv, btn, '开始生成', async () => {
        try {
          const st = await api(`/api/task/${data.task_id}`);
          run.status = st.status || 'done';
          run.output_url = st.output_url || '';
          run._has_segments = !!(st.segments && st.segments.length);
        } catch { run.status = 'done'; }
        _ipActiveVideoTaskId = null;
        _renderIPVideoRuns();
        _persistIPVideoTaskIds();
      }, (err) => {
        run.status = err === 'cancelled' ? 'cancelled' : 'failed';
        _ipActiveVideoTaskId = null;
        _renderIPVideoRuns();
        _persistIPVideoTaskIds();
      }, { terminal: ['done', 'failed', 'cancelled'] });
    } catch (e) {
      showToast('续跑失败: ' + e.message);
      if (statusDiv) statusDiv.textContent = '续跑失败';
      if (btn) { btn.disabled = false; btn.textContent = '开始生成'; }
    }
  }

  async function _restoreIPVideoRuns(taskIds) {
    _ipVideoRuns = taskIds.map(tid => ({ task_id: tid, status: 'loading', output_url: '', created: '' }));
    _renderIPVideoRuns();
    const promises = taskIds.map(async (tid, i) => {
      try {
        const st = await api(`/api/task/${tid}`);
        let status = st.status || 'unknown';
        const hasSegments = (st.segments && st.segments.length > 0);
        if (['generating', 'generating_video', 'generating_storyboard', 'pending'].includes(status) && !st.is_running) {
          status = st.output_url ? 'done' : hasSegments ? 'cancelled' : 'failed';
        }
        _ipVideoRuns[i].status = status;
        _ipVideoRuns[i].output_url = st.output_url || '';
        _ipVideoRuns[i]._has_segments = hasSegments;
      } catch {
        _ipVideoRuns[i].status = 'unknown';
      }
    });
    await Promise.all(promises);
    _renderIPVideoRuns();

    // 自动加载最后一个有成片的记录的段/视频
    for (let i = _ipVideoRuns.length - 1; i >= 0; i--) {
      if (_ipVideoRuns[i].output_url) {
        await _refreshIPSegmentsAndOutput(_ipVideoRuns[i].task_id);
        break;
      }
      if (_ipVideoRuns[i].status === 'generating' || _ipVideoRuns[i].status === 'generating_video') {
        _startIPSegmentPoll(_ipVideoRuns[i].task_id);
        break;
      }
    }
  }

  function _calcShotCount() {
    const mins = parseFloat(($('#ip-target-min') || {}).value) || 0;
    const secs = parseFloat(($('#ip-target-sec') || {}).value) || 0;
    const totalSec = mins * 60 + secs;
    const avgDur = parseFloat(($('#ip-avg-shot-dur') || {}).value) || 2.5;
    const ideal = Math.round(totalSec / avgDur);
    const minS = Math.max(3, ideal - 2);
    const maxS = ideal + 2;
    if ($('#ip-min-shots')) $('#ip-min-shots').value = minS;
    if ($('#ip-max-shots')) $('#ip-max-shots').value = maxS;
    if ($('#ip-shot-calc')) $('#ip-shot-calc').textContent = `${ideal} 镜 (${minS}~${maxS})，总计约 ${totalSec}s`;
    return { minS, maxS, avgDur, totalSec };
  }
  ['ip-target-min', 'ip-target-sec', 'ip-avg-shot-dur'].forEach(id => {
    const el = $('#' + id);
    if (el) el.oninput = _calcShotCount;
  });
  _calcShotCount();

  if ($('#btn-ip-gen-storyboard')) {
    $('#btn-ip-gen-storyboard').onclick = async () => {
      if (!currentIPId) return;
      const btn = $('#btn-ip-gen-storyboard');
      const { avgDur, totalSec } = _calcShotCount();
      const minShots = parseInt(($('#ip-min-shots') || {}).value) || 8;
      const maxShots = parseInt(($('#ip-max-shots') || {}).value) || 16;
      btn.disabled = true; btn.textContent = '生成中…';
      const statusDiv = $('#ip-storyboard-status');
      statusDiv.classList.remove('hidden');
      statusDiv.textContent = '正在生成分镜，请稍候…';
      try {
        const data = await api('/api/task/ip-theme', {
          method: 'POST', json: true,
          body: {
            ip_id: currentIPId,
            theme_hint: ($('#ip-theme-hint') || {}).value || '',
            min_shots: minShots,
            max_shots: maxShots,
            generate_video: false,
            story_outline: ipStoryOutline,
            avg_shot_duration: avgDur,
            target_duration: totalSec,
            dialogue_mode: ($('#ip-dialogue-mode') || {}).value || 'normal',
          },
        });
        statusDiv.textContent = '正在生成分镜…';
        _ipLastStoryboardTaskId = data.task_id;
        // 收到 task_id 后立即写入大纲，避免 SSE 完成前刷新/切页导致丢失续跑 ID，从而生视频时新建任务、磁盘上尚无分镜
        if (ipStoryOutline) {
          ipStoryOutline._last_storyboard_task_id = data.task_id;
          api('/api/ip/' + currentIPId + '/outline', { method: 'PUT', json: true, body: { outline: ipStoryOutline } }).catch(() => {});
        }
        _connectIPTaskSSE(data.task_id, statusDiv, btn, '生成 IP 分镜', async () => {
          await _loadIPStoryboardPreview(data.task_id);
          if (ipStoryOutline) {
            ipStoryOutline._last_storyboard_task_id = data.task_id;
            api('/api/ip/' + currentIPId + '/outline', { method: 'PUT', json: true, body: { outline: ipStoryOutline } }).catch(() => {});
          }
          ipSetStep(5);
        });
      } catch (e) { showToast('分镜生成失败: ' + e.message); statusDiv.textContent = '分镜生成失败'; btn.disabled = false; btn.textContent = '生成 IP 分镜'; }
    };
  }

  // ── Step 5: 生成视频 ──
  if ($('#ip-voice-mode')) {
    const _updateVoiceHint = () => {
      const hint = $('#ip-voice-mode-hint');
      if (!hint) return;
      const v = $('#ip-voice-mode').value;
      if (v === 'native') hint.textContent = '万相直接配音，需角色上传参考音频（克隆模式）。预置音色在此模式下不生效。';
      else if (v === 'pipeline') {
        let voiceInfo = '';
        if (currentIPData && currentIPData.characters) {
          const parts = currentIPData.characters.map(c => {
            const vp = c.voice_profile || {};
            if (vp.mode === 'preset') return `${c.name}: 预置·${vp.preset_name || vp.preset_id}`;
            if (vp.mode === 'clone') return `${c.name}: 克隆音色`;
            return `${c.name}: 未设置`;
          });
          voiceInfo = ' 当前音色：' + parts.join('，');
        }
        hint.textContent = '先生成静音视频，再用 CosyVoice TTS 按对白配音。使用第2步为每个角色设置的音色。' + voiceInfo;
      } else hint.textContent = '生成纯画面视频，不包含任何语音。';
    };
    $('#ip-voice-mode').onchange = _updateVoiceHint;
    _updateVoiceHint();
  }

  if ($('#btn-ip-gen-video')) {
    $('#btn-ip-gen-video').onclick = async () => {
      if (!currentIPId) return;
      const btn = $('#btn-ip-gen-video');
      btn.disabled = true; btn.textContent = '生成中…';
      const statusDiv = $('#ip-video-status');
      statusDiv.classList.remove('hidden');
      statusDiv.textContent = '正在创建视频生成任务…';

      // 清空上一轮的日志/段预览/输出
      _ipVideoProgressLines = [];
      const logEl = $('#ip-progress-log');
      if (logEl) logEl.innerHTML = '';
      const barWrap = $('#ip-gen-progress-wrap');
      const bar = $('#ip-gen-progress-bar');
      if (barWrap) { barWrap.classList.add('hidden'); bar.style.width = '0%'; }
      const segSection = $('#ip-segments-section');
      if (segSection) segSection.classList.add('hidden');
      const outWrap = $('#ip-output-wrap');
      if (outWrap) outWrap.classList.add('hidden');

      try {
        const voiceMode = ($('#ip-voice-mode') || {}).value || 'native';
        const resolution = ($('#ip-gen-resolution') || {}).value || '720*1280';

        const reqBody = {
          ip_id: currentIPId,
          theme_hint: ($('#ip-theme-hint') || {}).value || '',
          min_shots: parseInt(($('#ip-min-shots') || {}).value || '8'),
          max_shots: parseInt(($('#ip-max-shots') || {}).value || '16'),
          generate_video: true,
          story_outline: ipStoryOutline,
          voice_mode: voiceMode,
          resolution: resolution,
        };
        const resumeSb = _ipLastStoryboardTaskId || (ipStoryOutline && ipStoryOutline._last_storyboard_task_id);
        if (resumeSb) reqBody.resume_task_id = resumeSb;
        const data = await api('/api/task/ip-theme', {
          method: 'POST', json: true,
          body: reqBody,
        });

        const activeRunIdx = _ipVideoRuns.length;
        _ipVideoRuns.push({ task_id: data.task_id, status: 'generating', output_url: '', created: new Date().toLocaleTimeString() });
        _ipActiveVideoTaskId = data.task_id;
        _renderIPVideoRuns();
        _persistIPVideoTaskIds();
        statusDiv.textContent = `#${activeRunIdx + 1} 正在生成视频…`;

        _connectIPTaskSSE(data.task_id, statusDiv, btn, '开始生成', async () => {
          try {
            const st = await api(`/api/task/${data.task_id}`);
            _ipVideoRuns[activeRunIdx].status = st.status || 'done';
            _ipVideoRuns[activeRunIdx].output_url = st.output_url || '';
            _ipVideoRuns[activeRunIdx]._has_segments = !!(st.segments && st.segments.length);
          } catch { _ipVideoRuns[activeRunIdx].status = 'done'; }
          _ipActiveVideoTaskId = null;
          _renderIPVideoRuns();
          _persistIPVideoTaskIds();
        }, (err) => {
          _ipVideoRuns[activeRunIdx].status = err === 'cancelled' ? 'cancelled' : 'failed';
          _ipActiveVideoTaskId = null;
          _renderIPVideoRuns();
          _persistIPVideoTaskIds();
        }, { terminal: ['done', 'failed', 'cancelled'] });
      } catch (e) {
        showToast('视频生成失败: ' + e.message);
        statusDiv.textContent = '任务创建失败';
        btn.disabled = false;
        _updateIPGenButton();
      }
    };
  }

  // ── IP 创建流程 ──
  async function loadStylePresets() {
    try {
      const categories = await api('/api/styles');
      const tabs = $('#ip-style-tabs');
      const grid = $('#ip-style-grid');
      tabs.innerHTML = '';
      grid.innerHTML = '';

      categories.forEach((cat, ci) => {
        const tabBtn = document.createElement('button');
        tabBtn.type = 'button';
        tabBtn.textContent = cat.category_zh;
        tabBtn.dataset.cat = cat.category;
        if (ci === 0) tabBtn.classList.add('active');
        tabs.appendChild(tabBtn);
      });

      function renderGrid(category) {
        grid.innerHTML = '';
        const cat = categories.find(c => c.category === category);
        if (!cat) return;
        cat.styles.forEach(s => {
          const card = document.createElement('div');
          card.style.cssText = 'padding:8px 12px;border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:12px;';
          card.innerHTML = `<strong>${escapeHtml(s.name_zh)}</strong><br/><span style="color:var(--muted);">${escapeHtml(s.description_zh)}</span>`;
          card.dataset.styleId = s.id;
          card.onclick = () => {
            grid.querySelectorAll('div').forEach(d => d.style.borderColor = 'var(--border)');
            card.style.borderColor = 'var(--primary)';
            card.style.background = 'var(--surface)';
            $('#ip-selected-style').value = s.id;
          };
          grid.appendChild(card);
        });
      }

      tabs.onclick = (e) => {
        const btn = e.target.closest('button');
        if (!btn) return;
        tabs.querySelectorAll('button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderGrid(btn.dataset.cat);
      };

      if (categories.length) renderGrid(categories[0].category);
    } catch (e) { showToast('加载风格预设失败: ' + e.message); }
  }

  if ($('#btn-create-ip')) {
    $('#btn-create-ip').onclick = () => {
      openDrawer('ip-create');
      $('#ip-create-step1').classList.remove('hidden');
      $('#ip-create-step2').classList.add('hidden');
      loadStylePresets();
    };
  }
  if ($('#btn-close-ip-create')) $('#btn-close-ip-create').onclick = () => closeDrawer('ip-create');
  if ($('#overlay-ip-create')) $('#overlay-ip-create').onclick = () => closeDrawer('ip-create');

  if ($('#btn-ip-gen-proposal')) {
    $('#btn-ip-gen-proposal').onclick = async () => {
      const seed = ($('#ip-seed-idea') || {}).value || '';
      if (!seed.trim()) { showToast('请输入种子创意'); return; }
      const styleId = ($('#ip-selected-style') || {}).value || '';
      $('#ip-creating-spinner').classList.remove('hidden');
      try {
        const data = await api('/api/ip/create', {
          method: 'POST', json: true,
          body: { seed_idea: seed, style_preset_id: styleId },
        });
        ipProposal = data.proposal;
        showIPProposal(ipProposal);
      } catch (e) { showToast('IP 提案生成失败: ' + e.message); }
      $('#ip-creating-spinner').classList.add('hidden');
    };
  }

  function showIPProposal(p) {
    $('#ip-create-step1').classList.add('hidden');
    $('#ip-create-step2').classList.remove('hidden');
    $('#ip-proposal-name').textContent = `${p.name || ''} (${p.name_en || ''})`;
    $('#ip-proposal-tagline').textContent = p.tagline || '';

    const vd = p.visual_dna || {};
    $('#ip-proposal-visual').innerHTML =
      `<b>风格:</b> ${escapeHtml(vd.style_keywords || '')}<br/>` +
      `<b>色调:</b> ${escapeHtml(vd.color_tone || '')}<br/>` +
      `<b>光线:</b> ${escapeHtml(vd.lighting_preference || '')}`;

    const sd = p.story_dna || {};
    $('#ip-proposal-story').innerHTML =
      `<b>类型:</b> ${escapeHtml(sd.genre || '')}<br/>` +
      `<b>叙事模式:</b> ${escapeHtml(sd.narrative_pattern || '')}<br/>` +
      `<b>节奏:</b> ${escapeHtml(sd.pacing || '')}<br/>` +
      `<b>情节钩子:</b> ${(sd.typical_plot_hooks || []).map(h => escapeHtml(h)).join('; ')}`;

    const wd = p.world_dna || {};
    $('#ip-proposal-world').innerHTML =
      `<b>主场景:</b> ${escapeHtml(wd.primary_setting || '')}<br/>` +
      `<b>常见地点:</b> ${(wd.recurring_locations || []).join(', ')}<br/>` +
      `<b>世界规则:</b> ${escapeHtml(wd.world_rules || '')}`;

    const charDiv = $('#ip-proposal-characters');
    charDiv.innerHTML = '';
    (p.characters || []).forEach(c => {
      const el = document.createElement('div');
      el.style.cssText = 'padding:8px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px;font-size:12px;';
      el.innerHTML =
        `<strong>${escapeHtml(c.name)} (${escapeHtml(c.name_en)})</strong> — ${escapeHtml(c.role)}<br/>` +
        `<span style="color:var(--muted);">${escapeHtml((c.visual_description || '').substring(0, 150))}…</span>`;
      charDiv.appendChild(el);
    });
  }

  if ($('#btn-ip-confirm')) {
    $('#btn-ip-confirm').onclick = async () => {
      if (!ipProposal) return;
      $('#ip-confirming-spinner').classList.remove('hidden');
      try {
        const data = await api('/api/ip/confirm', {
          method: 'POST', json: true,
          body: { proposal: ipProposal },
        });
        showToast('IP 创建成功！角色图将在下一步生成。', 'success');
        closeDrawer('ip-create');
        loadIPList();
        if (data.ip) {
          currentIPId = data.ip.id;
          currentIPData = data.ip;
          showIPDetail(data.ip);
          $('#ip-select').value = data.ip.id;
          ipSetStep(2);
        }
      } catch (e) { showToast('IP 确认失败: ' + e.message); }
      $('#ip-confirming-spinner').classList.add('hidden');
    };
  }

  if ($('#btn-ip-regen-proposal')) {
    $('#btn-ip-regen-proposal').onclick = () => {
      $('#ip-create-step1').classList.remove('hidden');
      $('#ip-create-step2').classList.add('hidden');
    };
  }
  if ($('#btn-ip-cancel-proposal')) {
    $('#btn-ip-cancel-proposal').onclick = () => closeDrawer('ip-create');
  }

  // ── IP 管理/编辑 ──
  function showIPManage(ip) {
    $('#ip-manage-title').textContent = `管理: ${ip.name}`;
    const content = $('#ip-manage-content');
    content.innerHTML = '';

    // Visual DNA editable
    const vd = ip.visual_dna || {};
    content.innerHTML += `
      <div style="margin-bottom:16px;">
        <label>视觉风格 (Visual DNA)</label>
        <div class="ip-setting-item" style="margin-bottom:4px;"><b>风格关键词</b>
          <input type="text" id="ip-edit-style-kw" value="${escapeAttr(vd.style_keywords || '')}" style="margin-top:4px;" />
        </div>
        <div class="ip-setting-item" style="margin-bottom:4px;"><b>色调</b>
          <input type="text" id="ip-edit-color-tone" value="${escapeAttr(vd.color_tone || '')}" style="margin-top:4px;" />
        </div>
        <div class="ip-setting-item"><b>光线偏好</b>
          <input type="text" id="ip-edit-lighting" value="${escapeAttr(vd.lighting_preference || '')}" style="margin-top:4px;" />
        </div>
      </div>
    `;

    // Story DNA editable
    const sd = ip.story_dna || {};
    content.innerHTML += `
      <div style="margin-bottom:16px;">
        <label>故事模式 (Story DNA)</label>
        <div class="ip-setting-item" style="margin-bottom:4px;"><b>类型</b>
          <input type="text" id="ip-edit-genre" value="${escapeAttr(sd.genre || '')}" style="margin-top:4px;" />
        </div>
        <div class="ip-setting-item" style="margin-bottom:4px;"><b>叙事模式</b>
          <textarea id="ip-edit-narrative" style="margin-top:4px;min-height:60px;">${escapeHtml(sd.narrative_pattern || '')}</textarea>
        </div>
        <div class="ip-setting-item"><b>情感基调</b>
          <input type="text" id="ip-edit-emotional-tone" value="${escapeAttr(sd.emotional_tone || '')}" style="margin-top:4px;" />
        </div>
      </div>
    `;

    // Characters (read-only overview with per-char edit)
    content.innerHTML += '<label>角色</label>';
    (ip.characters || []).forEach(c => {
      const div = document.createElement('div');
      div.style.cssText = 'padding:10px;border:1px solid var(--border);border-radius:6px;margin-bottom:8px;';
      div.innerHTML =
        `<strong>${escapeHtml(c.name)}</strong> (${escapeHtml(c.name_en)}) — ${c.role}` +
        `<div style="font-size:11px;color:var(--muted);margin-top:4px;">${escapeHtml((c.visual_description || '').substring(0, 200))}</div>`;
      content.appendChild(div);
    });

    // Save button
    const saveBtn = document.createElement('button');
    saveBtn.type = 'button'; saveBtn.className = 'primary';
    saveBtn.textContent = '保存修改';
    saveBtn.onclick = async () => {
      const updated = {
        visual_dna: {
          ...vd,
          style_keywords: ($('#ip-edit-style-kw') || {}).value || '',
          color_tone: ($('#ip-edit-color-tone') || {}).value || '',
          lighting_preference: ($('#ip-edit-lighting') || {}).value || '',
        },
        story_dna: {
          ...sd,
          genre: ($('#ip-edit-genre') || {}).value || '',
          narrative_pattern: ($('#ip-edit-narrative') || {}).value || '',
          emotional_tone: ($('#ip-edit-emotional-tone') || {}).value || '',
        },
      };
      try {
        await api(`/api/ip/${ip.id}`, { method: 'PUT', json: true, body: updated });
        showToast('IP 设定已保存', 'success');
        reloadCurrentIP();
      } catch (e) { showToast('保存失败: ' + e.message); }
    };
    content.appendChild(saveBtn);
  }

  if ($('#btn-manage-ip')) {
    $('#btn-manage-ip').onclick = async () => {
      if (!currentIPId) return;
      try {
        const ip = await api(`/api/ip/${currentIPId}`);
        showIPManage(ip);
        openDrawer('ip-manage');
      } catch (e) { showToast('加载 IP 详情失败: ' + e.message); }
    };
  }

  // Delete IP
  if ($('#btn-delete-ip')) {
    $('#btn-delete-ip').onclick = async () => {
      if (!currentIPId || !currentIPData) return;
      if (!confirm(`确定删除 IP "${currentIPData.name}"？此操作不可恢复。`)) return;
      try {
        await api(`/api/ip/${currentIPId}`, { method: 'DELETE' });
        showToast('IP 已删除', 'success');
        currentIPId = '';
        currentIPData = null;
        $('#ip-detail-panel').classList.add('hidden');
        ipSetStep(1);
        loadIPList();
      } catch (e) { showToast('删除失败: ' + e.message); }
    };
  }

  if ($('#btn-close-ip-manage')) $('#btn-close-ip-manage').onclick = () => closeDrawer('ip-manage');
  if ($('#overlay-ip-manage')) $('#overlay-ip-manage').onclick = () => closeDrawer('ip-manage');

  // ── 修改润色弹窗（通用） ──
  function showRefinePopup(section, currentContent, onResult, phase) {
    const existing = document.querySelector('.refine-popup-global');
    if (existing) existing.remove();
    const popup = document.createElement('div');
    popup.className = 'refine-popup-global';
    popup.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:200;width:min(500px,90vw);background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    popup.innerHTML = `
      <h4 style="margin:0 0 12px;">修改润色</h4>
      <textarea placeholder="输入修改意见…" style="min-height:80px;"></textarea>
      <div class="flex" style="margin-top:12px;justify-content:flex-end;">
        <button type="button" class="primary sm" id="refine-go">润色</button>
        <button type="button" class="ghost sm" id="refine-cancel">取消</button>
      </div>
    `;
    document.body.appendChild(popup);
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:199;';
    document.body.appendChild(overlay);
    const close = () => { popup.remove(); overlay.remove(); };
    overlay.onclick = close;
    popup.querySelector('#refine-cancel').onclick = close;
    popup.querySelector('#refine-go').onclick = async () => {
      const instruction = popup.querySelector('textarea').value.trim();
      if (!instruction) { showToast('请输入修改意见'); return; }
      const goBtn = popup.querySelector('#refine-go');
      goBtn.disabled = true; goBtn.textContent = '润色中…';
      try {
        const data = await api('/api/ip/' + currentIPId + '/refine', {
          method: 'POST', json: true,
          body: { section, instruction, current_content: currentContent },
        });
        close();
        if (onResult) onResult(data.result);
        _recordFeedback(phase || 'proposal', section, instruction, currentContent, data.result);
      } catch (e) { showToast('润色失败: ' + e.message); goBtn.disabled = false; goBtn.textContent = '润色'; }
    };
  }

  function _recordFeedback(phase, section, instruction, before, after) {
    if (!currentIPId) return;
    const beforeSnap = typeof before === 'object' ? JSON.stringify(before).slice(0, 500) : String(before).slice(0, 500);
    const afterSnap = typeof after === 'object' ? JSON.stringify(after).slice(0, 500) : String(after).slice(0, 500);
    api('/api/ip/' + currentIPId + '/feedback', {
      method: 'POST', json: true,
      body: { phase, section, instruction, before_snapshot: beforeSnap, after_snapshot: afterSnap, accepted: true },
    }).then(resp => {
      if (resp.auto_distilled) showToast('已记录反馈并更新创作指南', 'success');
      else showToast('已记录反馈', 'success');
    }).catch(() => {});
  }

  // ── 初始化 ───────────────────────────────────────────────────────────────────
  loadCurrentUser();
  loadConfigForm();
  renderStatusBar();
  // 初次打开自动新建任务
  createNewTask().catch(() => {});
})();
