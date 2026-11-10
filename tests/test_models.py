from datetime import datetime, timedelta, timezone

from pixivviewer.models import Illust, DownloadLog, SearchCache, safe_commit


class TestIllustCreate:
    def test_create_and_query(self, clean_db, sample_illust):
        fetched = clean_db.query(Illust).filter(Illust.pixiv_id == 12345678).first()
        assert fetched is not None
        assert fetched.title == 'テスト作品'
        assert fetched.user_name == 'テスト画師'
        assert fetched.bookmark_count == 1500


class TestUniqueConstraints:
    """两处 UNIQUE 约束是同一条不变式：重复行必须被拒绝（IntegrityError），
    且 `rollback()` 后 session 仍能继续用。只有被测的表与冲突列不同 —— 属于
    "前置准备同构、只有输入不同"，故合为一条用例（逐段带断言消息/注释）。
    """

    def test_duplicate_row_is_rejected(self, clean_db):
        import pytest
        import sqlalchemy.exc

        # ① illusts.pixiv_id 唯一
        clean_db.add(Illust(pixiv_id=999, title='a'))
        safe_commit(clean_db)
        clean_db.add(Illust(pixiv_id=999, title='b'))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            safe_commit(clean_db)
        clean_db.rollback()
        clean_db.query(Illust).filter(Illust.pixiv_id == 999).delete()
        clean_db.commit()

        # ② search_cache.tag 即主键
        clean_db.add(SearchCache(tag='tag1'))
        safe_commit(clean_db)
        clean_db.add(SearchCache(tag='tag1'))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            safe_commit(clean_db)
        clean_db.rollback()
        clean_db.query(SearchCache).filter(SearchCache.tag == 'tag1').delete()
        clean_db.commit()


class TestIllustJsonProperties:
    def test_json_list_properties_map_to_text_columns(self, clean_db, sample_illust):
        """三个 JSON 文本列（tags / original_urls / local_paths）共用同一套 `*_list`
        property 映射规则：读出来是 list、写回去能持久化、没值就是 None。

        同一不变式的多个列/状态变体（原先是 5 条用例），合成一条并逐段标注。
        """
        # ① tags_list：读初值 → 写新值 → commit/refresh 后仍是新值
        assert sample_illust.tags_list == ['test', 'sample', 'original']
        sample_illust.tags_list = ['new', 'tags']
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.tags_list == ['new', 'tags'], 'tags 写回后必须能原样读出来'

        # ② original_urls_list：同样是解析成 list
        urls = sample_illust.original_urls_list
        assert len(urls) == 2, f'original_urls 应解析出两页：{urls}'
        assert urls[0].startswith('https://i.pximg.net/')

        # ③ local_paths 默认是 None（"没下载" 与 "下载了 0 个" 语义不同）
        assert sample_illust.local_paths_list is None, '未下载的作品 local_paths 必须是 None'

        # ④ 写入后读回一致
        paths = [r'C:\downloads\test_p0.jpg', r'C:\downloads\test_p1.jpg']
        sample_illust.local_paths_list = paths
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.local_paths_list == paths

        # ⑤ 再置回 None
        sample_illust.local_paths_list = None
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.local_paths_list is None, '清空后读回必须还是 None'


class TestIllustToDict:
    def test_to_dict_output_contract(self, clean_db, sample_illust):
        """`to_dict` 的输出契约一次钉全（同一个被测函数、同一套 fixture）。

        ① **字段完整性**用精确集合断言：多一个字段（例如把 `local_paths` 漏出去）或
        少一个字段都是契约变更，图库要裁剪字段必须同步这里（见 AGENTS.md）；
        ② 关键字段的取值口径；
        ③ 回归：磁盘绝对路径**不得**出现在序列化输出里（仅模型属性保留）。
        """
        d = sample_illust.to_dict()
        expected_keys = {
            'id', 'pixiv_id', 'title', 'user_id', 'user_name', 'tags',
            'page_count', 'bookmark_count', 'upload_date', 'thumb_url',
            'original_urls', 'download_status',
            'downloaded_at', 'file_size', 'created_at',
        }
        assert set(d.keys()) == expected_keys, f'字段集变了：{set(d.keys()) ^ expected_keys}'
        assert d['pixiv_id'] == 12345678
        assert d['title'] == 'テスト作品'
        assert d['tags'] == ['test', 'sample', 'original']
        assert d['page_count'] == 3
        assert d['bookmark_count'] == 1500
        assert d['download_status'] is None

        # 本地路径落库后仍然只留在模型属性里，不进 to_dict
        sample_illust.local_paths_list = ['/data/a.jpg']
        sample_illust.download_status = 'done'
        safe_commit(clean_db)
        d = sample_illust.to_dict()
        assert 'local_paths' not in d
        assert 'local_dir' not in d
        assert d['download_status'] == 'done'


