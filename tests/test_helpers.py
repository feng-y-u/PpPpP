import ast
import json
import os
import pathlib

import pytest

from pixivviewer import config
from pixivviewer import helpers


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

    def test_threshold_and_eviction_order(self):
        """同一套准备下的容量行为：没超限一个不动、超限按最旧优先删、`.meta` 随图一起走。

        末尾一并覆盖"目录不存在"这条边界（安静 no-op，不抛异常）。这几段拆成多条用例时
        前置准备完全一样、断言同构、只有输入与期望值不同，故合在一条里依次断言。
        """
        for i in range(4):                      # 4 × 200B = 800B < 上限 1000
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        assert helpers.enforce_image_cache_limit(str(self.dir), force=True) == 0
        assert _total(self.dir) == 800

        # 10 个 × 200B = 2000B，上限 1000、目标 900 → 应删到剩 4 个（800B）
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        removed = helpers.enforce_image_cache_limit(str(self.dir), force=True)
        assert removed > 0
        assert _total(self.dir) <= 900
        names = sorted(os.listdir(self.dir))
        assert names == [f'{_digest(i)}.jpg' for i in range(6, 10)], \
            '应保留最新的 4 个，淘汰最旧的'

        # 同名 .meta 必须随图片一起删：留着就是一个永远没人清理的孤儿
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
            _mk(self.dir, f'{_digest(i)}.jpg.meta', 10, 100 + i)
        helpers.enforce_image_cache_limit(str(self.dir), force=True)
        left = set(os.listdir(self.dir))
        assert f'{_digest(0)}.jpg' not in left
        assert f'{_digest(0)}.jpg.meta' not in left, '.meta 应随图片一起删掉'

        # 目录不存在 = 没有可淘汰的东西
        assert helpers.enforce_image_cache_limit(
            os.path.join(str(self.dir), 'nope'), force=True) == 0

    def test_never_touches_foreign_files(self):
        """目录里的非缓存文件必须原样保留——这是淘汰逻辑的安全底线。

        四种"看着像缓存、其实不是"的命名逐个过一遍（理由见行内注释），每例都放在 mtime
        最旧的位置：真被淘汰逻辑盯上就一定先删它。每例的失败消息带上是哪个命名。
        """
        for name in (
            'notes.txt',                 # 完全不是缓存文件
            'abc.jpg',                   # 名字不是 32 位 md5
            f'{"z" * 32}.jpg',           # 长度够但不是十六进制
            f'{_digest(99)}.jpg.bak',    # 后缀不对
        ):
            foreign = _mk(self.dir, name, 500, 1)      # mtime 最早，最该被"淘汰"
            for i in range(10):
                _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
            helpers.enforce_image_cache_limit(str(self.dir), force=True)
            assert foreign.exists(), f'{name} 不应被淘汰逻辑删除'

    def test_throttle_and_force(self):
        """节流与 `force` 是一对：间隔未到直接跳过，`force=True` 不受节流限制。

        两件事共用同一段准备（先超限 → 再写一批让它重新超限），第二段就是第一段的
        对照，拆开只会把这段准备抄两遍。
        """
        for i in range(10):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 100 + i)
        first = helpers.enforce_image_cache_limit(str(self.dir), force=True)
        assert first > 0
        # 立刻再写一批让它重新超限，但间隔未到 → 应跳过
        for i in range(10, 20):
            _mk(self.dir, f'{_digest(i)}.jpg', 200, 200 + i)
        assert helpers.enforce_image_cache_limit(str(self.dir)) == 0

        # 同一批超限文件，force=True 时必须真的处理（节流只挡"自动触发"）
        assert helpers.enforce_image_cache_limit(str(self.dir), force=True) > 0


