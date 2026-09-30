const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

// ── 翻页状态 ──
let loadedPages = [];
let nextCursor = null;
let currentPage = 1;
let hasMore = false;
let currentSearchType = null;
// 搜索代数：每次发起新搜索 +1。搜索中途改条件重搜时，旧任务的轮询
// 通过比对代数静默失效（服务端同时会取消旧任务），旧结果不再渲染
let searchGeneration = 0;

// ── 临时预览（搜索进行中已确认的作品）──
// 它们**绝不能**写进 loadedPages：服务端的逐条发布发生在 safe_commit **之前**
//（见 routes_search._make_search_publisher），未定稿的行可能随事务回滚，
// 写进分页状态就等于把不确定的数据提交成"页"。预览只在网格里额外展示，
// done 时被 canonical 页整体替换，partial 保留，失败/取消时清空。
let searchPreview = [];
let lastSearchRevision = 0;        // 已处理到的最新快照版本号（revision 每次事件都自增）

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
  // 已提交页：导航控件恢复可见（首次搜索的预览期可能把它们藏起来过，见 setPaginationNavHidden）
  setPaginationNavHidden(false);

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

// 首次搜索期间分页栏被强制显示（只为承载 #paginationStatus），但此时没有任何已提交页：
// 空白页码区 + 两个禁用按钮纯属占位，藏掉它们让分页栏只剩状态文字。
// 隐藏方式刻意采用"记下内联 display、原样还原"而不是写死 none/''：模板里 #pageNumbers
// 是 display:flex（页码之间的 4px 间距），清空或写死成别的值都会让**已提交页**的分页栏
// 外观发生变化 —— 而已提交页必须和以前一模一样。
function setPaginationNavHidden(hidden) {
  ['#pageNumbers', '#prevPageBtn', '#nextPageBtn'].forEach(sel => {
    const el = $(sel);
    if (!el) return;
    if (hidden) {
      if (el.dataset.navDisplay === undefined) el.dataset.navDisplay = el.style.display;
      el.style.display = 'none';
    } else if (el.dataset.navDisplay !== undefined) {
      el.style.display = el.dataset.navDisplay;
      delete el.dataset.navDisplay;
    }
  });
}

function renderPage(pageNum) {
  const page = loadedPages[pageNum - 1];
  if (!page) return;
  const grid = $('#masonryGrid');
  grid.innerHTML = '';
  renderInChunks(page, (r) => {
    const node = renderCard(r);
    grid.appendChild(node);
    return node;
  }, { chunk: 12, delay: 25 }).then(() => {
    // 重渲染把网格清空了，预览卡也跟着没了 —— 补一次同步。预览是"运行中搜索"的实时信息，
    // 不该因为用户翻了一页就消失到下一次轮询（下一次轮询同样会补，但别让用户白等）。
    // 没有预览时 syncPreviewCards 是空转，canonical 路径行为不变。
    syncPreviewCards();
    lazyLoad();
  });
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
  // 翻页期间用户改条件重搜：doSearch 会升代数，本函数的轮询静默失效
  const gen = searchGeneration;
  // 本次翻页是新任务：上一次尝试（可能以 partial 收尾、预览还留在页面上）的
  // 预览状态必须清掉，否则旧预览会与本次任务的已确认结果混在一起。
  // 放在按钮置为"加载中"之前：resetPreviewUI 是**带副作用的复位**（会重排/收起分页栏），
  // 先调它、再设按钮态，否则这里的"加载中..."会被它覆盖掉
  resetPreviewUI();
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
    if (gen !== searchGeneration) return;  // 已被新搜索取代
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
    if (gen !== searchGeneration) return;
    // 异步任务：按钮状态由轮询回调恢复（done 时 renderPaginationBar，
    // 失败/404 时 restoreBtn），避免用旧 hasMore 提前恢复导致重复翻页
    pollSearch(data.task_id, (res) => {
      // done：canonical 页整体替换预览（不是合并），先清预览卡再提交本页
      resetPreviewUI();
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
    }, () => {
      // error / 404 / 取消：清空预览 + 恢复按钮（toast 与 showLoading(false) 由 pollSearch 处理）
      resetPreviewUI();
      restoreBtn();
    }, gen, handleSearchProgress, (res) => finishPartial(res, restoreBtn));
  } catch {
    if (gen === searchGeneration) { showToast('网络错误', true); restoreBtn(); }
  }
}

