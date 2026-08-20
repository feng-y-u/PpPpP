const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

const FIELD_MAP = [
  'proxy', 'download_max_workers', 'fetch_detail_workers', 'medium_image_size',
  'per_page', 'search_pages', 'items_per_page',
  'max_bookmarks_default', 'auto_follow_interval', 'auto_follow_download',
  'prefetch_interval', 'prefetch_pages', 'prefetch_max_illusts',
];

async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    const data = await r.json();
    FIELD_MAP.forEach(key => {
      const el = $('#' + key);
      if (!el) return;
      if (el.type === 'checkbox') {
        el.checked = !!data[key];
      } else {
        el.value = data[key] != null ? data[key] : '';
      }
    });
  } catch { showToast('加载设置失败'); }
}

$('#saveBtn').addEventListener('click', async () => {
  const btn = $('#saveBtn');
  btn.disabled = true;
  btn.textContent = '保存中...';

  const body = {};
  FIELD_MAP.forEach(key => {
    const el = $('#' + key);
    if (!el) return;
    if (el.type === 'checkbox') {
      body[key] = el.checked;
    } else if (el.type === 'number') {
      body[key] = parseInt(el.value) || 0;
    } else {
      body[key] = el.value;
    }
  });

  const cookieVal = $('#cookie').value.trim();
  if (cookieVal) body.cookie = cookieVal;

  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (r.ok) {
      showToast('设置已保存（部分设置需重启后生效）');
      loadSettings();
    } else {
      showToast(data.error || '保存失败');
    }
  } catch { showToast('网络错误'); }
  finally {
    btn.disabled = false;
    btn.textContent = '保存设置';
  }
});

loadSettings();

// ── Prefetch Tag Management ──
let prefetchTags = [];

async function loadPrefetchTags() {
  try {
    const r = await fetch('/api/prefetch/tags');
    const data = await r.json();
    if (!Array.isArray(data)) { showToast('加载预取标签失败', true); return; }
    prefetchTags = data;
    renderPrefetchTags();
  } catch { showToast('加载预取标签失败', true); }
}

function renderPrefetchTags() {
  const list = $('#prefetchTagList');
  if (!list) return;
  if (!prefetchTags.length) {
    list.innerHTML = '<span style="color:var(--text-muted);font-size:0.8rem;">暂无预取标签</span>';
    return;
  }
  list.innerHTML = prefetchTags.map(t => {
    return `<span class="badge" style="background:var(--accent);font-size:0.78rem;cursor:pointer;display:inline-flex;align-items:center;gap:4px;" data-tag="${escAttr(t.tag)}" title="状态: ${escAttr(t.status)} · 条数: ${t.total || 0}">
      ${escHtml(t.tag)}
      <span style="opacity:.6;">&times;</span>
    </span>`;
  }).join('');
  list.querySelectorAll('[data-tag]').forEach(el => {
    el.addEventListener('click', () => removePrefetchTag(el.dataset.tag));
  });
}

async function addPrefetchTag() {
  const input = $('#prefetchTagInput');
  const btn = $('#addPrefetchTagBtn');
  const tag = (input.value || '').trim();
  if (!tag) return;
  btn.disabled = true;
  try {
    const resp = await fetch('/api/prefetch/tags', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify({tag}),
    });
    const err = await resp.json().catch(() => ({}));
    if (resp.ok) {
      showToast('已添加预取标签', false);
      input.value = '';
      loadPrefetchTags();
    } else {
      showToast(err.error || '添加失败', true);
    }
  } catch { showToast('网络错误', true); }
  finally { btn.disabled = false; }
}

async function removePrefetchTag(tag) {
  try {
    const resp = await fetch(`/api/prefetch/tags/${encodeURIComponent(tag)}`, {
      method: 'DELETE',
      headers: {'X-CSRF-Token': csrfToken},
    });
    if (resp.ok) {
      showToast('已删除', false);
      loadPrefetchTags();
    } else {
      const err = await resp.json().catch(() => ({}));
      showToast(err.error || '删除失败', true);
    }
  } catch { showToast('网络错误', true); }
}

$('#addPrefetchTagBtn').addEventListener('click', addPrefetchTag);
$('#prefetchTagInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') addPrefetchTag();
});

// 页面加载时拉取清单
loadPrefetchTags();

// ── Collection Management ──
let deleteCollectionId = null;
const deleteCollectionModal = new bootstrap.Modal($('#deleteCollectionModal'));

function loadCollections() {
  fetch('/api/collections')
    .then(r => r.json())
    .then(collections => {
      const list = $('#collectionList');
      if (collections.length === 0) {
        list.innerHTML = '<div class="text-muted py-2">暂无收藏夹</div>';
        return;
      }
      list.innerHTML = collections.map(c => `
        <div class="d-flex align-items-center gap-2 py-2 border-bottom border-subtle collection-item" data-id="${c.id}">
          <span class="fw-medium flex-fill small">${escHtml(c.name)}</span>
          <span class="text-muted" style="font-size:0.75rem;min-width:45px;text-align:right;">${c.item_count} 件</span>
          <button class="btn btn-soft btn-sm rename-btn">重命名</button>
          <button class="btn btn-outline-danger btn-sm delete-btn">删除</button>
        </div>
      `).join('');
    })
    .catch(() => showToast('加载收藏夹失败', true));
}

