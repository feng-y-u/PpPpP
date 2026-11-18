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

    def test_cache_hit_mime_is_pinned_by_extension(self, client, tmp_path, monkeypatch):
        """缓存命中的 Content-Type 按扩展名定死：mimetypes 在精简数据库/老 Python
        下认不出 .webp（None → image/jpeg → nosniff 拒绝渲染），缓存命中反而
        比首取更糟 —— /thumb 的 .meta 教训同款，这里用显式映射钉住。
        同一不变式的输入变体（四种入口扩展名）合成一条循环。
        """
        monkeypatch.setattr(xfeed, '_X_IMAGE_CACHE_DIR', str(tmp_path))
        cases = {
            '.jpg': ('image/jpeg', b'\xff\xd8'),
            '.jpeg': ('image/jpeg', b'\xff\xd8'),
            '.png': ('image/png', b'\x89PNG'),
            '.webp': ('image/webp', b'RIFF'),
        }
        for ext, (expected_mime, magic) in cases.items():
            url = f'https://pbs.twimg.com/media/cached{ext}?name=orig'
            key = hashlib.md5(url.encode()).hexdigest()
            (tmp_path / f'{key}{ext}').write_bytes(magic)

            resp = client.get('/ximg/' + _b64(url))
            assert resp.status_code == 200, f'{ext} 缓存命中失败'
            assert resp.mimetype == expected_mime, f'{ext} 命中时 Content-Type 走样'

    def test_failed_download_leaves_no_tmp_garbage(self, client, tmp_path, monkeypatch):
        """取图中途异常不得留下 *.tmp：容量淘汰器只认 md5 命名，tmp 是只进不出的垃圾。

        断流点必须在**写入过程中**（先写一块再炸），这样才能证明清理路径真的
        覆盖了"半写文件"；在发请求那一刻就失败的话测不出任何东西。
        """
        import requests as requests_mod

        monkeypatch.setattr(xfeed, '_X_IMAGE_CACHE_DIR', str(tmp_path))
        url = 'https://pbs.twimg.com/media/explode.jpg?name=orig'

        class _BoomResponse:
            headers = {'Content-Type': 'image/jpeg'}
            is_redirect = False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=8192):
                yield b'\xff\xd8fake'
                raise requests_mod.ConnectionError('mid-stream boom')

        class _BoomSession:
            headers = {}

            def get(self, *a, **kw):
                return _BoomResponse()

            def close(self):
                pass

        monkeypatch.setattr(xfeed.requests, 'Session', lambda: _BoomSession())
        with xfeed._x_thumb_failed_lock:
            xfeed._x_thumb_failed.pop(url, None)
        try:
            assert client.get('/ximg/' + _b64(url)).status_code == 502
        finally:
            with xfeed._x_thumb_failed_lock:
                xfeed._x_thumb_failed.pop(url, None)
        assert list(tmp_path.glob('*.tmp')) == [], '失败后残留 tmp 文件'
        assert list(tmp_path.iterdir()) == [], '失败后不得留下任何缓存文件'

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
