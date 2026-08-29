const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
const d = JSON.parse(document.getElementById('detailData').textContent);
const illust = d.illust;
const isDownloaded = d.isDownloaded;
const pageCount = d.pageCount;
const localUrls = d.localUrls;
const mediumUrls = d.mediumUrls;
const originalProxied = d.originalProxied;
// 每页一个"候选链"：中图 -> 原图代理 -> 缩略图。某个源加载失败时自动
// 切到下一个候选，而不是直接空白（中图拉不稳时仍能降级显示）。
let imgSources;
if (isDownloaded && localUrls.length) {
  imgSources = localUrls.map(u => [u]);
} else {
  const chains = [];
  const n = Math.max(mediumUrls.length, originalProxied.length, 1);
  for (let i = 0; i < n; i++) {
    const chain = [];
    if (mediumUrls[i]) chain.push(mediumUrls[i]);
    if (originalProxied[i]) chain.push(originalProxied[i]);
    if (i === 0 && d.thumbFallback) chain.push(d.thumbFallback);
    chains.push(chain);
  }
  imgSources = chains.filter(c => c.length);
}
let currentPage = 0;
let sourceIndex = 0;

function renderCurrentSource() {
  const img = $('#mainImage');
  const chain = imgSources[currentPage] || [];
  const fallback = $('#fallback');
  if (sourceIndex < chain.length) {
    img.src = chain[sourceIndex];
    img.style.display = 'block';
    fallback.style.display = 'none';
  } else {
    img.style.display = 'none';
    fallback.textContent = '[ 图片加载失败 ]';
    fallback.style.display = 'block';
  }
  $('#prevBtn').disabled = currentPage === 0;
  $('#nextBtn').disabled = currentPage >= imgSources.length - 1;
  if (imgSources.length > 1) {
    const pi = $('#pageIndicator');
    pi.textContent = `${currentPage + 1} / ${imgSources.length}`;
    pi.classList.add('show');
  } else {
    $('#pageIndicator').classList.remove('show');
  }
}

function showPage(index) {
  if (!imgSources.length) {
    $('#fallback').textContent = '[ 无可用图片 ]';
    $('#fallback').style.display = 'block';
    return;
  }
  currentPage = Math.max(0, Math.min(index, imgSources.length - 1));
  sourceIndex = 0;
  renderCurrentSource();
}

// 当前候选源加载失败：切到本页下一个候选；耗尽则显示失败占位
function onImageError() {
  const chain = imgSources[currentPage] || [];
  sourceIndex += 1;
  if (sourceIndex < chain.length) {
    $('#mainImage').src = chain[sourceIndex];
  } else {
    $('#mainImage').style.display = 'none';
    $('#fallback').textContent = '[ 图片加载失败 ]';
    $('#fallback').style.display = 'block';
  }
}
$('#mainImage').addEventListener('error', onImageError);

$('#prevBtn').addEventListener('click', () => showPage(currentPage - 1));
$('#nextBtn').addEventListener('click', () => showPage(currentPage + 1));

// ── Back button ──
// 优先 history.back()：从图库/搜索等站内页进入时会保留来源页状态
//（前端缓存、当前页、滚动位置）。仅当无历史栈可回退（如外部直接打开
// 详情页）时才用 referrer 显式跳转，最后兜底回首页。
$('#backBtn').addEventListener('click', () => {
  if (history.length > 1) {
    history.back();
    return;
  }
  const ref = document.referrer;
  if (ref && ref.startsWith(location.origin)) {
    location.href = ref;
  } else {
    location.href = '/';
  }
});

// ←→：跨作品翻页（图库上下文下）；无上下文时不动作，作品内翻页用图上 ‹ ›
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); navTo(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); navTo(1); }
});

// Touch swipe
(function() {
  let touchStartX = 0;
  const el = $('#imageArea');
  el.addEventListener('touchstart', e => { touchStartX = e.touches[0].clientX; }, {passive: true});
  el.addEventListener('touchend', e => {
    const diff = touchStartX - e.changedTouches[0].clientX;
    if (Math.abs(diff) > 50) {
      if (diff > 0) navTo(1);
      else navTo(-1);
    }
  }, {passive: true});
})();

