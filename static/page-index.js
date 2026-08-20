const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

// ── 翻页状态 ──
let loadedPages = [];
let nextCursor = null;
let currentPage = 1;
let hasMore = false;
let currentSearchType = null;

const R18_STATE_KEY = 'pixiv_r18_mode';
const SEARCH_STATE_KEY = 'pv_search_state';
const SEARCH_CACHE_TTL = 30 * 60 * 1000;         // 搜索结果前端缓存 30 分钟（页面刷新快速恢复）
const SEARCH_STATE_RESTORE_TTL = 24 * 60 * 60 * 1000;  // 游标过期兜底恢复用 24h（与游标 TTL 一致）

function saveSearchState() {
  try {
    pvCache.set(SEARCH_STATE_KEY, {
      type: $('#searchType').value,
      query: $('#searchQuery').value,
      min_bookmarks: $('#minBookmarks').value,
      sort: $('#sortOrder').value,
      tag_mode: $('#tagMode').value,
      r18_mode: $('#r18Mode').value,
      loadedPages,
      nextCursor,
      hasMore,
      currentPage,
    });
  } catch {}
}

function restoreSearchState(ttlMs, verifyParams) {
  const st = pvCache.get(SEARCH_STATE_KEY, ttlMs || SEARCH_CACHE_TTL);
  if (!st || !Array.isArray(st.loadedPages) || !st.loadedPages.length) return false;
  // 多标签页共享 localStorage：仅在"过期恢复"场景校验搜索参数与当前表单一致，
  // 避免恢复其他标签页的搜索结果（页面刷新恢复场景表单是默认值，不校验）
  if (verifyParams) {
    if (st.type !== $('#searchType').value) return false;
    if ((st.query || '') !== $('#searchQuery').value.trim()) return false;
  }
  $('#searchType').value = st.type || 'tag';
  $('#searchQuery').value = st.query || '';
  $('#minBookmarks').value = st.min_bookmarks || 0;
  if (st.sort) $('#sortOrder').value = st.sort;
  if (st.tag_mode) $('#tagMode').value = st.tag_mode;
  if (st.r18_mode) $('#r18Mode').value = st.r18_mode;
  updateSearchUI();
  loadedPages = st.loadedPages;
  nextCursor = st.nextCursor || null;
  hasMore = !!st.hasMore;
  currentSearchType = st.type || null;
  // 恢复到上次所在的页（可能因分页漂移去重而少于缓存页数，做边界钳制）
  const restored = Math.max(1, Math.min(parseInt(st.currentPage) || 1, loadedPages.length));
  currentPage = restored;
  renderPage(restored);
  renderPaginationBar();
  lazyLoad();
  return true;
}

function resetPagination() {
  loadedPages = [];
  nextCursor = null;
  currentPage = 1;
  hasMore = false;
  currentSearchType = null;
  $('#prevPageBtn').disabled = true;
  $('#nextPageBtn').disabled = true;
  $('#pageNumbers').innerHTML = '';
  $('#paginationBar').style.display = 'none';
}

function renderPaginationBar() {
  const bar = $('#paginationBar');
  const container = $('#pageNumbers');
  bar.style.display = loadedPages.length > 0 ? 'block' : 'none';
  if (!loadedPages.length) return;

  let html = '';
  for (let i = 0; i < loadedPages.length && i < 20; i++) {
    const num = i + 1;
    if (num === currentPage) {
      html += `<span style="padding:2px 10px;border-radius:4px;background:var(--accent);color:#fff;font-size:0.8rem;font-weight:600;">${num}</span>`;
    } else {
      html += `<button class="btn btn-sm btn-soft page-jump-btn" data-page="${num}" style="min-width:32px;font-size:0.75rem;padding:2px 8px;">${num}</button>`;
    }
  }
  container.innerHTML = html;

  container.querySelectorAll('.page-jump-btn').forEach(btn => {
    btn.addEventListener('click', () => jumpToPage(parseInt(btn.dataset.page)));
  });

  $('#prevPageBtn').disabled = currentPage <= 1;
  $('#nextPageBtn').disabled = !hasMore && currentPage >= loadedPages.length;
  $('#nextPageBtn').textContent = '下一页';  // 恢复翻页按钮文本（异步加载时曾置为"加载中..."）
  $('#paginationStatus').textContent = `第 ${currentPage} 页 · 已缓 ${loadedPages.length} 页`;
}

