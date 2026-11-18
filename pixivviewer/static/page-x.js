// ── X 图片页：标签抓取的浏览 / 下载 / 同作品判定 ──
// 依赖 app.js 的 $ / escHtml / showToast。所有图片经 /ximg 代理（CSP img-src 'self'）。
const xCsrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
const X_PAGE_SIZE = parseInt(
  document.querySelector('meta[name="items-per-page"]')?.getAttribute('content') || '', 10) || 24;

let xActiveTag = '';
let xDupMode = 'all';        // all / hide（藏已确认） / only（只看疑似+已确认）
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
    bits.push(st.enabled ? `抓取已开启（每 ${st.interval}s）` : '抓取已关闭');
    if (st.last_check) bits.push('上次：' + st.last_check.replace('T', ' ').slice(0, 19));
    if (st.last_error) bits.push(`<span class="warn">最近错误：${escHtml(st.last_error)}</span>`);
    if (!s.cookie_configured) bits.push('<span class="warn">未配置 X Cookie（设置页填写）</span>');
    if (!s.query_id_configured) bits.push('<span class="warn">未配置 query_id（instance/x_api.json）</span>');
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
    chip.title = t.error || `上次抓取：${t.last_fetch_at || '从未'}`;
    chip.innerHTML = `${escHtml(t.tag)} <span class="del">×</span>` +
      (t.error ? ' <span class="err">!</span>' : '');
    chip.addEventListener('click', (e) => {
      if (e.target.closest('.del')) {
        deleteXTag(t.tag);
        return;
      }
      xActiveTag = t.tag;
      reloadXItems();
    });
    bar.appendChild(chip);
  });
}

async function addXTag() {
  const input = $('#xTagInput');
  const tag = (input.value || '').trim();
  if (!tag) return;
  try {
    await xPost('/api/x/tags', { tag });
    input.value = '';
    showToast('已添加，正在后台抓取');
    xActiveTag = tag;
    await loadXTags();
    reloadXItems();
  } catch (e) {
    showToast('添加失败：' + e.message, true);
  }
}

