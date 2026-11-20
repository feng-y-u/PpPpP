// ── X 作品本地看图页：删除记录按钮（删除后跳回列表）──
const xvCsrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

$('#xvDeleteBtn')?.addEventListener('click', async () => {
  const id = window.location.pathname.split('/').pop();
  if (!confirm('删除这条 X 作品记录？（已下载的图片文件不会删除）')) return;
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