function renderPage(pageNum) {
  const page = loadedPages[pageNum - 1];
  if (!page) return;
  $('#masonryGrid').innerHTML = '';
  page.forEach(r => renderCard(r));
  lazyLoad();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function jumpToPage(pageNum) {
  if (pageNum < 1 || pageNum > loadedPages.length) return;
  currentPage = pageNum;
  renderPage(currentPage);
  renderPaginationBar();
  saveSearchState();  // 记录浏览位置，刷新/过期恢复时回到所在页
}

$('#prevPageBtn').addEventListener('click', () => jumpToPage(currentPage - 1));
$('#nextPageBtn').addEventListener('click', () => {
  if (currentPage < loadedPages.length) {
    jumpToPage(currentPage + 1);
  } else {
    loadNextPage();
  }
});

async function loadNextPage() {
  if (currentSearchType === 'following') {
    const nextPage = loadedPages.length + 1;
    const r18Mode = $('#r18Mode').value;
    $('#nextPageBtn').disabled = true;
    $('#nextPageBtn').textContent = '加载中...';
    try {
      const resp = await fetch(`/api/following?page=${nextPage}&r18_mode=${r18Mode}`);
      if (!resp.ok) { showToast('加载失败', true); renderPaginationBar(); return; }
      const data = await resp.json();
      if (!data.results.length) { hasMore = data.has_more || false; renderPaginationBar(); return; }
      loadedPages.push(data.results);
      hasMore = data.has_more || false;
      currentPage = loadedPages.length;
      renderPage(currentPage);
      renderPaginationBar();
      saveSearchState();
    } catch { showToast('网络错误', true); }
    finally {
      $('#nextPageBtn').disabled = !hasMore;
      $('#nextPageBtn').textContent = '下一页';
    }
    return;
  }

  if (!nextCursor) return;
  $('#nextPageBtn').disabled = true;
  $('#nextPageBtn').textContent = '加载中...';
  const restoreBtn = () => {
    $('#nextPageBtn').disabled = !hasMore;
    $('#nextPageBtn').textContent = '下一页';
  };

  try {
    const params = new URLSearchParams();
    params.set('cursor', nextCursor);
    const resp = await fetch('/search?' + params.toString());
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (err.error_code === 'CURSOR_EXPIRED') {
        // 游标过期（24h 上限）：恢复已加载的缓存页，保留浏览进度，不从头重搜。
        // 用 24h TTL 读取缓存（游标与缓存同时写入，缓存必须比游标活得久才能恢复）
        const restored = restoreSearchState(SEARCH_STATE_RESTORE_TTL, true);
        // 死游标作废，禁止继续翻页（继续翻页需重新搜索）
        nextCursor = null;
        hasMore = false;
        renderPaginationBar();
        if (restored) {
          showToast('搜索游标已过期，已恢复已加载的页面（继续翻页请重新搜索）', true);
        } else {
          showToast('搜索已过期，请重新搜索', true);
        }
        return;
      }
      showToast(err.error || '加载失败', true);
      renderPaginationBar();
      return;
    }
    const data = await resp.json();
    // 异步任务：按钮状态由轮询回调恢复（done 时 renderPaginationBar，
    // 失败/404 时 restoreBtn），避免用旧 hasMore 提前恢复导致重复翻页
    pollSearch(data.task_id, (res) => {
      if (!res.results.length) {
        hasMore = res.has_more || false;
        renderPaginationBar();
        return;
      }
      const dedup = dedupResults(res.results);
      if (!dedup.length) {
        // 本页全部与本会话已显示的作品重复（Pixiv 分页漂移）→ 跳过空页，
        // 但必须推进游标，否则下次点击会重复请求同一页导致翻页卡死
        nextCursor = res.cursor || null;
        hasMore = res.has_more || false;
        renderPaginationBar();
        saveSearchState();
        return;
      }
      loadedPages.push(dedup);
      nextCursor = res.cursor || null;
      hasMore = res.has_more || false;
      currentPage = loadedPages.length;
      renderPage(currentPage);
      renderPaginationBar();
      saveSearchState();
      maybeToastFetchStats(res.fetch_stats);
    }, restoreBtn);
  } catch { showToast('网络错误', true); restoreBtn(); }
}

