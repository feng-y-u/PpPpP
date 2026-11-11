// ── 首页：最近 48 小时热门 + 缓存随机推荐 ──
// 第一屏只读本地库（GET /api/home）；滚到底后从预取缓存里随机抽高收藏作品
// （POST /api/home/more，带已展示 pid 去重）。整个页面不发任何 Pixiv 请求。
// 卡片是纯图：作者与名称在左下角信息牌里，默认隐藏（CSS 负责悬停/触屏常显）。

let homeItems = [];          // 已展示的全部作品数据（第一屏 + 随机批次），用于计数
let shownIds = [];           // 已展示的 pixiv_id：随机接口靠它跨屏去重
let randomMinBookmarks = 500; // 服务端下发的"高收藏"门槛，只用于文案
let feedObserver = null;
let feedLoading = false;
let feedDone = false;
let feedSentinel = null;

// ── 分列布局 ──
// 为什么不用 CSS 多列（column-count）：多列是"列优先 + 自动平衡"，每追加一批就会
// 重算断列点，已经看过的卡片被搬到别的列（用户反馈："后两列看着看着就刷新"）。
// 这里改成固定个数的独立列容器，新卡片只往"当前最矮的列"追加，已落位的卡片永不移动。
const MASONRY_BREAKPOINT = '(max-width: 992px)'; // 与旧 CSS 的 2 列断点一致
let homeColCount = 0;        // 当前列数（3 / 2），由视口宽度判定
let hotNodes = [];           // 第一屏节点（数据顺序；跨断点重排时复用，不重建 img）
let moreNodes = [];          // 随机推荐节点（数据顺序）
let bandHot = null;          // { root, cols } —— 第一屏那一段
let bandMore = null;         // { root, cols } —— 随机推荐那一段
let dividerEl = null;        // 分隔条节点（存在即已插入）
let resizeTimer = null;

// 页大小跟随服务端下发（与图库同一约定）；读不到才退回 24
const HOME_PAGE_SIZE = (() => {
  const raw = document.querySelector('meta[name="items-per-page"]')?.getAttribute('content') || '';
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : 24;
})();

function homeCsrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
}

function renderHomeCard(r) {
  // 瀑布流要中图：服务端已把原图地址转成 master1200 并代理成 /thumb/<b64>
  // （没原图地址的行退回 250px 缩略图，首页不为画质去拉详情）
  const imgSrc = r.medium_url || proxyThumb(r.thumb_url);
  const item = document.createElement('div');
  item.className = 'masonry-item';
  item.innerHTML = `
    <div class="photo-card" tabindex="0" data-pixiv-id="${r.pixiv_id}">
      <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='400' fill='%23f2e8ea'%3E%3C/svg%3E"
           data-src="${escAttr(imgSrc)}" loading="lazy" class="img-fade" alt="">
      <div class="card-meta">
        <div class="card-meta-title">${escHtml(r.title || '无题')}</div>
        <div class="card-meta-artist">${escHtml(r.user_name || '')}</div>
      </div>
    </div>`;

  const card = item.querySelector('.photo-card');
  // 点卡片 → 详情页（原图/中图候选链在详情页里）
  const open = () => { window.location.href = `/detail/${r.pixiv_id}`; };
  card.addEventListener('click', open);
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });
  return item;
}

function columnCountForViewport() {
  return window.matchMedia(MASONRY_BREAKPOINT).matches ? 2 : 3;
}

// 一段内容 = 一个 .masonry 容器 + N 个 .masonry-col
function createBand() {
  const root = document.createElement('div');
  root.className = 'masonry';
  const cols = [];
  for (let i = 0; i < homeColCount; i++) {
    const col = document.createElement('div');
    col.className = 'masonry-col';
    root.appendChild(col);
    cols.push(col);
  }
  return { root, cols, placed: 0 };
}

