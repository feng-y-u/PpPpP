"""Pixiv 适配层（`pixiv_client`）的**离线**契约测试。

目的：把"我们的代码依赖 Pixiv 响应的哪些字段名与嵌套路径"变成可执行断言。
Pixiv 是非官方内部 Ajax API，字段改名/移动不会提前通知 —— 以前只能等用户报障
（搜索突然变空、原图拉不到、收藏数恒为 0）。这个文件就是那道回归网。

接口变更时的处理顺序：
1. 用 `scripts/pixiv_capture.py` 抓一份新的脱敏样本覆盖 `tests/fixtures/pixiv/`；
2. 跑本文件 —— 哪条红了，就是哪条契约变了（断言信息里带 JSON 路径）；
3. 只改 `pixiv_client.py` 的解析让契约重新变绿，**业务层（`fetcher`）不该跟着动**。

本文件不发任何网络请求、不读 Cookie：样本是静态脱敏 JSON（见 fixtures/pixiv/README.md）。
断言一律"样本 → 规范字段"的相对关系，不写死具体值 —— 这样刷新样本不会误伤测试。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

import config
import pixiv_client

FIXTURES = Path(__file__).parent / 'fixtures' / 'pixiv'

# `parse_illust_detail` 的规范键集合：多一个键（解析器擅自加字段）或少一个键
#（Pixiv 删字段导致解析器降级成默认值）都要在评审里被看见。
_DETAIL_KEYS = {
    'title', 'user_id', 'user_name', 'page_count', 'bookmark_count',
    'thumb_url', 'upload_date', 'original_urls', 'tags',
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


class _Response:
    """最小响应替身：只需 raise_for_status / json。"""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            resp = requests.Response()
            resp.status_code = self.status_code
            raise requests.HTTPError(f'HTTP {self.status_code}', response=resp)

    def json(self) -> dict:
        return self._payload


class _FixtureSession:
    """把样本当作 Pixiv 的响应，并记录实际请求到的 URL。"""

    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status
        self.calls: list[str] = []

    def get(self, url, **kwargs) -> _Response:
        self.calls.append(url)
        return _Response(self.payload, self.status)


def _session_for(name: str, status: int = 200) -> _FixtureSession:
    return _FixtureSession(_load(name), status)


@pytest.fixture(autouse=True)
def _fast_limiters(monkeypatch):
    """契约测试不关心限流速率：把令牌桶换成高速桶。

    真桶是 60/分钟（全局状态，跨用例积累），不旁路会让"第三条详情请求"白等 1 秒 ——
    本文件测的是**响应结构**，与节奏无关。
    """
    monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(600000))
    monkeypatch.setattr(pixiv_client, '_detail_limiter', pixiv_client._TokenBucket(600000))


# ── 端点契约：URL 形状（Ajax 路径变了的唯一防线）──

class TestEndpointContract:
    def test_illust_detail_path(self):
        assert pixiv_client.endpoint_illust_detail(100000001) == \
            f'{config.PIXIV_BASE_URL}/ajax/illust/100000001'

    def test_search_illustrations_path_and_params(self):
        url = pixiv_client.endpoint_search_illustrations(
            'サンプル', order='date_d', mode='all', page=2)
        path, _, query = url.partition('?')
        # 检索词必须**同时**出现在路径段与 word= 参数里，且都被 URL-encode
        quoted = requests.utils.quote('サンプル')
        assert path == f'{config.PIXIV_BASE_URL}/ajax/search/illustrations/{quoted}'
        for fragment in (f'word={quoted}', 'order=date_d', 'mode=all',
                         'p=2', 's_mode=s_tag', 'type=illust'):
            assert fragment in query, f'搜索端点缺少参数片段 {fragment!r}: {url}'

    def test_discovery_artworks_path_and_params(self):
        url = pixiv_client.endpoint_discovery_artworks(
            mode='all', page=3, limit=60, order='popular_d')
        assert url.startswith(f'{config.PIXIV_BASE_URL}/ajax/discovery/artworks?')
        for fragment in ('mode=all', 'p=3', 'limit=60', 'order=popular_d'):
            assert fragment in url, f'发现页端点缺少参数片段 {fragment!r}: {url}'

    def test_user_profile_all_path(self):
        assert pixiv_client.endpoint_user_profile_all('9000001') == \
            f'{config.PIXIV_BASE_URL}/ajax/user/9000001/profile/all'

    def test_follow_latest_path_and_params(self):
        url = pixiv_client.endpoint_follow_latest_illust(mode='safe', page=2)
        assert url.startswith(f'{config.PIXIV_BASE_URL}/ajax/follow_latest/illust?')
        for fragment in ('mode=safe', 'p=2'):
            assert fragment in url, f'关注端点缺少参数片段 {fragment!r}: {url}'

    def test_fetch_functions_use_the_endpoint_builders(self):
        """抓请求必须经端点函数 —— 防止有人在 fetch_* 里另拼一份 URL。"""
        cases = [
            ('search_illustrations.json',
             lambda s: pixiv_client.fetch_search_illusts(
                 s, 'サンプル', sort_order='date_d', r18_mode='all', page=1),
             pixiv_client.endpoint_search_illustrations(
                 'サンプル', order='date_d', mode='all', page=1)),
            ('discovery_artworks.json',
             lambda s: pixiv_client.fetch_discovery_artworks(
                 s, sort_order='popular_d', r18_mode='all', page=1),
             pixiv_client.endpoint_discovery_artworks(
                 mode='all', page=1, limit=60, order='popular_d')),
            ('user_profile_all.json',
             lambda s: pixiv_client.fetch_user_profile_ids(s, '9000001'),
             pixiv_client.endpoint_user_profile_all('9000001')),
            ('follow_latest.json',
             lambda s: pixiv_client.fetch_following_latest(
                 s, r18_mode='all', page=1),
             pixiv_client.endpoint_follow_latest_illust(mode='all', page=1)),
            ('illust_detail_meta_pages.json',
             lambda s: pixiv_client.fetch_illust_detail(s, 100000001),
             pixiv_client.endpoint_illust_detail(100000001)),
        ]
        for fixture, call, expected_url in cases:
            session = _session_for(fixture)
            call(session)
            assert session.calls == [expected_url], \
                f'{fixture}: 请求的 URL 与端点函数不一致（有人另拼了一份？）'


# ── 字段漂移清单：Pixiv 改名/移动字段时，这里给出精确的 JSON 路径 ──
#
# 路径语法：`a.b` 取键；`x[]` 表示"取 x 的第一个元素"（x 是列表或 id→对象的字典）。
# 这份清单是**代码读过的字段**的白名单式声明：样本里少了任何一条，说明 Pixiv 改了
# 结构（或样本过期），必须先在 `pixiv_client` 里处理，而不是"解析器悄悄降级成空值"。
_DEPENDENCIES: dict[str, list[str]] = {
    'illust_detail_meta_pages.json': [
        'error',
        'body.illustTitle', 'body.userId', 'body.userName', 'body.pageCount',
        'body.bookmarkCount', 'body.uploadDate', 'body.urls.thumb',
        'body.tags.tags[].tag', 'body.metaPages[].urls.original',
    ],
    'illust_detail_meta_single.json': [
        'error',
        'body.illustTitle', 'body.userId', 'body.userName', 'body.pageCount',
        'body.bookmarkCount', 'body.uploadDate', 'body.urls.thumb',
        'body.tags.tags[].tag', 'body.metaSinglePage.originalImageUrl',
    ],
    'illust_detail_legacy_urls.json': [
        'error',
        'body.illustTitle', 'body.userId', 'body.userName', 'body.pageCount',
        'body.bookmarkCount', 'body.uploadDate', 'body.urls.thumb',
        'body.urls.original', 'body.tags.tags[].tag',
    ],
    'search_illustrations.json': [
        'error', 'body.illust.total', 'body.illust.data[]',
        'body.illust.data[].id', 'body.illust.data[].title',
        'body.illust.data[].userId', 'body.illust.data[].userName',
        'body.illust.data[].pageCount', 'body.illust.data[].url',
        'body.illust.data[].updateDate', 'body.illust.data[].tags',
    ],
    'discovery_artworks.json': [
        'error', 'body.total', 'body.thumbnails.illust[]',
        'body.thumbnails.illust[].id', 'body.thumbnails.illust[].type',
        'body.thumbnails.illust[].tags',
    ],
    'user_profile_all.json': [
        'error', 'body.illusts',
    ],
    'follow_latest.json': [
        'error', 'body.thumbnails.illust[]',
        'body.thumbnails.illust[].id', 'body.thumbnails.illust[].updateDate',
        'body.page.isLastPage',
    ],
    'error_envelope_auth.json': ['error', 'message'],
    'error_envelope_deleted.json': ['error', 'message'],
}


def _resolves(payload, path: str) -> bool:
    """清单路径是否能在样本里走通。"""
    node = payload
    for token in path.split('.'):
        seq = token.endswith('[]')
        key = token[:-2] if seq else token
        if key:
            if not isinstance(node, dict) or key not in node:
                return False
            node = node[key]
        if seq:
            if isinstance(node, dict):
                if not node:
                    return False
                node = node[next(iter(node))]
            elif isinstance(node, list):
                if not node:
                    return False
                node = node[0]
            else:
                return False
    return True


class TestFieldDrift:
    """样本里必须仍然存在"代码读过的字段"。红了就是 Pixiv 改了结构。"""

    @pytest.mark.parametrize('fixture', sorted(_DEPENDENCIES))
    def test_sample_still_has_every_field_we_read(self, fixture):
        payload = _load(fixture)
        missing = [p for p in _DEPENDENCIES[fixture] if not _resolves(payload, p)]
        assert not missing, (
            f'{fixture} 缺少代码依赖的字段路径（Pixiv 改了字段名/结构，或样本过期）：'
            f'{missing}')

    def test_every_fixture_is_covered_by_the_manifest(self):
        """新增样本必须同时登记契约，否则测试会静默漏掉它。"""
        on_disk = {p.name for p in FIXTURES.glob('*.json')}
        assert on_disk == set(_DEPENDENCIES), (
            f'未登记契约的样本: {sorted(on_disk - set(_DEPENDENCIES))}；'
            f'清单里已失效的条目: {sorted(set(_DEPENDENCIES) - on_disk)}')


# ── 详情：规范字段 + 三条原图地址路径 ──

class TestIllustDetailContract:
    def test_meta_pages_multipage(self):
        body = _load('illust_detail_meta_pages.json')['body']
        detail = pixiv_client.parse_illust_detail(body)

        assert set(detail) == _DETAIL_KEYS
        assert detail['title'] == body['illustTitle']
        assert detail['user_id'] == int(body['userId'])
        assert detail['user_name'] == body['userName']
        assert detail['page_count'] == body['pageCount']
        assert detail['bookmark_count'] == body['bookmarkCount']
        assert detail['thumb_url'] == body['urls']['thumb']
        assert detail['upload_date'] == body['uploadDate']
        # tags 是 `{tags: [{tag: …}]}` 形态（详情接口的现代形态）
        assert detail['tags'] == [t['tag'] for t in body['tags']['tags']]
        # 原图地址整体取自 metaPages，且页序正确（下载按顺序落盘，错序=错图）
        assert len(detail['original_urls']) == body['pageCount']
        assert [u.rsplit('/', 1)[-1] for u in detail['original_urls']] == \
            [p['urls']['original'].rsplit('/', 1)[-1] for p in body['metaPages']]

    def test_meta_single_page(self):
        body = _load('illust_detail_meta_single.json')['body']
        detail = pixiv_client.parse_illust_detail(body)

        assert set(detail) == _DETAIL_KEYS
        assert detail['original_urls'] == [body['metaSinglePage']['originalImageUrl']]
        assert detail['tags'] == [t['tag'] for t in body['tags']['tags']]
        assert 'R-18' in detail['tags'], 'R18 标签必须被原样带出（下游按它过滤）'

    def test_legacy_urls_derives_every_page_from_p0(self):
        """旧形态没有 metaPages：按 `_p0` → `_pN` 推导全部页面。"""
        body = _load('illust_detail_legacy_urls.json')['body']
        detail = pixiv_client.parse_illust_detail(body)

        p0_tail = body['urls']['original'].rsplit('/', 1)[-1]
        expected = [p0_tail.replace('_p0', f'_p{i}') for i in range(body['pageCount'])]
        assert [u.rsplit('/', 1)[-1] for u in detail['original_urls']] == expected

    def test_single_page_legacy_keeps_original_as_is(self):
        body = _load('illust_detail_legacy_urls.json')['body']
        body['pageCount'] = 1
        detail = pixiv_client.parse_illust_detail(body)
        assert detail['original_urls'] == [body['urls']['original']]

    def test_upload_date_falls_back_to_create_date(self):
        """Pixiv 有的响应只有 createDate：解析必须回退，不能返回空串。"""
        body = _load('illust_detail_meta_pages.json')['body']
        body.pop('uploadDate')
        detail = pixiv_client.parse_illust_detail(body)
        assert detail['upload_date'] == body['createDate']

    def test_tags_tolerates_the_string_list_form(self):
        """tags 历史上出现过纯字符串列表形态，解析不能因此返回空。"""
        body = _load('illust_detail_meta_pages.json')['body']
        body['tags'] = ['サンプル', 'オリジナル']
        assert pixiv_client.parse_illust_detail(body)['tags'] == ['サンプル', 'オリジナル']

    def test_fetch_illust_detail_over_the_fixture(self):
        session = _session_for('illust_detail_meta_pages.json')
        detail = pixiv_client.fetch_illust_detail(session, 100000001)
        assert detail is not None and set(detail) == _DETAIL_KEYS
        assert session.calls == [pixiv_client.endpoint_illust_detail(100000001)]

    def test_fetch_original_urls_returns_only_the_urls(self):
        session = _session_for('illust_detail_meta_single.json')
        urls = pixiv_client.fetch_original_urls(session, 100000002)
        assert urls == [_load('illust_detail_meta_single.json')['body']
                        ['metaSinglePage']['originalImageUrl']]


# ── 列表端点：信封遍历路径 + 条目透传 ──

class TestSearchContract:
    def test_items_and_total_come_from_body_illust(self):
        payload = _load('search_illustrations.json')
        session = _FixtureSession(payload)
        items, total = pixiv_client.fetch_search_illusts(
            session, 'サンプル', sort_order='date_d', r18_mode='all', page=1)

        # 条目**原样透传**（字段访问由 item_* / parse_illust_summary 负责）
        assert items == payload['body']['illust']['data']
        assert total == payload['body']['illust']['total']

    def test_summary_normalises_every_field_we_store(self):
        payload = _load('search_illustrations.json')
        item = payload['body']['illust']['data'][0]
        summary = pixiv_client.parse_illust_summary(item)

        assert summary['pixiv_id'] == int(item['id'])
        assert summary['title'] == item['title']
        assert summary['user_id'] == int(item['userId'])
        assert summary['user_name'] == item['userName']
        assert summary['page_count'] == item['pageCount']
        assert summary['thumb_url'] == item['url']
        assert summary['upload_date'] == item['updateDate']
        assert summary['tags'] == item['tags']
        assert pixiv_client.item_pixiv_id(item) == int(item['id'])

    def test_bookmark_count_absent_in_list_payload(self):
        """列表接口不带 bookmarkCount（实测恒缺失）→ 缺失必须归 0，不是崩溃。"""
        item = _load('search_illustrations.json')['body']['illust']['data'][0]
        assert 'bookmarkCount' not in item
        assert pixiv_client.item_bookmark_count(item) == 0
        assert pixiv_client.item_bookmark_count({'bookmarkCount': 321}) == 321

    def test_missing_id_is_a_protocol_error_not_a_zero(self):
        """条目缺 id 属于协议不符：必须抛错，不能静默写成 pixiv_id=0。"""
        with pytest.raises(KeyError):
            pixiv_client.item_pixiv_id({'title': 'no id'})


class TestDiscoveryContract:
    def test_items_come_from_body_thumbnails_illust(self):
        payload = _load('discovery_artworks.json')
        session = _FixtureSession(payload)
        items, total = pixiv_client.fetch_discovery_artworks(
            session, sort_order='popular_d', r18_mode='all', page=1)

        # 条目来自 body.thumbnails.illust，但**非插画条目（漫画/小说）被剔除**
        entries = payload['body']['thumbnails']['illust']
        expected = [t for t in entries if t.get('type') in (None, 'illust')]
        assert items == expected
        assert len(items) < len(entries), '样本里必须留一条非插画条目，否则这条断言是空的'
        assert total == payload['body']['total']

    def test_non_illust_entries_are_dropped(self):
        """`type != illust` 的条目（漫画/小说）不能进图库。"""
        payload = _load('discovery_artworks.json')
        items = pixiv_client.parse_discovery_items(payload['body'])
        assert items, '样本必须至少留一条插画条目，否则这条断言是空的'
        assert all(i.get('type') in (None, 'illust') for i in items)

    def test_legacy_illusts_key_is_still_read(self):
        """旧形态把条目放在 `body.illusts`：解析必须兼容。"""
        payload = _load('discovery_artworks.json')
        entries = payload['body']['thumbnails']['illust']
        expected = [t for t in entries if t.get('type') in (None, 'illust')]
        assert pixiv_client.parse_discovery_items({'illusts': entries}) == expected


class TestUserProfileContract:
    def test_ids_come_from_the_keys_of_body_illusts(self):
        payload = _load('user_profile_all.json')
        session = _FixtureSession(payload)
        ids = pixiv_client.fetch_user_profile_ids(session, '9000001')

        assert ids == sorted((int(k) for k in payload['body']['illusts']), reverse=True)
        assert all(isinstance(i, int) for i in ids)


class TestFollowLatestContract:
    def test_items_and_is_last_page(self):
        payload = _load('follow_latest.json')
        session = _FixtureSession(payload)
        items, has_next = pixiv_client.fetch_following_latest(
            session, r18_mode='all', page=1)

        assert items == payload['body']['thumbnails']['illust']
        assert has_next is (not payload['body']['page']['isLastPage'])

    def test_missing_page_block_means_no_next_page(self):
        """`body.page` 缺失时必须当作"没有下一页"，不能抛错。"""
        items, has_next = pixiv_client.parse_follow_latest(
            {'thumbnails': {'illust': []}})
        assert (items, has_next) == ([], False)


# ── 错误信封与请求异常分类 ──

class TestEnvelopeContract:
    def test_auth_envelope_raises_auth_error(self):
        with pytest.raises(pixiv_client.PixivAuthError):
            pixiv_client.envelope_error(_load('error_envelope_auth.json'))

    def test_deleted_envelope_is_permanent_death(self):
        msg = pixiv_client.envelope_error(_load('error_envelope_deleted.json'))
        assert msg, 'error:true 必须带出 message（判死/采样都靠它）'
        assert pixiv_client.is_permanently_removed_message(msg)

    def test_ok_envelope_is_not_an_error(self):
        for fixture in ('search_illustrations.json', 'illust_detail_meta_pages.json',
                        'follow_latest.json'):
            assert pixiv_client.envelope_error(_load(fixture)) is None

    @pytest.mark.parametrize('fixture,call', [
        ('error_envelope_auth.json',
         lambda s: pixiv_client.fetch_search_illusts(
             s, 'サンプル', sort_order='date_d', r18_mode='all', page=1)),
        ('error_envelope_deleted.json',
         lambda s: pixiv_client.fetch_search_illusts(
             s, 'サンプル', sort_order='date_d', r18_mode='all', page=1)),
    ])
    def test_search_envelope_classification(self, fixture, call):
        """认证类冒泡（让用户去更新 Cookie）；其余按失败返回空结果。"""
        session = _FixtureSession(_load(fixture))
        if 'auth' in fixture:
            with pytest.raises(pixiv_client.PixivAuthError):
                call(session)
        else:
            assert call(session) == ([], 0)

    @pytest.mark.parametrize('fixture', [
        'error_envelope_deleted.json',
    ])
    def test_non_auth_envelope_returns_empty_from_every_list_endpoint(self, fixture):
        payload = _load(fixture)
        assert pixiv_client.fetch_discovery_artworks(
            _FixtureSession(payload), sort_order='popular_d',
            r18_mode='all', page=1)[0] == []
        assert pixiv_client.fetch_following_latest(
            _FixtureSession(payload), r18_mode='all', page=1)[0] == []
        assert pixiv_client.fetch_user_profile_ids(
            _FixtureSession(payload), '9000001') == []

    def test_http_401_is_auth_error_for_list_endpoints(self):
        session = _FixtureSession(_load('search_illustrations.json'), status=401)
        with pytest.raises(pixiv_client.PixivAuthError):
            pixiv_client.fetch_search_illusts(
                session, 'サンプル', sort_order='date_d', r18_mode='all', page=1)

    def test_http_403_is_not_auth_error_for_list_endpoints(self):
        """403 = 限流（Pixiv 并发过高也回 403），必须按失败返回空、不冒泡认证错误。"""
        session = _FixtureSession(_load('search_illustrations.json'), status=403)
        assert pixiv_client.fetch_search_illusts(
            session, 'サンプル', sort_order='date_d', r18_mode='all', page=1) == ([], 0)

    def test_detail_404_is_dead_sentinel_only_when_requested(self):
        """404 = 确定性永久失败：默认 None（旧语义），return_dead=True 才给哨兵。"""
        session = _FixtureSession({}, status=404)
        assert pixiv_client.fetch_illust_detail(session, 100000001) is None
        session = _FixtureSession({}, status=404)
        assert pixiv_client.fetch_illust_detail(
            session, 100000001, return_dead=True) is pixiv_client.DEAD_DETAIL

    def test_detail_deleted_envelope_is_dead_sentinel(self):
        session = _FixtureSession(_load('error_envelope_deleted.json'))
        assert pixiv_client.fetch_illust_detail(
            session, 100000001, return_dead=True) is pixiv_client.DEAD_DETAIL

    def test_detail_auth_envelope_raises(self):
        session = _FixtureSession(_load('error_envelope_auth.json'))
        with pytest.raises(pixiv_client.PixivAuthError):
            pixiv_client.fetch_illust_detail(session, 100000001)


# ── 检索词拼装（Pixiv 查询语法）──

class TestSearchQueryContract:
    @pytest.mark.parametrize('keyword,tag_mode,expected', [
        ('初音ミク', 'or', '初音ミク'),
        ('初音ミク,オリジナル', 'or', '(初音ミク OR オリジナル)'),
        ('初音ミク，オリジナル', 'or', '(初音ミク OR オリジナル)'),
        ('初音ミク,オリジナル', 'and', '初音ミク オリジナル'),
    ])
    def test_query_syntax(self, keyword, tag_mode, expected):
        assert pixiv_client.build_search_query(keyword, tag_mode) == expected