class TestIsoUtc:
    def test_utc_columns_get_offset_but_upload_date_does_not(self, clean_db):
        """「缓存页显示 UTC 时间」的根因回归：UTC 列必须序列化成带偏移量的字符串。

        存储层是 naive UTC（SQLite 不保存 tzinfo），裸 `isoformat()` 出来没有偏移标记，
        浏览器 `new Date()` 会按**本地时间**解释 → 上海用户看到的正是 UTC 时刻，
        且改时区不会挪动它。补 `+00:00` 后 `new Date()` 才会换算。
        `upload_date` 是 Pixiv 的 JST 墙面时间，不能被同一规则波及（补 Z 会整体偏 9 小时）。
        """
        from pixivviewer.models import iso_utc

        assert iso_utc(None) is None
        assert iso_utc(datetime(2025, 1, 3)) == '2025-01-03T00:00:00+00:00'
        assert iso_utc(
            datetime(2025, 1, 3, 9, 0, tzinfo=timezone(timedelta(hours=9)))
        ) == '2025-01-03T00:00:00+00:00', '带偏移量的输入要换算成 UTC 再输出'

        row = Illust(
            pixiv_id=424242,
            upload_date=datetime(2025, 1, 3, 12, 0),
            created_at=datetime(2025, 1, 3, 0, 0),
        )
        clean_db.add(row)
        safe_commit(clean_db)
        d = row.to_dict()
        assert d['created_at'] == '2025-01-03T00:00:00+00:00'
        assert d['upload_date'] == '2025-01-03T12:00:00', \
            'upload_date 是 JST 墙面时间，不能带 UTC 偏移'


class TestSearchCacheAliasTags:
    def test_alias_tags_list_default_roundtrip_and_corrupt_json(self, clean_db):
        """同义词列的三种状态：默认空、写回可读、坏 JSON 退化成空。

        坏 JSON 必须退化成 `[]` 而不是抛：标签列表接口与预取循环都会读这一列，
        一条脏数据不能让整个缓存页 500（与 `Illust.tags_list` 同一容错口径）。
        """
        row = SearchCache(tag='吉星排队')
        clean_db.add(row)
        safe_commit(clean_db)
        assert row.alias_tags_list == []

        row.alias_tags_list = ['星引擎party', '另一个同义词']
        safe_commit(clean_db)
        clean_db.refresh(row)
        assert row.alias_tags_list == ['星引擎party', '另一个同义词']

        row.alias_tags = 'not-json'
        assert row.alias_tags_list == []
        row.alias_tags = '{"unexpected": "object"}'
        assert row.alias_tags_list == []


class TestIllustDownloadStatus:
    def test_status_lifecycle_including_default(self, clean_db, sample_illust):
        """默认 None（= 从没下载过）与三态往返是同一条状态机不变式，合在一起看。"""
        assert sample_illust.download_status is None, '新建作品默认没有下载状态'

        sample_illust.download_status = 'downloading'
        safe_commit(clean_db)
        assert sample_illust.download_status == 'downloading'

        sample_illust.download_status = 'done'
        safe_commit(clean_db)
        assert sample_illust.download_status == 'done'

        sample_illust.download_status = None
        safe_commit(clean_db)
        assert sample_illust.download_status is None


class TestIsRemoved:
    def test_is_favorite_fully_removed(self, clean_db):
        """收藏夹功能移除必须**彻底**：表结构、模型属性、app 命名空间三处都不能留。

        三处是同一条不变式的三个截面（漏任何一处，旧代码都会以幽灵列 /
        AttributeError 的形式复活），且共用同一套准备，故合为一条用例。
        """
        import app
        from pixivviewer import models
        from pixivviewer.models import Illust as IllustModel

        # ① 表结构里没有这两列
        with models.engine.connect() as conn:
            cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()]
        assert 'is_favorite' not in cols
        assert 'favorited_at' not in cols

        # ② 模型实例上也没有（旧代码按属性名访问）
        il = IllustModel(pixiv_id=99999, title='t')
        clean_db.add(il); clean_db.commit()
        assert not hasattr(il, 'is_favorite')
        assert not hasattr(il, 'favorited_at')

        # ③ app 命名空间里不再有那个同步函数
        assert not hasattr(app, '_sync_is_favorite')