async function doSearch() {
  const type = $('#searchType').value;
  const query = $('#searchQuery').value.trim();
  const minBookmarks = parseInt($('#minBookmarks').value) || 0;
  if (type === 'user' && !query) { showToast('请输入画师ID'); return; }

  const sort = $('#sortOrder').value || 'date_d';
  const tagMode = $('#tagMode').value || 'or';
  const r18Mode = $('#r18Mode').value;
  resetPagination();
  currentSearchType = type;
  $('#masonryGrid').innerHTML = '';
  $('#emptyState').style.display = 'none';
  $('#authErrorState').style.display = 'none';
  batchInProgress = false;

  showLoading(true);
  try {
    let url;
    if (type === 'following') url = `/api/following?page=1&r18_mode=${r18Mode}`;
    else url = `/search?${new URLSearchParams({type,query,min_bookmarks:minBookmarks,sort,tag_mode:tagMode,r18_mode:r18Mode})}`;

    const resp = await fetch(url);
    if (!resp.ok) {
      if (resp.status === 401) {
        showToast('Cookie 已过期，请更新 cookies.txt', true);
        $('#authErrorState').style.display = 'block';
        showLoading(false);
        return;
      }
      const err = await resp.json().catch(() => ({}));
      if (err.error_code === 'CURSOR_EXPIRED') {
        showToast('搜索已过期，请重新搜索', true);
        showLoading(false);
        return;
      }
      showToast(resp.status === 429 ? '请求过于频繁' : (err.error || '搜索失败'), true);
      showLoading(false);
      return;
    }
    const data = await resp.json();
    if (type === 'following') {
      finishSearch(data);
      return;
    }
    pollSearch(data.task_id, finishSearch);
  } catch { showToast('网络错误', true); showLoading(false); }
}

function finishSearch(data) {
  if (!data.results.length) {
    $('#emptyState').style.display = 'block';
    showLoading(false);
    return;
  }
  loadedPages = [dedupResults(data.results)];
  nextCursor = data.cursor || null;
  hasMore = data.has_more || false;
  currentPage = 1;
  loadedPages[0].forEach(r => renderCard(r));
  renderPaginationBar();
  lazyLoad();
  saveSearchState();
  maybeToastFetchStats(data.fetch_stats);
  showLoading(false);
}

// 分页去重：Pixiv 搜索分页在 date_d 排序下会漂移（新作品插入导致页间重叠），
// 后端已尽量消除（early_stop 不再丢弃已启动的拉取），此处显示层再兜底一层，
// 跳过本会话已显示过的作品，保证同一作品不重复出现
function dedupResults(items) {
  if (!loadedPages.length) return items;
  const seen = new Set(loadedPages.flat().map(r => r.pixiv_id));
  return items.filter(r => !seen.has(r.pixiv_id));
}

function pollSearch(taskId, onDone, onFail) {
  fetch(`/api/search/status/${taskId}`)
    .then(async resp => {
      if (resp.status === 404) {
        showToast('搜索任务已失效，请重新搜索', true);
        showLoading(false);
        if (onFail) onFail();
        return;
      }
      const data = await resp.json();
      if (data.status === 'running') {
        setTimeout(() => pollSearch(taskId, onDone, onFail), 2000);
        return;
      }
      if (data.status === 'error') {
        if (resp.status === 401) {
          showToast('Cookie 已过期，请更新 cookies.txt', true);
          $('#authErrorState').style.display = 'block';
        } else {
          showToast(data.error || '搜索失败', true);
        }
        showLoading(false);
        if (onFail) onFail();
        return;
      }
      onDone(data);
    })
    .catch(() => { showToast('网络错误', true); showLoading(false); if (onFail) onFail(); });
}

