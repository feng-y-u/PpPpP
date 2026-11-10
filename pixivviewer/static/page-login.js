const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
let nextUrl = '';
try { nextUrl = JSON.parse(document.getElementById('loginData')?.textContent || '"/"') || '/'; } catch {}

$('#passwordInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#unlockBtn').click();
});

$('#unlockBtn').addEventListener('click', async () => {
  const pw = $('#passwordInput').value;
  if (!pw) return;
  $('#unlockBtn').disabled = true;
  $('#unlockBtn').textContent = '登录中...';
  $('#unlockError').style.display = 'none';
  try {
    const r = await fetch('/login', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw, next: nextUrl }),
    });
    if (r.ok) {
      const d = await r.json();
      window.location.href = d.next || '/';
    } else if (r.status === 429) {
      $('#unlockError').textContent = '尝试过于频繁，请稍后再试';
      $('#unlockError').style.display = 'block';
    } else {
      $('#unlockError').style.display = 'block';
    }
  } catch {
    $('#unlockError').textContent = '网络错误';
    $('#unlockError').style.display = 'block';
  } finally {
    $('#unlockBtn').disabled = false;
    $('#unlockBtn').textContent = '登录';
  }
});