async function doSearch() {
  const type = $('#searchType').value;
  const query = $('#searchQuery').value.trim();
  const minBookmarks = parseInt($('#minBookmarks').value) || 0;
  if (type === 'user' && !query) { showToast('请输入画师ID'); return; }

  // 校验通过才升代数：升了代数，仍在轮询的旧任务就成了"上一代"，静默失效
  const gen = ++searchGeneration;
  const sort = $('#sortOrder').value || 'date_d';
  const tagMode = $('#tagMode').value || 'or';
  const r18Mode = $('#r18Mode').value;
  resetPagination();
  resetPreviewUI();  // 新一代搜索开始：上一代（可能是 partial 保留下来）的预览必须清掉
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
    if (gen !== searchGeneration) return;  // 期间又发起了新搜索，本次作废
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
    if (gen !== searchGeneration) return;
    if (type === 'following') {
      finishSearch(data);
      return;
    }
    pollSearch(
      data.task_id,
      (res) => {
        // done：预览整体**替换**为 canonical 页，绝不合并 —— 多页扫描可能接受多于
        // ITEMS_PER_PAGE 件，canonical 取扫描顺序前 N 件而预览是完成顺序，两边合法地
        // 可以不一致。必须先清预览卡片，否则 canonical 卡会追加在预览卡后面
        resetPreviewUI();
        finishSearch(res);
      },
      () => {
        // error / 404 / 取消：清空预览，否则失败的搜索会在页面上留下幽灵卡片
        //（toast 与 showLoading(false) 已由 pollSearch 处理）
        resetPreviewUI();
      },
      gen,
      handleSearchProgress,
      (res) => finishPartial(res)
    );
  } catch { if (gen === searchGeneration) { showToast('网络错误', true); showLoading(false); } }
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
  const grid = $('#masonryGrid');
  const results = loadedPages[0];
  renderInChunks(results, (r) => {
    const node = renderCard(r);
    grid.appendChild(node);
    return node;
  }, { chunk: 12, delay: 25 }).then(() => {
    lazyLoad();
  });
  renderPaginationBar();
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

// ── 预览态收口 ──
// 预览与分页状态完全隔离：全程不写 nextCursor / hasMore / currentPage / currentSearchType，
// 也不调 saveSearchState()（没有完整页可记，写进去会让刷新恢复出半页结果）。

// 注意这是**带副作用的复位**，不只是"删掉预览卡"：它还会重排或收起分页栏、复位翻页按钮与
// 状态文案。名字必须点出这一点，否则调用方很容易在"先把按钮置成加载中"之后才调它，
// 于是按钮态被悄悄覆盖（loadNextPage 就是靠调整调用顺序避开的，见那里的注释）。
function resetPreviewUI() {
  searchPreview = [];
  lastSearchRevision = 0;
  // 只删带 data-preview-pid 标记的预览卡：翻页中断时网格里还有已提交的页，
  // 整块清空会把它们一起抹掉
  $$('#masonryGrid [data-preview-pid]').forEach(n => n.remove());
  if (loadedPages.length) {
    // 已有已提交页：状态文案与按钮交回分页栏统一维护
    //（失败路径下按钮随后由调用方的 restoreBtn 恢复）
    renderPaginationBar();
  } else {
    // 首次搜索还没提交任何页：收起预览期间临时显示出来的分页栏并清掉临时文案
    $('#paginationBar').style.display = 'none';
    $('#prevPageBtn').disabled = true;
    $('#nextPageBtn').disabled = true;
    $('#paginationStatus').textContent = '';
  }
}

// 把预览卡补进网格：只追加"还没显示过"的（DOM 里已有的 + 已提交页里的都跳过）。
// 这个函数是**幂等**的：集合没变时 fresh 为空，等于一次廉价空转，不会重复渲染。
// 因此调用方可以（也必须）在每次轮询都无脑调它 —— 它自己负责比对，调用方不要在外面
// 加"集合没变就跳过"的门禁：跳页/重渲染会整块清空网格，那种门禁会让清空后的预览
// 卡一直不回来，直到某个**新** PID 到达（旧实现在此处踩过这个坑）。
function syncPreviewCards() {
  const shown = new Set();
  $$('#masonryGrid [data-preview-pid]').forEach(n => {
    const pid = parseInt(n.dataset.previewPid, 10);
    if (!isNaN(pid)) shown.add(pid);
  });
  const committed = new Set(loadedPages.flat().map(r => r.pixiv_id));
  const fresh = searchPreview.filter(r => !shown.has(r.pixiv_id) && !committed.has(r.pixiv_id));
  if (!fresh.length) return;
  renderInChunks(fresh, (r) => {
    // 预览卡走独立的渲染分支：不打下载/跳详情的行为（见 renderCard 的 opts.preview）
    const node = renderCard(r, { preview: true });
    node.dataset.previewPid = String(r.pixiv_id);
    return node;
  }, { chunk: 12, delay: 25 }).then(() => lazyLoad());
}