// ── UI Toggle ──
function updateSearchUI() {
  const type = $('#searchType').value;
  const isTag = type === 'tag';
  $('#tagMode').style.display = isTag ? '' : 'none';
  $('#searchQuery').placeholder = type === 'following' ? '' : isTag ? '多个标签用逗号分隔' : '输入画师UID...';

  const show = type !== 'following';
  ['#sortOrder','#minBookmarks'].forEach(id => {
    const el = $(id);
    if (el) el.style.display = show ? '' : 'none';
  });
  $$('.filters-row label').forEach(l => l.style.display = show ? '' : 'none');
  if (!show) {
    ['#sortOrder','#minBookmarks'].forEach(id => $(id).style.display = 'none');
  } else {
    ['#sortOrder','#minBookmarks'].forEach(id => $(id).style.display = '');
  }
}
$('#searchType').addEventListener('change', updateSearchUI);

$('#r18Mode').addEventListener('change', () => {
  sessionStorage.setItem(R18_STATE_KEY, $('#r18Mode').value);
  doSearch();
});

// ── Render Card ──
function renderCard(r) {
  const isDone = r.download_status === 'done';
  const isDl = r.download_status === 'downloading';
  let btnHtml;
  if (isDl) btnHtml = '<span style="font-size:0.68rem;color:var(--text-muted);">下载中...</span>';
  else if (isDone) btnHtml = `<button class="btn btn-dl-done btn-sm dl-file-btn" data-pid="${r.pixiv_id}">下载</button>`;
  else btnHtml = `<button class="btn btn-soft btn-sm dl-btn" data-pid="${r.pixiv_id}">下载</button>`;

  const badges = [];
  badges.push(`<span class="photo-badge">♥ ${fmtNum(r.bookmark_count)}</span>`);
  if (r.page_count > 1) badges.push(`<span class="photo-badge">${r.page_count}P</span>`);
  if (isDone) badges.push(`<span class="photo-badge" style="background:rgba(59,138,94,.85);color:#fff;">已下载</span>`);

  const tags = (r.tags||[]).slice(0,6).map(t =>
    `<span class="photo-tag" data-tag="${escAttr(t)}">${escHtml(t)}<span class="tag-block-x" data-block="${escAttr(t)}">&times;</span></span>`).join('');

  const item = document.createElement('div');
  item.className = 'masonry-item';
  item.innerHTML = `
    <div class="photo-card" data-pixiv-id="${r.pixiv_id}">
      <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='400' fill='%23ecece7'%3E%3C/svg%3E"
           data-src="${escAttr(proxyThumb(r.thumb_url))}" loading="lazy" alt="">
      <div class="photo-badges">${badges.join('')}</div>
      <div class="photo-card-info">
        <div class="photo-card-title">${escHtml(r.title)}</div>
        <div class="photo-card-artist artist-link" data-uid="${r.user_id}">${escHtml(r.user_name)}</div>
        <div class="photo-tags">${tags}</div>
        <div class="photo-card-actions">${btnHtml}</div>
      </div>
    </div>`;
  $('#masonryGrid').appendChild(item);

  // Tag click → search; × → block
  item.querySelectorAll('.photo-tag').forEach(el => {
    el.addEventListener('click', e => {
      if (e.target.closest('.tag-block-x')) {
        e.stopPropagation();
        addBlockedTag(el.dataset.tag);
        return;
      }
      e.stopPropagation();
      $('#searchType').value = 'tag';
      $('#searchQuery').value = el.dataset.tag;
      updateSearchUI();
      doSearch();
    });
  });

  // Card click → detail page
  item.querySelector('.photo-card').addEventListener('click', e => {
    if (e.target.closest('.photo-tag') || e.target.closest('.artist-link') || e.target.closest('.photo-card-actions')) return;
    window.location.href = `/detail/${r.pixiv_id}`;
  });

  // Artist click
  const al = item.querySelector('.artist-link');
  if (al) al.addEventListener('click', e => {
    e.stopPropagation();
    $('#searchType').value = 'user';
    $('#searchQuery').value = al.dataset.uid;
    updateSearchUI();
    doSearch();
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
}

// ── Batch Download ──
let batchInProgress = false;
$('#downloadAllBtn').addEventListener('click', async () => {
  if (batchInProgress) return;
  const ids = Array.from($$('.photo-card')).map(c => parseInt(c.dataset.pixivId));
  if (!ids.length) return;
  batchInProgress = true;
  const btn = $('#downloadAllBtn'), st = $('#downloadAllStatus');
  btn.disabled = true;
  try {
    const r = await fetch('/api/download/batch', { method:'POST', headers:{'X-CSRF-Token':csrfToken,'Content-Type':'application/json'}, body:JSON.stringify({ids}) });
    const d = await r.json();
    if (r.ok) { st.textContent = d.message; btn.textContent='下载中...'; btn.className='btn btn-secondary btn-sm'; pollBatch(ids); }
    else { showToast(d.error||'失败', true); btn.disabled=false; batchInProgress=false; }
  } catch { showToast('网络错误', true); btn.disabled=false; batchInProgress=false; }
});

function pollBatch(ids) {
  const pending = new Set(ids); let n = 0;
  const iv = setInterval(async () => {
    n++;
    try {
      const r = await fetch(`/api/download/status/batch?ids=${[...pending].join(',')}`);
      const d = await r.json();
      for (const [pid, status] of Object.entries(d.statuses || {})) {
        const pidNum = parseInt(pid);
        if (status === 'done') { pending.delete(pidNum); updateDlDone(pidNum); }
        else if (status === 'failed') { pending.delete(pidNum); resetDlBtn(pidNum); }
      }
    } catch {}
    if (pending.size===0) { clearInterval(iv); $('#downloadAllStatus').textContent='全部完成'; batchInProgress=false; }
    else if (n>=150) { clearInterval(iv); $('#downloadAllStatus').textContent=`部分完成(${pending.size})`; batchInProgress=false; }
    else $('#downloadAllStatus').textContent = `剩余 ${pending.size} 个...`;
  }, 2000);
}

// ── Helpers ──
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
let loadingHintTimer = null;
function showLoading(on) {
  $('#loadingIndicator').style.display = on ? 'block' : 'none';
  $('#searchBtn').disabled = on;
  const hint = $('#loadingHint');
  if (loadingHintTimer) { clearTimeout(loadingHintTimer); loadingHintTimer = null; }
  if (on) {
    hint.style.display = 'none';
    loadingHintTimer = setTimeout(() => {
      if ($('#loadingIndicator').style.display !== 'none') hint.style.display = 'block';
    }, 5000);
  } else {
    hint.style.display = 'none';
  }
}

function maybeToastFetchStats(st) {
  if (st && st.detail_fetched > 0 && st.seconds > 5) {
    showToast(`已拉取 ${st.detail_fetched} 条详情（失败 ${st.detail_failed}），用时 ${Math.round(st.seconds)}s`);
  }
}

function loadR18Mode() {
  try {
    const saved = sessionStorage.getItem(R18_STATE_KEY);
    if (saved === 'safe' || saved === 'all') $('#r18Mode').value = saved;
  } catch {}
}

// ── Init ──
loadR18Mode();

$('#searchBtn').addEventListener('click', () => doSearch());
$('#searchQuery').addEventListener('keydown', e => { if (e.key==='Enter') doSearch(); });

// Check URL params (e.g., from detail page redirects)
const urlParams = new URLSearchParams(location.search);
if (urlParams.has('query')) {
  $('#searchQuery').value = urlParams.get('query');
  if (urlParams.has('type')) $('#searchType').value = urlParams.get('type');
  updateSearchUI();
  doSearch();
} else {
  restoreSearchState();
}