// 最短列优先：读实时列高决定落点。卡片高度各不相同（懒加载后才是真实比例），
// 按 i % 列数轮询会让列高越拉越偏。
// 但列高不可靠时必须退回按序号轮询：图片还是占位 SVG 时（未解码）所有列的
// offsetHeight 都是 0，`0 < 0` 恒为 false，首屏卡片会全部堆进第 1 列、右边整列空白
// （手机实测踩过，2026-11-11）。所以"高度全不可用"时按序号兜底。
function placeIntoColumns(band, node) {
  const heights = band.cols.map((col) => col.offsetHeight);
  const usable = heights.some((h) => h > 0);
  let idx = 0;
  if (usable) {
    for (let i = 1; i < heights.length; i++) {
      if (heights[i] < heights[idx]) idx = i;   // 空列高度为 0，天然优先
    }
  } else {
    idx = band.placed % band.cols.length;
  }
  band.placed += 1;
  band.cols[idx].appendChild(node);
  return node;
}

// 跨断点（3 列 ↔ 2 列）时整体重排：复用已有节点引用，img 的 src 早已落地，
// 浏览器直接用缓存渲染，不会重新下载。
function rebuildBands() {
  if (!bandHot) return;   // 第一屏还没渲染，没有可重排的内容
  // 正在往列容器里追加时不能整体重排：清空容器会让这批卡片落到游离节点上
  if (feedLoading) { resizeTimer = setTimeout(rebuildBands, 150); return; }
  const grid = $('#homeGrid');
  if (!grid) return;

  homeColCount = columnCountForViewport();
  grid.innerHTML = '';
  bandHot = createBand();
  grid.appendChild(bandHot.root);
  hotNodes.forEach((n) => placeIntoColumns(bandHot, n));

  bandMore = null;
  dividerEl = null;
  if (moreNodes.length) {
    insertDivider();
    bandMore = createBand();
    grid.appendChild(bandMore.root);
    moreNodes.forEach((n) => placeIntoColumns(bandMore, n));
  }
}

// 时间窗说明条：正常态说口径，降级态说"已放宽"，并给一个回到严格 48 小时的入口。
// 两种状态都要带一句"第 N 张起是随机推荐"——否则整页都会被误读成 48 小时热门。
function renderWindowNote(data) {
  const note = $('#windowNote');
  note.style.display = 'flex';
  note.classList.toggle('fallback', !!data.fell_back);
  const randomHint = `第 ${HOME_PAGE_SIZE} 张起为缓存随机推荐（收藏数 > ${randomMinBookmarks}）`;
  if (data.fell_back) {
    note.innerHTML = '<span class="dot"></span>最近 48 小时内的作品不够一屏，已放宽到最近 7 天'
      + `<span class="window-note-extra">· ${randomHint}</span>`
      + '<button class="window-note-action" id="strictWindowBtn">仍只看 48 小时</button>';
    $('#strictWindowBtn').addEventListener('click', () => loadHome('48'));
    return;
  }
  note.innerHTML = `<span class="dot"></span>时间窗：最近 ${data.window_hours} 小时（发布时间，日本时间）`
    + `<span class="window-note-extra">· ${randomHint}</span>`;
}

function updateHomeCount() {
  $('#homeCount').textContent = homeItems.length;
  $('#homeUpdated').textContent = '更新于 ' + new Date().toTimeString().slice(0, 5);
}

// 随机推荐分隔条：现在是 #homeGrid 里的普通块，天然整行（旧实现的 column-span 已随多列一起去掉）
function insertDivider() {
  if (dividerEl) return;
  dividerEl = document.createElement('div');
  dividerEl.className = 'feed-divider';
  dividerEl.textContent = `以下为缓存随机推荐（收藏数 > ${randomMinBookmarks}）`;
  $('#homeGrid').appendChild(dividerEl);
}

function finishFeed(message) {
  feedDone = true;
  if (feedObserver) { feedObserver.disconnect(); feedObserver = null; }
  const end = $('#homeFeedEnd');
  if (end) {
    end.textContent = message;
    end.style.display = 'block';
  }
}

// 哨兵挂在瀑布流容器外：多列/列段布局里塞一个 1px 空 div 会参与排布、被挪到别处
function startFeed() {
  if (feedObserver) { feedObserver.disconnect(); feedObserver = null; }
  feedDone = false;
  feedLoading = false;
  if (!feedSentinel) {
    feedSentinel = document.createElement('div');
    feedSentinel.id = 'homeFeedSentinel';
    feedSentinel.style.height = '1px';
    $('#homeGrid').after(feedSentinel);
  }
  feedObserver = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) maybeLoadMore();
  }, { rootMargin: '600px' });
  feedObserver.observe(feedSentinel);
}

