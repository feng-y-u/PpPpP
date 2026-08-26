// ── 共享 Lightbox 预览组件 ──
// 用法：lightbox.open(items, index)
//   items: [{ pixiv_id, thumbUrl, isFav, collectionView }]
//   index: 初始显示下标
// 键盘 ←→ 切换 / Esc 关闭；触摸左右滑动；遮罩点击关闭。
// 图源策略：先用缩略图即时渲染，后台静默调 /api/detail/<id>，
// 已下载作品自动升级为原图（未下载保持缩略图，操作条提供「打开详情」）。

const lightbox = (() => {
  let items = [];
  let index = 0;
  let upgraded = new Set();
  let root = null, imgEl = null, prevBtn = null, nextBtn = null,
      closeBtn = null, bar = null, counter = null, favBtn = null,
      detailBtn = null, dlBtn = null;
  let touchX = 0;
  let pollTimer = null, closeTimer = null;

  function build() {
    root = document.createElement('div');
    root.className = 'lightbox-overlay';
    root.tabIndex = -1;
    root.innerHTML = `
      <div class="lightbox-main">
        <img id="lightboxImg" alt="">
        <div class="lightbox-bar">
          <button type="button" class="lightbox-btn" id="lbPrev">←</button>
          <span class="lightbox-counter" id="lbCounter"></span>
          <button type="button" class="lightbox-btn" id="lbNext">→</button>
          <span class="lightbox-spacer"></span>
          <button type="button" class="lightbox-btn lb-fav" id="lbFav" hidden>♥ 收藏</button>
          <button type="button" class="lightbox-btn" id="lbDl">下载</button>
          <button type="button" class="lightbox-btn" id="lbDetail">打开详情</button>
          <button type="button" class="lightbox-btn" id="lbClose">✕</button>
        </div>
      </div>`;
    document.body.appendChild(root);
    imgEl = root.querySelector('#lightboxImg');
    prevBtn = root.querySelector('#lbPrev');
    nextBtn = root.querySelector('#lbNext');
    counter = root.querySelector('#lbCounter');
    favBtn = root.querySelector('#lbFav');
    dlBtn = root.querySelector('#lbDl');
    detailBtn = root.querySelector('#lbDetail');
    closeBtn = root.querySelector('#lbClose');

    prevBtn.addEventListener('click', () => showAt(index - 1));
    nextBtn.addEventListener('click', () => showAt(index + 1));
    closeBtn.addEventListener('click', close);
    root.addEventListener('click', (e) => { if (e.target === root) close(); });
    detailBtn.addEventListener('click', () => {
      const pid = items[index] && items[index].pixiv_id;
      close();
      if (pid) window.location.href = `/detail/${pid}`;
    });
    dlBtn.addEventListener('click', () => {
      const it = items[index];
      if (!it) return;
      const pollPid = it.pixiv_id;
      if (pollTimer) clearInterval(pollTimer);
      dlBtn.disabled = true;
      dlBtn.textContent = '...';
      fetch(`/download/${pollPid}`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '', 'Content-Type': 'application/json' },
      }).then(r => r.ok ? r.json() : null).then(d => {
        if (!d) { dlBtn.disabled = false; dlBtn.textContent = '下载'; return; }
        dlBtn.disabled = false;
        if (d.status === 'done') { dlBtn.textContent = '已下载'; return; }
        dlBtn.textContent = '下载中...';
        pollTimer = setInterval(() => {
          if (!items[index] || items[index].pixiv_id !== pollPid) return;
          fetch(`/download_status/${pollPid}`).then(r => r.json()).then(s => {
            if (!items[index] || items[index].pixiv_id !== pollPid) return;
            if (s.status === 'done') { clearInterval(pollTimer); dlBtn.textContent = '已下载'; }
            else if (s.status === 'failed') { clearInterval(pollTimer); dlBtn.textContent = '下载'; dlBtn.disabled = false; }
          }).catch(() => {});
        }, 2000);
        setTimeout(() => {
          clearInterval(pollTimer);
          if (items[index] && items[index].pixiv_id === pollPid && dlBtn.textContent === '下载中...') {
            dlBtn.disabled = false;
            dlBtn.textContent = '下载';
          }
        }, 300000);
      }).catch(() => { dlBtn.disabled = false; dlBtn.textContent = '下载'; });
    });
    if (favBtn) favBtn.addEventListener('click', toggleFav);

    document.addEventListener('keydown', onKey);
    imgEl.parentElement.addEventListener('touchstart', (e) => { touchX = e.touches[0].clientX; }, { passive: true });
    imgEl.parentElement.addEventListener('touchend', (e) => {
      const diff = touchX - e.changedTouches[0].clientX;
      if (Math.abs(diff) > 50) showAt(index + (diff > 0 ? 1 : -1));
    }, { passive: true });
  }

  function onKey(e) {
    if (!root || root.style.display === 'none') return;
    const et = e.target;
    if (et && (et.tagName === 'INPUT' || et.tagName === 'TEXTAREA' || et.isContentEditable)) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); showAt(index - 1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); showAt(index + 1); }
    else if (e.key === 'Escape') close();
  }

  function showAt(i) {
    if (!items.length) return;
    index = Math.max(0, Math.min(i, items.length - 1));
    const it = items[index];
    imgEl.src = it.thumbUrl || '';
    prevBtn.disabled = index === 0;
    nextBtn.disabled = index >= items.length - 1;
    counter.textContent = `${index + 1} / ${items.length}`;
    const showFav = typeof it.isFav === 'boolean' && !it.collectionView;
    favBtn.hidden = !showFav;
    if (showFav) {
      favBtn.textContent = it.isFav ? '♥ 已收藏' : '♡ 收藏';
      favBtn.classList.toggle('lb-fav-on', !!it.isFav);
    }
    dlBtn.disabled = false;
    dlBtn.textContent = '下载';
    maybeUpgrade();
  }

  // 已下载作品升级为原图（静默，失败忽略）
  function maybeUpgrade() {
    const it = items[index];
    if (!it || upgraded.has(it.pixiv_id)) return;
    upgraded.add(it.pixiv_id);
    fetch(`/api/detail/${it.pixiv_id}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d && d.local_urls && d.local_urls.length) {
          if (index < items.length && items[index].pixiv_id === it.pixiv_id) {
            imgEl.src = d.local_urls[0];
          } else {
            upgraded.delete(it.pixiv_id);
          }
        }
      }).catch(() => {});
  }

  async function toggleFav() {
    const it = items[index];
    if (!it) return;
    const resp = await fetch(`/api/favorite/${it.pixiv_id}`, {
      method: 'POST',
      headers: {
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '',
        'Content-Type': 'application/json',
      },
    });
    if (!resp.ok) return;
    const d = await resp.json();
    it.isFav = d.is_favorite;
    favBtn.textContent = it.isFav ? '♥ 已收藏' : '♡ 收藏';
    favBtn.classList.toggle('lb-fav-on', !!it.isFav);
    // 同步网格卡片上的 ♥ 状态（页面 JS 提供卡片更新钩子）
    if (window.__lbSyncFav) window.__lbSyncFav(it.pixiv_id, d.is_favorite);
  }

  function open(list, startIndex) {
    if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
    if (!root) build();
    items = list || [];
    upgraded.clear();  // 每次打开重新探测，避免陈旧
    showAt(startIndex || 0);
    root.style.display = 'flex';
    requestAnimationFrame(() => root.classList.add('lb-open'));
    root.focus({ preventScroll: true });
    document.body.style.overflow = 'hidden';
  }

  function close() {
    if (!root) return;
    root.classList.remove('lb-open');
    closeTimer = setTimeout(() => { root.style.display = 'none'; }, 200);
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    document.body.style.overflow = '';
  }

  return { open, close };
})();
