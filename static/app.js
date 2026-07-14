// ── Pixiv Viewer — Shared Utils ──

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

// ── Download Functions (shared across index and detail pages) ──

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
  // Card-based UI (index.html search results)
  const card = document.querySelector(`.photo-card[data-pixiv-id="${pixivId}"]`);
  if (card) {
    const actions = card.querySelector('.photo-card-actions');
    if (actions) {
      actions.innerHTML = `<button class="btn btn-dl-done btn-sm dl-file-btn" data-pid="${pixivId}">下载</button>`;
      actions.querySelector('.dl-file-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        downloadFile(pixivId);
      });
    }
    return;
  }
  // Button-based UI (detail.html)
  const btn = document.getElementById('downloadBtn');
  if (btn) {
    btn.textContent = '⬇ 下载原图';
    btn.className = 'btn btn-dl-done';
    btn.disabled = false;
    btn.onclick = () => downloadFile(pixivId);
  }
}

function resetDlBtn(pixivId) {
  // Card-based UI (index.html search results)
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
  // Button-based UI (detail.html)
  const btn = document.getElementById('downloadBtn');
  if (btn) {
    btn.textContent = '⬇ 下载原图';
    btn.className = 'btn btn-soft';
    btn.disabled = false;
  }
}

function downloadFile(pixivId) {
  window.open(`/download_file/${pixivId}`, '_blank');
}