class TestAtomicWriteJson:
    """settings.json 的原子写（审计 S16）。

    直接 open('w') + json.dump 时，进程被杀 / 磁盘写满 / 断电会留下**截断的**
    settings.json；读取侧遇损坏只能整体回退默认值，用户刚改的一整份配置全丢。
    """

    def test_success_path_replaces_content_and_creates_missing_directory(self, tmp_path):
        """成功路径的两种起点：替换已有文件、以及父目录还不存在（JSON 版会补建）。

        两次写都不许留下半份 `.tmp` —— 那正是"原子写"的意义（下次写入踩到半份内容，
        用户目录里还会多一个含义不明的文件）。两段准备只差目标路径，故合在一起。
        """
        target = tmp_path / 'settings.json'
        target.write_text('{"old": 1}', encoding='utf-8')

        helpers._atomic_write_json(str(target), {'new': 2, '中文': '中文值'})

        assert json.loads(target.read_text(encoding='utf-8')) == {'new': 2, '中文': '中文值'}
        assert list(tmp_path.iterdir()) == [target], '同目录不得残留 .tmp'

        nested = tmp_path / 'nested' / 'deep' / 'settings.json'

        helpers._atomic_write_json(str(nested), {'a': 1})

        assert json.loads(nested.read_text(encoding='utf-8')) == {'a': 1}
        assert not os.path.exists(f'{nested}.tmp')

    def test_failure_keeps_old_bytes_and_cleans_tmp(self, tmp_path, monkeypatch):
        """两个失败阶段都不许破坏旧文件、也不许留半份 tmp。

        ① 替换失败（磁盘满/权限）：`os.replace` 抛 OSError；
        ② 序列化阶段就失败（不可序列化对象）：`json.dump` 抛 TypeError。
        两者的根因不同，但对旧文件的保证是同一条，而且断言形状完全一样。
        """
        # ① 替换失败
        target = tmp_path / 'settings.json'
        original = '{"keep": "me"}'
        target.write_text(original, encoding='utf-8')
        real_replace = helpers.os.replace

        def _boom(src, dst, *a, **kw):
            raise OSError('模拟 os.replace 失败')

        monkeypatch.setattr(helpers.os, 'replace', _boom)

        with pytest.raises(OSError):
            helpers._atomic_write_json(str(target), {'should': 'not land'})

        assert target.read_text(encoding='utf-8') == original, '旧内容必须完整保留'
        assert list(tmp_path.iterdir()) == [target], '失败路径也必须清掉 .tmp'

        # ② 序列化阶段失败：旧文件同样一个字节都不能动（先恢复正常 os.replace）
        monkeypatch.setattr(helpers.os, 'replace', real_replace)

        class _Unserializable:
            pass

        with pytest.raises(TypeError):
            helpers._atomic_write_json(str(target), {'bad': _Unserializable()})

        assert target.read_text(encoding='utf-8') == original
        assert list(tmp_path.iterdir()) == [target], '序列化失败也必须清掉 .tmp'


