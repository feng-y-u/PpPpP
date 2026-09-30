const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

let deleteTarget = null, deleteMode = 'single', activeTag = '';
let selectedPids = new Set();
let galleryCurrentPage = 1, galleryTotal = 0, galleryTotalPages = 0;
let allTags = [];
let sortOrder = 'downloaded';
let galleryR18 = 'safe';   // R18 过滤：默认隐藏（与缓存页/搜索页一致）
let currentResults = [];
const PAGE_SIZE = 50;
const GALLERY_CACHE_TTL = 30 * 60 * 1000; // 图库分页前端缓存 30 分钟

function invalidateGalleryCache() {
  pvCache.clearPrefix('pv_gallery_');
}
const deleteModal = new bootstrap.Modal($('#deleteModal'));

// Read ?tag= from URL on load
const urlParams = new URLSearchParams(location.search);
if (urlParams.has('tag')) {
  activeTag = urlParams.get('tag');
}

$('#sortSelect').value = 'downloaded';
loadGallery(1);
loadTags();

if (activeTag) {
  const el = $('#tagFilterActive');
  el.style.display = 'inline-block';
  el.innerHTML = `${escHtml(activeTag)} <span class="tag-filter-clear">✕</span>`;
  $('#tagFilter').value = activeTag;
}

function setActiveTag(tag) {
  if (tag === activeTag) return;   // 已处于该标签，跳过重复加载
  activeTag = tag;
  const el = $('#tagFilterActive');
  const input = $('#tagFilter');
  if (tag) {
    el.style.display = 'inline-block';
    el.innerHTML = `${escHtml(tag)} <span class="tag-filter-clear">✕</span>`;
    input.value = tag;
  } else {
    el.style.display = 'none';
    input.value = '';
  }
  invalidateGalleryCache();
  loadGallery(1);
}

$('#tagFilter').addEventListener('input', function() {
  const val = this.value.trim();
  if (!val) { setActiveTag(''); return; }
  if (allTags.includes(val)) { setActiveTag(val); }
});

$('#tagFilter').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') {
    const val = this.value.trim();
    if (val && allTags.includes(val)) setActiveTag(val);
  }
});

$('#tagFilterActive')?.addEventListener('click', function(e) {
  if (e.target.classList.contains('tag-filter-clear')) setActiveTag('');
});


function loadGallery(pageNum) {
  const offset = (pageNum - 1) * PAGE_SIZE;
  const cacheKey = `pv_gallery_${activeTag || ''}_${sortOrder}_${galleryR18}_${pageNum}`;
  const cached = pvCache.get(cacheKey, GALLERY_CACHE_TTL);
  if (cached) {
    renderGalleryData(cached, pageNum);
    return;
  }
  showGallerySkeleton();
  const params = new URLSearchParams();
  if (activeTag) params.set('tag', activeTag);
  params.set('sort', sortOrder);
  params.set('r18', galleryR18);
  params.set('limit', PAGE_SIZE);
  params.set('offset', offset);
  fetch('/api/gallery?' + params.toString())
    .then(r => r.json())
    .then(data => {
      pvCache.set(cacheKey, data);
      renderGalleryData(data, pageNum);
    })
    .catch(() => {
      showToast('加载图库失败', true);
      $('#cardGrid').innerHTML = '';
    });
}

// 骨架屏：数据到达前的灰块占位，避免空白等待
function showGallerySkeleton() {
  const grid = $('#cardGrid');
  grid.innerHTML = '';
  for (let i = 0; i < 12; i++) {
    const col = document.createElement('div');
    col.className = 'col-lg-3 col-md-4 col-sm-6 col-6 mb-3';
    col.innerHTML = '<div class="skeleton-card"><div class="skeleton-card-inner"></div></div>';
    grid.appendChild(col);
  }
}

