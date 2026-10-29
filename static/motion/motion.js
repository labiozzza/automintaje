(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    project: null,
    storyboard: null,
    outputJobId: '',
    operation: null,
    eventSource: null,
    pollTimer: null,
    models: [],
    compareAfter: false,
    sourceObject: null,
  };

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));

  const formatTime = (milliseconds) => {
    const ms = Math.max(0, Number(milliseconds) || 0);
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    const millis = Math.floor(ms % 1000);
    return `${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}.${String(millis).padStart(3,'0')}`;
  };

  async function api(path, options = {}) {
    const response = await fetch(`/api/motion${path}`, {
      headers: options.body ? {'Content-Type':'application/json', ...(options.headers || {})} : (options.headers || {}),
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }

  function showError(message) {
    const box = $('motionError');
    box.textContent = message || '';
    box.hidden = !message;
  }

  function statusLabel(status) {
    return ({
      created:'Создан', analyzing:'Анализ', designing:'Проектирование', review:'Ждёт согласования',
      approved:'Утверждён', rendering:'Рендер', ready:'Готово', failed:'Ошибка',
      cancelled:'Отменён', interrupted:'Прерван перезапуском',
    })[status] || status || 'Нет проекта';
  }

  async function loadCapabilitiesAndModels() {
    try {
      const [caps, modelResponse] = await Promise.all([
        api('/capabilities'), api('/models'),
      ]);
      state.models = modelResponse.available_models || [];
      const select = $('motionModelSelect');
      const selected = state.project?.model_id || select.value;
      select.innerHTML = '<option value="">Выберите модель</option>' + state.models
        .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join('');
      if (selected && state.models.includes(selected)) select.value = selected;
      const openCode = $('motionOpenCodeStatus');
      const canRun = !!modelResponse.available && !!caps.opencode?.available;
      openCode.textContent = canRun ? `OpenCode ${caps.opencode?.version || 'доступен'} · подключена модель` : (modelResponse.error || 'Нет подключённых моделей OpenCode');
      openCode.dataset.state = canRun ? 'ready' : 'failed';
      if (!caps.renderer?.available) {
        $('motionOutputMeta').textContent = 'Нужны Node.js и установленный Remotion renderer';
      }
      updateButtons();
    } catch (error) {
      $('motionOpenCodeStatus').textContent = 'Проверка OpenCode не удалась';
      $('motionOpenCodeStatus').dataset.state = 'failed';
      showError(error.message);
    }
  }

  async function loadSourceJobs() {
    const select = $('motionSourceSelect');
    try {
      const data = await fetch('/api/montage/status').then((response) => response.json());
      const jobs = (data.jobs || []).filter((job) => job.has_result && ['done','completed'].includes(job.status));
      select.innerHTML = '<option value="">Выберите готовый монтаж…</option>' + jobs.map((job) =>
        `<option value="${escapeHtml(job.id)}">${escapeHtml(job.name || job.id)} · ${escapeHtml(job.id.slice(0,8))}</option>`
      ).join('');
      $('motionCreateProjectButton').disabled = !jobs.length;
    } catch (error) {
      select.innerHTML = '<option value="">Не удалось загрузить список монтажей</option>';
      showError(error.message);
    }
  }

  async function loadProjectList(preferredId = '') {
    const select = $('motionProjectSelect');
    try {
      const data = await api('/projects');
      const projects = data.projects || [];
      select.innerHTML = '<option value="">Выберите Motion-проект…</option>' + projects.map((project) =>
        `<option value="${escapeHtml(project.id)}">${escapeHtml(project.source_name || project.source_job_id)} · ${escapeHtml(statusLabel(project.status))}</option>`
      ).join('');
      const savedId = preferredId || localStorage.getItem('motionProjectId') || '';
      if (savedId && projects.some((project) => project.id === savedId)) {
        select.value = savedId;
        await loadProject(savedId);
      } else if (!state.project && projects.length) {
        select.value = projects[0].id;
        await loadProject(projects[0].id);
      }
    } catch (error) {
      showError(error.message);
    }
  }

  async function load() {
    await Promise.all([loadCapabilitiesAndModels(), loadSourceJobs()]);
    await loadProjectList();
  }

  async function loadProject(projectId) {
    if (!projectId) return;
    closeMonitor();
    try {
      state.project = await api(`/projects/${encodeURIComponent(projectId)}`);
      localStorage.setItem('motionProjectId', state.project.id);
      $('motionProjectStatus').textContent = statusLabel(state.project.status);
      $('motionProjectStatus').dataset.state = state.project.status;
      $('motionSourceMeta').textContent = `${state.project.source_name || state.project.source_job_id} · ${state.project.source_kind || 'source'}`;
      const meta = state.project.source_metadata || {};
      $('motionSourceDuration').textContent = `${formatTime(meta.duration_ms)} · ${meta.fps || ''} FPS · ${meta.width || '?'}×${meta.height || '?'}`;
      $('motionSourceVideo').src = `/api/motion/projects/${encodeURIComponent(projectId)}/source?t=${Date.now()}`;
      $('motionSourceVideo').hidden = false;
      state.compareAfter = false;
      $('motionCompareButton').textContent = 'До / после';
      if (state.project.model_id && state.models.includes(state.project.model_id)) $('motionModelSelect').value = state.project.model_id;
      $('motionStyleSelect').value = state.project.style || 'minimal';
      $('motionIntensitySelect').value = state.project.intensity || 'balanced';
      $('motionIntentInput').value = state.project.user_intent || '';
    state.storyboard = await api(`/projects/${encodeURIComponent(projectId)}/storyboard`);
    renderProject();
      await loadProjectOutputs();
      const active = state.project.active_operation;
      if (active && ['queued','running','cancelling'].includes(active.status)) monitor(active);
      updateButtons();
      showError('');
    } catch (error) {
      showError(error.message);
    }
  }

  async function loadProjectOutputs() {
    if (!state.project) return;
    try {
      const data = await api(`/projects/${encodeURIComponent(state.project.id)}/outputs`);
      const outputs = data.outputs || [];
      const output = outputs[0];
      if (!output) {
        state.outputJobId = '';
        $('motionOutputCard').hidden = true;
        return;
      }
      state.outputJobId = output.output_job_id;
      $('motionOutputCard').hidden = false;
      $('motionOutputMeta').textContent = `Ревизия ${output.revision} · ${output.output_job_id}`;
      $('motionMirrorStatus').textContent = output.mirrored_ready
        ? 'Отражённая версия готова; публикация использует читаемые титры.'
        : (state.project.mirror_available
          ? (output.mirror_error || 'Отражённая версия ещё не подготовлена.')
          : (state.project.mirror_error || 'Для этого исходника нет закреплённого render-source для читаемого отражения.'));
      $('motionMirrorButton').disabled = !state.project.mirror_available || !!output.mirrored_ready || ['queued','running','cancelling'].includes(state.operation?.status);
      $('motionMirrorButton').textContent = output.mirrored_ready ? 'Отражённая версия готова' : 'Подготовить отражённую Motion-версию';
      const url = `/api/montage/preview/${encodeURIComponent(state.outputJobId)}?t=${Date.now()}`;
      $('motionFinalVideo').src = url;
      $('motionDownloadLink').href = `/api/montage/download/${encodeURIComponent(state.outputJobId)}`;
      $('motionPublishButton').onclick = () => {
        if (typeof window.openMotionPublishWindow === 'function') {
          window.openMotionPublishWindow(state.outputJobId);
        }
      };
    } catch (error) {
      showError(error.message);
    }
  }

  function renderProject() {
    if (!state.project) return;
    const plan = state.storyboard?.plan;
    const revision = Number(state.storyboard?.revision || state.project.current_revision || 0);
    $('motionRevisionLabel').textContent = revision ? `Ревизия ${revision} · ${plan?.scenes?.length || 0} сцен` : 'План ещё не разработан';
    const previewWrap = $('motionPreviewWrap');
    const previewVideo = $('motionOutputVideo');
    const previewUrl = state.storyboard?.preview_url || '';
    previewWrap.hidden = !previewUrl;
    if (previewUrl && previewVideo.dataset.url !== previewUrl) {
      previewVideo.dataset.url = previewUrl;
      previewVideo.src = `${previewUrl}?t=${Date.now()}`;
      previewVideo.load();
    }
    const frames = state.storyboard?.frames || [];
    const duration = Number(state.project.source_metadata?.duration_ms || 1);
    const timeline = $('motionTimeline');
    timeline.replaceChildren();
    (plan?.scenes || []).forEach((scene) => {
      const marker = document.createElement('button');
      marker.type = 'button';
      marker.className = 'motion-timeline-item';
      marker.dataset.status = scene.review_status || 'proposed';
      marker.title = `${formatTime(scene.start_ms)} · ${scene.semantic_goal || scene.effect_type}`;
      marker.style.flex = `${Math.max(1, scene.end_ms - scene.start_ms)} 1 0`;
      marker.onclick = () => document.getElementById(`motion-scene-${scene.scene_id}`)?.scrollIntoView({behavior:'smooth', block:'center'});
      timeline.appendChild(marker);
    });
    const list = $('motionSceneList');
    const scenes = plan?.scenes || [];
    const generatedArtifacts = state.project.generated_scene_artifacts || [];
    if (!scenes.length) {
      list.innerHTML = '<div class="motion-empty">Пока нет сцен. Выберите модель OpenCode и запустите проектирование.</div>';
    } else {
      list.innerHTML = scenes.map((scene) => {
        const frame = nearestFrame(frames, scene.evidence_time_ms ?? scene.start_ms);
        const image = frame ? `<img class="motion-scene-thumb" src="/api/motion/projects/${encodeURIComponent(state.project.id)}/frames/${encodeURIComponent(frame.file.split('/').pop())}" alt="Кадр ${escapeHtml(formatTime(frame.time_ms))}" loading="lazy">` : '';
        const artifact = generatedArtifacts
          .filter((item) => item.scene_id === scene.scene_id && Number(item.design_revision) === Number(plan.revision))
          .sort((a, b) => Number(b.scene_revision) - Number(a.scene_revision))[0];
        const generated = artifact ? `<div class="motion-generated-scene">
          <strong>Motion Engineer · ${escapeHtml(artifact.status || 'generated')}</strong>
          ${artifact.preview_url ? `<video controls preload="metadata" src="${escapeHtml(artifact.preview_url)}" aria-label="Реальный предпросмотр сгенерированной сцены"></video>` : ''}
          <div class="motion-scene-meta">QA: ${escapeHtml(artifact.qa_status || '—')} · Code SHA: ${escapeHtml(String(artifact.code_sha256 || '').slice(0,12))}</div>
          ${artifact.review_status === 'proposed' ? `<div class="motion-scene-actions">
            <button class="btn" data-artifact-action="accept" data-artifact-revision="${Number(artifact.scene_revision)}">Принять TSX-сцену</button>
            <button class="btn" data-artifact-action="reject" data-artifact-revision="${Number(artifact.scene_revision)}">Отклонить TSX</button>
          </div>` : `<div class="motion-scene-meta">Сцена: ${escapeHtml(artifact.review_status || '')}</div>`}
        </div>` : '';
        return `<article class="motion-scene" id="motion-scene-${escapeHtml(scene.scene_id)}" data-status="${escapeHtml(scene.review_status)}">
          <div class="motion-scene-main">
            <div class="motion-scene-time">${escapeHtml(formatTime(scene.start_ms))} — ${escapeHtml(formatTime(scene.end_ms))} · ${escapeHtml(scene.effect_type)}</div>
            <div class="motion-scene-title">${escapeHtml(scene.semantic_goal)}</div>
            <div class="motion-scene-evidence">${escapeHtml(scene.spoken_evidence || 'Без цитаты')}</div>
            <div class="motion-scene-meta">${escapeHtml(scene.explanation || '')} · ${escapeHtml(scene.review_status)}</div>
            ${image}
          </div>
          <div class="motion-scene-actions">
            <button class="btn" data-action="accept">Принять</button>
            <button class="btn" data-action="reject">Отклонить</button>
            <button class="btn" data-action="disable">Отключить</button>
            <button class="btn" data-action="revise">Изменить</button>
            <button class="btn motion-engineer-button" data-action="generate-tsx">Motion Engineer: создать оригинальную сцену</button>
          </div>
          ${generated}
        </article>`;
      }).join('');
      list.querySelectorAll('.motion-scene').forEach((card) => {
        const sceneId = card.id.replace('motion-scene-', '');
        card.querySelectorAll('[data-action]').forEach((button) => {
          button.onclick = () => sceneAction(sceneId, button.dataset.action);
        });
        const engineerButton = card.querySelector('[data-action="generate-tsx"]');
        const activeOperation = !!state.operation && ['queued','running','cancelling'].includes(state.operation.status);
        if (engineerButton) {
          engineerButton.disabled = activeOperation || !state.project.approved_revision
            || Number(state.project.approved_revision) !== Number(plan.revision)
            || !$("motionModelSelect").value
            || !!artifact && artifact.review_status === 'accepted';
          if (artifact && artifact.status !== 'ready_for_review') engineerButton.textContent = 'Motion Engineer: переделать сцену';
        }
        card.querySelectorAll('[data-artifact-action]').forEach((button) => {
          button.onclick = () => reviewGeneratedScene(sceneId, Number(button.dataset.artifactRevision), button.dataset.artifactAction);
        });
      });
    }
    renderHistory(state.project.agent_messages || []);
    updateButtons();
  }

  function nearestFrame(frames, atMs) {
    if (!frames.length) return null;
    return frames.reduce((best, frame) => Math.abs(frame.time_ms - atMs) < Math.abs(best.time_ms - atMs) ? frame : best, frames[0]);
  }

  function renderHistory(messages) {
    const history = $('motionHistory');
    history.innerHTML = (messages || []).slice(-20).map((item) =>
      `<div class="motion-message" data-role="${escapeHtml(item.role)}"><strong>${item.role === 'user' ? 'Вы' : 'Motion Director'}</strong>${item.model_id ? ` · ${escapeHtml(item.model_id)}` : ''}${Number.isFinite(Number(item.cost)) ? ` · cost ${Number(item.cost).toFixed(4)}` : ''}<br>${escapeHtml(item.text)}</div>`
    ).join('');
    history.scrollTop = history.scrollHeight;
  }

  async function sceneAction(sceneId, action) {
    if (!state.project || !state.storyboard?.revision) return;
    let feedback = '';
    if (action === 'generate-tsx') {
      try {
        const operation = await api(`/projects/${encodeURIComponent(state.project.id)}/scenes/${encodeURIComponent(sceneId)}/generate`, {
          method:'POST', body:JSON.stringify({
            model:$('motionModelSelect').value,
            expected_revision:Number(state.project.approved_revision),
            request_id:crypto.randomUUID(),
            comment:$('motionMessageInput').value || '',
          }),
        });
        monitor(operation);
      } catch (error) { showError(error.message); }
      return;
    }
    if (action === 'revise') {
      feedback = window.prompt('Что изменить в этой сцене?') || '';
      if (!feedback.trim()) return;
      $('motionMessageInput').value = `Измени сцену ${sceneId}: ${feedback}`;
      return sendMessage();
    }
    try {
      state.storyboard = await api(`/projects/${encodeURIComponent(state.project.id)}/scenes/${encodeURIComponent(sceneId)}`, {
        method:'PATCH', body:JSON.stringify({action, expected_revision:state.storyboard.revision, feedback}),
      });
      state.project = await api(`/projects/${encodeURIComponent(state.project.id)}`);
      renderProject();
      showError('');
    } catch (error) { showError(error.message); }
  }

  async function reviewGeneratedScene(sceneId, sceneRevision, decision) {
    if (!state.project || !state.project.approved_revision) return;
    try {
      await api(`/projects/${encodeURIComponent(state.project.id)}/scenes/${encodeURIComponent(sceneId)}/artifacts/review`, {
        method:'POST', body:JSON.stringify({
          expected_revision:Number(state.project.approved_revision),
          scene_revision:sceneRevision,
          decision:decision === 'accept' ? 'accept' : 'reject',
          request_id:crypto.randomUUID(),
        }),
      });
      await loadProject(state.project.id);
      showError('');
    } catch (error) { showError(error.message); }
  }

  async function createProject(sourceJobId) {
    const jobId = sourceJobId || $('motionSourceSelect').value;
    if (!jobId) { showError('Выберите готовый монтаж.'); return; }
    try {
      const project = await api('/projects', {
        method:'POST', body:JSON.stringify({
          source_job_id:jobId, source_kind:'prefer_master',
          user_intent:$('motionIntentInput').value,
          style:$('motionStyleSelect').value,
          intensity:$('motionIntensitySelect').value,
        }),
      });
      await loadProjectList(project.id);
      await loadProject(project.id);
      window.switchView('motion');
      showError('');
    } catch (error) { showError(error.message); }
  }

  async function openFromMontage() {
    const jobId = typeof window.getActiveMontageJobId === 'function' ? window.getActiveMontageJobId() : '';
    if (!jobId) { showError('Сначала завершите автомонтаж.'); return; }
    try {
      const list = await api(`/projects?source_job_id=${encodeURIComponent(jobId)}`);
      const existing = (list.projects || [])[0];
      if (existing) {
        await loadProjectList(existing.id);
        await loadProject(existing.id);
      } else {
        await createProject(jobId);
      }
      window.switchView('motion');
    } catch (error) { showError(error.message); window.switchView('motion'); }
  }

  function updateButtons() {
    const active = !!state.operation && ['queued','running','cancelling'].includes(state.operation.status);
    const hasProject = !!state.project;
    $('motionDesignButton').disabled = !hasProject || active || !$('motionModelSelect').value;
    $('motionCancelButton').hidden = !active;
    $('motionCancelButton').disabled = state.operation?.status === 'cancelling';
    $('motionMessageInput').disabled = !hasProject || active || !state.project?.current_revision;
    $('motionSendMessageButton').disabled = !hasProject || active || !state.project?.current_revision || !$('motionModelSelect').value;
    $('motionApproveButton').disabled = !state.storyboard?.plan || active || state.project?.approved_revision === state.storyboard?.revision;
    $('motionRenderButton').disabled = !state.project?.approved_revision || active;
    $('motionMirrorButton').disabled = !state.project?.mirror_available || !state.outputJobId || active;
    const hasAccepted = (state.storyboard?.plan?.scenes || []).some((scene) => scene.review_status === 'accepted' && scene.effect_type !== 'none');
    $('motionPreviewButton').disabled = !state.storyboard?.revision || active || !hasAccepted;
  }

  function renderOperation(operation) {
    state.operation = operation;
    const progress = $('motionProgress');
    progress.hidden = !operation || ['completed','failed','cancelled','interrupted'].includes(operation.status);
    $('motionProgressBar').value = Number(operation?.progress || 0);
    $('motionProgressLabel').textContent = operation ? `${operation.detail || operation.stage} · ${operation.progress == null ? '…' : `${Math.round(operation.progress)}%`}` : '';
    if (operation?.status === 'failed') showError(operation.error || operation.detail);
    updateButtons();
  }

  function closeMonitor() {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = null;
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function monitor(operation) {
    closeMonitor();
    renderOperation(operation);
    const endpoint = `/api/motion/projects/${encodeURIComponent(state.project.id)}/events?operation_id=${encodeURIComponent(operation.id)}`;
    if ('EventSource' in window) {
      state.eventSource = new EventSource(endpoint);
      state.eventSource.addEventListener('motion', async (event) => {
        try {
          const current = JSON.parse(event.data);
          renderOperation(current);
          if (['completed','failed','cancelled','interrupted'].includes(current.status)) {
            closeMonitor();
            await loadProject(state.project.id);
          }
        } catch (error) { showError(error.message); }
      });
      state.eventSource.onerror = () => {
        if (!state.pollTimer) state.pollTimer = setInterval(() => pollOperation(operation.id), 1500);
      };
    } else {
      state.pollTimer = setInterval(() => pollOperation(operation.id), 1500);
    }
  }

  async function pollOperation(operationId) {
    try {
      const operation = await api(`/projects/${encodeURIComponent(state.project.id)}/operations/${encodeURIComponent(operationId)}`);
      renderOperation(operation);
      if (['completed','failed','cancelled','interrupted'].includes(operation.status)) {
        closeMonitor();
        await loadProject(state.project.id);
      }
    } catch (error) { showError(error.message); }
  }

  async function design() {
    if (!state.project || !$('motionModelSelect').value) return;
    try {
      const operation = await api(`/projects/${encodeURIComponent(state.project.id)}/design`, {
        method:'POST', body:JSON.stringify({
          model:$('motionModelSelect').value,
          instruction:$('motionIntentInput').value,
          request_id:crypto.randomUUID(), max_agent_calls:3,
        }),
      });
      monitor(operation);
    } catch (error) { showError(error.message); }
  }

  async function sendMessage() {
    if (!state.project || !$('motionModelSelect').value) return;
    const instruction = $('motionMessageInput').value.trim();
    if (!instruction) return;
    $('motionSendMessageButton').disabled = true;
    try {
      const operation = await api(`/projects/${encodeURIComponent(state.project.id)}/messages`, {
        method:'POST', body:JSON.stringify({
          model:$('motionModelSelect').value, instruction,
          expected_revision:state.project.current_revision, request_id:crypto.randomUUID(),
        }),
      });
      $('motionMessageInput').value = '';
      monitor(operation);
    } catch (error) { showError(error.message); }
    finally { updateButtons(); }
  }

  async function approve() {
    if (!state.project || !state.storyboard?.revision) return;
    try {
      state.storyboard = await api(`/projects/${encodeURIComponent(state.project.id)}/approve`, {
        method:'POST', body:JSON.stringify({expected_revision:state.storyboard.revision}),
      });
      state.project = await api(`/projects/${encodeURIComponent(state.project.id)}`);
      renderProject();
      showError('');
    } catch (error) { showError(error.message); }
  }

  async function renderPreview() {
    if (!state.project || !state.storyboard?.revision) return;
    try {
      const operation = await api(`/projects/${encodeURIComponent(state.project.id)}/preview`, {
        method:'POST', body:JSON.stringify({expected_revision:state.storyboard.revision, max_duration_ms:15000}),
      });
      monitor(operation);
    } catch (error) { showError(error.message); }
  }

  async function renderFinal() {
    if (!state.project || !state.project.approved_revision) return;
    try {
      const operation = await api(`/projects/${encodeURIComponent(state.project.id)}/render`, {
        method:'POST', body:JSON.stringify({expected_revision:state.project.approved_revision, request_id:crypto.randomUUID()}),
      });
      monitor(operation);
    } catch (error) { showError(error.message); }
  }

  async function prepareMirror() {
    if (!state.project || !state.outputJobId) return;
    try {
      const operation = await api(`/projects/${encodeURIComponent(state.project.id)}/outputs/${encodeURIComponent(state.outputJobId)}/mirror`, {
        method:'POST', body:JSON.stringify({request_id:crypto.randomUUID()}),
      });
      monitor(operation);
    } catch (error) { showError(error.message); }
  }

  async function cancel() {
    if (!state.project) return;
    try { renderOperation(await api(`/projects/${encodeURIComponent(state.project.id)}/cancel`, {method:'POST'})); }
    catch (error) { showError(error.message); }
  }

  function toggleCompare() {
    if (!state.outputJobId) return;
    const video = $('motionSourceVideo');
    state.compareAfter = !state.compareAfter;
    video.src = state.compareAfter
      ? `/api/montage/preview/${encodeURIComponent(state.outputJobId)}?t=${Date.now()}`
      : `/api/motion/projects/${encodeURIComponent(state.project.id)}/source?t=${Date.now()}`;
    video.load();
    $('motionCompareButton').textContent = state.compareAfter ? 'После · вернуть исходник' : 'До / после';
  }

  function bind() {
    $('motionReloadButton').onclick = () => loadProjectList(state.project?.id || '');
    $('motionProjectSelect').onchange = (event) => loadProject(event.target.value);
    $('motionSourceSelect').onchange = () => { $('motionCreateProjectButton').disabled = !$('motionSourceSelect').value; };
    $('motionCreateProjectButton').onclick = () => createProject();
    $('motionModelSelect').onchange = updateButtons;
    $('motionDesignButton').onclick = design;
    $('motionSendMessageButton').onclick = sendMessage;
    $('motionApproveButton').onclick = approve;
    $('motionPreviewButton').onclick = renderPreview;
    $('motionRenderButton').onclick = renderFinal;
    $('motionMirrorButton').onclick = prepareMirror;
    $('motionCancelButton').onclick = cancel;
    $('motionCompareButton').onclick = toggleCompare;
  }

  bind();
  window.MotionUI = {load, openFromMontage, createProject, loadProject};
})();
