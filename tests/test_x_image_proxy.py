"""`/ximg` 图片代理的边界：只允许 pbs.twimg.com、缓存命中不联网、失败冷却。"""
from __future__ import annotations

import base64
import hashlib
import time

import pytest

from pixivviewer.routes import xfeed


def _b64(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')


class TestXImageProxy:
    def test_rejects_non_twimg_host(self, client):
        assert client.get('/ximg/' + _b64('https://evil.example.com/a.jpg')).status_code == 403

    def test_rejects_non_image_path(self, client):
        assert client.get('/ximg/' + _b64('https://pbs.twimg.com/media/a.html')).status_code == 403

    def test_rejects_bad_base64(self, client):
        assert client.get('/ximg/!!!not-base64!!!').status_code == 400

    def test_cache_hit_does_not_touch_network(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(xfeed, '_X_IMAGE_CACHE_DIR', str(tmp_path))
        url = 'https://pbs.twimg.com/media/cached.jpg?name=orig'
        key = hashlib.md5(url.encode()).hexdigest()
        (tmp_path / f'{key}.jpg').write_bytes(b'\xff\xd8\xff\xe0fake-jpeg')

        resp = client.get('/ximg/' + _b64(url))
        assert resp.status_code == 200
        assert resp.mimetype == 'image/jpeg'
        assert resp.data.startswith(b'\xff\xd8')

    def test_failure_cooldown_returns_502_without_request(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(xfeed, '_X_IMAGE_CACHE_DIR', str(tmp_path))
        url = 'https://pbs.twimg.com/media/cooldown.jpg?name=orig'
        with xfeed._x_thumb_failed_lock:
            xfeed._x_thumb_failed[url] = time.time()
        try:
            assert client.get('/ximg/' + _b64(url)).status_code == 502
        finally:
            with xfeed._x_thumb_failed_lock:
                xfeed._x_thumb_failed.pop(url, None)