async function deleteXTag(tag) {
  if (!confirm(`删除标签「${tag}」以及它抓到的作品记录？（已下载的图片文件不会删除）`)) return;
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
function renderXCard(item) {
  const col = document.createElement('div');
  col.className = 'col-lg-3 col-md-4 col-sm-6';
  const media = (item.media || [])[0] || {};
  const img = xProxy(media.url);
  const badges = [];
  if (item.dup_status === 'confirmed') {
    badges.push(`<span class="x-badge ok">同一作品${item.dup_pixiv_id ? ' #' + item.dup_pixiv_id : ''}</span>`);
  } else if (item.dup_status === 'suspected') {
    badges.push('<span class="x-badge warn">疑似同一作品</span>');
  }
  if (item.saved_status === 'done') badges.push('<span class="x-badge">已下载</span>');
  const pixivLink = item.dup_pixiv_id
    ? `<a class="btn btn-sm btn-soft" href="/detail/${item.dup_pixiv_id}">看 Pixiv</a>` : '';
  const dupActions = item.dup_status === 'suspected'
    ? `<button class="btn btn-sm btn-outline-warning" data-dup="confirm">确认同一作品</button>
       <button class="btn btn-sm btn-outline-secondary" data-dup="ignore">不是</button>` : '';
  const downloadBtn = item.saved_status === 'done'
    ? '<span class="btn btn-sm btn-dl-done">已下载</span>'
    : `<button class="btn btn-sm btn-soft" data-dl>下载${item.media_count > 1 ? `（${item.media_count} 张）` : ''}</button>`;

  col.innerHTML = `
    <div class="x-card" data-id="${item.id}">
      <div class="x-media">
        ${img ? `<img src="${img}" loading="lazy" alt="">` : ''}
        ${badges.join('')}
        ${item.media_count > 1 ? `<span class="x-badge" style="left:auto;right:6px;">${item.media_count} 张</span>` : ''}
      </div>
      <div class="x-body">
        <div class="x-author">${escHtml(item.author_name || '')} @${escHtml(item.author_screen_name || '')}</div>
        <div class="x-text">${escHtml((item.text || '').slice(0, 120))}</div>
        <div class="x-actions">
          ${downloadBtn}
          <a class="btn btn-sm btn-soft" target="_blank" rel="noopener noreferrer"
             href="https://x.com/${encodeURIComponent(item.author_screen_name || 'i')}/status/${encodeURIComponent(item.tweet_id)}">原推</a>
          ${pixivLink}
          ${dupActions}
        </div>
      </div>
    </div>`;

  const card = col.querySelector('.x-card');
  card.querySelector('.x-media').addEventListener('click', () => {
    window.open(`https://x.com/${encodeURIComponent(item.author_screen_name || 'i')}/status/${encodeURIComponent(item.tweet_id)}`, '_blank', 'noopener');
  });
  const dl = card.querySelector('[data-dl]');
  if (dl) dl.addEventListener('click', () => downloadXWork(item.id, dl));
  card.querySelectorAll('[data-dup]').forEach(btn => {
    btn.addEventListener('click', () => markXDup(item.id, btn.dataset.dup));
  });
  return col;
}

async function loadXItems(reset) {
  if (xLoading) return;
  xLoading = true;
  if (reset) { xOffset = 0; $('#xGrid').innerHTML = ''; }
  try {
    const params = new URLSearchParams({ offset: xOffset, limit: X_PAGE_SIZE, dup: xDupMode });
    if (xActiveTag) params.set('tag', xActiveTag);
    const data = await xApi('/api/x/items?' + params.toString());
    xTotal = data.total || 0;
    (data.data || []).forEach(item => $('#xGrid').appendChild(renderXCard(item)));
    xOffset += (data.data || []).length;
    $('#xEmpty').style.display = (xTotal === 0) ? 'block' : 'none';
    $('#xMoreBtn').style.display = (xOffset < xTotal) ? 'inline-block' : 'none';
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

async function downloadXWork(id, btn) {
  btn.disabled = true;
  btn.textContent = '下载中...';
  try {
    await xPost(`/api/x/works/${id}/download`);
    // 简单轮询：下载完成后刷新这张卡的状态
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      try {
        const row = await xApi(`/api/x/works/${id}`);
        if (row.saved_status === 'done' || row.saved_status === 'failed') {
          clearInterval(timer);
          reloadXItems();
          showToast(row.saved_status === 'done' ? '下载完成' : '下载失败', row.saved_status !== 'done');
        }
      } catch { /* 忽略轮询错误 */ }
      if (tries > 60) clearInterval(timer);
    }, 3000);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '下载';
    showToast('下载失败：' + e.message, true);
  }
}

async function markXDup(id, action) {
  try {
    await xPost(`/api/x/works/${id}/dup`, { action });
    showToast(action === 'confirm' ? '已记为同一作品' : '已忽略，不再提示');
    reloadXItems();
  } catch (e) {
    showToast('操作失败：' + e.message, true);
  }
}

// ── 绑定 ──
$('#xTagAddBtn').addEventListener('click', addXTag);
$('#xTagInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') addXTag(); });
$('#xMoreBtn').addEventListener('click', () => loadXItems(false));
$('#xRefreshBtn').addEventListener('click', reloadXItems);
$('#xDupFilter').addEventListener('click', (e) => {
  const btn = e.currentTarget;
  const order = ['all', 'hide', 'only'];
  const labels = { all: '显示：全部', hide: '显示：隐藏已确认', only: '只看疑似/已确认' };
  xDupMode = order[(order.indexOf(xDupMode) + 1) % order.length];
  btn.dataset.mode = xDupMode;
  btn.textContent = labels[xDupMode];
  loadXItems(true);
});

loadXStatus();
loadXTags();
loadXItems(true);