function renderGalleryData(data, pageNum) {
  const offset = (pageNum - 1) * PAGE_SIZE;
  $('#cardGrid').innerHTML = '';
  selectedPids.clear();
  updateFloatBar();
  currentResults = data.data;

  if (data.data.length === 0) {
    $('#emptyState').style.display = 'block';
    $('#stats').style.display = 'none';
    $('#paginationBar').style.display = 'none';
    $('#emptyTitle').textContent = '暂无已下载作品';
    $('#emptyDesc').textContent = '搜索作品并下载后在此查看';
    return;
  }
  $('#emptyState').style.display = 'none';
  let totalSize = 0;
  data.data.forEach(item => { totalSize += item.file_size || 0; });
  // 分帧渲染 50 张卡：每批 15 张，批间让出主线程，卡片依次浮现
  const grid = $('#cardGrid');
  renderInChunks(data.data, (item) => {
    const col = renderCard(item);
    grid.appendChild(col);
    return col;
  }, { chunk: 15, delay: 25 }).then(() => {
    lazyLoad();
  });
  updateFloatBar();
  galleryCurrentPage = pageNum;
  galleryTotal = data.total;
  galleryTotalPages = Math.ceil(galleryTotal / PAGE_SIZE);
  $('#stats').style.display = 'block';
  const start = offset + 1;
  const end = Math.min(galleryTotal, offset + data.data.length);
  let statsText = `${start}-${end} / ${galleryTotal} 个作品，总大小 ${fmtSize(totalSize)}`;
  if (activeTag) statsText += ` · 标签: ${activeTag}`;  // textContent 赋值天然安全，无需 escHtml
  $('#stats').textContent = statsText;
  renderGalleryPagination();
}

function renderGalleryPagination() {
  const bar = $('#paginationBar');
  const container = $('#pageNumbers');
  const total = galleryTotalPages;
  if (total <= 1) { bar.style.display = 'none'; return; }
  bar.style.display = 'block';

  // 页码窗口化：当前页 ±2 + 首尾页 + 省略号（作品多时避免一长串按钮）
  const cur = galleryCurrentPage;
  const pages = new Set([1, total]);
  for (let p = cur - 2; p <= cur + 2; p++) {
    if (p >= 1 && p <= total) pages.add(p);
  }
  const sorted = [...pages].sort((a, b) => a - b);
  let html = '';
  let prev = 0;
  for (const num of sorted) {
    if (prev && num - prev > 1) html += '<span style="padding:0 4px;color:var(--text-muted);">…</span>';
    if (num === cur) {
      html += `<span style="padding:2px 10px;border-radius:4px;background:var(--accent);color:#fff;font-size:0.8rem;font-weight:600;">${num}</span>`;
    } else {
      html += `<button class="btn btn-sm btn-soft gallery-page-btn" data-page="${num}" style="min-width:32px;font-size:0.75rem;padding:2px 8px;">${num}</button>`;
    }
    prev = num;
  }
  container.innerHTML = html;

  container.querySelectorAll('.gallery-page-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = parseInt(btn.dataset.page);
      loadGallery(p);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });

  $('#prevPageBtn').disabled = galleryCurrentPage <= 1;
  $('#nextPageBtn').disabled = galleryCurrentPage >= total;
  $('#paginationStatus').textContent = `第 ${galleryCurrentPage} 页 · 共 ${total} 页`;
}