class TestSearchCache:
    def test_row_values_and_defaults(self, clean_db):
        """同一张表两种建行方式：显式赋值原样读回、只给 tag 时走列默认值。

        前置准备就是"建一行 SearchCache"，只有输入与期望值不同，故合为一条用例。
        """
        # ① 显式赋值
        clean_db.add(SearchCache(
            tag='初音ミク',
            illust_ids='[123, 456, 789]',
            status='done',
            total=3,
        ))
        safe_commit(clean_db)
        fetched = clean_db.query(SearchCache).filter(SearchCache.tag == '初音ミク').first()
        assert fetched is not None
        assert fetched.tag == '初音ミク'
        assert fetched.illust_ids == '[123, 456, 789]'
        assert fetched.status == 'done'
        assert fetched.total == 3

        # ② 默认值（后台预取建行时只给 tag）
        sc = SearchCache(tag='test')
        clean_db.add(sc)
        safe_commit(clean_db)
        assert sc.illust_ids == '[]'
        assert sc.status == 'idle'
        assert sc.error == ''
        assert sc.total == 0

    def test_prefetch_source_kept_and_description_removed(self, clean_db, sample_illust):
        """v2 迁移之后 illusts 列集的两面（同一张表、同一次迁移）：加进来的
        `prefetch_source` 必须能落库；删掉的 `description` 必须从模型与表里都消失。
        """
        # ① prefetch_source 能落库并读回
        sample_illust.prefetch_source = 1
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.prefetch_source == 1

        # ② description 从模型属性与表结构里都消失了
        assert not hasattr(sample_illust, 'description')
        from pixivviewer import models
        with models.engine.connect() as conn:
            cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()]
        assert 'description' not in cols


class TestDescriptionPrefetchMigration:
    def test_migration_drops_description_adds_prefetch_source(self, clean_db):
        from pixivviewer import models as m
        with m.engine.connect() as conn:
            conn.exec_driver_sql('ALTER TABLE illusts ADD COLUMN description TEXT DEFAULT ""')
            conn.exec_driver_sql('ALTER TABLE illusts DROP COLUMN prefetch_source')
            conn.commit()
        m.init_db()
        with m.engine.connect() as conn:
            cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()]
        assert 'description' not in cols
        assert 'prefetch_source' in cols


class TestSafeCommitLocked:
    def test_locked_rolls_back_and_keeps_session_usable(self, clean_db, monkeypatch):
        """回归：commit 遇 `database is locked` 必须 `rollback()` **并**让 session
        恢复到可用状态（旧实现只抛错，后续任何操作都抛 PendingRollbackError）。

        两段是同一次失败恢复的两个截面（先证明 rollback 被调用，再证明恢复后
        session 真的能用），准备完全相同，故合为一条用例。
        """
        from pixivviewer import models
        import pytest
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError
        rollback_called = []

        def fake_commit():
            raise OperationalError('stmt', {}, Exception('database is locked'))

        # ① 失败必须 rollback
        monkeypatch.setattr(clean_db, 'commit', fake_commit)
        monkeypatch.setattr(clean_db, 'rollback', lambda: rollback_called.append(1))
        with pytest.raises(OperationalError):
            models.safe_commit(clean_db)
        assert rollback_called == [1]

        # ② 恢复后 session 直接可用；这一段必须用**真实** rollback（替身测不出恢复）
        monkeypatch.undo()
        monkeypatch.setattr(clean_db, 'commit', fake_commit)
        with pytest.raises(OperationalError):
            models.safe_commit(clean_db)
        assert clean_db.execute(text('SELECT 1')).scalar() == 1


class TestDownloadLog:
    def test_defaults_and_to_dict(self, clean_db):
        log = DownloadLog(pixiv_id=123, action='start', message='测试')
        clean_db.add(log)
        clean_db.commit()
        d = log.to_dict()
        assert d['pixiv_id'] == 123
        assert d['action'] == 'start'
        assert d['message'] == '测试'
        assert d['created_at'] is not None


class TestRebuildIllustsTable:
    def test_rebuild_preserves_data_and_schema(self, clean_db, sample_illust):
        """SQLite<3.35 的重建路径（高危未测）：数据与索引必须保留。"""
        from pixivviewer import models as m
        from sqlalchemy import text
        m._rebuild_illusts_table(set())
        with m.get_session() as s:
            row = s.query(Illust).filter(Illust.pixiv_id == 12345678).first()
            assert row is not None
            assert row.title == 'テスト作品'
            assert row.bookmark_count == 1500
        with m.engine.connect() as conn:
            idx = [r[1] for r in conn.exec_driver_sql('PRAGMA index_list(illusts)').fetchall()]
        assert 'ix_illusts_pixiv_id' in idx
        assert 'ix_illusts_dl_status_created' in idx
