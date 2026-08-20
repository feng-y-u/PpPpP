const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
const actionMap = {
  start: { label: '开始', cls: 'bg-info' },
  done: { label: '完成', cls: 'bg-success' },
  failed: { label: '失败', cls: 'bg-danger' },
  deleted: { label: '删除', cls: 'bg-warning text-dark' },
};

let pollTimer = null;

async function loadData() {
  try {
    const resp = await fetch('/api/downloads');
    const data = await resp.json();
    renderActive(data.active);
    renderQueued(data.queued || []);
    renderCompleted(data.completed);
    renderLogs(data.logs);
  } catch {}
}

function renderActive(items) {
  const container = $('#activeList');
  $('#activeCount').textContent = items.length || '';

  if (!items.length) {
    container.innerHTML = '<div class="empty-state small">暂无活跃下载</div>';
    return;
  }

  container.innerHTML = items.map(i => `
    <div class="dl-item">
      <img class="dl-thumb" src="${proxyThumb(i.thumb_url)}"
           loading="lazy">
      <div class="dl-info">
        <div class="dl-title" title="${escAttr(i.title)}">${escHtml(i.title || '#' + i.pixiv_id)}</div>
        <div class="dl-meta">#${i.pixiv_id} · ${escHtml(i.user_name)} · ${i.page_count}P${progressText(i)}</div>
        <div class="progress mt-1">
          <div class="progress-bar bg-primary" style="width:${progressPct(i)}%"></div>
        </div>
      </div>
      <div class="dl-status d-flex gap-1">
        <button class="btn btn-outline-danger btn-sm cancel-dl-btn" data-pid="${i.pixiv_id}">取消</button>
        <button class="btn btn-outline-warning btn-sm reset-dl-btn" data-pid="${i.pixiv_id}" title="强制清除卡住的下载">清除</button>
      </div>
    </div>`).join('');

  container.querySelectorAll('.cancel-dl-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = '取消中...';
      try {
        const t = await fetch('/csrf-token').then(r => r.json());
        await fetch(`/download/cancel/${btn.dataset.pid}`, {
          method: 'POST',
          headers: { 'X-CSRF-Token': t.token },
        });
        loadData();
      } catch { btn.disabled = false; btn.textContent = '取消'; }
    });
  });

  container.querySelectorAll('.reset-dl-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = '清除中...';
      try {
        const t = await fetch('/csrf-token').then(r => r.json());
        await fetch(`/download/reset/${btn.dataset.pid}`, {
          method: 'POST',
          headers: { 'X-CSRF-Token': t.token },
        });
        loadData();
      } catch { btn.disabled = false; btn.textContent = '清除'; }
    });
  });
}

function renderQueued(items) {
  const container = $('#queuedList');
  $('#queuedCount').textContent = items.length || '';

  if (!items.length) {
    container.innerHTML = '<div class="empty-state small">队列为空</div>';
    return;
  }

  container.innerHTML = items.map(i => `
    <div class="dl-item">
      <img class="dl-thumb" src="${proxyThumb(i.thumb_url)}"
           loading="lazy">
      <div class="dl-info">
        <div class="dl-title" title="${escAttr(i.title)}">${escHtml(i.title || '#' + i.pixiv_id)}</div>
        <div class="dl-meta">#${i.pixiv_id} · ${escHtml(i.user_name)} · ${i.page_count}P · 等待中</div>
        <div class="progress mt-1">
          <div class="progress-bar bg-info" style="width:0%"></div>
        </div>
      </div>
      <div class="dl-status d-flex gap-1">
        <button class="btn btn-outline-danger btn-sm cancel-dl-btn" data-pid="${i.pixiv_id}">取消</button>
      </div>
    </div>`).join('');

  container.querySelectorAll('.cancel-dl-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = '取消中...';
      try {
        const t = await fetch('/csrf-token').then(r => r.json());
        await fetch(`/download/cancel/${btn.dataset.pid}`, {
          method: 'POST',
          headers: { 'X-CSRF-Token': t.token },
        });
        loadData();
      } catch { btn.disabled = false; btn.textContent = '取消'; }
    });
  });
}

function renderCompleted(items) {
  const container = $('#completedList');
  $('#completedCount').textContent = items.length || '';

  if (!items.length) {
    container.innerHTML = '<div class="empty-state small">暂无完成记录</div>';
    return;
  }

  container.innerHTML = items.map(i => `
    <div class="dl-item">
      <img class="dl-thumb" src="${proxyThumb(i.thumb_url)}"
           loading="lazy">
      <div class="dl-info">
        <div class="dl-title" title="${escAttr(i.title)}">${escHtml(i.title || '#' + i.pixiv_id)}</div>
        <div class="dl-meta">#${i.pixiv_id} · ${escHtml(i.user_name)}</div>
      </div>
      <div class="dl-status d-flex gap-1">
        <a href="/download_file/${i.pixiv_id}" class="btn btn-success btn-sm">下载</a>
      </div>
    </div>`).join('');
    
  }

function renderLogs(logs) {
  const container = $('#logList');
  if (!logs.length) {
    container.innerHTML = '<div class="empty-state small">暂无日志</div>';
    return;
  }

  container.innerHTML = logs.map(l => {
    const a = actionMap[l.action] || { label: l.action, cls: 'bg-secondary' };
    const time = new Date(l.created_at).toLocaleString('zh-CN');
    return `<div class="log-entry">
      <span class="badge ${a.cls} me-1" style="font-size:0.65rem;">${a.label}</span>
      <span class="text-muted me-1">#${l.pixiv_id}</span>
      ${escHtml(l.message)}
      <span class="text-muted float-end" style="font-size:0.7rem;">${time}</span>
    </div>`;
  }).join('');
}

function progressPct(i) {
  if (i.progress && i.progress.total > 0) return Math.round((i.progress.current / i.progress.total) * 100);
  return 0;
}

function progressText(i) {
  if (i.progress && i.progress.total > 0) return ` · ${i.progress.current}/${i.progress.total}`;
  return ' · ?/?';
}

// Poll for active downloads
function startPolling() {
  loadData();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(loadData, 3000);
}

$('#refreshBtn').addEventListener('click', loadData);
startPolling();

// Stop polling when page hidden, resume when visible
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  } else {
    loadData();
    pollTimer = setInterval(loadData, 3000);
  }
});
