// ── X 作品详情页（/x/view）：下载前后共用。
// 未下载：代理原图预览 + 「下载原图」按钮（POST + 轮询，与卡片上的下载同链路）；
// 已下载：本地文件 + 「删除记录」按钮。
const xvCsrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

// ── 未下载分支：下载按钮 ──
const xvDlBtn = $('#xvDownloadBtn');
if (xvDlBtn) {
  xvDlBtn.addEventListener('click', async () => {
    const id = xvDlBtn.dataset.xId;
    const count = xvDlBtn.dataset.count || '?';
    xvDlBtn.disabled = true;
    const hint = $('#xvDlHint');
    try {
      const resp = await fetch(`/api/x/works/${id}/download`, {
        method: 'POST', headers: { 'X-CSRF-Token': xvCsrf },
      });
      const d = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(d.error || `HTTP ${resp.status}`);
      if (d.status === 'done') { window.location.reload(); return; }
      xvDlBtn.textContent = '下载中…';
      if (hint) hint.textContent = `已加入队列（${count} 张），完成后本页自动切换为本地文件`;
      // 轮询与卡片上的下载一致：连续失败封顶终止
      let tries = 0, misses = 0;
      const timer = setInterval(async () => {
        tries += 1;
        try {
          const r = await fetch(`/api/x/works/${id}`).then(x => x.json());
          misses = 0;
          if (r.saved_status === 'done') {
            clearInterval(timer);
            window.location.reload();
          } else if (r.saved_status === 'failed') {
            clearInterval(timer);
            showToast('下载失败，可回列表重试', true);
            if (hint) hint.textContent = '';
            xvDlBtn.disabled = false;
            xvDlBtn.textContent = '重试下载';
          }
        } catch { misses += 1; }
        if (misses >= 5 || tries > 200) {
          clearInterval(timer);
          if (hint) hint.textContent = '仍在下载中，稍后刷新本页查看';
        }
      }, 3000);
    } catch (e) {
      showToast('下载失败：' + e.message, true);
      xvDlBtn.disabled = false;
      xvDlBtn.textContent = '重试下载';
    }
  });
}

// ── 已下载分支：删除记录按钮 ──
$('#xvDeleteBtn')?.addEventListener('click', async () => {
  const id = window.location.pathname.split('/').pop();
  const ok = await pvConfirm('删除这条 X 作品记录？',
    { detail: '只删记录，已下载的图片文件保留在磁盘上。', okText: '删除' });
  if (!ok) return;
  const btn = $('#xvDeleteBtn');
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/x/works/${id}`, {
      method: 'DELETE', headers: { 'X-CSRF-Token': xvCsrf },
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).error || `HTTP ${resp.status}`);
    showToast('已删除，正在返回列表');
    setTimeout(() => { window.location.href = '/x'; }, 600);
  } catch (e) {
    showToast('删除失败：' + e.message, true);
    btn.disabled = false;
  }
});
