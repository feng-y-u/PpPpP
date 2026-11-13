// ── Pixiv Viewer — 共享工具函数 ──

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function escAttr(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function proxyThumb(url) {
  if (!url) return '';
  return `/thumb/${btoa(url).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')}`;
}

function fmtSize(bytes) {
  if (!bytes) return '0 B';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

function fmtNum(n) {
  if (!n) return '0';
  n = parseInt(n);
  return n >= 10000 ? (n/10000).toFixed(1)+'w' : String(n);
}

function showToast(msg, isError) {
  const toast = $('#liveToast');
  toast.className = 'toast align-items-center border-0';
  toast.style.background = isError ? 'rgba(30,27,46,.92)' : 'rgba(5,150,105,.85)';
  $('#toastMsg').textContent = msg;
  bootstrap.Toast.getOrCreateInstance(toast).show();
}

// ── localStorage 缓存工具 ──
// 用于搜索状态、图库分页等前端缓存；带 TTL，空间不足时降级重试。
const pvCache = {
  get(key, ttlMs) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return null;
      const item = JSON.parse(raw);
      if (!item || typeof item.ts !== 'number') return null;
      if (Date.now() - item.ts > ttlMs) {
        localStorage.removeItem(key);
        return null;
      }
      return item.value;
    } catch { return null; }
  },
  set(key, value) {
    const payload = JSON.stringify({ ts: Date.now(), value });
    try {
      localStorage.setItem(key, payload);
    } catch (e) {
      // 空间不足：删掉该 key 重试一次
      try { localStorage.removeItem(key); localStorage.setItem(key, payload); } catch {}
    }
  },
  del(key) {
    try { localStorage.removeItem(key); } catch {}
  },
  clearPrefix(prefix) {
    try {
      const keys = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith(prefix)) keys.push(k);
      }
      keys.forEach(k => localStorage.removeItem(k));
    } catch {}
  }
};

// ── 下载功能（首页和详情页共享）──

async function triggerDownload(pixivId, btn) {
  btn.disabled = true;
  btn.textContent = '...';
  btn.className = 'btn btn-sm';
  try {
    const r = await fetch(`/download/${pixivId}`, {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
    });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || '失败', true); resetDlBtn(pixivId); return; }
    if (d.status === 'done') { updateDlDone(pixivId); return; }
    pollDl(pixivId);
  } catch { showToast('网络错误', true); resetDlBtn(pixivId); }
}

function pollDl(pixivId) {
  const iv = setInterval(async () => {
    try {
      const d = await fetch(`/download_status/${pixivId}`).then(r => r.json());
      if (d.status === 'done') { clearInterval(iv); updateDlDone(pixivId); }
      else if (d.status === 'failed') {
        clearInterval(iv);
        showToast(`#${pixivId} 下载失败`, true);
        resetDlBtn(pixivId);
      }
    } catch {}
  }, 2000);
  setTimeout(() => clearInterval(iv), 300000);
}

function updateDlDone(pixivId) {
  // 卡片式 UI（index.html 搜索结果页 / cache.html 缓存页）
  // 已下载态统一是一个真链接：文案「下载原图」、同标签页取文件。不再挂点击处理器
  // （旧实现是 button + downloadFile() 走 window.open，与图库/详情页的 <a> 不一致）。
  const card = document.querySelector(`.photo-card[data-pixiv-id="${pixivId}"]`);
  if (card) {
    const actions = card.querySelector('.photo-card-actions');
    if (actions) {
      actions.innerHTML = `<a href="/download_file/${pixivId}" class="btn btn-dl-done btn-sm dl-file-btn">下载原图</a>`;
    }
    return;
  }
  // 按钮式 UI（detail.html 详情页）：换成下载链而不是改按钮属性。
  // 只改 onclick 的话，页面加载时绑在按钮上的 click 监听器仍在 —— 点一下会同时
  // 跳文件页和再 POST 一次 /download/<pid>（下载已完成，纯属多余请求）。
  const btn = document.getElementById('downloadBtn');
  if (btn) {
    const a = document.createElement('a');
    a.id = 'downloadBtn';
    a.className = 'btn btn-dl-done';
    a.href = `/download_file/${pixivId}`;
    a.textContent = '⬇ 下载原图';
    btn.replaceWith(a);
  }
}

function resetDlBtn(pixivId) {
  // 卡片式 UI（index.html 搜索结果页）
  const card = document.querySelector(`.photo-card[data-pixiv-id="${pixivId}"]`);
  if (card) {
    const actions = card.querySelector('.photo-card-actions');
    if (actions) {
      actions.innerHTML = `<button class="btn btn-soft btn-sm dl-btn" data-pid="${pixivId}">下载</button>`;
      actions.querySelector('.dl-btn').addEventListener('click', function (e) {
        e.stopPropagation();
        triggerDownload(pixivId, this);
      });
    }
    return;
  }
  // 按钮式 UI（detail.html 详情页）
  const btn = document.getElementById('downloadBtn');
  if (btn) {
    btn.textContent = '⬇ 下载原图';
    btn.className = 'btn btn-soft';
    btn.disabled = false;
  }
}