class TestAtomicWriteText:
    """纯文本的原子写（审计 S19 遗留：设置页写 cookies.txt）。

    与 JSON 版同一套纪律，但读侧完全不同：`pixiv_client._load_cookie()` 在其它线程读同一
    路径，读到空串还会**把空值连同 mtime 一起缓存住**。所以这里除了"失败不动旧文件"，
    还多两条契约：不创建父目录（目录写错要明确失败）、Windows 共享冲突有界重试。
    """

    def test_success_path_replaces_content_and_leaves_no_tmp(self, tmp_path):
        """成功路径的两种起点：目标已存在（替换）与还不存在（新建），都不留 `.tmp`。

        读侧（`pixiv_client._load_cookie()`）只认"要么旧值要么新值"，所以"不留半份"
        与"内容正确"是同一件事的两面，一起验。
        """
        target = tmp_path / 'cookies.txt'
        target.write_text('PHPSESSID=old\n', encoding='utf-8')

        helpers._atomic_write_text(str(target), 'PHPSESSID=new\n')

        assert target.read_text(encoding='utf-8') == 'PHPSESSID=new\n'
        assert list(tmp_path.iterdir()) == [target], '同目录不得残留 .tmp'

        # 目标还不存在（父目录已存在）：新建而不是失败
        fresh_dir = tmp_path / 'fresh'
        fresh_dir.mkdir()
        fresh = fresh_dir / 'cookies.txt'

        helpers._atomic_write_text(str(fresh), 'PHPSESSID=fresh\n')

        assert fresh.read_text(encoding='utf-8') == 'PHPSESSID=fresh\n'
        assert list(fresh_dir.iterdir()) == [fresh]

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

    def test_permission_error_retry_is_bounded(self, tmp_path, monkeypatch):
        """Windows：读侧正持着目标文件时 `os.replace` 抛 PermissionError，要有界重试。

        POSIX 的 rename 没有这个问题，所以这条是为开发机（以及可能的 Windows 部署）
        准备的：不重试会把"保存 Cookie"变成偶发 500，无上限重试则会把一次请求挂死 ——
        "次数有界"才是契约本身，所以"前两次失败后成功"与"一直失败到耗尽"必须一起验。
        两段准备完全一样，只有出牌方式不同。
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

        # 一直失败：重试耗尽要原样抛出（调用方据此回 500），并清掉 tmp
        attempts.clear()
        target.write_text('PHPSESSID=old\n', encoding='utf-8')

        def _always_denied(src, dst, *a, **kw):
            attempts.append(1)
            raise PermissionError(13, 'Permission denied（模拟持续占用）')

        monkeypatch.setattr(helpers.os, 'replace', _always_denied)

        with pytest.raises(PermissionError):
            helpers._atomic_write_text(str(target), 'PHPSESSID=new\n')

        assert len(attempts) == 5, '重试次数必须有界'
        assert target.read_text(encoding='utf-8') == 'PHPSESSID=old\n'
        assert list(tmp_path.iterdir()) == [target], '失败路径也必须清掉 .tmp'


class TestOriginalToResized:
    """原图 URL → 中图 URL（`_original_to_resized`）。

    回归来源：中图扩展名曾经沿用**原图的**扩展名，于是 PNG 原图的作品被拼成
    `_master1200.png` —— 图床对这种地址一律 404（master1200 是统一重编码的 JPEG，
    `.jpg` 才是唯一有效形式）。实测同一作品 `.png` → 404、`.jpg` → 200。
    库里 PNG 原图是多数（实测 2120 条原图地址里 1375 条是 `.png`），所以症状是
    "**很多**作品的中图加载不出来"，而不是边缘情况。
    """

    def test_png_original_maps_to_jpg_master(self, monkeypatch):
        """核心回归 + 同一条代码路径上的另外三条规则。

        核心回归：PNG 原图的中图地址必须是 `.jpg`，而且地址形状要**逐字符**正确
        （只查后缀会漏掉 `c/600x600`、`img-master` 这些段落被写错的情形）。

        随后三条（扩展名矩阵恒 `.jpg`、query 被丢弃、尺寸取自 `MEDIUM_IMAGE_SIZE`）
        与它走的是同一个函数、同一行拼接、同一套"造个 img-original 地址再调"的准备，
        只有输入与期望值不同 —— 拆成多条用例只是把这段准备抄几遍。
        """
        url = 'https://i.pximg.net/img-original/img/2026/08/29/17/21/55/149022521_p0.png'
        assert helpers._original_to_resized(url) == (
            f'https://i.pximg.net/c/{config.MEDIUM_IMAGE_SIZE}x{config.MEDIUM_IMAGE_SIZE}'
            '/img-master/img/2026/08/29/17/21/55/149022521_p0_master1200.jpg')

        # 任何原图扩展名都产出 `.jpg` 中图（不是"jpg 才转、其它照抄"）
        for ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
            out = helpers._original_to_resized(
                f'https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p0.{ext}')
            assert out.endswith('_master1200.jpg'), f'{ext} 原图的中图也必须是 .jpg：{out}'

        assert helpers._original_to_resized(
            'https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p0.png?foo=1'
        ) == ('https://i.pximg.net/c/600x600/img-master/img/2026/01/01/00/00/00/'
              '123_p0_master1200.jpg')

        monkeypatch.setattr(helpers, 'MEDIUM_IMAGE_SIZE', 540)
        assert helpers._original_to_resized(
            'https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p0.jpg'
        ).startswith('https://i.pximg.net/c/540x540/img-master/')

    def test_non_original_url_is_returned_unchanged(self):
        """不匹配就原样返回：调用方的候选链本就有"原图"这一档，别造出畸形地址。"""
        for url in (
            # 已经是中图/缩略图形态：没有 img-original 段
            'https://i.pximg.net/img-master/img/2026/01/01/00/00/00/123_p0_master1200.jpg',
            'https://i.pximg.net/c/250x250_80_a2/img-master/img/2026/01/01/00/00/00/'
            '123_p0_square1200.jpg',
            # 非白名单主机
            'https://example.com/img-original/img/2026/01/01/00/00/00/123_p0.jpg',
            '',
        ):
            assert helpers._original_to_resized(url) == url, f'应原样返回：{url!r}'


# 模块级不得 import 的"上游"：models 会绑 engine，其余是业务/组装层。
# 包化后有两副面孔：`from .models import ...`（module 名仍是 models）与
# `from . import models`（module 为 None、名字在 names 里），下面按"末段名"统一判定。
_FORBIDDEN_MODULE_IMPORTS = {'models', 'fetcher', 'app', 'background', 'middleware',
                             'gallery'}


def test_helpers_does_not_import_models_at_module_level():
    """`helpers` 必须是叶子：模块级不得 import `models`（以及业务/组装层），且修法要在。

    为什么值得钉住：`models.py` 在 import 那一刻就 `create_engine(config.DATABASE_PATH)`，
    engine 建好后再改 `config` 也改不动它 —— 于是**谁先 import 到 models** 决定了本轮
    进程连的是哪个库。2026-09-30 的测试清空生产库事故正是这条链：一个 `-p` 加载的 pytest
    插件在模块级 `import helpers`（helpers 当时 `from models import ...`）→ engine 绑到
    真实库 → `clean_db` 清空了 1456 件作品。

    后半段（延迟 import 必须存在）与前半段是一件事的两面：只断言"没在模块级 import"是
    不够的 —— 把 models 的使用整段删掉同样能过，而那时 `query_cached_tag` 会在运行期抛
    `NameError`。两段合在一个用例里，因为它们共用同一次 AST 解析，且违反任何一条都意味着
    "这条边界已经不成立"。

    检查用 AST 而不是"新解释器里 import helpers 再看 sys.modules"：本文件跑在测试进程里，
    `models` 早已被 conftest/app 导入，运行期断言看不出差别；AST 直接回答"这份源码在
    模块级 import 了什么"，与被测进程状态无关。函数体内的延迟 import 不算（那正是修法）。
    """
    tree = ast.parse(pathlib.Path(helpers.__file__).read_text(encoding='utf-8'))

    def _targets(node) -> list[str]:
        """一条 import 命中的模块名（按末段判定，相对导入同样适用）。

        `from .models import X` 的 node.module 是 'models'；`from . import models`
        的 node.module 是 None、模块名在 names 里 —— 两种写法都得算。
        """
        if isinstance(node, ast.ImportFrom):
            if node.module:
                return [node.module.split('.')[-1]]
            return [alias.name.split('.')[0] for alias in node.names]
        return [alias.name.split('.')[-1] for alias in node.names]

    offenders: list[str] = []
    for node in tree.body:                   # 只看模块级顶层语句
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            hits = [t for t in _targets(node) if t in _FORBIDDEN_MODULE_IMPORTS]
            if hits:
                offenders.append(f'{ast.unparse(node)}（第 {node.lineno} 行）')

    assert not offenders, (
        f'helpers 不再是叶子，模块级出现了违规 import：{offenders}。'
        f'需要 DB 的函数请在函数体内 `from .models import ...`。'
    )

    func = next((node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == 'query_cached_tag'), None)
    assert func is not None, 'query_cached_tag 不见了？'
    lazy = [node for node in ast.walk(func)
            if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[-1] == 'models']
    assert lazy, 'query_cached_tag 必须在函数体内延迟 `from .models import ...`（见该函数注释）'


def test_fetch_original_urls_uses_pooled_session(monkeypatch):
    """惰性拉原图是详情页/下载入口的热点，必须复用线程内连接池（审计 P2-3）。

    每调用一次 `build_pixiv_session()` 都会新建 urllib3 连接池，等于每次详情页
    补原图都重做一次 TCP+TLS 握手；这里钉住它走 `get_pooled_session()`。
    """
    from pixivviewer import pixiv_client
    session = object()
    monkeypatch.setattr(pixiv_client, 'get_pooled_session', lambda: session)
    monkeypatch.setattr(
        pixiv_client, 'fetch_original_urls',
        lambda sess, pid: [f'{sess}-{pid}'])
    monkeypatch.setattr(
        pixiv_client, 'build_pixiv_session',
        lambda: (_ for _ in ()).throw(AssertionError('不得新建 Session')))

    assert helpers._fetch_original_urls(7) == [f'{session}-7']
