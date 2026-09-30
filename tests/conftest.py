import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── ⚠ 防线一：`models` 必须在本文件覆盖 DATABASE_PATH **之后**才被导入 ──
# `models.py` 在 import 的那一刻就 `create_engine(config.DATABASE_PATH)`；engine 一旦
# 建好，本文件下面那些 `config.DATABASE_PATH = <临时库>` 就**再也改不动它**。所以任何
# 在 conftest 之前 import 到 models 的东西 —— `-p` 加载的自定义 pytest 插件、
# `sitecustomize`、手写的 runpy/包装脚本 —— 都会让整轮测试直连真实库。
# 2026-09-30 就真的发生过：一个变异验证用的 `-p` 插件在模块级 `import helpers`，
# 而 `helpers` 又 `from models import ...`，于是 engine 绑到生产库，`clean_db` 把
# 1456 件作品的表清空了（靠迁移前自动备份救回）。
# 这里硬失败：宁可这一轮跑不起来，也绝不能写坏真实数据。
if 'models' in sys.modules:
    raise RuntimeError(
        'models 已在 conftest 之前被导入：engine 已绑到 config.DATABASE_PATH（可能是真实库），'
        '本轮测试会直连并清空它。不要在 conftest 之前 import 任何业务模块'
        '（典型来源：`-p` 插件的模块级 `import helpers` / `import app`）。'
        f'当前 sys.modules 里已有：{[m for m in sys.modules if m in ("models", "app", "helpers")]}'
    )

# ── ⚠ 必须在 import config 之前重定向实例目录 ──
# config.py 在 import 时就把实例目录（.cursor_secret / settings.json / pixiv.db /
# image_cache）算好了，app.py 还会往里写 .secret_key，事后覆盖无效：测试会读写
# 真实 instance/（首次运行生成密钥，或写 settings.json / image_cache / 发现表）。
# 这里强制覆盖（不用 setdefault）：外部环境变量不能把测试指向真实实例目录。
_TEST_INSTANCE_DIR = os.path.join(tempfile.gettempdir(), f'pixiv_test_instance_{os.getpid()}')
os.environ['PIXIV_INSTANCE_DIR'] = _TEST_INSTANCE_DIR

# ── ⚠ 必须在 import models/app 之前覆盖数据库路径 ──
# models.py 在 import 时即 create_engine(DATABASE_PATH)，事后覆盖无效，
# 会导致测试直连并清空生产数据库（2026-07-25 审查 P0-1）。
import config
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f'pixiv_test_{os.getpid()}.db')
config.DATABASE_PATH = _TEST_DB_PATH
config.AUTO_FOLLOW_INTERVAL = 0
config.PREFETCH_INTERVAL = 0

import pytest

from models import get_session, safe_commit, Illust, BlockedTag, DownloadLog, SearchCache


@pytest.fixture
def live_pixiv_required():
    """Skip opt-in live Pixiv tests when credentials are not configured."""
    if not os.path.exists(config.COOKIE_PATH):
        pytest.skip(f'live Pixiv test requires {config.COOKIE_PATH}')


@pytest.fixture(scope='session', autouse=True)
def _assert_engine_points_at_test_db():
    """防线二：整轮测试开始前确认 engine 指向临时库。

    防线一挡的是"conftest 之前就 import 了 models"；这一条挡的是其它任何路径把
    engine 绑到别处（例如 `models.DATABASE_PATH` 被外部改过、或将来有人把 engine
    改成延迟创建）。**autouse + session 级**是关键：即使整轮只跑一条
    `-k <关键字>` 选中的用例，它也会先执行 —— 上一级防线当时正是因为相关用例被
    `-k` 过滤掉才没能报出来。
    """
    import models
    url = str(models.engine.url)
    assert 'pixiv_test_' in url, (
        f'engine 指向了非测试库：{url}\n'
        f'测试会直连并清空它（期望路径含 "pixiv_test_"）。'
    )
    yield


@pytest.fixture(scope='session')
def app():
    from app import app as flask_app
    flask_app.config.update({'TESTING': True, 'SESSION_COOKIE_SECURE': False})
    yield flask_app
    # Windows 上需先释放引擎持有的文件句柄，否则 unlink 报 WinError 32
    import models
    models.engine.dispose()
    for suffix in ('', '-wal', '-shm'):
        p = _TEST_DB_PATH + suffix
        if os.path.exists(p):
            os.unlink(p)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    with get_session() as session:
        yield session
        session.rollback()


@pytest.fixture
def clean_db(db):
    """Clean all tables before the test.

    防线三：**删除动作前**再确认一次连的是测试库。前两道防线都在"测试开始前"，
    而这里是真正会丢数据的那一行；把校验贴着危险动作放，任何绕过前面防线的路径
    （将来新增的 fixture、直接 `Session(engine)`、外部工具复用这个夹具）都拦得住。
    """
    import models
    url = str(models.engine.url)
    assert 'pixiv_test_' in url, (
        f'拒绝清表：engine 指向 {url}，这不是测试库。'
        f'（2026-09-30 曾因 pytest 插件提前 import models 而清空真实库）'
    )
    for table in [BlockedTag, DownloadLog, Illust, SearchCache]:
        db.query(table).delete()
    db.commit()
    # 重置图库性能缓存（目录扫描 / 孤儿全表 pid），避免测试间脏数据残留
    import app as _app
    _app._scan_cache['ts'] = 0.0
    _app._db_pids_cache['ts'] = 0.0
    return db


@pytest.fixture
def sample_illust(clean_db):
    illust = Illust(
        pixiv_id=12345678,
        title='テスト作品',
        user_id=87654321,
        user_name='テスト画師',
        page_count=3,
        bookmark_count=1500,
        thumb_url='https://i.pximg.net/c/250x250/img/test.jpg',
        upload_date=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    illust.tags_list = ['test', 'sample', 'original']
    illust.original_urls_list = [
        'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/12345678_p0.jpg',
        'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/12345678_p1.jpg',
    ]
    clean_db.add(illust)
    safe_commit(clean_db)
    return illust
