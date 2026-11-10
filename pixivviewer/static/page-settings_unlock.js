const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

$('#passwordInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#unlockBtn').click();
});

$('#unlockBtn').addEventListener('click', async () => {
  const pw = $('#passwordInput').value;
  if (!pw) return;
  $('#unlockBtn').disabled = true;
  $('#unlockBtn').textContent = '验证中...';
  $('#unlockError').style.display = 'none';

  try {
    const r = await fetch('/api/settings/unlock', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }),
    });
    if (r.ok) {
      window.location.href = '/settings';
    } else {
      $('#unlockError').style.display = 'block';
    }
  } catch {
    $('#unlockError').textContent = '网络错误';
    $('#unlockError').style.display = 'block';
  } finally {
    $('#unlockBtn').disabled = false;
    $('#unlockBtn').textContent = '验证';
  }
});
