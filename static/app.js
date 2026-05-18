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
