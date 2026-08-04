(() => {
  const $ = (id) => document.getElementById(id);

  async function getJSON(url) {
    const res = await fetch(url);
    try { return await res.json(); } catch (e) { return { ok: false, error: 'Bad response' }; }
  }
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
  async function deleteJSON(url) {
    const res = await fetch(url, { method: 'DELETE' });
    try { return await res.json(); } catch (e) { return { ok: false, error: `Bad response (HTTP ${res.status})` }; }
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

  let models = [];

  async function loadModels() {
    const data = await getJSON('/api/models');
    if (!data.ok) { msg('addModelsSaveMsg', data.error || 'Failed to load models.', 'err'); return; }
    models = data.models || [];
    renderList();
  }

  function renderList() {
    const host = $('addModelsList');
    if (!host) return;
    host.innerHTML = '';
    if (!models.length) {
      host.innerHTML = '<div class="board-empty">No models registered yet.</div>';
      return;
    }
    models.forEach((m, idx) => host.appendChild(modelRow(m, idx)));
  }

  function modelRow(m, idx) {
    const row = document.createElement('div');
    row.className = 'profile-row';
    row.innerHTML = `
      <label>Profile name<input type="text" data-f="name" value="${escapeHtml(m.name || '')}"></label>
      <label>Match (blank = default)<input type="text" data-f="match" value="${escapeHtml(m.match || '')}"></label>
      <label>Checkpoint path(s), comma-separated<input type="text" data-f="model_paths" value="${escapeHtml((m.model_paths || []).join(', '))}"></label>
      <button class="btn btn-danger btn-sm" type="button">Remove</button>
    `;
    row.querySelectorAll('input').forEach(inp => {
      inp.addEventListener('input', () => {
        const f = inp.dataset.f;
        if (f === 'model_paths') models[idx][f] = inp.value.split(',').map(s => s.trim()).filter(Boolean);
        else models[idx][f] = inp.value;
      });
    });
    row.querySelector('button').addEventListener('click', async () => {
      const target = models[idx];
      if (target.id) {
        const data = await deleteJSON(`/api/models/${target.id}`);
        if (!data.ok) { msg('addModelsSaveMsg', data.error || 'Failed to delete.', 'err'); return; }
      }
      models.splice(idx, 1);
      renderList();
    });
    return row;
  }

  // ---- quick add (single name + path) ----
  $('btnAddModelQuick').addEventListener('click', async () => {
    const name = $('addmodel-name').value.trim();
    const path = $('addmodel-path').value.trim();
    if (!name || !path) { msg('addModelMsg', 'Model name and path are required.', 'err'); return; }

    const data = await postJSON('/api/models', { name, model_path: path });
    if (!data.ok) { msg('addModelMsg', data.error || 'Failed to add model.', 'err'); return; }

    msg('addModelMsg', 'Model added.', 'ok');
    $('addmodel-name').value = '';
    $('addmodel-path').value = '';
    models = data.models || [];
    renderList();
  });

  // ---- save all edited rows (bulk replace) ----
  $('btnSaveAddModels').addEventListener('click', async () => {
    const payload = models.map(m => ({
      id: m.id || null,
      name: m.name || '',
      match: m.match || '',
      model_paths: m.model_paths || [],
    }));
    const data = await postJSON('/api/models/bulk', { models: payload });
    if (!data.ok) { msg('addModelsSaveMsg', data.error || 'Failed to save.', 'err'); return; }
    models = data.models || [];
    renderList();
    msg('addModelsSaveMsg', 'Saved.', 'ok');
  });

  // exposed so app.js's nav handler can refresh this list when the
  // "Add Models" view is opened, keeping it in sync with Settings
  window.reloadAddModels = loadModels;

  loadModels();
})();