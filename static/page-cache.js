const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

// ── Render Card ──
// 简化版：标签/画师不可点击（无搜索集成），卡片点击进详情，下载按钮与搜索页一致
function renderCard(r) {
  const isDone = r.download_status === 'done';
  const isDl = r.download_status === 'downloading';
  let btnHtml;
  if (isDl) btnHtml = '<span style="font-size:0.68rem;color:var(--text-muted);">下载中...</span>';
  else if (isDone) btnHtml = `<button class="btn btn-dl-done btn-sm dl-file-btn" data-pid="${r.pixiv_id}">下载</button>`;
  else btnHtml = `<button class="btn btn-soft btn-sm dl-btn" data-pid="${r.pixiv_id}">下载</button>`;
  // 删除按钮：仅未下载/未下载中显示
  const delBtnHtml = (isDl || isDone) ? '' :
    `<button class="btn btn-soft btn-sm cache-del-btn" data-pid="${r.pixiv_id}" title="从缓存删除">删除</button>`;

  const badges = [];
  badges.push(`<span class="photo-badge">♥ ${fmtNum(r.bookmark_count)}</span>`);
  if (r.page_count > 1) badges.push(`<span class="photo-badge">${r.page_count}P</span>`);
  if (isDone) badges.push(`<span class="photo-badge" style="background:rgba(59,138,94,.85);color:#fff;">已下载</span>`);

  const tags = (r.tags||[]).slice(0,6).map(t =>
    `<span class="photo-tag">${escHtml(t)}</span>`).join('');

  const item = document.createElement('div');
  item.className = 'masonry-item';
  item.innerHTML = `
    <div class="photo-card" data-pixiv-id="${r.pixiv_id}">
      <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='400' fill='%23ecece7'%3E%3C/svg%3E"
           data-src="${escAttr(proxyThumb(r.thumb_url))}" loading="lazy" alt="">
      <div class="photo-badges">${badges.join('')}</div>
      <div class="photo-card-info">
        <div class="photo-card-title">${escHtml(r.title)}</div>
        <div class="photo-card-artist">${escHtml(r.user_name)}</div>
        <div class="photo-tags">${tags}</div>
        <div class="photo-card-actions">${btnHtml}${delBtnHtml}</div>
      </div>
    </div>`;
  $('#masonryGrid').appendChild(item);

  // Card click → detail page
  item.querySelector('.photo-card').addEventListener('click', e => {
    if (e.target.closest('.photo-card-actions')) return;
    window.location.href = `/detail/${r.pixiv_id}`;
  });

  // Download button
  item.querySelector('.dl-btn')?.addEventListener('click', function(e) {
    e.stopPropagation();
    triggerDownload(r.pixiv_id, this);
  });
  item.querySelector('.dl-file-btn')?.addEventListener('click', function(e) {
    e.stopPropagation();
    downloadFile(r.pixiv_id);
  });

  // 删除按钮
  item.querySelector('.cache-del-btn')?.addEventListener('click', function(e) {
    e.stopPropagation();
    deleteCacheItem(r.pixiv_id, r.title, this);
  });
}

