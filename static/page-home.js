// ── 首页：最近 48 小时热门 ──
// 只读本地库（GET /api/home），整个页面不发任何 Pixiv 请求。
// 卡片是纯图：作者与名称在左下角信息牌里，默认隐藏（CSS 负责悬停/触屏常显）。

let homeItems = [];

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

// 时间窗说明条：正常态说口径，降级态说"已放宽"，并给一个回到严格 48 小时的入口
function renderWindowNote(data) {
  const note = $('#windowNote');
  note.style.display = 'flex';
  note.classList.toggle('fallback', !!data.fell_back);
  if (data.fell_back) {
    note.innerHTML = '<span class="dot"></span>最近 48 小时内的作品不够一屏，已放宽到最近 7 天'
      + '<button class="window-note-action" id="strictWindowBtn">仍只看 48 小时</button>';
    $('#strictWindowBtn').addEventListener('click', () => loadHome('48'));
    return;
  }
  note.innerHTML = `<span class="dot"></span>时间窗：最近 ${data.window_hours} 小时（发布时间，日本时间）`;
}

async function loadHome(windowMode) {
  const grid = $('#homeGrid');
  const loading = $('#homeLoading');
  const empty = $('#homeEmpty');
  const note = $('#windowNote');

  loading.style.display = 'block';
  grid.innerHTML = '';
  empty.style.display = 'none';
  note.style.display = 'none';

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

  homeItems = Array.isArray(data.results) ? data.results : [];
  $('#homeCount').textContent = homeItems.length;
  $('#homeUpdated').textContent = '更新于 ' + new Date().toTimeString().slice(0, 5);

  if (!homeItems.length) {
    // 空状态要如实说"需要预取/关注"，不能只说"暂无结果"
    empty.style.display = 'block';
    return;
  }

  renderWindowNote(data);
  // renderInChunks 只负责分帧调用、不负责插入：卡片必须由这里挂进网格
  // （曾经漏掉这一步 —— 计数与说明条都正常，网格永远是空的）
  await renderInChunks(homeItems, (r, i) => {
    const node = renderHomeCard(r, i);
    grid.appendChild(node);
    return node;
  }, { chunk: 12, delay: 25 });
  lazyLoad();
}

// 初始加载：默认 auto（48 小时不足时自动放宽到 7 天）
loadHome('auto');