function jumpGalleryPage() {
  const total = galleryTotalPages;
  const p = parseInt($('#pageJumpInput').value);
  if (!p || p < 1 || p > total) {
    showToast(`页码需在 1-${total} 之间`, true);
    return;
  }
  loadGallery(p);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

$('#pageJumpBtn').addEventListener('click', jumpGalleryPage);
$('#pageJumpInput').addEventListener('keydown', e => { if (e.key === 'Enter') jumpGalleryPage(); });

$('#prevPageBtn').addEventListener('click', () => {
  if (galleryCurrentPage > 1) loadGallery(galleryCurrentPage - 1);
  window.scrollTo({ top: 0, behavior: 'smooth' });
});
$('#nextPageBtn').addEventListener('click', () => {
  if (galleryCurrentPage < galleryTotalPages) loadGallery(galleryCurrentPage + 1);
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

function loadTags() {
  fetch('/api/gallery/tags')
    .then(r => r.json())
    .then(tags => {
      allTags = tags;
      $('#tagList').innerHTML = tags.map(t => `<option value="${escAttr(t)}">`).join('');
    });
}

// ── 详情页跨作品翻页的上下文传递 ──
// 点卡片信息区进详情时：URL 参数定位（排序/筛选/位置），sessionStorage 存
// 当前页 id 序列（与图库 30 分钟前端缓存同一份数据，约几 KB）。详情页据此
// 算上一作/下一作；序列获取失败时详情页自动降级为无翻页。
function buildDetailUrl(r, idx) {
  const pos = (galleryCurrentPage - 1) * PAGE_SIZE + idx;
  const params = new URLSearchParams();
  params.set('ctx', 'gallery');
  params.set('sort', sortOrder);
  if (activeTag) params.set('tag', activeTag);
  params.set('pos', pos);
  params.set('page', galleryCurrentPage);
  saveDetailSeq();
  return `/detail/${r.pixiv_id}?${params}`;
}

function saveDetailSeq() {
  try {
    sessionStorage.setItem('pv_detail_seq', JSON.stringify({
      v: 1,
      sort: sortOrder,
      tag: activeTag || '',
      total: galleryTotal,
      pages: { [galleryCurrentPage]: currentResults.map(x => x.pixiv_id) },
    }));
  } catch (e) { /* sessionStorage 不可用（隐私模式等）时详情页自动降级 */ }
}

function renderCard(r) {
  const tags = (r.tags || []).slice(0, 6).map(t =>
    `<span class="badge tag-badge">${escHtml(t)}</span>`
  ).join('');

  const thumbUrl = proxyThumb(r.thumb_url);

  const col = document.createElement('div');
  col.className = 'col-lg-3 col-md-4 col-sm-6 col-6 mb-3';
  col.innerHTML = `
    <div class="gallery-card" data-pid="${r.pixiv_id}" style="cursor:pointer;">
      <div class="card-img-wrap">
        <input type="checkbox" class="card-checkbox" data-pid="${r.pixiv_id}">
        <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='250' height='250' fill='%23ecece7'%3E%3C/svg%3E"
             data-src="${escAttr(thumbUrl)}" loading="lazy" class="img-fade" alt="">
        <span class="page-badge">${r.file_count || r.page_count || 1} 张</span><span class="size-badge">${fmtSize(r.file_size || 0)}</span>
      </div>
      <div class="card-body">
        <div class="card-title" title="${escAttr(r.title)}">${escHtml(r.title)}</div>
        <div class="card-info">#${r.pixiv_id} · ${escHtml(r.user_name)} · ${r.file_count || 1} 文件</div>
        <div class="tags-wrap">${tags}</div>
        <div class="d-flex gap-1 mt-1">
          <button class="btn btn-outline-danger btn-sm flex-fill delete-btn" data-pid="${r.pixiv_id}">删除</button>
          <a href="/download_file/${r.pixiv_id}" class="btn btn-soft btn-sm dl-file-btn" onclick="event.stopPropagation()">下载</a>
        </div>
      </div>
    </div>`;

  col.querySelector('.gallery-card').addEventListener('click', function(e) {
    if (e.target.closest('.delete-btn') || e.target.closest('.card-checkbox') || e.target.closest('.dl-file-btn') || e.target.closest('a')) return;
    const idx = currentResults.findIndex(x => x.pixiv_id === r.pixiv_id);
    if (e.target.closest('.card-body')) {
      location.href = buildDetailUrl(r, idx >= 0 ? idx : 0);
      return;
    }
    lightbox.open(currentResults.map(x => ({
      pixiv_id: x.pixiv_id,
      thumbUrl: proxyThumb(x.thumb_url),
    })), idx >= 0 ? idx : 0);
  });

  col.querySelector('.delete-btn').addEventListener('click', function() {
    deleteTarget = r.pixiv_id;
    deleteMode = 'single';
    $('#deleteModalBody').textContent = `确定删除 #${r.pixiv_id} 的所有已下载文件？`;
    deleteModal.show();
  });

  col.querySelector('.card-checkbox').addEventListener('change', function() {
    const pid = parseInt(this.dataset.pid);
    if (this.checked) selectedPids.add(pid);
    else selectedPids.delete(pid);
    col.querySelector('.gallery-card').classList.toggle('card-selected', this.checked);
    updateFloatBar();
  });

  return col;
}

$('#confirmDeleteBtn').addEventListener('click', async () => {
  if (!deleteTarget) return;
  const btn = $('#confirmDeleteBtn');
  btn.disabled = true;
  btn.textContent = '删除中...';
  deleteModal.hide();

  if (deleteMode === 'batch') {
    const ids = deleteTarget;
    try {
      const resp = await fetch('/api/gallery/batch-delete', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      const data = await resp.json();
      if (resp.ok) {
        showToast(data.message);
        invalidateGalleryCache();
        ids.forEach(pid => {
          document.querySelectorAll(`[data-pid="${pid}"].delete-btn`).forEach(btn => {
            btn.closest('.gallery-card')?.parentElement?.remove();
          });
        });
        currentResults = currentResults.filter(x => !ids.includes(x.pixiv_id));
        selectedPids.clear();
        updateFloatBar();
        btn.disabled = false;
        btn.textContent = '删除';
      } else {
        showToast(data.error || '批量删除失败', true);
        btn.disabled = false;
        btn.textContent = '删除';
      }
    } catch {
      showToast('网络错误', true);
      btn.disabled = false;
      btn.textContent = '删除';
    }
    deleteTarget = null;
    deleteMode = 'single';
    return;
  }

  try {
    const resp = await fetch(`/api/gallery/${deleteTarget}`, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': csrfToken },
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message);
      invalidateGalleryCache();
      document.querySelectorAll(`[data-pid="${deleteTarget}"].delete-btn`).forEach(btn => {
        btn.closest('.gallery-card')?.parentElement?.remove();
      });
      currentResults = currentResults.filter(x => x.pixiv_id !== deleteTarget);
      btn.disabled = false;
      btn.textContent = '删除';
    } else {
      showToast(data.error || '删除失败', true);
      btn.disabled = false;
      btn.textContent = '删除';
    }
  } catch {
    showToast('网络错误', true);
    btn.disabled = false;
    btn.textContent = '删除';
  }
  deleteTarget = null;
});

// ── Float Batch Bar ──

function updateFloatBar() {
  const bar = $('#batchFloatBar');
  const count = selectedPids.size;
  if (count === 0) {
    bar.classList.add('hidden');
    return;
  }
  bar.classList.remove('hidden');
  $('#batchFloatCount').textContent = `已选 ${count} 个`;
  const c = $('#batchFloatCount');
  c.classList.remove('bounce');
  void c.offsetWidth;  // 重触发
  c.classList.add('bounce');
  $('#btnFloatDelete').textContent = `删除 (${count})`;
}

$('#btnFloatSelectAll').addEventListener('click', () => {
  document.querySelectorAll('.card-checkbox').forEach(cb => {
    cb.checked = true;
    selectedPids.add(parseInt(cb.dataset.pid));
    cb.closest('.gallery-card')?.classList.add('card-selected');
  });
  updateFloatBar();
});

$('#btnFloatDelete').addEventListener('click', () => {
  if (selectedPids.size === 0) return;
  deleteTarget = Array.from(selectedPids);
  deleteMode = 'batch';
  $('#deleteModalBody').textContent = `确定删除选中的 ${selectedPids.size} 个作品？此操作不可恢复。`;
  deleteModal.show();
});

$('#sortSelect').addEventListener('change', function() {
  sortOrder = this.value;
  invalidateGalleryCache();
  loadGallery(1);
});

$('#r18Filter').addEventListener('change', function() {
  galleryR18 = this.value;
  invalidateGalleryCache();
  loadGallery(1);
});