// 从缓存删除单条作品
async function deleteCacheItem(pixivId, title, btn) {
  if (!confirm(`确定从缓存中删除「${title || pixivId}」吗？\n（不会删除已下载的文件）`)) return;
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/cache/items/${pixivId}/delete`, {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken },
    });
    const data = await resp.json();
    if (!resp.ok) {
      showToast(data.error || '删除失败', true);
      btn.disabled = false;
      return;
    }
    const card = btn.closest('.photo-card');
    card?.closest('.masonry-item')?.remove();
    showToast('已从缓存删除');
  } catch {
    showToast('删除失败', true);
    btn.disabled = false;
  }
}

// ── Lazy Load ──
function lazyLoad() {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const img = entry.target;
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
        observer.unobserve(img);
      }
    });
  }, { rootMargin: '200px' });
  $$('img[data-src]').forEach(img => observer.observe(img));
}

// ── 缓存浏览 ──
let cacheTags = [];
let currentOffset = 0;
let cacheHasMore = false;
let cachePageSize = 24;
let cacheFilteredTotal = 0;

async function loadCacheTags() {
  try {
    const prevTag = $('#cacheTagSelect').value;
    cacheTags = await fetch('/api/prefetch/tags').then(r => r.json());
    const sel = $('#cacheTagSelect');
    sel.innerHTML = cacheTags.length
      ? cacheTags.map(t => `<option value="${escAttr(t.tag)}">${escHtml(t.tag)}（${escHtml(t.status)} · ${t.total || 0}条）</option>`).join('')
      : '<option value="">暂无预取标签</option>';
    sel.value = cacheTags.some(t => t.tag === prevTag) ? prevTag : (cacheTags[0] ? cacheTags[0].tag : '');
    $('#cacheRefreshBtn').disabled = !cacheTags.length;
    updateCacheMeta();
  } catch { $('#cacheTagSelect').innerHTML = '<option value="">加载失败</option>'; }
}

async function loadCacheTagList() {
  try {
    const tags = await fetch('/api/cache/tags').then(r => r.json());
    $('#cacheTagList').innerHTML = tags.map(t => `<option value="${escAttr(t)}">`).join('');
  } catch { /* 提示列表加载失败不影响浏览 */ }
}

function updateCacheMeta() {
  const tag = $('#cacheTagSelect').value;
  const t = cacheTags.find(x => x.tag === tag);
  const el = $('#cacheMeta');
  if (!t) { el.textContent = ''; return; }
  el.textContent = `状态: ${t.status} · 缓存时间: ${t.cached_at ? new Date(t.cached_at).toLocaleString('zh-CN') : '从未'} · 条数: ${t.total || 0}`;
  if (t.status === 'error') el.textContent += ` · 错误: ${t.error || '未知'}`;
}

async function browseCache() {
  const tag = $('#cacheTagSelect').value;
  if (!tag) return;
  const minBookmarks = parseInt($('#cacheMinBookmarks').value) || 0;
  const sort = $('#cacheSortOrder').value;
  const r18 = $('#cacheR18').value;
  const filterTag = $('#cacheFilterTag').value.trim();
  const resp = await fetch(`/api/cache/items?tag=${encodeURIComponent(tag)}&min_bookmarks=${minBookmarks}&sort=${sort}&r18=${r18}&filter_tag=${encodeURIComponent(filterTag)}&offset=0`);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    showToast(err.error || '加载失败', true);
    return;
  }
  const data = await resp.json();
  cacheHasMore = data.has_more;
  currentOffset = data.offset;
  renderCacheResults(data);
}

function renderCacheResults(data) {
  cachePageSize = data.page_size || cachePageSize;
  cacheFilteredTotal = data.filtered_total || 0;
  $('#masonryGrid').innerHTML = '';
  if (!data.results.length) {
    $('#emptyState').style.display = 'block';
    $('#paginationBar').style.display = 'none';
    return;
  }
  $('#emptyState').style.display = 'none';
  data.results.forEach(r => renderCard(r));
  lazyLoad();
  updateCacheMeta();
  renderCachePagination();
}

function renderCachePagination() {
  $('#paginationBar').style.display = 'block';
  const pageNum = Math.floor(currentOffset / cachePageSize) + 1;
  const totalPages = cacheFilteredTotal > 0 ? Math.ceil(cacheFilteredTotal / cachePageSize) : 1;
  $('#prevPageBtn').disabled = currentOffset === 0;
  $('#nextPageBtn').disabled = !cacheHasMore;
  $('#paginationStatus').textContent = `第 ${pageNum} / ${totalPages} 页`;
}

function jumpToPage() {
  const totalPages = cacheFilteredTotal > 0 ? Math.ceil(cacheFilteredTotal / cachePageSize) : 1;
  const p = parseInt($('#pageJumpInput').value);
  if (!p || p < 1 || p > totalPages) {
    showToast(`页码需在 1-${totalPages} 之间`, true);
    return;
  }
  browseCacheWithOffset((p - 1) * cachePageSize);
}

$('#pageJumpBtn').addEventListener('click', jumpToPage);
$('#pageJumpInput').addEventListener('keydown', e => { if (e.key === 'Enter') jumpToPage(); });
$('#cacheFilterTag').addEventListener('keydown', e => { if (e.key === 'Enter') browseCache(); });

$('#cacheBrowseBtn').addEventListener('click', browseCache);
$('#cacheTagSelect').addEventListener('change', () => { updateCacheMeta(); browseCache(); });
$('#prevPageBtn').addEventListener('click', async () => {
  if (currentOffset === 0) return;
  await browseCacheWithOffset(Math.max(0, currentOffset - cachePageSize));
});
$('#nextPageBtn').addEventListener('click', async () => {
  if (!cacheHasMore) return;
  await browseCacheWithOffset(currentOffset + cachePageSize);
});
async function browseCacheWithOffset(offset) {
  const tag = $('#cacheTagSelect').value;
  const minBookmarks = parseInt($('#cacheMinBookmarks').value) || 0;
  const sort = $('#cacheSortOrder').value;
  const r18 = $('#cacheR18').value;
  const filterTag = $('#cacheFilterTag').value.trim();
  const resp = await fetch(`/api/cache/items?tag=${encodeURIComponent(tag)}&min_bookmarks=${minBookmarks}&sort=${sort}&r18=${r18}&filter_tag=${encodeURIComponent(filterTag)}&offset=${offset}`);
  if (!resp.ok) { showToast('加载失败', true); return; }
  const data = await resp.json();
  currentOffset = data.offset;
  cacheHasMore = data.has_more;
  renderCacheResults(data);
}
$('#cacheRefreshBtn').addEventListener('click', async () => {
  const tag = $('#cacheTagSelect').value;
  if (!tag) return;
  const btn = $('#cacheRefreshBtn');
  btn.disabled = true;
  btn.textContent = '刷新中...';
  try {
    const resp = await fetch('/api/prefetch/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify({tag}),
    });
    const err = await resp.json().catch(() => ({}));
    if (resp.ok) { showToast('已开始刷新缓存'); loadCacheTags(); }
    else { showToast(err.error || '刷新失败', true); }
  } catch { showToast('网络错误', true); }
  finally {
    btn.disabled = false;
    btn.textContent = '刷新';
  }
});

loadCacheTags();
loadCacheTagList();