function updatePreviewStatus(progress) {
  const accepted = progress.accepted ?? 0;
  const examined = progress.examined ?? 0;
  // examined 只能当活动计数器：它在任何详情完成之前就整批发出，"N/N examined"
  // 会瞬间到 100% 而 accepted 仍是 0 —— 绝不能渲染成完成比例。
  // accepted 不封顶，可以大于网格里的预览条数（预览被服务端截到一页），所以
  // "已找到 N 件"取 accepted，网格渲染取 results，不能假定两者一致。
  $('#paginationStatus').textContent = `已找到 ${accepted} 件，仍在筛选（已检查 ${examined} 条）`;
  // paginationBar 平时只在有已提交页时才显示（由 renderPaginationBar 决定）。首次
  // 搜索期间 loadedPages 为空 —— 这里必须把状态栏显出来，否则"已找到 N 件"没地方看。
  // 但此时没有任何已提交页：页码区必然是空的、翻页按钮必然无处可去，留着只是白占一行，
  // 所以把导航控件一起藏掉，让分页栏只承载状态文案（renderPaginationBar 会恢复它们）。
  // 翻页中断时状态栏本就可见，且按钮状态由 loadNextPage 自己维护，不能覆盖。
  if (!loadedPages.length) {
    $('#paginationBar').style.display = 'block';
    $('#prevPageBtn').disabled = true;
    $('#nextPageBtn').disabled = true;  // 首次搜索还没有游标，翻页必然无处可去
    setPaginationNavHidden(true);
  }
}

// running 响应的预览处理器（done 之前的每次轮询都会走这里）。
// **每次轮询都无条件重跑 syncPreviewCards**：跳页/分页导航会调 renderPage 整块清空网格，
// 而"预览 PID 集合没变就跳过同步"这种门禁会让清空后的预览一直不回来 —— 集合没变，
// 门禁不放行，用户只能等某个新 PID 到达（审查已用假 DOM 复现）。幂等性与去重都在
// syncPreviewCards 里（它按 DOM + 已提交页算差集），所以这里不需要再看集合是否变化。
// revision 仍然只用于节流状态文案：它每次事件都自增（连 examined 计数也推进），
// 拿它当渲染判据会在 examined 突发时白重渲染一轮。
function handleSearchProgress(data) {
  const results = Array.isArray(data.results) ? data.results : [];
  const ids = new Set();
  const preview = [];
  for (const r of results) {
    const pid = r?.pixiv_id;
    if (pid === undefined || pid === null || ids.has(pid)) continue;
    ids.add(pid);
    preview.push(r);
  }
  searchPreview = preview;
  if (data.revision !== lastSearchRevision) {
    lastSearchRevision = data.revision;
    updatePreviewStatus(data.progress || {});
  }
  syncPreviewCards();
}

// partial 收口：详情请求被限流闸截断，本页没搜完，但已确认的预览对用户仍是有效信息，
// 所以保留展示；关键语义是**不**把它提交成"页" —— loadedPages / nextCursor /
// hasMore / currentPage 一律不动，也不写前端搜索缓存。
// restoreBtn：翻页中断时恢复按钮，让用户冷却后按**同一个游标**重试；首次搜索本来
// 就没有游标，下一页保持禁用。
// 只走状态行这一条通道（warning 已经写在里面）：不再额外弹 toast，同一件事报两遍是噪声。
function finishPartial(data, restoreBtn) {
  showLoading(false);
  const accepted = data.progress?.accepted ?? searchPreview.length;
  const warning = data.warning || '本页结果不完整，请稍后重试';
  $('#paginationStatus').textContent = accepted ? `${warning}（已找到 ${accepted} 件）` : warning;
  if (!loadedPages.length) {
    $('#paginationBar').style.display = 'block';  // 首次搜索时状态栏平时是隐藏的
    // 同上：没有任何已提交页，页码区与翻页按钮都是空摆设，藏掉让分页栏只剩状态文字
    setPaginationNavHidden(true);
  }
  if (restoreBtn) restoreBtn();
  else $('#nextPageBtn').disabled = true;
}