function maybeLoadMore() {
  if (feedDone || feedLoading || !feedSentinel) return;
  const rect = feedSentinel.getBoundingClientRect();
  if (rect.top > window.innerHeight + 600) return;
  loadMore();
}

async function loadMore() {
  if (feedDone || feedLoading) return;
  feedLoading = true;
  try {
    const resp = await fetch('/api/home/more', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': homeCsrfToken() },
      body: JSON.stringify({ exclude: shownIds, limit: HOME_PAGE_SIZE }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const items = Array.isArray(data.results) ? data.results : [];
    if (!items.length) {
      finishFeed('缓存里暂时没有更多高收藏作品');
      return;
    }

    insertDivider();
    if (!bandMore) {
      bandMore = createBand();
      $('#homeGrid').appendChild(bandMore.root);
    }
    homeItems = homeItems.concat(items);
    items.forEach((r) => shownIds.push(Number(r.pixiv_id)));
    updateHomeCount();

    // renderInChunks 只负责分帧调用、不负责插入：卡片必须由这里挂进列容器
    await renderInChunks(items, (r) => {
      const node = renderHomeCard(r);
      moreNodes.push(node);
      return placeIntoColumns(bandMore, node);
    }, { chunk: 12, delay: 25 });
    lazyLoad();

    if (!data.has_more) finishFeed('已经到底了 · 以上为缓存里的全部高收藏作品');
  } catch {
    // 不自动重试：离线时每帧重试只会刷屏打请求，留给用户刷新
    showToast('加载更多失败', true);
    finishFeed('加载更多失败，刷新页面重试');
  } finally {
    feedLoading = false;
  }
  // 一屏没填满时哨兵还在视口里，不会再有 intersection 事件 —— 主动续一轮
  if (!feedDone) requestAnimationFrame(maybeLoadMore);
}

async function loadHome(windowMode) {
  const grid = $('#homeGrid');
  const loading = $('#homeLoading');
  const empty = $('#homeEmpty');
  const note = $('#windowNote');
  const end = $('#homeFeedEnd');

  if (feedObserver) { feedObserver.disconnect(); feedObserver = null; }
  feedDone = true;   // 重新加载期间禁止触发"加载更多"，避免往清空中的网格追加

  loading.style.display = 'block';
  grid.innerHTML = '';
  hotNodes = [];
  moreNodes = [];
  bandHot = null;
  bandMore = null;
  dividerEl = null;
  empty.style.display = 'none';
  note.style.display = 'none';
  end.style.display = 'none';
  end.textContent = '';

  let data;
  try {
    const resp = await fetch(`/api/home?window=${windowMode || 'auto'}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch {
    loading.style.display = 'none';
    showToast('首页加载失败，请刷新重试', true);
    return;
  }
  loading.style.display = 'none';

  randomMinBookmarks = data.random_min_bookmarks || randomMinBookmarks;
  homeItems = Array.isArray(data.results) ? data.results : [];
  shownIds = homeItems.map((r) => Number(r.pixiv_id));
  updateHomeCount();

  if (!homeItems.length) {
    // 空状态要如实说"需要预取/关注"，不能只说"暂无结果"
    empty.style.display = 'block';
    return;
  }

  renderWindowNote(data);

  // 列数在这里读：fetch 期间用户可能已经改过窗口大小
  homeColCount = columnCountForViewport();
  bandHot = createBand();
  grid.appendChild(bandHot.root);

  // renderInChunks 只负责分帧调用、不负责插入：卡片必须由这里挂进列容器
  // （曾经漏掉这一步 —— 计数与说明条都正常，网格永远是空的）
  await renderInChunks(homeItems, (r) => {
    const node = renderHomeCard(r);
    hotNodes.push(node);
    return placeIntoColumns(bandHot, node);
  }, { chunk: 12, delay: 25 });
  lazyLoad();
  startFeed();
}

// 只在真正跨断点时重排：同断点内列容器自适应宽度，不需要动 DOM（也就不会闪）
window.addEventListener('resize', () => {
  if (columnCountForViewport() === homeColCount) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(rebuildBands, 150);
});

// 初始加载：默认 auto（48 小时不足时自动放宽到 7 天）
loadHome('auto');
