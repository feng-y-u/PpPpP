// ── X 图片页：标签抓取的浏览 / 下载 / 同作品判定 ──
// 依赖 app.js 的 $ / escHtml / showToast。所有图片经 /ximg 代理（CSP img-src 'self'）。
const xCsrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
const X_PAGE_SIZE = parseInt(
  document.querySelector('meta[name="items-per-page"]')?.getAttribute('content') || '', 10) || 24;

let xActiveTag = '';
let xDupMode = 'all';        // all / hide（藏已确认） / only（只看疑似+已确认）
let xSavedMode = 'all';       // all / done（只看已下载）
let xOffset = 0;
let xTotal = 0;
let xLoading = false;

function xUrl(b64) {
  return '/ximg/' + b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function xProxy(url) {
  if (!url) return '';
  try { return xUrl(btoa(url)); } catch { return ''; }
}

async function xApi(path, options) {
  const resp = await fetch(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function xPost(path, body) {
  return xApi(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': xCsrf },
    body: JSON.stringify(body || {}),
  });
}

// ── 状态条 ──
async function loadXStatus() {
  try {
    const s = await xApi('/api/x/status');
    const bits = [];
    const st = s.state || {};
    const interval = Number(st.interval) || 0;
    // 文案与后端循环的判定同口径（enabled && interval > 0 才抓）：
    // "每 0s" 会让用户以为在抓，实际循环空转
    if (st.enabled && interval > 0) bits.push(`抓取已开启（每 ${interval}s）`);
    else if (st.enabled) bits.push('<span class="warn">已开启但间隔为 0（不会抓取）</span>');
    else bits.push('抓取已关闭');
    if (st.last_check) {
      const d = new Date(st.last_check);
      bits.push('上次：' + (isNaN(d.getTime()) ? st.last_check : d.toLocaleString()));
    }
    if (st.last_error) bits.push(`<span class="warn">最近错误：${escHtml(st.last_error)}</span>`);
    if (!s.cookie_configured) bits.push('<span class="warn">未配置 X Cookie（设置页填写）</span>');
    if (!s.query_id_configured) bits.push('<span class="warn">未配置 query_id（instance/x_api.json）</span>');
    if (!s.transaction_available) bits.push('<span class="warn">未安装 XClientTransaction：搜索会被 404 拒绝</span>');
    if (!s.hash_available) bits.push('未安装 Pillow：哈希去重不可用');
    $('#xStatus').innerHTML = bits.join(' · ');
    $('#xCount').textContent = `${s.works} 条作品 / ${s.tags} 个标签`;
  } catch (e) {
    $('#xStatus').textContent = '状态获取失败：' + e.message;
  }
}

// ── 标签 ──
async function loadXTags() {
  const tags = await xApi('/api/x/tags').catch(() => []);
  const bar = $('#xTagBar');
  bar.innerHTML = '';
  const all = document.createElement('span');
  all.className = 'x-tag' + (xActiveTag === '' ? ' active' : '');
  all.textContent = '全部';
  all.addEventListener('click', () => { xActiveTag = ''; reloadXItems(); });
  bar.appendChild(all);
  tags.forEach(t => {
    const chip = document.createElement('span');
    chip.className = 'x-tag' + (xActiveTag === t.tag ? ' active' : '');
    chip.dataset.tag = t.tag;
    chip.title = t.error || `上次抓取：${t.last_fetch_at || '从未'}`;
    const live = _xLiveText(t.progress);
    chip.innerHTML = `${escHtml(t.tag)} <span class="live">${live}</span>` +
      `<span class="ref" title="立即抓取这个标签">↻</span>` +
      `<span class="del" title="删除标签">×</span>` +
      (t.error ? ' <span class="err">!</span>' : '');
    if (live) chip.classList.add('fetching');
    chip.addEventListener('click', (e) => {
      if (e.target.closest('.del')) {
        deleteXTag(t.tag);
        return;
      }
      if (e.target.closest('.ref')) {
        refreshXTag(t.tag);
        return;
      }
      xActiveTag = t.tag;
      reloadXItems();
    });
    bar.appendChild(chip);
  });
  if (tags.some(t => (t.progress || {}).status === 'fetching' || t.status === 'fetching')) {
    startXTagPolling();
  }
}

// 抓取进度的短文案：与原生搜索页"已加载 N/预计 M"同款信息密度
function _xLiveText(p) {
  if (!p || !p.status) return '';
  if (p.status === 'fetching') {
    const pages = p.pages_total ? `${p.pages_done || 0}/${p.pages_total}` : String(p.pages_done || 0);
    return `抓取中 ${pages} 页 · ${p.found || 0} 条`;
  }
  if (p.status === 'error') return '上次失败';
  return '';
}

// 标签轮询：抓取期间只更新 chip 上的 live 文案，不整条重渲染（避免闪烁）；
// 有标签从 fetching 变 done 时刷新列表（新图到了，用户点完刷新该看到东西长出来）
let xTagPollTimer = null;
function startXTagPolling() {
  if (xTagPollTimer) return;
  xTagPollTimer = setInterval(async () => {
    let anyFetching = false;
    let completed = false;
    try {
      const tags = await xApi('/api/x/tags');
      tags.forEach(t => {
        const chip = document.querySelector(`.x-tag[data-tag="${CSS.escape(t.tag)}"]`);
        if (!chip) return;
        const p = t.progress || {};
        const live = chip.querySelector('.live');
        const wasFetching = chip.classList.contains('fetching');
        if (p.status === 'fetching') {
          anyFetching = true;
          chip.classList.add('fetching');
          if (live) live.textContent = _xLiveText(p);
        } else if (wasFetching) {
          // fetching → done/error 的收口：摘掉转圈态，done 触发一次列表刷新
          chip.classList.remove('fetching');
          if (live) live.textContent = p.status === 'error' ? '上次失败' : '';
          if (p.status === 'done') completed = true;
        }
      });
    } catch { /* 轮询失败下轮再试 */ }
    if (!anyFetching) {
      clearInterval(xTagPollTimer);
      xTagPollTimer = null;
      if (completed) {
        showToast('抓取完成，列表已更新');
        reloadXItems();
      }
    }
  }, 2500);
}

async function refreshXTag(tag) {
  try {
    await xPost('/api/x/tags/' + encodeURIComponent(tag) + '/refresh');
    // 立刻把 chip 置成"抓取中"并开始轮询：用户需要马上看到动作被接受了
    const chip = document.querySelector(`.x-tag[data-tag="${CSS.escape(tag)}"] .live`);
    if (chip) chip.textContent = '抓取中…';
    startXTagPolling();
  } catch (e) {
    showToast('刷新失败：' + e.message, true);
  }
}

async function addXTag() {
  const input = $('#xTagInput');
  const addBtn = $('#xTagAddBtn');
  const tag = (input.value || '').trim();
  if (!tag || input.disabled) return;
  input.disabled = true;
  addBtn.disabled = true;
  try {
    const data = await xPost('/api/x/tags', { tag });
    input.value = '';
    // triggered=false 时这轮不会真的抓（总开关/间隔没开）：不能承诺"正在抓取"
    showToast(data.triggered ? '已添加，正在后台抓取' : '已添加（X 抓取未启用，去设置页开启）');
    xActiveTag = tag;
    await loadXTags();
    if (data.triggered) startXTagPolling();
    reloadXItems();
  } catch (e) {
    showToast('添加失败：' + e.message, true);
  } finally {
    input.disabled = false;
    addBtn.disabled = false;
  }
}

async function deleteXTag(tag) {
  const ok = await pvConfirm(`删除标签「${tag}」？`,
    { detail: '它抓到的作品记录会一起删除；已下载的图片文件保留在磁盘上。', okText: '删除' });
  if (!ok) return;
  try {
    await xApi('/api/x/tags/' + encodeURIComponent(tag), {
      method: 'DELETE', headers: { 'X-CSRF-Token': xCsrf },
    });
    if (xActiveTag === tag) xActiveTag = '';
    showToast('已删除');
    await loadXTags();
    reloadXItems();
  } catch (e) {
    showToast('删除失败：' + e.message, true);
  }
}

// ── 列表 ──
// 卡片结构与缓存浏览页（page-cache.js renderCard）同构：同一个 .photo-card
// 组件、同一套徽标/操作区。X 差异只在：标题行是作者而非作品名、多一条推文
// 文本、操作区多一枚「原推」。一致性改造 2026-11-20 —— 此前 X 用的是
// Bootstrap row/col + 自有 .x-card，与全站瀑布流断崖式不一致。
function renderXCard(item) {
  const wrap = document.createElement('div');
  wrap.className = 'masonry-item';
  const media = (item.media || [])[0] || {};
  const img = xProxy(media.url);

  const badges = [];
  if (item.media_count > 1) badges.push(`<span class="photo-badge">${item.media_count}P</span>`);
  if (item.dup_status === 'confirmed') {
    badges.push(`<span class="photo-badge ok">同一作品${item.dup_pixiv_id ? ' #' + item.dup_pixiv_id : ''}</span>`);
  } else if (item.dup_status === 'suspected') {
    badges.push('<span class="photo-badge warn">疑似重复</span>');
  }
  if (item.saved_status === 'done') badges.push('<span class="photo-badge ok">已下载</span>');
  else if (item.saved_status === 'downloading' || item.saved_status === 'queued') {
    badges.push('<span class="photo-badge">下载中</span>');
  } else if (item.saved_status === 'failed') badges.push('<span class="photo-badge warn">下载失败</span>');

  const pixivLink = item.dup_pixiv_id
    ? `<a class="btn btn-sm btn-soft" href="/detail/${item.dup_pixiv_id}">看 Pixiv</a>` : '';
  const dupActions = item.dup_status === 'suspected'
    ? `<button class="btn btn-sm btn-outline-warning" data-dup="confirm">确认同一作品</button>
       <button class="btn btn-sm btn-outline-secondary" data-dup="ignore">不是</button>` : '';
  // 下载区按 saved_status 全枚举：queued/downloading 期间按钮必须不可点，
  // 否则任何重渲染（别的下载完成、dup 操作、切标签）都会把"下载中"的按钮
  // 复活成可点状态 → 后端 409 兜底之前的重复提交窗口
  let downloadBtn;
  if (item.saved_status === 'done') {
    downloadBtn = '<span class="btn btn-sm btn-dl-done">已下载</span>';
  } else if (item.saved_status === 'downloading' || item.saved_status === 'queued') {
    downloadBtn = '<span class="btn btn-sm btn-dl-done">下载中…</span>';
  } else {
    downloadBtn = `<button class="btn btn-sm btn-soft" data-dl>` +
      (item.saved_status === 'failed' ? '重试下载' : `下载${item.media_count > 1 ? `（${item.media_count} 张）` : ''}`) +
      '</button>';
  }

  const author = item.author_name || '未知作者';
  const handle = item.author_screen_name ? ` @${item.author_screen_name}` : '';
  const when = item.tweeted_at ? new Date(item.tweeted_at).toLocaleDateString('zh-CN') : '';

  wrap.innerHTML = `
    <div class="photo-card" data-id="${item.id}">
      ${img ? `<img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='400' fill='%23ecece7'%3E%3C/svg%3E"
           data-src="${escAttr(img)}" loading="lazy" class="img-fade" alt="">`
           : '<div class="img-placeholder" style="aspect-ratio:4/5;"><span>无图</span></div>'}
      <div class="photo-badges">${badges.join('')}</div>
      <div class="photo-card-info">
        <div class="photo-card-title" title="${escAttr(author + handle)}">${escHtml(author)}${escHtml(handle)}</div>
        <div class="photo-card-artist">${escHtml(when)} · 来自标签 ${escHtml(item.tag || '')}</div>
        <div class="photo-card-text">${escHtml((item.text || '').slice(0, 160))}</div>
        <div class="photo-card-actions">
          ${downloadBtn}
          <a class="btn btn-sm btn-soft" target="_blank" rel="noopener noreferrer"
             href="https://x.com/${encodeURIComponent(item.author_screen_name || 'i')}/status/${encodeURIComponent(item.tweet_id)}">原推</a>
          ${pixivLink}
          ${dupActions}
          <button class="btn btn-sm btn-outline-danger btn-del" data-del title="删除这条记录">×</button>
        </div>
      </div>
    </div>`;

  const card = wrap.querySelector('.photo-card');
  // 与全站同动线：点卡片进详情页（/x/view 下载前后都能看）
  card.addEventListener('click', (e) => {
    if (e.target.closest('.photo-card-actions') || e.target.closest('a')) return;
    window.location.href = `/x/view/${item.id}`;
  });
  const dl = card.querySelector('[data-dl]');
  if (dl) dl.addEventListener('click', () => downloadXWork(item.id, dl));
  const del = card.querySelector('[data-del]');
  if (del) del.addEventListener('click', () => deleteXWork(item.id));
  card.querySelectorAll('[data-dup]').forEach(btn => {
    btn.addEventListener('click', () => markXDup(item.id, btn.dataset.dup));
  });
  return wrap;
}

async function loadXItems(reset) {
  if (xLoading) return;
  xLoading = true;
  if (reset) { xOffset = 0; $('#xGrid').innerHTML = ''; }
  try {
    const params = new URLSearchParams({ offset: xOffset, limit: X_PAGE_SIZE, dup: xDupMode });
    if (xSavedMode !== 'all') params.set('saved', xSavedMode);
    if (xActiveTag) params.set('tag', xActiveTag);
    const data = await xApi('/api/x/items?' + params.toString());
    xTotal = data.total || 0;
    (data.data || []).forEach(item => $('#xGrid').appendChild(renderXCard(item)));
    xOffset += (data.data || []).length;
    lazyLoad();
    // 空态要区分"从没抓过"和"这个筛选下没有"（后者给换筛选的指引）
    const empty = $('#xEmpty');
    if (xTotal === 0) {
      const filtered = (xDupMode !== 'all' || xSavedMode !== 'all' || xActiveTag);
      empty.textContent = filtered
        ? '这个筛选下没有作品，试试切回「全部」。'
        : '还没有抓取过。先在上面添加一个标签。';
      empty.style.display = 'block';
    } else {
      empty.style.display = 'none';
    }
    $('#xMoreBtn').style.display = (xOffset < xTotal) ? 'inline-block' : 'none';
    $('#xPageNote').textContent = xTotal ? `已显示 ${xOffset} / ${xTotal} 条` : '';
  } catch (e) {
    showToast('列表加载失败：' + e.message, true);
  } finally {
    xLoading = false;
  }
}

function reloadXItems() {
  loadXItems(true);
  loadXTags();
  loadXStatus();
}

// 单卡原地更新：下载完成 / dup 确认后只换这一张卡，不整页 reload ——
// 整页 reload 会把 xOffset 清零，翻页翻得深的用户被弹回第一屏
async function refreshXCard(id) {
  try {
    const row = await xApi(`/api/x/works/${id}`);
    const old = document.querySelector(`.photo-card[data-id="${id}"]`);
    if (old) old.parentElement.replaceWith(renderXCard(row));
    lazyLoad();
  } catch { /* 卡片被过滤/删除时静默跳过 */ }
}

async function downloadXWork(id, btn) {
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = '已加入队列';
  try {
    await xPost(`/api/x/works/${id}/download`);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '下载';
    showToast('下载失败：' + e.message, true);
    return;
  }
  // 轮询：30 张图 × 60s 读超时的最坏情况远超 3 分钟，封顶要够长；
  // 连续网络错误也要终止轮询 —— 否则按钮永远停在"下载中…"没人管
  let tries = 0;
  let misses = 0;
  const timer = setInterval(async () => {
    tries += 1;
    try {
      const row = await xApi(`/api/x/works/${id}`);
      misses = 0;
      if (row.saved_status === 'done' || row.saved_status === 'failed') {
        clearInterval(timer);
        await refreshXCard(id);
        showToast(row.saved_status === 'done' ? '下载完成' : '下载失败', row.saved_status !== 'done');
      }
    } catch { misses += 1; }
    if (misses >= 5 || tries > 200) {
      clearInterval(timer);
      showToast('下载仍在进行或状态未知，稍后点「刷新列表」查看', true);
    }
  }, 3000);
}

async function deleteXWork(id) {
  const ok = await pvConfirm('删除这条 X 作品记录？',
    { detail: '只删记录，已下载的图片文件保留在磁盘上。', okText: '删除' });
  if (!ok) return;
  try {
    await xApi(`/api/x/works/${id}`, {
      method: 'DELETE', headers: { 'X-CSRF-Token': xCsrf },
    });
    const card = document.querySelector(`.photo-card[data-id="${id}"]`);
    if (card) card.parentElement.remove();
    xTotal = Math.max(0, xTotal - 1);
    $('#xEmpty').style.display = (xTotal === 0) ? 'block' : 'none';
    showToast('已删除');
  } catch (e) {
    showToast('删除失败：' + e.message, true);
  }
}

async function markXDup(id, action) {
  try {
    await xPost(`/api/x/works/${id}/dup`, { action });
    showToast(action === 'confirm' ? '已记为同一作品' : '已忽略，不再提示');
    await refreshXCard(id);
  } catch (e) {
    showToast('操作失败：' + e.message, true);
  }
}

// ── 画师映射（H1 疑似判定的前提数据）──
async function loadXLinks() {
  const links = await xApi('/api/x/artist-links').catch(() => []);
  const list = $('#xLinkList');
  if (!list) return;
  list.innerHTML = links.length
    ? links.map(l =>
        `<span class="x-tag" data-uid="${l.pixiv_user_id}" title="点击移除这条映射">` +
        `Pixiv #${l.pixiv_user_id} ↔ @${escHtml(l.x_screen_name)} <span class="del">×</span></span>`).join('')
    : '<span class="x-sub">还没有映射。H1「疑似同一作品」只对有映射的画师生效。</span>';
  list.querySelectorAll('[data-uid]').forEach(chip => {
    chip.addEventListener('click', async (e) => {
      if (!e.target.closest('.del')) return;
      const uid = chip.dataset.uid;
      try {
        await xApi(`/api/x/artist-links/${uid}`, {
          method: 'DELETE', headers: { 'X-CSRF-Token': xCsrf },
        });
        showToast('已移除映射');
        loadXLinks();
      } catch (err) {
        showToast('移除失败：' + err.message, true);
      }
    });
  });
}

async function addXLink() {
  const uid = ($('#xLinkUid')?.value || '').trim();
  const handle = ($('#xLinkHandle')?.value || '').trim();
  const name = (handle || '').replace(/^@+/, '');
  if (!/^\d+$/.test(uid) || !name) {
    showToast('画师映射需要 Pixiv 数字 UID 和 X 用户名', true);
    return;
  }
  try {
    await xPost('/api/x/artist-links', { pixiv_user_id: uid, x_screen_name: name });
    $('#xLinkUid').value = '';
    $('#xLinkHandle').value = '';
    showToast('已保存映射');
    loadXLinks();
  } catch (e) {
    showToast('保存失败：' + e.message, true);
  }
}

// ── 绑定 ──
$('#xTagAddBtn').addEventListener('click', addXTag);
$('#xTagInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') addXTag(); });
$('#xMoreBtn').addEventListener('click', () => loadXItems(false));
$('#xRefreshBtn').addEventListener('click', reloadXItems);
$('#xLinkAddBtn')?.addEventListener('click', addXLink);
$('#xDupFilter').addEventListener('click', (e) => {
  const btn = e.currentTarget;
  const order = ['all', 'hide', 'only'];
  const labels = { all: '显示：全部', hide: '显示：隐藏已确认', only: '只看疑似/已确认' };
  xDupMode = order[(order.indexOf(xDupMode) + 1) % order.length];
  btn.dataset.mode = xDupMode;
  btn.textContent = labels[xDupMode];
  loadXItems(true);
});
$('#xSavedFilter')?.addEventListener('click', (e) => {
  const btn = e.currentTarget;
  xSavedMode = xSavedMode === 'done' ? 'all' : 'done';
  btn.dataset.mode = xSavedMode;
  btn.textContent = xSavedMode === 'done' ? '只看已下载' : '全部作品';
  btn.classList.toggle('btn-soft', xSavedMode === 'done');
  btn.classList.toggle('btn-outline-secondary', xSavedMode !== 'done');
  loadXItems(true);
});

loadXStatus();
loadXTags();
loadXLinks();
loadXItems(true);
