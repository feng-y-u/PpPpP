import json
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


class TestAtomicWriteJson:
    """settings.json 的原子写（审计 S16）。

    直接 open('w') + json.dump 时，进程被杀 / 磁盘写满 / 断电会留下**截断的**
    settings.json；读取侧遇损坏只能整体回退默认值，用户刚改的一整份配置全丢。
    """

    def test_write_replaces_content_and_leaves_no_tmp(self, tmp_path):
        target = tmp_path / 'settings.json'
        target.write_text('{"old": 1}', encoding='utf-8')

        helpers._atomic_write_json(str(target), {'new': 2, '中文': '中文值'})

        assert json.loads(target.read_text(encoding='utf-8')) == {'new': 2, '中文': '中文值'}
        assert list(tmp_path.iterdir()) == [target], '同目录不得残留 .tmp'

    def test_write_creates_missing_directory(self, tmp_path):
        target = tmp_path / 'nested' / 'deep' / 'settings.json'

        helpers._atomic_write_json(str(target), {'a': 1})

        assert json.loads(target.read_text(encoding='utf-8')) == {'a': 1}
        assert not os.path.exists(f'{target}.tmp')

    def test_replace_failure_keeps_old_bytes_and_cleans_tmp(self, tmp_path, monkeypatch):
        """替换失败（磁盘满/权限）时旧文件必须原样，且不留半份 tmp。"""
        target = tmp_path / 'settings.json'
        original = '{"keep": "me"}'
        target.write_text(original, encoding='utf-8')

        def _boom(src, dst, *a, **kw):
            raise OSError('模拟 os.replace 失败')

        monkeypatch.setattr(helpers.os, 'replace', _boom)

        with pytest.raises(OSError):
            helpers._atomic_write_json(str(target), {'should': 'not land'})

        assert target.read_text(encoding='utf-8') == original, '旧内容必须完整保留'
        assert list(tmp_path.iterdir()) == [target], '失败路径也必须清掉 .tmp'

    def test_dump_failure_preserves_old_file(self, tmp_path, monkeypatch):
        """序列化阶段就失败（不可序列化对象）：旧文件不动，也不留 tmp。"""
        target = tmp_path / 'settings.json'
        original = '{"keep": "me"}'
        target.write_text(original, encoding='utf-8')

        class _Unserializable:
            pass

        with pytest.raises(TypeError):
            helpers._atomic_write_json(str(target), {'bad': _Unserializable()})

        assert target.read_text(encoding='utf-8') == original


class TestAtomicWriteText:
    """纯文本的原子写（审计 S19 遗留：设置页写 cookies.txt）。

    与 JSON 版同一套纪律，但读侧完全不同：`fetcher._load_cookie()` 在其它线程读同一
    路径，读到空串还会**把空值连同 mtime 一起缓存住**。所以这里除了"失败不动旧文件"，
    还多两条契约：不创建父目录（目录写错要明确失败）、Windows 共享冲突有界重试。
    """

    def test_write_replaces_content_and_leaves_no_tmp(self, tmp_path):
        target = tmp_path / 'cookies.txt'
        target.write_text('PHPSESSID=old\n', encoding='utf-8')

        helpers._atomic_write_text(str(target), 'PHPSESSID=new\n')

        assert target.read_text(encoding='utf-8') == 'PHPSESSID=new\n'
        assert list(tmp_path.iterdir()) == [target], '同目录不得残留 .tmp'

    def test_write_creates_the_file_when_missing(self, tmp_path):
        target = tmp_path / 'cookies.txt'

        helpers._atomic_write_text(str(target), 'PHPSESSID=fresh\n')

        assert target.read_text(encoding='utf-8') == 'PHPSESSID=fresh\n'
        assert list(tmp_path.iterdir()) == [target]

    def test_does_not_create_missing_parent_directory(self, tmp_path):
        """父目录不存在要**明确失败**，不能静默补出来。

        与 `_atomic_write_json` 的差别是刻意的：Cookie 落点由 `config.COOKIE_PATH`
        决定（Linux 上可能是 `/etc/pixiv-viewer/`），目录不存在属于部署配置错误，
        悄悄 makedirs 会把错误藏起来；旧实现 `open(path, 'w')` 也是直接失败。
        """
        target = tmp_path / 'missing-dir' / 'cookies.txt'

        with pytest.raises(FileNotFoundError):
            helpers._atomic_write_text(str(target), 'PHPSESSID=x\n')

        assert not (tmp_path / 'missing-dir').exists()

    def test_replace_failure_keeps_old_bytes_and_cleans_tmp(self, tmp_path, monkeypatch):
        """替换失败时旧 Cookie 必须原样可用，且不留半份 tmp。

        旧实现"先截断再写"会把保存失败升级成"立刻断网"；原子写不会。
        """
        target = tmp_path / 'cookies.txt'
        original = 'PHPSESSID=still-good\n'
        target.write_text(original, encoding='utf-8')

        def _boom(src, dst, *a, **kw):
            raise OSError('模拟 os.replace 失败')

        monkeypatch.setattr(helpers.os, 'replace', _boom)

        with pytest.raises(OSError):
            helpers._atomic_write_text(str(target), 'PHPSESSID=new\n')

        assert target.read_text(encoding='utf-8') == original
        assert list(tmp_path.iterdir()) == [target], '失败路径也必须清掉 .tmp'

    def test_permission_error_is_retried(self, tmp_path, monkeypatch):
        """Windows：读侧正持着目标文件时 `os.replace` 抛 PermissionError，要有界重试。

        POSIX 的 rename 没有这个问题，所以这条是为开发机（以及可能的 Windows 部署）
        准备的，不重试就会把"保存 Cookie"变成偶发 500。
        """
        target = tmp_path / 'cookies.txt'
        target.write_text('PHPSESSID=old\n', encoding='utf-8')
        real_replace = helpers.os.replace
        attempts = []

        def _flaky(src, dst, *a, **kw):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(13, 'Permission denied（模拟共享冲突）')
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(helpers.os, 'replace', _flaky)

        helpers._atomic_write_text(str(target), 'PHPSESSID=new\n')

        assert len(attempts) == 3, '前两次失败后应重试，第三次成功'
        assert target.read_text(encoding='utf-8') == 'PHPSESSID=new\n'
        assert list(tmp_path.iterdir()) == [target], '成功后不得残留 .tmp'

    def test_permission_error_after_retries_is_raised(self, tmp_path, monkeypatch):
        """重试耗尽要原样抛出（调用方据此回 500），并清掉 tmp。"""
        target = tmp_path / 'cookies.txt'
        original = 'PHPSESSID=old\n'
        target.write_text(original, encoding='utf-8')
        attempts = []

        def _always_denied(src, dst, *a, **kw):
            attempts.append(1)
            raise PermissionError(13, 'Permission denied（模拟持续占用）')

        monkeypatch.setattr(helpers.os, 'replace', _always_denied)

        with pytest.raises(PermissionError):
            helpers._atomic_write_text(str(target), 'PHPSESSID=new\n')

        assert len(attempts) == 5, '重试次数必须有界'
        assert target.read_text(encoding='utf-8') == original
        assert list(tmp_path.iterdir()) == [target], '失败路径也必须清掉 .tmp'
        assert list(tmp_path.iterdir()) == [target]
