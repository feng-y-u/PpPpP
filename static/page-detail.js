const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
const d = JSON.parse(document.getElementById('detailData').textContent);
const illust = d.illust;
const isDownloaded = d.isDownloaded;
const pageCount = d.pageCount;
const localUrls = d.localUrls;
const mediumUrls = d.mediumUrls;
const originalProxied = d.originalProxied;
const imgSources = !isDownloaded || !localUrls.length
  ? (mediumUrls.length ? mediumUrls : (originalProxied.length ? originalProxied : [(d.thumbFallback || '') ]))
  : localUrls;
let currentPage = 0;

function showPage(index) {
  if (!imgSources.length || !imgSources[0]) {
    $('#fallback').textContent = '[ 无可用图片 ]';
    return;
  }
  currentPage = Math.max(0, Math.min(index, imgSources.length - 1));
  const img = $('#mainImage');
  img.src = imgSources[currentPage];
  img.style.display = 'block';
  $('#fallback').style.display = 'none';

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

document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); showPage(currentPage - 1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); showPage(currentPage + 1); }
});

// Touch swipe
(function() {
  let touchStartX = 0;
  const el = $('#imageArea');
  el.addEventListener('touchstart', e => { touchStartX = e.touches[0].clientX; }, {passive: true});
  el.addEventListener('touchend', e => {
    const diff = touchStartX - e.changedTouches[0].clientX;
    if (Math.abs(diff) > 50) {
      if (diff > 0) showPage(currentPage + 1);
      else showPage(currentPage - 1);
    }
  }, {passive: true});
})();

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

// Handle image load error
$('#mainImage').addEventListener('error', function() {
  this.style.display = 'none';
  $('#fallback').textContent = '[ 图片加载失败 ]';
});
