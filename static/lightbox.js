// ── 共享 Lightbox 预览组件 ──
// 用法：lightbox.open(items, index)
//   items: [{ pixiv_id, thumbUrl, isFav, collectionView }]
//   index: 初始显示下标（作品级）
// 键盘 ←→ 切换 / Esc 关闭；触摸左右滑动；遮罩点击关闭。
// 图源策略：先用缩略图即时渲染，后台静默调 /api/detail/<id> 探测页源——
//   已下载作品 → local_urls（原图原尺寸，逐页）；
//   未下载但有 stored 原图 URL → medium_urls（master1200 中图，逐页）；
//   都没有 → 保持缩略图（单张）。
// 导航是"扁平图片级"：←/→ 在图片之间移动，跨过作品边界自动进入下一个/上一个
// 作品；操作条按钮（下载/收藏/打开详情）始终作用于当前作品 items[index]。

const lightbox = (() => {
  let items = [];
  let index = 0;        // 当前作品下标
  let pageIndex = 0;    // 当前作品内页下标
  let loadedPids = new Set();       // 已探测过页源的作品（每次 open 清空，避免陈旧）
  let activePollPid = null;         // 正在轮询下载状态的作品：保护其下载按钮不被渲染重置
  let root = null, imgEl = null, prevBtn = null, nextBtn = null,
      closeBtn = null, counter = null, favBtn = null,
      detailBtn = null, dlBtn = null;
  let touchX = 0;
  let pollTimer = null, closeTimer = null;

  // ── 扁平图片级辅助 ──
  function getPages(it) {
    return (it && Array.isArray(it.pages)) ? it.pages : [];
  }
  // 未探测页数的作品按 1 张计：总数为上界，探测完成后 render 会更新
  function pageCount(it) {
    const pages = getPages(it);
    return pages.length ? pages.length : 1;
  }
  function totalImages() {
    return items.reduce((sum, it) => sum + pageCount(it), 0);
  }
  function flatPosition() {
    let flat = pageIndex;
    for (let i = 0; i < index; i++) flat += pageCount(items[i]);
    return flat;
  }
  function locate(flat) {
    let rest = flat;
    for (let i = 0; i < items.length; i++) {
      const c = pageCount(items[i]);
      if (rest < c) return { index: i, pageIndex: rest };
      rest -= c;
    }
    const last = items.length - 1;
    return { index: last, pageIndex: Math.max(0, pageCount(items[last]) - 1) };
  }
  function moveFlat(delta) {
    const total = totalImages();
    if (total <= 0) return;
    const target = Math.max(0, Math.min(flatPosition() + delta, total - 1));
    const loc = locate(target);
    index = loc.index;
    pageIndex = loc.pageIndex;
    render();
  }

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

    prevBtn.addEventListener('click', () => moveFlat(-1));
    nextBtn.addEventListener('click', () => moveFlat(1));
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
      activePollPid = pollPid;
      dlBtn.disabled = true;
      dlBtn.textContent = '...';
      fetch(`/download/${pollPid}`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '', 'Content-Type': 'application/json' },
      }).then(r => r.ok ? r.json() : null).then(d => {
        if (!d) { dlBtn.disabled = false; dlBtn.textContent = '下载'; activePollPid = null; return; }
        dlBtn.disabled = false;
        if (d.status === 'done') { dlBtn.textContent = '已下载'; activePollPid = null; return; }
        dlBtn.textContent = '下载中...';
        const iv = setInterval(() => {
          if (!items[index] || items[index].pixiv_id !== pollPid) return;
          fetch(`/download_status/${pollPid}`).then(r => r.json()).then(s => {
            if (!items[index] || items[index].pixiv_id !== pollPid) return;
            if (s.status === 'done') { clearInterval(iv); dlBtn.textContent = '已下载'; activePollPid = null; }
            else if (s.status === 'failed') { clearInterval(iv); dlBtn.textContent = '下载'; dlBtn.disabled = false; activePollPid = null; }
          }).catch(() => {});
        }, 2000);
        pollTimer = iv;  // 共享槽指向当前轮询：close() 与下次点击靠它清理
        setTimeout(() => {
          clearInterval(iv);   // 清理本下载自己的轮询（不误杀新下载的 pollTimer）
          if (items[index] && items[index].pixiv_id === pollPid && dlBtn.textContent === '下载中...') {
            dlBtn.disabled = false;
            dlBtn.textContent = '下载';
          }
          if (activePollPid === pollPid) activePollPid = null;
        }, 300000);
      }).catch(() => { dlBtn.disabled = false; dlBtn.textContent = '下载'; activePollPid = null; });
    });
    if (favBtn) favBtn.addEventListener('click', toggleFav);

    document.addEventListener('keydown', onKey);
    imgEl.parentElement.addEventListener('touchstart', (e) => { touchX = e.touches[0].clientX; }, { passive: true });
    imgEl.parentElement.addEventListener('touchend', (e) => {
      const diff = touchX - e.changedTouches[0].clientX;
      if (Math.abs(diff) > 50) moveFlat(diff > 0 ? 1 : -1);
    }, { passive: true });
  }

  function onKey(e) {
    if (!root || root.style.display === 'none') return;
    const et = e.target;
    if (et && (et.tagName === 'INPUT' || et.tagName === 'TEXTAREA' || et.isContentEditable)) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); moveFlat(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); moveFlat(1); }
    else if (e.key === 'Escape') close();
  }

  function render() {
    if (!items.length) return;
    const it = items[index];
    if (!it) return;
    const pages = getPages(it);
    imgEl.src = pages.length ? pages[pageIndex] : (it.thumbUrl || '');
    const total = totalImages();
    const flat = flatPosition();
    prevBtn.disabled = flat === 0;
    nextBtn.disabled = flat >= total - 1;
    counter.textContent = `第 ${flat + 1} / ${total} 张`;
    const showFav = typeof it.isFav === 'boolean' && !it.collectionView;
    favBtn.hidden = !showFav;
    if (showFav) {
      favBtn.textContent = it.isFav ? '♥ 已收藏' : '♡ 收藏';
      favBtn.classList.toggle('lb-fav-on', !!it.isFav);
    }
    if (it.pixiv_id !== activePollPid) {
      dlBtn.disabled = false;
      dlBtn.textContent = '下载';
    }
    discover();
  }

  function showAt(i) {
    if (!items.length) return;
    index = Math.max(0, Math.min(i, items.length - 1));
    pageIndex = 0;
    render();
  }

  // 页源探测（静默，失败忽略）：local_urls（原图）→ medium_urls（中图）→ 缩略图兜底。
  // 每个作品只探测一次；结果存到 item.pages，供渲染与再次访问复用。
  function discover() {
    const it = items[index];
    if (!it) return;
    const pid = it.pixiv_id;
    if (loadedPids.has(pid)) return;
    loadedPids.add(pid);
    fetch(`/api/detail/${pid}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d) return;
        let pages = null;
        if (d.local_urls && d.local_urls.length) pages = d.local_urls;
        else if (d.medium_urls && d.medium_urls.length) pages = d.medium_urls;
        if (!pages) return;
        it.pages = pages;
        if (root.style.display !== 'none' && items[index] === it) {
          pageIndex = 0;   // 页数已探明：回到首张重新渲染（含计数更新）
          render();
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
    loadedPids.clear();  // 每次打开重新探测页源，避免陈旧
    activePollPid = null;
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
    activePollPid = null;
    document.body.style.overflow = '';
  }

  return { open, close };
})();