// ── 跨作品翻页（仅图库上下文 ctx=gallery 启用）──
// 顺序跟随图库当前视图（排序/收藏夹/标签筛选）。序列来源：图库跳转时写入的
// sessionStorage（pv_detail_seq）；新标签页直接粘贴 URL 时按 URL 参数现拉
// /api/gallery 定位。两者都失败则不渲染翻页 UI。
const PAGE_SIZE = 50;
const DETAIL_SEQ_KEY = 'pv_detail_seq';
const navCtx = (() => {
  const q = new URLSearchParams(location.search);
  if (q.get('ctx') !== 'gallery') return null;
  return {
    sort: q.get('sort') || 'downloaded',
    collectionId: q.get('collection_id') || '',
    tag: q.get('tag') || '',
    pos: parseInt(q.get('pos'), 10),
    page: Math.max(1, parseInt(q.get('page'), 10) || 1),
  };
})();
let seq = null;        // { total, pages: {页码: [pixiv_id, ...]} }
let navReady = false;
let navPending = false;   // 防并发：pidAt 现拉相邻页期间忽略后续翻页触发

function loadStoredSeq() {
  try {
    const s = JSON.parse(sessionStorage.getItem(DETAIL_SEQ_KEY) || 'null');
    if (!s || s.v !== 1) return null;
    if (s.sort !== navCtx.sort || (s.collection_id || '') !== navCtx.collectionId
        || (s.tag || '') !== navCtx.tag) return null;
    return s;
  } catch { return null; }
}

function saveSeq() {
  try { sessionStorage.setItem(DETAIL_SEQ_KEY, JSON.stringify(seq)); } catch {}
}

function galleryParams(pageNo) {
  const p = new URLSearchParams();
  p.set('sort', navCtx.sort);
  if (navCtx.collectionId) p.set('collection_id', navCtx.collectionId);
  if (navCtx.tag) p.set('tag', navCtx.tag);
  p.set('limit', PAGE_SIZE);
  p.set('offset', (pageNo - 1) * PAGE_SIZE);
  return p.toString();
}

async function fetchPage(pageNo) {
  const resp = await fetch('/api/gallery?' + galleryParams(pageNo));
  if (!resp.ok) throw new Error('gallery fetch failed');
  const data = await resp.json();
  seq.total = data.total;
  seq.pages[pageNo] = data.data.map(x => x.pixiv_id);
  // 只保留目标页 ±1，防 sessionStorage 膨胀
  Object.keys(seq.pages).forEach(k => {
    if (Math.abs(parseInt(k, 10) - pageNo) > 1) delete seq.pages[k];
  });
  saveSeq();
}

async function resolveSeq() {
  if (!navCtx || isNaN(navCtx.pos)) return;
  seq = loadStoredSeq() || { total: 0, pages: {} };
  // 当前页必须包含本作品（sessionStorage 可能已过期或被其他筛选覆盖）
  if (!seq.pages[navCtx.page] || !seq.pages[navCtx.page].includes(illust.pixiv_id)) {
    try {
      await fetchPage(navCtx.page);
    } catch {
      seq = null;
      return;
    }
  }
  if (!seq.pages[navCtx.page].includes(illust.pixiv_id)) { seq = null; return; }
  // pos 以图库实际序列为准（图库数据可能在跳转后已变化）
  navCtx.pos = (navCtx.page - 1) * PAGE_SIZE + seq.pages[navCtx.page].indexOf(illust.pixiv_id);
  navReady = true;
}

async function pidAt(pos) {
  const pageNo = Math.floor(pos / PAGE_SIZE) + 1;
  if (!seq.pages[pageNo]) await fetchPage(pageNo);   // 跨页续翻：现拉相邻页
  return seq.pages[pageNo][pos % PAGE_SIZE] || null;
}

async function navTo(delta) {
  if (!navReady || navPending) return;
  navPending = true;
  try {
    const target = navCtx.pos + delta;
    if (target < 0 || (seq.total > 0 && target >= seq.total)) return;
    const pid = await pidAt(target).catch(() => null);
    if (!pid) { showToast('加载翻页数据失败', true); return; }
    const params = new URLSearchParams(location.search);
    params.set('pos', target);
    params.set('page', Math.floor(target / PAGE_SIZE) + 1);
    // replace 不压历史栈：连翻多个作品后按返回仍一次回到图库
    location.replace(`/detail/${pid}?${params}`);
  } finally {
    navPending = false;
  }
}

