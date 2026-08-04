(() => {
  const $ = (id) => document.getElementById(id);
  const GAUGE_CIRC = 333.5;

  // ------------------------------------------------------------------
  // Generic helpers
  // ------------------------------------------------------------------
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let data;
    try { data = await res.json(); }
    catch (e) { data = { ok: false, error: `Bad response (HTTP ${res.status})` }; }
    if (!res.ok && data.ok === undefined) data.ok = false;
    return data;
  }
  async function getJSON(url) {
    const res = await fetch(url);
    try { return await res.json(); } catch (e) { return { ok: false, error: 'Bad response' }; }
  }
  function msg(elId, text, kind) {
    const el = $(elId);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'inline-msg' + (kind ? ' ' + kind : '');
  }
  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function toast(text, kind) {
    const host = $('toastHost');
    const el = document.createElement('div');
    el.className = 'toast-item' + (kind ? ' ' + kind : '');
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => el.remove(), 5000);
  }
  function setStatus(kind, text) {
    $('globalStatus').innerHTML = `<span class="dot dot-${kind}"></span>${text}`;
  }

  // ------------------------------------------------------------------
  // Nav / view switching
  // ------------------------------------------------------------------
  const VIEW_META = {
    rotation: { title: 'Auto-Rotation', subtitle: 'Classify page orientation & rotate upright' },
    crop: { title: 'Auto-Crop', subtitle: 'Batch black-border removal' },
    autofill: { title: 'Auto-Fill', subtitle: 'Fill black damage patches with white' },
    settings: { title: 'Automation & Settings', subtitle: 'Pipeline statuses, scheduler, models, Ollama fallback' },
    reports: { title: 'Reports', subtitle: 'Every run, persisted to disk' },
  };
  function showView(name) {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('hidden', v.id !== `view-${name}`));
    $('topbarTitle').textContent = VIEW_META[name].title;
    $('topbarSubtitle').textContent = VIEW_META[name].subtitle;
  }
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => showView(btn.dataset.view));
  });

  const KIND_TO_PREFIX = { rotation: 'rot', crop: 'crop', autofill: 'fill' };
  const PREFIX_TO_VIEW = { rot: 'rotation', crop: 'crop', fill: 'autofill' };
  const NAV_BADGE = { rot: 'navRotBadge', crop: 'navCropBadge', fill: 'navFillBadge' };

  // ------------------------------------------------------------------
  // Per-stage controller factory
  // ------------------------------------------------------------------
  function createStage(kind, prefix) {
    const id = (name) => `${prefix}-${name}`;
    const state = { source: 'database', batches: [], dbCreds: null, dbDatabase: null, jobId: null, pollTimer: null };

    // ---- source toggle ----
    document.querySelectorAll(`.source-toggle[data-scope="${prefix}"] .toggle-btn`).forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll(`.source-toggle[data-scope="${prefix}"] .toggle-btn`).forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.source = btn.dataset.source;
        $(id('source-database')).classList.toggle('hidden', state.source !== 'database');
        $(id('source-folder')).classList.toggle('hidden', state.source !== 'folder');
        $(id('updateStatusRow')).classList.toggle('hidden', state.source !== 'database');
      });
    });

    // ---- database source ----
    $(id('btnDbConnect')).addEventListener('click', async () => {
      const server = $(id('dbServer')).value.trim();
      const driver = $(id('dbDriver')).value.trim();
      const uid = $(id('dbUser')).value.trim();
      const pwd = $(id('dbPass')).value;
      if (!server || !driver || !uid) { msg(id('dbConnectMsg'), 'Server, driver and username are required.', 'err'); return; }

      msg(id('dbConnectMsg'), 'Connecting...', '');
      setStatus('busy', 'Connecting');
      const data = await postJSON('/api/db/test', { server, driver, uid, pwd });
      if (!data.ok) { msg(id('dbConnectMsg'), data.error || 'Connection failed.', 'err'); setStatus('err', 'Connection failed'); return; }

      state.dbCreds = { server, driver, uid, pwd };
      const sel = $(id('dbDatabase'));
      sel.innerHTML = '';
      data.databases.forEach(name => {
        const opt = document.createElement('option'); opt.value = name; opt.textContent = name; sel.appendChild(opt);
      });
      $(id('dbDatabaseRow')).classList.remove('hidden');
      msg(id('dbConnectMsg'), `Connected -- ${data.databases.length} database(s) found.`, 'ok');
      setStatus('ok', 'Connected');
      refreshDbPill();
    });

    // ---- status column and filter ----
    $(id('btnDbListBatches')).addEventListener('click', async () => {
      if (!state.dbCreds) return;
      const database = $(id('dbDatabase')).value;
      const status_column = $(id('dbStatusCol')).value;
      const status_filter = $(id('dbStatusFilter')).value.trim();
      const batch_id = ($(id('dbBatchId')) && $(id('dbBatchId')).value.trim()) || '';
      const batch_name = ($(id('dbBatchName')) && $(id('dbBatchName')).value.trim()) || '';
      state.dbDatabase = database;

      // Refresh the status autocomplete -- always unfiltered by other criteria.
      const distinct = await postJSON('/api/db/distinct', { ...state.dbCreds, database, status_column });
      if (distinct.ok) {
        const dl = $(id('statusList'));
        if (dl) {
          dl.innerHTML = '';
          distinct.values.forEach(v => {
            const opt = document.createElement('option');
            opt.value = v;
            dl.appendChild(opt);
          });
        }
      }

      msg(id('dbBatchesMsg'), 'Querying batchtable...', '');
      setStatus('busy', 'Listing batches');
      const data = await postJSON('/api/db/batches', {
        ...state.dbCreds,
        database,
        status_filter,
        status_column,
        batch_id,
        batch_name,
        stage: kind,   // lets the backend remember THIS stage's connection separately
      });
      if (!data.ok) { msg(id('dbBatchesMsg'), data.error || 'Query failed.', 'err'); setStatus('err', 'Query failed'); return; }

      loadBatches(data.batches);
      msg(id('dbBatchesMsg'), `${data.batches.length} batch(es) found.`, 'ok');
      setStatus('ok', `${data.batches.length} batch(es) loaded`);
      refreshDbPill();
      pollScheduler();
    });

    // ---- folder source ----
    $(id('btnFolderDiscover')).addEventListener('click', async () => {
      const parent_folder = $(id('folderPath')).value.trim();
      if (!parent_folder) { msg(id('folderDiscoverMsg'), 'Enter a parent folder path.', 'err'); return; }
      msg(id('folderDiscoverMsg'), 'Scanning folder...', '');
      setStatus('busy', 'Scanning folder');
      const data = await postJSON('/api/folder/discover', { parent_folder });
      if (!data.ok) { msg(id('folderDiscoverMsg'), data.error || 'Discovery failed.', 'err'); setStatus('err', 'Discovery failed'); return; }

      loadBatches(data.batches);
      msg(id('folderDiscoverMsg'), `${data.batches.length} batch folder(s) found.`, 'ok');
      setStatus('busy', 'Preparing QCCBackups folders');
      if (data.batches.length) {
        const prep = await postJSON('/api/folder/prepare', { batches: data.batches });
        if (prep.ok) applyPrepareResults(prep.results);
      }
      setStatus('ok', `${data.batches.length} batch(es) loaded`);
    });

    // ---- batch board ----
    function loadBatches(batches) {
      state.batches = batches.map(b => ({ ...b, _selected: true, _tag: '' }));
      renderBoard();
      $(id('panel-batches')).classList.remove('hidden');
      $(id('panel-actions')).classList.remove('hidden');
      $(id('panel-preview')).classList.add('hidden');
      $(id('panel-run')).classList.add('hidden');
      const badgeId = NAV_BADGE[prefix];
      if (badgeId && $(badgeId)) $(badgeId).textContent = batches.length;
      if (kind === 'rotation' && batches.length && $('rot-selectiveBatchDir')) {
        $('rot-selectiveBatchDir').value = batches[0].BatchDirectory || '';
      }
    }

    function renderBoard() {
      const board = $(id('batchBoard'));
      board.innerHTML = '';
      if (!state.batches.length) { board.innerHTML = '<div class="board-empty">No batches found.</div>'; return; }
      state.batches.forEach((b, idx) => {
        const row = document.createElement('div');
        row.className = 'board-row';
        const typeTag = b.DocumentType ? `<span class="src-tag">${escapeHtml(b.DocumentType)}</span>` : '';
        row.innerHTML = `
          <input type="checkbox" data-idx="${idx}" ${b._selected ? 'checked' : ''}>
          <div>
            <span class="board-name">${escapeHtml(b.BatchName)} ${typeTag}</span>
            <span class="board-dir">${escapeHtml(b.BatchDirectory || '')}</span>
          </div>
          <span class="board-count">${b.ImageCount ?? '?'} img</span>
          <span class="board-tag">${escapeHtml(b._tag || '')}</span>
        `;
        row.querySelector('input').addEventListener('change', (e) => { state.batches[idx]._selected = e.target.checked; });
        board.appendChild(row);
      });
    }

    function applyPrepareResults(results) {
      results.forEach(r => {
        const b = state.batches.find(x => x.BatchName === r.batch_name);
        if (b) b._tag = 'backup: ' + r.status.replace('skipped ', '').replace(/[()]/g, '');
      });
      renderBoard();
    }

    $(id('btnSelectAll')).addEventListener('click', () => { state.batches.forEach(b => b._selected = true); renderBoard(); });
    $(id('btnSelectNone')).addEventListener('click', () => { state.batches.forEach(b => b._selected = false); renderBoard(); });

    $(id('btnPrepareBackups')).addEventListener('click', async () => {
      const selected = getSelectedBatches();
      if (!selected.length) { msg(id('prepareMsg'), 'Select at least one batch.', 'err'); return; }
      msg(id('prepareMsg'), 'Creating QCCBackups folders...', '');
      const data = await postJSON('/api/folder/prepare', { batches: selected });
      if (!data.ok) { msg(id('prepareMsg'), data.error || 'Failed.', 'err'); return; }
      applyPrepareResults(data.results);
      msg(id('prepareMsg'), `Done -- ${data.results.length} batch(es) processed.`, 'ok');
    });

    function getSelectedBatches() { return state.batches.filter(b => b._selected); }

    // ---- extra params for each kind ----
    function extraParams() {
      if (kind === 'crop') return { threshold: parseInt($(id('threshold')).value, 10) || 100 };
      if (kind === 'autofill') return { threshold: parseInt($(id('threshold')).value, 10) || 60 };
      if (kind === 'rotation') {
        const deskew = $(id('deskew')).checked;
        return { deskew };
      }
      return {};
    }

    // ---- preview ----
    $(id('btnPreview')).addEventListener('click', async () => {
      const selected = getSelectedBatches();
      if (!selected.length) { toast('Select at least one batch first.', 'err'); return; }

      const sample_size = parseInt($(id('sampleSize')).value, 10) || 6;
      $(id('panel-preview')).classList.remove('hidden');
      const grid = $(id('previewGrid'));
      grid.innerHTML = '';
      msg(id('previewMeta'), 'Generating preview -- nothing is written to disk...', '');
      setStatus('busy', 'Generating preview');

      for (const b of selected) {
        const payload = { kind, batch_directory: b.BatchDirectory, sample_size, ...extraParams() };
        if (kind === 'rotation') {
          payload.document_type = $(id('docTypeOverride')).value.trim() || b.DocumentType || '';
          payload.deskew = $(id('deskew')).checked;
        }

        const data = await postJSON('/api/preview', payload);
        if (!data.ok) {
          const err = document.createElement('div');
          err.className = 'inline-msg err';
          err.style.gridColumn = '1 / -1';
          err.textContent = `${b.BatchName}: ${data.error}`;
          grid.appendChild(err);
          continue;
        }
        const header = document.createElement('div');
        header.style.gridColumn = '1 / -1';
        const profileNote = data.model_profile ? ` &mdash; model profile: <code>${escapeHtml(data.model_profile)}</code>` : '';
        header.innerHTML = `<strong style="font-family:var(--mono);font-size:12px;color:var(--txt)">${escapeHtml(b.BatchName)}</strong> <span class="inline-msg">-- ${data.sample_count} of ${data.total_images} image(s) sampled${profileNote}</span>`;
        grid.appendChild(header);
        data.previews.forEach(p => grid.appendChild(previewCard(p)));
      }

      msg(id('previewMeta'), `Preview complete for ${selected.length} batch(es).`, '');
      setStatus('ok', 'Preview ready');
    });

    // ---- preview card ----
    function previewCard(p) {
      const card = document.createElement('div');
      card.className = 'preview-card';
      if (p.status === 'error') {
        // Show error message
        card.innerHTML = `
          <div class="preview-meta">
            <span>${escapeHtml(p.filename)}</span>
            <span class="status-pill status-error">error</span>
          </div>
          <div style="padding:6px 11px; font-size:11px; color:var(--err); background:var(--s1); border-top:1px solid var(--brd);">
            ${escapeHtml(p.error || 'unknown error')}
          </div>
        `;
        return card;
      }
      const pillClass = (p.status === 'cropped' || p.status === 'rotated' || p.status === 'filled') ? 'status-cropped' : 'status-unchanged';
      let extra = '';
      if (kind === 'rotation') {
        extra = `<span>${p.angle}&deg; &middot; conf ${p.confidence} ${p.source === 'ollama' ? '<span class="src-tag">ollama</span>' : ''}</span>`;
      }
      card.innerHTML = `
        <div class="preview-imgs">
          <figure><img src="data:image/jpeg;base64,${p.before}"><figcaption>BEFORE</figcaption></figure>
          <figure><img src="data:image/jpeg;base64,${p.after}"><figcaption>AFTER</figcaption></figure>
        </div>
        <div class="preview-meta">
          <span>${escapeHtml(p.filename)}</span>
          ${extra}
          <span class="status-pill ${pillClass}">${p.status}</span>
        </div>`;
      return card;
    }

    // ---- run ----
    $(id('btnRun')).addEventListener('click', () => {
      const selected = getSelectedBatches();
      if (!selected.length) { toast('Select at least one batch first.', 'err'); return; }
      $('confirmCount').textContent = selected.length;
      const titleMap = {
        rotation: 'Confirm rotation run',
        crop: 'Confirm crop run',
        autofill: 'Confirm auto-fill run'
      };
      $('confirmTitle').textContent = titleMap[kind] || 'Confirm run';
      $('confirmModal').classList.remove('hidden');
      $('confirmModal').dataset.forStage = prefix;
    });

    async function startRun(trigger) {
      const selected = getSelectedBatches();
      const update_status = $(id('updateStatus')).checked && state.source === 'database';
      const payload = {
        kind,
        batches: selected.map(b => ({ BatchID: b.BatchID, BatchName: b.BatchName, BatchDirectory: b.BatchDirectory, DocumentType: b.DocumentType, _server_creds: b._server_creds })),
        update_status,
        ...extraParams(),
      };
      if (update_status && state.dbCreds && state.dbDatabase) payload.db_creds = { ...state.dbCreds, database: state.dbDatabase };

      $(id('panel-run')).classList.remove('hidden');
      $(id('resultsWrap')).classList.add('hidden');
      $(id('resultsBody')).innerHTML = '';
      $(id('logPanel')).innerHTML = '';
      $(id('btnRun')).disabled = true;
      setStatus('busy', 'Running');

      const data = await postJSON('/api/run/start', payload);
      if (!data.ok) {
        toast('Failed to start job: ' + (data.error || 'unknown error'), 'err');
        $(id('btnRun')).disabled = false;
        setStatus('err', 'Failed to start');
        return;
      }
      state.jobId = data.job_id;
      if (state.pollTimer) clearInterval(state.pollTimer);
      state.pollTimer = setInterval(pollJob, 900);
      pollJob();
    }

    $(id('btnCancelJob')).addEventListener('click', async () => {
      if (!state.jobId) return;
      await postJSON(`/api/run/cancel/${state.jobId}`, {});
    });

    // CSV download
    $(id('btnDownloadCSV')).addEventListener('click', () => {
      if (state.jobId) {
        window.open(`/api/run/csv/${state.jobId}`, '_blank');
      }
    });

    async function pollJob() {
      if (!state.jobId) return;
      const data = await getJSON(`/api/run/status/${state.jobId}`);
      if (!data.ok) return;

      const pctFiles = data.files_total_current ? data.files_done_current / data.files_total_current : 0;
      const overall = data.batches_total
        ? Math.min(1, (data.batches_done + (data.status === 'running' ? pctFiles : 0)) / data.batches_total) : 0;

      const offset = GAUGE_CIRC * (1 - overall);
      $(id('gaugeFill')).style.strokeDashoffset = offset.toFixed(1);
      $(id('gaugePct')).textContent = Math.round(overall * 100) + '%';
      $(id('gaugeSub')).textContent = `batch ${data.batches_done} / ${data.batches_total}`;

      $(id('runCurrentBatch')).textContent = data.current_batch || '\u2014';
      $(id('runCurrentFile')).textContent = data.current_file || '\u2014';
      $(id('runFilesProgress')).textContent = data.files_total_current ? `${data.files_done_current} / ${data.files_total_current}` : '\u2014';
      $(id('runTrigger')).textContent = data.trigger || '\u2014';
      $(id('runStatus')).textContent = data.status;

      const logPanel = $(id('logPanel'));
      logPanel.innerHTML = data.log.map(l => `<div class="${l.includes('ERROR') || l.includes('FAILED') ? 'err' : ''}">${escapeHtml(l)}</div>`).join('');
      logPanel.scrollTop = logPanel.scrollHeight;

      renderResultsTable(data.results);

      if (data.status !== 'running') {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        $(id('btnRun')).disabled = false;
        setStatus(data.status === 'done' ? 'ok' : (data.status === 'error' ? 'err' : 'idle'), data.status === 'done' ? 'Run complete' : data.status);
        if (data.status === 'done') {
          $(id('btnDownloadCSV')).style.display = 'inline-block';
        }
      }
    }

    function renderResultsTable(results) {
      if (!results || !results.length) return;
      $(id('resultsWrap')).classList.remove('hidden');
      const body = $(id('resultsBody'));
      body.innerHTML = '';
      results.forEach(r => {
        const tr = document.createElement('tr');
        if (r.status === 'error') {
          tr.innerHTML = `<td>${escapeHtml(r.batch_name)}</td><td colspan="6" style="color:var(--err)">${escapeHtml(r.error)}</td>`;
        } else if (kind === 'crop') {
          tr.innerHTML = `
            <td>${escapeHtml(r.batch_name)}</td><td>${r.total_images}</td>
            <td style="color:var(--ok)">${r.cropped_success}</td><td>${r.cropped_unchanged}</td>
            <td style="color:${r.cropped_failed ? 'var(--err)' : 'inherit'}">${r.cropped_failed}</td>
            <td>${r.moved_to_backup} new / ${r.already_backed_up} existing</td>
            <td>${r.elapsed_seconds != null ? r.elapsed_seconds + 's' : '\u2014'}</td>`;
        } else if (kind === 'autofill') {
          tr.innerHTML = `
            <td>${escapeHtml(r.batch_name)}</td><td>${r.total_images}</td>
            <td style="color:var(--ok)">${r.filled_success}</td><td>${r.filled_unchanged}</td>
            <td style="color:${r.filled_failed ? 'var(--err)' : 'inherit'}">${r.filled_failed}</td>
            <td>${r.moved_to_backup} new / ${r.already_backed_up} existing</td>
            <td>${r.elapsed_seconds != null ? r.elapsed_seconds + 's' : '\u2014'}</td>`;
        } else {
          tr.innerHTML = `
            <td>${escapeHtml(r.batch_name)}</td><td>${r.total_images}</td>
            <td style="color:var(--ok)">${r.rotated_success}</td><td>${r.rotated_unchanged}</td>
            <td style="color:${r.rotated_failed ? 'var(--err)' : 'inherit'}">${r.rotated_failed}</td>
            <td>${r.ollama_assisted || 0}</td>
            <td>${r.moved_to_backup} new / ${r.already_backed_up} existing</td>
            <td>${r.elapsed_seconds != null ? r.elapsed_seconds + 's' : '\u2014'}</td>`;
        }
        body.appendChild(tr);
      });
    }

    return { state, startRun, loadBatches, prefix };
  }

  const rotStage = createStage('rotation', 'rot');
  const cropStage = createStage('crop', 'crop');
  const fillStage = createStage('autofill', 'fill');
  const STAGES = { rot: rotStage, crop: cropStage, fill: fillStage };

  // shared confirm modal
  $('btnConfirmCancel').addEventListener('click', () => $('confirmModal').classList.add('hidden'));
  $('btnConfirmRun').addEventListener('click', () => {
    const p = $('confirmModal').dataset.forStage;
    $('confirmModal').classList.add('hidden');
    if (STAGES[p]) STAGES[p].startRun('manual');
  });

  // ------------------------------------------------------------------
  // Theme toggle
  // ------------------------------------------------------------------
  let currentTheme = 'dark';
  async function toggleTheme() {
    const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
    const data = await postJSON('/api/theme', { theme: newTheme });
    if (data.ok) {
      currentTheme = newTheme;
      document.getElementById('appShell').classList.toggle('light-theme', newTheme === 'light');
      $('themeToggle').textContent = newTheme === 'dark' ? '🌓' : '☀️';
    }
  }
  $('themeToggle').addEventListener('click', toggleTheme);

  // ------------------------------------------------------------------
  // Settings / Automation view
  // ------------------------------------------------------------------
  async function loadSettings() {
    const data = await getJSON('/api/settings');
    if (!data.ok) { toast('Failed to load settings.', 'err'); return; }
    const s = data.settings;

    // Pipeline
    $('set-rot-in').value = s.pipeline.rotation.in_status;
    $('set-rot-out').value = s.pipeline.rotation.out_status;
    $('set-rot-code').value = s.pipeline.rotation.out_code;
    $('set-rot-status-col').value = s.pipeline.rotation.status_column || 'StatusText';

    $('set-crop-in').value = s.pipeline.crop.in_status;
    $('set-crop-out').value = s.pipeline.crop.out_status;
    $('set-crop-code').value = s.pipeline.crop.out_code;
    $('set-crop-status-col').value = s.pipeline.crop.status_column || 'StatusText';

    $('set-fill-in').value = s.pipeline.autofill.in_status;
    $('set-fill-out').value = s.pipeline.autofill.out_status;
    $('set-fill-code').value = s.pipeline.autofill.out_code;
    $('set-fill-status-col').value = s.pipeline.autofill.status_column || 'StatusText';

    $('set-rotation-next').value = s.scheduler.rotation_next || 'none';

    // Scheduler
    $('set-rot-sched-enabled').checked = !!s.scheduler.rotation.enabled;
    $('set-rot-interval').value = s.scheduler.rotation.interval_minutes;
    $('set-rot-count').value = s.scheduler.rotation.batch_count_trigger;

    $('set-crop-sched-enabled').checked = !!s.scheduler.crop.enabled;
    $('set-crop-interval').value = s.scheduler.crop.interval_minutes;
    $('set-crop-count').value = s.scheduler.crop.batch_count_trigger;

    $('set-fill-sched-enabled').checked = !!s.scheduler.autofill.enabled;
    $('set-fill-interval').value = s.scheduler.autofill.interval_minutes;
    $('set-fill-count').value = s.scheduler.autofill.batch_count_trigger;

    // Ollama
    $('set-ollama-enabled').checked = !!s.ollama.enabled;
    $('set-ollama-url').value = s.ollama.base_url;
    $('set-ollama-model').value = s.ollama.model;
    $('set-ollama-trigger').value = s.ollama.trigger;
    $('set-ollama-threshold').value = s.ollama.confidence_threshold;
    $('set-ollama-timeout').value = s.ollama.timeout_seconds;

    // Selective/prompt-driven Ollama rotation (independent toggle)
    $('set-ollama-selective-enabled').checked = !!s.ollama.selective_enabled;
    $('set-ollama-selective-model').value = s.ollama.selective_model || 'qwen2.5vl';

    // prefill status filters
    $('rot-dbStatusFilter').value = s.pipeline.rotation.in_status;
    $('rot-dbStatusCol').value = s.pipeline.rotation.status_column || 'StatusText';
    $('crop-dbStatusFilter').value = s.pipeline.crop.in_status;
    $('crop-dbStatusCol').value = s.pipeline.crop.status_column || 'StatusText';
    $('fill-dbStatusFilter').value = s.pipeline.autofill.in_status;
    $('fill-dbStatusCol').value = s.pipeline.autofill.status_column || 'StatusText';

    // Theme
    if (s.theme === 'light') {
      currentTheme = 'light';
      document.getElementById('appShell').classList.add('light-theme');
      $('themeToggle').textContent = '☀️';
    } else {
      currentTheme = 'dark';
      document.getElementById('appShell').classList.remove('light-theme');
      $('themeToggle').textContent = '🌓';
    }

    renderProfiles(s.orientation_models || []);
  }

  // ---- pipeline save ----
  $('btnSavePipeline').addEventListener('click', async () => {
    const patch = {
      pipeline: {
        rotation: {
          in_status: $('set-rot-in').value.trim(),
          out_status: $('set-rot-out').value.trim(),
          out_code: parseInt($('set-rot-code').value, 10) || 0,
          status_column: $('set-rot-status-col').value,
        },
        crop: {
          in_status: $('set-crop-in').value.trim(),
          out_status: $('set-crop-out').value.trim(),
          out_code: parseInt($('set-crop-code').value, 10) || 0,
          status_column: $('set-crop-status-col').value,
        },
        autofill: {
          in_status: $('set-fill-in').value.trim(),
          out_status: $('set-fill-out').value.trim(),
          out_code: parseInt($('set-fill-code').value, 10) || 0,
          status_column: $('set-fill-status-col').value,
        }
      },
      scheduler: {
        rotation_next: $('set-rotation-next').value,
      },
    };
    const data = await postJSON('/api/settings', patch);
    if (!data.ok) { msg('pipelineSaveMsg', data.error || 'Failed to save.', 'err'); return; }
    msg('pipelineSaveMsg', 'Saved.', 'ok');
    // refresh filter fields
    $('rot-dbStatusFilter').value = data.settings.pipeline.rotation.in_status;
    $('rot-dbStatusCol').value = data.settings.pipeline.rotation.status_column || 'StatusText';
    $('crop-dbStatusFilter').value = data.settings.pipeline.crop.in_status;
    $('crop-dbStatusCol').value = data.settings.pipeline.crop.status_column || 'StatusText';
    $('fill-dbStatusFilter').value = data.settings.pipeline.autofill.in_status;
    $('fill-dbStatusCol').value = data.settings.pipeline.autofill.status_column || 'StatusText';
    toast('Pipeline settings saved.', 'ok');
  });

  // ---- scheduler save ----
  $('btnSaveScheduler').addEventListener('click', async () => {
    const patch = {
      scheduler: {
        rotation: {
          enabled: $('set-rot-sched-enabled').checked,
          interval_minutes: parseInt($('set-rot-interval').value, 10) || 0,
          batch_count_trigger: parseInt($('set-rot-count').value, 10) || 0,
        },
        crop: {
          enabled: $('set-crop-sched-enabled').checked,
          interval_minutes: parseInt($('set-crop-interval').value, 10) || 0,
          batch_count_trigger: parseInt($('set-crop-count').value, 10) || 0,
        },
        autofill: {
          enabled: $('set-fill-sched-enabled').checked,
          interval_minutes: parseInt($('set-fill-interval').value, 10) || 0,
          batch_count_trigger: parseInt($('set-fill-count').value, 10) || 0,
        }
      }
    };
    const data = await postJSON('/api/settings', patch);
    if (!data.ok) { msg('schedSaveMsg', data.error || 'Failed to save.', 'err'); return; }
    msg('schedSaveMsg', 'Saved.', 'ok');
    toast('Scheduler settings saved.', 'ok');
  });

  // ---- manual check buttons ----
  $('btnCheckRotNow').addEventListener('click', () => checkNow('rotation'));
  $('btnCheckCropNow').addEventListener('click', () => checkNow('crop'));
  $('btnCheckFillNow').addEventListener('click', () => checkNow('autofill'));

  async function checkNow(kind) {
    msg('schedStatusMsg', `Checking ${kind} batches...`, '');
    const data = await postJSON(`/api/scheduler/check/${kind}`, {});
    if (!data.ok) { msg('schedStatusMsg', data.error || 'Check failed.', 'err'); toast(data.error || 'Check failed.', 'err'); return; }
    if (!data.job_id) { msg('schedStatusMsg', data.message || 'Nothing to process.', ''); return; }
    msg('schedStatusMsg', `Started ${kind} run on ${data.found} batch(es).`, 'ok');
    toast(`Started ${kind} run on ${data.found} batch(es).`, 'ok');
    const prefix = KIND_TO_PREFIX[kind];
    const stage = STAGES[prefix];
    stage.state.jobId = data.job_id;
    showView(PREFIX_TO_VIEW[prefix]);
    $(`${prefix}-panel-run`).classList.remove('hidden');
    if (stage.state.pollTimer) clearInterval(stage.state.pollTimer);
    const poll = async () => {
      const st = await getJSON(`/api/run/status/${data.job_id}`);
      if (!st.ok) return;
      $(`${prefix}-runStatus`).textContent = st.status;
      $(`${prefix}-runCurrentBatch`).textContent = st.current_batch || '\u2014';
      $(`${prefix}-runTrigger`).textContent = st.trigger || '\u2014';
      const logPanel = $(`${prefix}-logPanel`);
      logPanel.innerHTML = st.log.map(l => `<div class="${l.includes('ERROR') || l.includes('FAILED') ? 'err' : ''}">${escapeHtml(l)}</div>`).join('');
      logPanel.scrollTop = logPanel.scrollHeight;
      if (st.status !== 'running') clearInterval(stage.state.pollTimer);
    };
    stage.state.pollTimer = setInterval(poll, 900);
    poll();
  }

  // ---- orientation model profiles ----
  let profileRowsData = [];

  function populateDocTypeOverride(profiles) {
  const sel = $('rot-docTypeOverride');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">Use batch\'s own DocumentType / default profile</option>';
  profiles.forEach(p => {
    if (!p.name && !p.match) return;
    const opt = document.createElement('option');
    opt.value = p.match || p.name;
    opt.textContent = p.match ? `${p.name} (${p.match})` : p.name;
    sel.appendChild(opt);
  });
  if ([...sel.options].some(o => o.value === current)) sel.value = current;
}