function pollSearch(taskId, onDone, onFail, gen, onProgress, onPartial) {
  // gen：发起本次轮询的搜索代数。新搜索会升代数 —— 旧任务的轮询发现代数
  // 不匹配就静默退出，不弹错误、不碰 UI（新搜索正在接管界面）
  const stale = () => gen !== undefined && gen !== searchGeneration;
  fetch(`/api/search/status/${taskId}`)
    .then(async resp => {
      if (stale()) return;
      if (resp.status === 404) {
        showToast('搜索任务已失效，请重新搜索', true);
        showLoading(false);
        if (onFail) onFail();
        return;
      }
      const data = await resp.json();
      if (stale()) return;
      if (data.status === 'running') {
        // 增量预览只追加不提交：onProgress 里不碰 loadedPages / 游标 / 分页状态
        if (onProgress) onProgress(data);
        setTimeout(() => pollSearch(taskId, onDone, onFail, gen, onProgress, onPartial), 2000);
        return;
      }
      if (data.status === 'cancelled') {
        // 服务端取消了本任务（正常只发生在被新搜索取代时 —— 那种情况代数
        // 已经不匹配、上面就 return 了）。能走到这里说明当前搜索被取消但
        // 没有新搜索接管，按失败处理并恢复按钮
        showToast('搜索已取消，请重新搜索', true);
        showLoading(false);
        if (onFail) onFail();
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
      if (data.status === 'partial') {
        // partial：本页被限流闸截断，**绝不能**落进 finishSearch —— 那会把预览当成
        // 完整页提交、写进分页状态，还会吃掉 warning。先按 running 处理把最新的
        // 已确认结果渲染出来，再单独收口（保留预览、停 loading、报 warning、不碰游标）
        if (onProgress) onProgress(data);
        if (onPartial) onPartial(data);
        // 两个调用方都传了 onPartial；万一将来漏传，这里只留一条可观测的告警，
        // 绝不落到"把 partial 当失败"的兜底：那会 showLoading(false) + onFail()，
        // 而 onFail 会清掉刚由 onProgress 渲染出来的预览，把"不完整页"误报成"失败"。
        else console.warn('pollSearch: partial 响应缺少 onPartial 处理器，预览保持原样');
        return;
      }
      onDone(data);
    })
    .catch(() => { if (!stale()) { showToast('网络错误', true); showLoading(false); if (onFail) onFail(); } });
}

// ── UI Toggle ──
function updateSearchUI() {
  const type = $('#searchType').value;
  const isTag = type === 'tag';
  $('#searchQuery').placeholder = type === 'following' ? '' : isTag ? '多个标签用逗号分隔（中英逗号均可）' : '输入画师UID...';

  // 筛选行是「标签 + 控件」绑成的盒子（.filter-box）：显隐必须切**整个盒子**。
  // 只切盒内的 label / select / input 会留下一个空边框（2026-09-23 改）。
  const show = type !== 'following';
  // 关注模式隐藏排序与收藏数下限；R18 过滤三模式通用（关注直接复用搜索的 R18 选项）
  ['#fbSort', '#fbMinBookmarks'].forEach(sel => {
    const el = $(sel);
    if (el) el.style.display = show ? '' : 'none';
  });
  // 组合方式只对标签搜索有意义：画师与关注都不适用，整盒一起收起
  const tagModeBox = $('#fbTagMode');
  if (tagModeBox) tagModeBox.style.display = isTag ? '' : 'none';
  // 页签 active 态同步
  $$('#searchTypeTabs .st-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.type === $('#searchType').value);
  });
}
$('#searchType').addEventListener('change', updateSearchUI);

$('#r18Mode').addEventListener('change', () => {
  sessionStorage.setItem(R18_STATE_KEY, $('#r18Mode').value);
  doSearch();
});