function renderIllustNav() {
  if (!navReady) return;
  $('#illustNav').style.display = 'flex';
  $('#illustPos').textContent = `${navCtx.pos + 1} / ${seq.total}`;
  $('#prevIllustBtn').disabled = navCtx.pos <= 0;
  $('#nextIllustBtn').disabled = seq.total > 0 && navCtx.pos >= seq.total - 1;
}

// 屏幕按钮绑定：上一作/下一作，与键盘 ←→、触摸滑动共用 navTo
$('#prevIllustBtn').addEventListener('click', () => navTo(-1));
$('#nextIllustBtn').addEventListener('click', () => navTo(1));

// ── Collection Picker ──
let savedCollectionIds = new Set();

$('#favBtn').addEventListener('click', async function() {
  if (this.disabled) return;
  // Fetch collections and current membership
  try {
    const [collectionsResp, membershipResp] = await Promise.all([
      fetch('/api/collections'),
      fetch(`/api/illust/${illust.pixiv_id}/collections`),
    ]);
    if (!collectionsResp.ok || !membershipResp.ok) { showToast('加载收藏夹失败', true); return; }
    const collections = await collectionsResp.json();
    const membership = await membershipResp.json();
    savedCollectionIds = new Set(membership);

    const body = $('#collectionPickerBody');
    if (collections.length === 0) {
      body.innerHTML = '<div style="color:var(--text-muted);padding:0.5rem 0;">暂无收藏夹，请先在设置页创建</div>';
    } else {
      body.innerHTML = collections.map(c => `
        <label class="collection-check-item">
          <input type="checkbox" value="${c.id}" ${savedCollectionIds.has(c.id) ? 'checked' : ''}>
          <span>${escHtml(c.name)}</span>
          <span class="collection-check-count">${c.item_count} 件</span>
        </label>
      `).join('');
    }
    new bootstrap.Modal($('#collectionPickerModal')).show();
  } catch { showToast('网络错误', true); }
});

$('#saveCollectionBtn').addEventListener('click', async function() {
  if (this.disabled) return;
  this.disabled = true;
  const checkboxes = $$('#collectionPickerBody input[type="checkbox"]');
  const newIds = new Set();
  checkboxes.forEach(cb => { if (cb.checked) newIds.add(parseInt(cb.value)); });

  try {
    // Remove uncheck, add newly checked
    const toRemove = [...savedCollectionIds].filter(id => !newIds.has(id));
    const toAdd = [...newIds].filter(id => !savedCollectionIds.has(id));
    const promises = [];
    for (const cid of toRemove) {
      promises.push(fetch(`/api/collections/${cid}/items/${illust.pixiv_id}`, {
        method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken },
      }));
    }
    for (const cid of toAdd) {
      promises.push(fetch(`/api/collections/${cid}/items`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
        body: JSON.stringify({ pixiv_id: illust.pixiv_id }),
      }));
    }
    await Promise.all(promises);
    savedCollectionIds = newIds;

    // Update fav button state
    const isFav = newIds.size > 0;
    const btn = $('#favBtn');
    if (isFav) {
      btn.className = 'btn btn-dl-done';
      btn.textContent = '❤ 已收藏';
    } else {
      btn.className = 'btn btn-primary-accent';
      btn.textContent = '♥ 收藏';
    }
    bootstrap.Modal.getInstance($('#collectionPickerModal')).hide();
    if (toRemove.length || toAdd.length) showToast('收藏已更新');
  } catch { showToast('保存失败', true); }
  finally { this.disabled = false; }
});

// ── Download ──
$('#downloadBtn')?.addEventListener('click', async function() {
  if (this.tagName === 'A') return; // direct link for already-downloaded
  if (this.disabled) return;
  triggerDownload(illust.pixiv_id, this);
});

// ── Tag click → gallery ──
$('#tagList')?.addEventListener('click', e => {
  const tag = e.target.closest('.tag-item');
  if (tag) window.location.href = `/gallery?tag=${encodeURIComponent(tag.dataset.tag)}`;
});

// ── Init ──
showPage(0);
resolveSeq().then(renderIllustNav).catch(() => {});

// Handle image load error
$('#mainImage').addEventListener('error', function() {
  this.style.display = 'none';
  $('#fallback').textContent = '[ 图片加载失败 ]';
});