// ── 屏蔽标签（全站共享）──
// 搜索页卡片标签上的 × 与设置页「添加」按钮共用这一份实现。此前它只定义在
// page-settings.js 里，而搜索页并不加载那个脚本 —— 点 × 会抛 ReferenceError，
// 界面毫无反应、标签也没被屏蔽（用户以为已经屏蔽了）。
// CSRF 直接读 meta，不依赖各页脚本里的 csrfToken 变量（首页没有该变量）。
async function addBlockedTag(tag) {
  const t = (tag || '').trim();
  if (!t) return false;
  try {
    const r = await fetch('/api/blocked-tags', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '',
      },
      body: JSON.stringify({ tag: t }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { showToast(d.error || '添加失败', true); return false; }
    showToast('已屏蔽：' + t);
    return true;
  } catch {
    showToast('网络错误', true);
    return false;
  }
}

// ── 丝滑化公共能力 ──

// 图片懒加载（IntersectionObserver）：data-src 占位 → 进入视口才加载。
// 搜索页/图库/缓存页共用；渲染完卡片后调用一次即可。
//
// 只能复用一个 observer：旧实现每次 lazyLoad() 都 new 一个并重新 observe 所有
// 未加载图片，首页无限加载时 observer 会越积越多，同一张图被多个 observer
// 重复回调（审计：首页加载几十批后滚动卡顿）。resetLazyLoad() 在清空列表时
// 断开旧 observer，避免它继续持有已移除的图片节点。
let _lazyObserver = null;

function _getLazyObserver() {
  if (_lazyObserver) return _lazyObserver;
  _lazyObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const img = entry.target;
      if (img.dataset.src) {
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
      }
      _lazyObserver.unobserve(img);
    });
  }, { rootMargin: '200px' });
  return _lazyObserver;
}

function lazyLoad() {
  const observer = _getLazyObserver();
  $$('img[data-src]').forEach(img => {
    if (img.dataset.lazyObserved === '1') return;
    img.dataset.lazyObserved = '1';
    observer.observe(img);
  });
}

function resetLazyLoad() {
  if (_lazyObserver) {
    _lazyObserver.disconnect();
    _lazyObserver = null;
  }
  // 如果节点仍在 DOM 里（例如调用方以后复用列表），下一次 lazyLoad 要能重新 observe。
  $$('img[data-lazy-observed="1"]').forEach(img => {
    delete img.dataset.lazyObserved;
  });
}

// 分帧渲染：把 items 按 chunks 分批用 requestAnimationFrame 渲染，
// 批间让出主线程（避免长任务卡顿），并给每张卡加 stagger 浮现动画（索引前 24 张递增延迟，之后同批）。
// renderFn(item, i) 需返回创建的 DOM 元素（或 null 跳过动画）。
// opts: { chunk=12, delay=30ms } — 返回 Promise，全部完成后 resolve。
function renderInChunks(items, renderFn, opts) {
  const { chunk = 12, delay = 30 } = opts || {};
  let index = 0;
  return new Promise((resolve) => {
    function step() {
      if (index >= items.length) { resolve(); return; }
      const end = Math.min(index + chunk, items.length);
      for (let i = index; i < end; i++) {
        const node = renderFn(items[i], i);
        if (node && node.addEventListener) {
          node.classList.add('card-enter');
          if (node.style) node.style.animationDelay = `${Math.min(i, 24) * delay}ms`; // 前 24 张递增 stagger，之后同批进入（避免大页尾部过慢）
          // 动画结束后清理内联 delay，避免影响后续 hover 过渡
          node.addEventListener('animationend', function handler(ev) {
            if (ev.animationName === 'cardIn') {
              node.style.animationDelay = '';
              node.removeEventListener('animationend', handler);
            }
          });
        }
      }
      index = end;
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });
}

// 全局图片加载完成监听：给 .img-fade 缩略图加 loaded 态（浮现动画）。
// 捕获阶段监听，与 error 监听同模式；排除详情页主图（有独立候选链逻辑）。
document.addEventListener('load', function (e) {
  const t = e.target;
  if (!t || t.tagName !== 'IMG') return;
  if (t.id === 'mainImage') return;
  if (t.classList.contains('img-fade')) t.classList.add('img-loaded');
}, true);
// 缓存命中场景：图片可能已在监听器绑定前加载完（complete 且非失败态）
function markLoadedImages() {
  $$('img.img-fade').forEach(img => {
    if (img.complete && img.naturalWidth > 0) img.classList.add('img-loaded');
  });
}
// 兜底：对后续动态插入且可能已加载完的图片做一次检查（渲染批次间隙调用）
setInterval(markLoadedImages, 1000);

// ── 全局兜底 ──
// CSP script-src 'self' 会禁用内联 onclick/onerror 属性，这里统一用事件绑定替代。
// 移动端导航切换（模板 nav-toggle 不再用内联 onclick）
document.querySelector('.nav-toggle')?.addEventListener('click', function () {
  this.nextElementSibling.classList.toggle('open');
});
// 卡片缩略图加载失败：显示占位背景，限次自动重试（避免整批"消失"）。
// 详情页主图除外——由 page-detail.js 走"中图→原图→缩略图"候选链降级。
document.addEventListener('error', function (e) {
  const t = e.target;
  if (!t || t.tagName !== 'IMG') return;
  if (t.id === 'mainImage') return;
  const tries = parseInt(t.dataset.retry || '0', 10);
  if (tries < 2) {
    t.dataset.retry = String(tries + 1);
    t.classList.add('img-failed');
    setTimeout(() => {
      // 追加时间戳强制重新请求（不让浏览器走失败缓存）
      const base = (t.src || '').split('?')[0];
      if (base) t.src = base + '?t=' + Date.now();
    }, 2000);
  } else {
    // 重试耗尽：保留占位布局，不彻底隐藏（避免页面"图全没了"）
    t.classList.add('img-failed');
  }
}, true);