// ── Render Card ──
// opts.preview：预览卡（搜索中已确认、但服务端**还没 safe_commit** 的作品，见 routes_search
// 的 _make_search_publisher）。这一步之差有用户可见的后果：该 pid 的行可能随后随事务回滚，
// 此时 /detail/<pid> 会 abort(404)、下载接口返回 404 作品不存在（routes_download）——
// 作者搜索一页要 1.33s/件，最长几十秒都能点到这种"还不存在的作品"。
// 选择的组合（最小且稳妥）：
//   ① 渲染层不打行为：预览卡不渲染下载按钮、卡片点击只弹"仍在筛选中"，不跳详情；
//   ② 捕获阶段兜底：即使别的路径（app.js 的 updateDlDone / resetDlBtn 会按 pid 重写
//      .photo-card-actions）往预览卡塞回下载按钮，点击也到不了它自己的监听器；
//   ③ 批量下载按预览标记剔除（见 #downloadAllBtn），不提交未定稿的 PID。
// 不传 opts 时（canonical 路径）行为与从前逐字一致 —— 已提交卡照旧下载、跳详情、开灯箱。
function renderCard(r, opts) {
  const preview = !!(opts && opts.preview);
  const isDone = r.download_status === 'done';
  const isDl = r.download_status === 'downloading';
  let btnHtml;
  if (preview) btnHtml = '<span class="preview-hint" style="font-size:0.68rem;color:var(--text-muted);">筛选中…</span>';
  else if (isDl) btnHtml = '<span style="font-size:0.68rem;color:var(--text-muted);">下载中...</span>';
  else if (isDone) btnHtml = `<button class="btn btn-dl-done btn-sm dl-file-btn" data-pid="${r.pixiv_id}">下载</button>`;
  else btnHtml = `<button class="btn btn-soft btn-sm dl-btn" data-pid="${r.pixiv_id}">下载</button>`;

  const badges = [];
  // 预览卡多一枚"筛选中"徽标 + 虚线描边（内联，样式表不动）：用户必须能一眼分辨
  // "这是刚刚筛出来的预览"与"这是已提交、可以放心点开的结果"
  if (preview) badges.push('<span class="photo-badge" style="background:rgba(226,87,126,.9);color:#fff;">筛选中</span>');
  badges.push(`<span class="photo-badge">♥ ${fmtNum(r.bookmark_count)}</span>`);
  if (r.page_count > 1) badges.push(`<span class="photo-badge">${r.page_count}P</span>`);
  if (isDone) badges.push(`<span class="photo-badge" style="background:rgba(59,138,94,.85);color:#fff;">已下载</span>`);
  const cardStyle = preview ? ' style="outline:2px dashed var(--accent); outline-offset:-2px;"' : '';

  const tags = (r.tags||[]).slice(0,6).map(t =>
    `<span class="photo-tag" data-tag="${escAttr(t)}">${escHtml(t)}<span class="tag-block-x" data-block="${escAttr(t)}">&times;</span></span>`).join('');

  const item = document.createElement('div');
  item.className = preview ? 'masonry-item preview-card' : 'masonry-item';
  item.innerHTML = `
    <div class="photo-card"${cardStyle} data-pixiv-id="${r.pixiv_id}">
      <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='400' fill='%23ecece7'%3E%3C/svg%3E"
           data-src="${escAttr(proxyThumb(r.thumb_url))}" loading="lazy" class="img-fade" alt="">
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
  item.querySelector('.photo-card').addEventListener('click', (e) => {
    if (e.target.closest('.photo-tag') || e.target.closest('.artist-link') || e.target.closest('.photo-card-actions')) return;
    // 预览卡不跳详情：行可能还没提交，/detail/<pid> 此时是 404。给个短提示优于静默无反应。
    if (preview) { showToast('该作品仍在筛选中'); return; }
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

  return item;
}

// 兜底不变量：预览卡不能触发下载。renderCard 的 preview 分支根本不渲染下载按钮，
// 但 app.js 的 updateDlDone / resetDlBtn 会按 pid 重写 .photo-card-actions 的 innerHTML ——
// 将来任何新路径都可能往预览卡里塞回一个可点的下载按钮。捕获阶段拦下，按钮自身的
// 监听器（含将来新加的）就不会执行，这条保证不依赖"渲染时不放按钮"这个巧合。
document.addEventListener('click', (e) => {
  const btn = e.target.closest?.('.dl-btn, .dl-file-btn');
  if (!btn || !btn.closest('[data-preview-pid]')) return;
  e.stopPropagation();
  e.preventDefault();
  showToast('该作品仍在筛选中');
}, true);

// ── Batch Download ──
let batchInProgress = false;
$('#downloadAllBtn').addEventListener('click', async () => {
  if (batchInProgress) return;
  // 排除预览卡（data-preview-pid）：它们指向尚未 safe_commit 的行，批量接口此时查不到、
  // 提交未定稿的 PID 毫无意义（还可能随事务回滚）。已提交卡片不受影响。
  const ids = Array.from($$('.photo-card'))
    .filter(c => !c.closest('[data-preview-pid]'))
    .map(c => parseInt(c.dataset.pixivId));
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
let loadingHintTimer = null;
function showLoading(on) {
  $('#loadingIndicator').style.display = on ? 'block' : 'none';
  // 搜索按钮保持可点：搜索中改条件再点搜索 = 取消旧任务、按新条件重搜
  // （服务端提交新任务时自动取消在途任务，见 _submit_search_task）
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
updateSearchUI();  // 首次加载默认高亮「标签」页签（无缓存/无 URL 参数时）

// 搜索类型页签
$('#searchTypeTabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.st-tab');
  if (!tab) return;
  $('#searchType').value = tab.dataset.type;
  updateSearchUI();
});

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
