import os

import pytest

import config
import helpers


def _mk(path, name, size, mtime):
    p = path / name
    p.write_bytes(b'x' * size)
    os.utime(p, (mtime, mtime))
    return p


def _digest(i):
    """缓存文件的命名格式：32 位十六进制 md5。"""
    return f'{i:032x}'


def _total(path):
    return sum(f.stat().st_size for f in path.iterdir() if f.is_file())


class TestImageCacheEviction:
    """instance/image_cache 的容量淘汰。

    该目录过去只写不删、磁盘无限增长（1 万条预取缓存的规模下可达 GB 级）。
    淘汰的核心风险是误删目录里的其他文件，所以"只认 md5 命名"这条必须守住。
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, 'IMAGE_CACHE_MAX_BYTES', 1000)
        monkeypatch.setattr(config, 'IMAGE_CACHE_TARGET_RATIO', 0.9)
        monkeypatch.setattr(config, 'IMAGE_CACHE_CLEANUP_INTERVAL', 300.0)
        helpers._image_cache_last_scan = 0.0
        self.dir = tmp_path / 'image_cache'
        self.dir.mkdir()

    def test_under_limit_deletes_nothing(self):
        for i in range(4):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        assert helpers.enforce_image_cache_limit(str(self.dir), force=True) == 0
        assert _total(self.dir) == 800

    def test_over_limit_evicts_oldest_first(self):
        # 10 个 × 200B = 2000B，上限 1000、目标 900 → 应删到剩 4 个（800B）
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        removed = helpers.enforce_image_cache_limit(str(self.dir), force=True)
        assert removed > 0
        assert _total(self.dir) <= 900
        names = sorted(os.listdir(self.dir))
        assert names == [f'{_digest(i)}.jpg' for i in range(6, 10)], \
            '应保留最新的 4 个，淘汰最旧的'

    def test_matching_meta_is_removed_alongside(self):
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
            _mk(self.dir, f'{_digest(i)}.jpg.meta', 10, 100 + i)
        helpers.enforce_image_cache_limit(str(self.dir), force=True)
        left = set(os.listdir(self.dir))
        assert f'{_digest(0)}.jpg' not in left
        assert f'{_digest(0)}.jpg.meta' not in left, '.meta 应随图片一起删掉'

    @pytest.mark.parametrize('name', [
        'notes.txt',                 # 完全不是缓存文件
        'abc.jpg',                   # 名字不是 32 位 md5
        f'{"z" * 32}.jpg',           # 长度够但不是十六进制
        f'{_digest(99)}.jpg.bak',    # 后缀不对
    ])
    def test_never_touches_foreign_files(self, name):
        """目录里的非缓存文件必须原样保留——这是淘汰逻辑的安全底线。"""
        foreign = _mk(self.dir, name, 500, 1)      # mtime 最早，最该被"淘汰"
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        helpers.enforce_image_cache_limit(str(self.dir), force=True)
        assert foreign.exists(), f'{name} 不应被淘汰逻辑删除'

    def test_throttled_within_interval(self):
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        first = helpers.enforce_image_cache_limit(str(self.dir), force=True)
        assert first > 0
        # 立刻再写一批让它重新超限，但间隔未到 → 应跳过
        for i in range(10, 20):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 200 + i)
        assert helpers.enforce_image_cache_limit(str(self.dir)) == 0

    def test_force_bypasses_throttle(self):
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        helpers.enforce_image_cache_limit(str(self.dir), force=True)
        for i in range(10, 20):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 200 + i)
        assert helpers.enforce_image_cache_limit(str(self.dir), force=True) > 0

    def test_missing_dir_is_noop(self):
        assert helpers.enforce_image_cache_limit(
            os.path.join(str(self.dir), 'nope'), force=True) == 0