$('#createCollectionBtn').addEventListener('click', async function() {
  const name = $('#newCollectionName').value.trim();
  if (!name) return;
  this.disabled = true;
  try {
    const r = await fetch('/api/collections', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (r.ok) {
      $('#newCollectionName').value = '';
      loadCollections();
      showToast('收藏夹已创建');
    } else {
      const data = await r.json();
      showToast(data.error || '创建失败', true);
    }
  } catch { showToast('网络错误', true); }
  finally { this.disabled = false; }
});

$('#newCollectionName').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') $('#createCollectionBtn').click();
});

$('#collectionList').addEventListener('click', function(e) {
  const item = e.target.closest('.collection-item');
  if (!item) return;
  const cid = parseInt(item.dataset.id);

  if (e.target.classList.contains('delete-btn')) {
    const name = item.querySelector('.fw-medium').textContent;
    deleteCollectionId = cid;
    $('#deleteCollectionModalBody').textContent = `确定删除收藏夹「${name}」？作品本身不会被删除。`;
    deleteCollectionModal.show();
    return;
  }

  if (e.target.classList.contains('rename-btn')) {
    const nameEl = item.querySelector('.fw-medium');
    const currentName = nameEl.textContent;
    nameEl.innerHTML = `<input class="form-control form-control-sm" type="text" value="${escAttr(currentName)}" style="max-width:160px;">`;
    const input = nameEl.querySelector('input');
    input.focus();
    input.select();

    const done = async () => {
      const newName = input.value.trim();
      if (!newName || newName === currentName) { nameEl.textContent = currentName; return; }
      try {
        const r = await fetch(`/api/collections/${cid}`, {
          method: 'PUT',
          headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: newName }),
        });
        if (r.ok) { nameEl.textContent = newName; showToast('已重命名'); }
        else { const d = await r.json(); showToast(d.error || '重命名失败', true); nameEl.textContent = currentName; }
      } catch { showToast('网络错误', true); nameEl.textContent = currentName; }
    };
    input.addEventListener('blur', done);
    input.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
      if (ev.key === 'Escape') { nameEl.textContent = currentName; input.blur(); }
    });
    return;
  }
});

$('#confirmDeleteCollectionBtn').addEventListener('click', async function() {
  if (!deleteCollectionId) return;
  this.disabled = true;
  deleteCollectionModal.hide();
  try {
    const r = await fetch(`/api/collections/${deleteCollectionId}`, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': csrfToken },
    });
    if (r.ok) {
      loadCollections();
      showToast('收藏夹已删除');
    } else {
      const d = await r.json();
      showToast(d.error || '删除失败', true);
    }
  } catch { showToast('网络错误', true); }
  finally {
    this.disabled = false;
    deleteCollectionId = null;
  }
});

// ── 屏蔽标签（全局：搜索/图库/缓存）──
let blockedTags = [];
async function loadBlockedTags() {
  try { blockedTags = await fetch('/api/blocked-tags').then(r => r.json()); renderBlockedTags(); } catch {}
}
function renderBlockedTags() {
  const list = $('#blockedTagList');
  const c = (blockedTags || []).length;
  const cnt = $('#blockedCount');
  cnt.style.display = c ? '' : 'none';
  cnt.textContent = c ? `${c} 个` : '';
  list.innerHTML = (blockedTags || []).map(t =>
    `<span class="badge" style="background:rgba(196,74,74,.1);color:var(--danger);padding:6px 10px;font-weight:500;display:inline-flex;align-items:center;gap:6px;">${escHtml(t.tag)}
       <span style="cursor:pointer;font-size:1rem;line-height:1;" data-tag="${escAttr(t.tag)}" title="移除屏蔽">×</span></span>`
  ).join('');
  list.querySelectorAll('[data-tag]').forEach(el => {
    el.addEventListener('click', () => removeBlockedTag(el.dataset.tag));
  });
}
async function addBlockedTag(tag) {
  try {
    const r = await fetch('/api/blocked-tags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ tag }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) { loadBlockedTags(); showToast('已屏蔽：' + tag); }
    else showToast(d.error || '添加失败', true);
  } catch { showToast('网络错误', true); }
}
async function removeBlockedTag(tag) {
  try {
    const r = await fetch(`/api/blocked-tags/${encodeURIComponent(tag)}`, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': csrfToken },
    });
    if (r.ok) { loadBlockedTags(); showToast('已取消屏蔽'); }
  } catch { showToast('网络错误', true); }
}
$('#addBlockedBtn').addEventListener('click', () => {
  const t = $('#newBlockedTag').value.trim();
  if (t) { addBlockedTag(t); $('#newBlockedTag').value = ''; }
});
$('#newBlockedTag').addEventListener('keydown', e => { if (e.key === 'Enter') $('#addBlockedBtn').click(); });

loadCollections();
loadBlockedTags();