function renderProfiles(profiles) {
  profileRowsData = profiles.map(p => ({ ...p, model_paths: (p.model_paths || []) }));
  const host = $('profileRows');
  host.innerHTML = '';
  profileRowsData.forEach((p, idx) => host.appendChild(profileRow(p, idx)));
  populateDocTypeOverride(profileRowsData);   // <-- add this line
}
  function profileRow(p, idx) {
    const row = document.createElement('div');
    row.className = 'profile-row';
    row.innerHTML = `
      <label>Profile name<input type="text" data-f="name" value="${escapeHtml(p.name || '')}"></label>
      <label>Match (blank = default)<input type="text" data-f="match" value="${escapeHtml(p.match || '')}"></label>
      <label>Checkpoint path(s), comma-separated<input type="text" data-f="model_paths" value="${escapeHtml((p.model_paths || []).join(', '))}"></label>
      <button class="btn btn-danger btn-sm" type="button">Remove</button>
    `;
    row.querySelectorAll('input').forEach(inp => {
      inp.addEventListener('input', () => {
        const f = inp.dataset.f;
        if (f === 'model_paths') profileRowsData[idx][f] = inp.value.split(',').map(s => s.trim()).filter(Boolean);
        else profileRowsData[idx][f] = inp.value;
      });
    });
    row.querySelector('button').addEventListener('click', () => {
      profileRowsData.splice(idx, 1);
      renderProfiles(profileRowsData);
    });
    return row;
  }
  $('btnAddProfile').addEventListener('click', () => {
    profileRowsData.push({ name: '', match: '', model_paths: [] });
    renderProfiles(profileRowsData);
  });
  $('btnSaveProfiles').addEventListener('click', async () => {
    const data = await postJSON('/api/settings', { orientation_models: profileRowsData });
    if (!data.ok) { msg('profilesSaveMsg', data.error || 'Failed to save.', 'err'); return; }
    msg('profilesSaveMsg', 'Saved.', 'ok');
    toast('Orientation model profiles saved.', 'ok');
  });

  // ---- Ollama ----
  $('btnSaveOllama').addEventListener('click', async () => {
    const patch = {
      ollama: {
        enabled: $('set-ollama-enabled').checked,
        base_url: $('set-ollama-url').value.trim() || 'http://localhost:11434',
        model: $('set-ollama-model').value.trim() || 'llava',
        trigger: $('set-ollama-trigger').value,
        confidence_threshold: parseFloat($('set-ollama-threshold').value) || 0.55,
        timeout_seconds: parseInt($('set-ollama-timeout').value, 10) || 45,
      },
    };
    const data = await postJSON('/api/settings', patch);
    if (!data.ok) { msg('ollamaMsg', data.error || 'Failed to save.', 'err'); return; }
    msg('ollamaMsg', 'Saved.', 'ok');
    toast('Ollama settings saved.', 'ok');
  });
  $('btnTestOllama').addEventListener('click', async () => {
    msg('ollamaMsg', 'Testing connection...', '');
    const data = await postJSON('/api/ollama/test', { base_url: $('set-ollama-url').value.trim(), model: $('set-ollama-model').value.trim() });
    if (!data.ok) { msg('ollamaMsg', data.error || 'Unreachable.', 'err'); return; }
    msg('ollamaMsg', data.has_model ? 'Reachable -- model found.' : `Reachable, but model not in list (${data.models.join(', ') || 'none pulled'}).`, data.has_model ? 'ok' : 'err');
  });

  // ------------------------------------------------------------------
  // Sidebar status
  // ------------------------------------------------------------------
  function refreshDbPill() {
    const connected = rotStage.state.dbCreds || cropStage.state.dbCreds || fillStage.state.dbCreds;
    $('dbPill').innerHTML = connected
      ? `<span class="dot dot-ok"></span>DB session active`
      : `<span class="dot dot-idle"></span>No DB connection`;
  }

  async function pollScheduler() {
    const data = await getJSON('/api/scheduler/status');
    if (!data.ok) return;
    const anyBusy = data.active_jobs && (data.active_jobs.rotation || data.active_jobs.crop || data.active_jobs.autofill);
    $('schedPill').innerHTML = anyBusy
      ? `<span class="dot dot-busy"></span>Scheduler running a job`
      : (data.running ? `<span class="dot dot-ok"></span>Scheduler watching` : `<span class="dot dot-idle"></span>Scheduler idle`);

    // Per-stage DB status, now that each stage has its own connection.
    if (data.has_db_creds) {
      if ($('schedDbStatusRot')) $('schedDbStatusRot').textContent = data.has_db_creds.rotation ? 'connected' : 'not connected';
      if ($('schedDbStatusCrop')) $('schedDbStatusCrop').textContent = data.has_db_creds.crop ? 'connected' : 'not connected';
      if ($('schedDbStatusFill')) $('schedDbStatusFill').textContent = data.has_db_creds.autofill ? 'connected' : 'not connected';
    }
  }

  // ------------------------------------------------------------------
  // Selective/prompt-driven Ollama rotation
  // ------------------------------------------------------------------
  $('btnSaveOllamaSelective').addEventListener('click', async () => {
    const patch = {
      ollama: {
        selective_enabled: $('set-ollama-selective-enabled').checked,
        selective_model: $('set-ollama-selective-model').value.trim() || 'qwen2.5vl',
      },
    };
    const data = await postJSON('/api/settings', patch);
    if (!data.ok) { msg('ollamaSelectiveMsg', data.error || 'Failed to save.', 'err'); return; }
    msg('ollamaSelectiveMsg', 'Saved.', 'ok');
    toast('Selective rotation settings saved.', 'ok');
  });

  let lastSelectiveMatches = null;

  $('rot-btnSelectivePreview').addEventListener('click', async () => {
    const batch_directory = $('rot-selectiveBatchDir').value.trim();
    const prompt = $('rot-selectivePrompt').value.trim();
    if (!batch_directory) { msg('rot-selectiveMsg', 'Enter or select a batch directory first.', 'err'); return; }
    if (!prompt) { msg('rot-selectiveMsg', 'Enter a prompt, e.g. "identify all pages with a table".', 'err'); return; }

    msg('rot-selectiveMsg', 'Checking pages against the prompt (this can take a while)...', '');
    $('rot-btnSelectiveApply').disabled = true;
    lastSelectiveMatches = null;
    const data = await postJSON('/api/ollama/selective/preview', { batch_directory, prompt });
    if (!data.ok) { msg('rot-selectiveMsg', data.error || 'Preview failed.', 'err'); return; }

    lastSelectiveMatches = data.matched;
    msg('rot-selectiveMsg', `${data.matched.length} of ${data.total_checked} page(s) matched.`, 'ok');
    const grid = $('rot-selectiveResults');
    grid.innerHTML = '';
    data.matched.forEach(m => {
      const card = document.createElement('div');
      card.className = 'preview-card';
      card.innerHTML = `
        <div class="preview-imgs">
          <figure>${m.thumb_b64 ? `<img src="data:image/jpeg;base64,${m.thumb_b64}">` : ''}<figcaption>${escapeHtml(m.filename)}</figcaption></figure>
        </div>
        <div class="preview-meta">
          <span>rotate ${m.rotation_degrees_cw}&deg;</span>
          <span>${escapeHtml(m.reason || '')}</span>
        </div>`;
      grid.appendChild(card);
    });
    $('rot-btnSelectiveApply').disabled = data.matched.length === 0;
  });

  $('rot-btnSelectiveApply').addEventListener('click', async () => {
    const batch_directory = $('rot-selectiveBatchDir').value.trim();
    if (!batch_directory || !lastSelectiveMatches || !lastSelectiveMatches.length) return;
    msg('rot-selectiveMsg', 'Rotating matched pages...', '');
    const data = await postJSON('/api/ollama/selective/apply', { batch_directory, matched: lastSelectiveMatches });
    if (!data.ok) { msg('rot-selectiveMsg', data.error || 'Apply failed.', 'err'); return; }
    msg('rot-selectiveMsg', `Rotated ${data.rotated} page(s), ${data.skipped_zero_rotation} needed no rotation, ${data.failed} failed.`, data.failed ? 'err' : 'ok');
    toast(`Selective rotation done -- ${data.rotated} page(s) rotated.`, 'ok');
  });

  // ------------------------------------------------------------------
  // Reports tab
  // ------------------------------------------------------------------
  async function loadReports() {
    const stage = $('reportsStage').value;
    const data = await getJSON(`/api/reports${stage ? `?stage=${stage}` : ''}`);
    $('btnReportsCsv').href = `/api/reports/csv${stage ? `?stage=${stage}` : ''}`;
    if (!data.ok) { toast(data.error || 'Failed to load reports.', 'err'); return; }
    const tbody = $('reportsBody');
    if (!data.rows.length) { tbody.innerHTML = '<tr><td colspan="5">No runs recorded yet.</td></tr>'; return; }
    tbody.innerHTML = data.rows.map(r => `
      <tr>
        <td>${escapeHtml(r.kind)}</td>
        <td>${escapeHtml(r.batch_name || '')}</td>
        <td>${escapeHtml(r.status || '')}</td>
        <td>${escapeHtml(r.trigger || '')}</td>
        <td>${r.recorded_at ? new Date(r.recorded_at * 1000).toLocaleString() : '\u2014'}</td>
      </tr>
    `).join('');
  }
  $('btnReportsRefresh').addEventListener('click', loadReports);
  $('reportsStage').addEventListener('change', loadReports);
  document.querySelector('.nav-btn[data-view="reports"]').addEventListener('click', loadReports);

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------
  loadSettings();
  refreshDbPill();
  pollScheduler();
  setInterval(pollScheduler, 5000);
})();