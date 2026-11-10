"""首页「最近 48 小时热门」的查询口径与页面路由。

不变式只有一条主线：**首页是本地库的只读聚合** —— 时间窗按 JST 口径、
只认已补全的收藏数、R18 与屏蔽标签恒定生效、48 小时不够时如实降级。
每条用例钉一个不变式，不合并（降级与排序是两件事，缓存与清缓存是第三件）。
"""
from datetime import datetime, timedelta, timezone
import pathlib

import pytest

import helpers
import routes_home
from models import BlockedTag, Illust


def _jst_now() -> datetime:
    """Pixiv 的 upload_date 是 JST 墙面时间（偏移在写库时丢了），测试必须同口径造数据。"""
    return (datetime.now(timezone.utc) + timedelta(hours=9)).replace(tzinfo=None)


def _illust(db, pixiv_id, *, hours_ago=1.0, bookmarks=100, tags=(), title='作品', user='画师'):
    """造一行作品；hours_ago=None 表示没有发布时间。"""
    illust = Illust(
        pixiv_id=pixiv_id,
        title=title,
        user_id=pixiv_id,
        user_name=user,
        bookmark_count=bookmarks,
        thumb_url=f'https://i.pximg.net/x/{pixiv_id}.jpg',
        upload_date=None if hours_ago is None else _jst_now() - timedelta(hours=hours_ago),
    )
    illust.tags_list = list(tags)
    db.add(illust)
    db.commit()
    return illust


@pytest.fixture(autouse=True)
def _clear_home_cache():
    """首页有 60 秒进程内缓存：每条用例前后都清，否则上一条的数据会漏到下一条。"""
    routes_home.clear_home_cache()
    yield
    routes_home.clear_home_cache()


class TestHomeQuery:
    def test_orders_by_bookmarks_and_skips_unfilled(self, clean_db):
        """按收藏数降序；收藏数为 0（未补全）与无发布时间的行不参与。

        收藏数 0 有两种含义（真没人收藏 / 详情从未补全），无法区分；把它当候选
        会让新入库作品整页沉底甚至占满一页，所以直接排除。
        """
        _illust(clean_db, 1001, hours_ago=1, bookmarks=10)
        _illust(clean_db, 1002, hours_ago=2, bookmarks=500)
        _illust(clean_db, 1003, hours_ago=3, bookmarks=200)
        _illust(clean_db, 1004, hours_ago=4, bookmarks=0)
        _illust(clean_db, 1005, hours_ago=None, bookmarks=900)

        results, window = helpers.query_home_feed(24, 'auto')

        assert [r['pixiv_id'] for r in results] == [1002, 1003, 1001], \
            f'必须按收藏数降序且排除 0 收藏/无日期，实际 {[r["pixiv_id"] for r in results]}'
        assert window == helpers.HOME_WINDOW_HOURS

    def test_window_is_jst_and_strict_mode_never_widens(self, clean_db):
        """48 小时窗口按 JST 墙面时间算；`window='48'` 时 49 小时前的作品不许进来。

        用 UTC now 直接比会整体偏 9 小时 —— 这条用例同时钉住口径与边界。
        """
        _illust(clean_db, 2001, hours_ago=47, bookmarks=100)
        _illust(clean_db, 2002, hours_ago=49, bookmarks=900)

        results, window = helpers.query_home_feed(24, '48')

        assert [r['pixiv_id'] for r in results] == [2001], \
            '49 小时前的作品落在 48 小时窗口外，严格模式不许放宽'
        assert window == 48

    def test_auto_falls_back_to_7_days_when_48h_is_thin(self, clean_db):
        """48 小时内不足一屏时放宽到 7 天，并把**实际**窗口报给前端。"""
        _illust(clean_db, 2003, hours_ago=5, bookmarks=10)
        _illust(clean_db, 2004, hours_ago=30, bookmarks=20)
        _illust(clean_db, 2005, hours_ago=100, bookmarks=800)

        results, window = helpers.query_home_feed(24, 'auto')

        assert [r['pixiv_id'] for r in results] == [2005, 2004, 2003], \
            '降级后要按 7 天内的收藏数重新排序'
        assert window == helpers.HOME_FALLBACK_HOURS, 'window_hours 必须是实际窗口，前端靠它标注'

    def test_limit_caps_results(self, clean_db):
        """一次最多一批（跟随页大小），多出来的不返回。"""
        for i in range(30):
            _illust(clean_db, 3000 + i, hours_ago=1, bookmarks=1000 - i)

        results, window = helpers.query_home_feed(24, 'auto')

        assert len(results) == 24
        assert results[0]['pixiv_id'] == 3000, '收藏数最高的排第一'
        assert window == helpers.HOME_WINDOW_HOURS


class TestHomeApi:
    def test_r18_always_hidden_even_with_a_query_param(self, client, clean_db):
        """R18 是恒定过滤：没有任何查询参数能把它关掉（首页不给开关）。"""
        _illust(clean_db, 4001, tags=['R-18'], bookmarks=900)
        _illust(clean_db, 4002, tags=['R-18G'], bookmarks=800)
        _illust(clean_db, 4003, tags=['少女'], bookmarks=700)

        data = client.get('/api/home?r18_mode=all').get_json()

        assert [r['pixiv_id'] for r in data['results']] == [4003], \
            f'首页必须恒隐藏 R-18/R-18G，实际 {[r["pixiv_id"] for r in data["results"]]}'
        assert data['window_hours'] == 48
        assert data['fell_back'] is False

    def test_blocked_tags_always_apply(self, client, clean_db):
        """全局屏蔽标签与搜索/图库同口径：命中的作品首页也不出现。"""
        clean_db.add(BlockedTag(tag='巨人'))
        clean_db.commit()
        _illust(clean_db, 4101, tags=['巨人'], bookmarks=900)
        _illust(clean_db, 4102, tags=['少女'], bookmarks=800)

        data = client.get('/api/home').get_json()

        assert [r['pixiv_id'] for r in data['results']] == [4102]

    def test_broken_tags_json_degrades_instead_of_500(self, client, clean_db):
        """单条损坏的 tags 会让 json_each 抛错：降级跳过标签过滤，不许整页 500。"""
        _illust(clean_db, 4201, tags=['少女'], bookmarks=800)
        clean_db.add(Illust(
            pixiv_id=4202, title='坏标签', user_name='u', bookmark_count=900,
            thumb_url='', tags='{oops', upload_date=_jst_now() - timedelta(hours=1),
        ))
        clean_db.commit()

        resp = client.get('/api/home')

        assert resp.status_code == 200, '损坏的 tags 必须降级，不能把首页打成 500'
        ids = [r['pixiv_id'] for r in resp.get_json()['results']]
        assert ids == [4202, 4201], f'降级后跳过标签过滤并按收藏数排序，实际 {ids}'

    def test_cache_is_used_until_cleared(self, client, clean_db):
        """60 秒缓存：期内新增作品不出现；`clear_home_cache()`（屏蔽标签变更会调）后立刻生效。"""
        _illust(clean_db, 4301, bookmarks=10)
        first = client.get('/api/home').get_json()

        _illust(clean_db, 4302, bookmarks=999)
        second = client.get('/api/home').get_json()
        assert second['results'] == first['results'], 'TTL 内应命中缓存，不去查库'

        routes_home.clear_home_cache()
        third = client.get('/api/home').get_json()
        assert third['results'][0]['pixiv_id'] == 4302, '清缓存后新增作品必须立刻可见'


class TestHomePages:
    def test_home_page_card_wiring(self, client):
        """首页脚本必须把卡片挂进网格，并接上灯箱与信息牌。

        回归背景：`renderInChunks` 只分帧调用渲染函数、**不负责插入**（搜索页也是
        调用方自己 `appendChild`）。第一版 `page-home.js` 漏了这一步，症状是
        计数与时间窗说明条都正常、网格永远空白 —— 所以这条用源码断言钉住。
        """
        page = client.get('/')
        assert page.status_code == 200
        assert b'page-home.js' in page.data

        js = pathlib.Path('static/page-home.js').read_text(encoding='utf-8')
        assert 'grid.appendChild(node)' in js, '卡片必须显式挂进 #homeGrid'
        assert 'lightbox.open(' in js, '点卡片要开灯箱（导航上下文 = 首页整批）'
        assert 'card-meta-title' in js and 'card-meta-artist' in js, \
            '信息牌只放名称与作者'

    def test_home_and_explore_render_their_own_pages(self, client):
        """`/` 是首页（page-home.js），搜索页迁到 `/explore`（`/search` 是异步 API）。"""
        home = client.get('/')
        assert home.status_code == 200
        assert b'page-home.js' in home.data, '首页要加载自己的脚本'
        assert '最近 48 小时热门'.encode() in home.data
        assert b'href="/explore"' in home.data, '导航里的搜索入口必须指向 /explore'

        explore = client.get('/explore')
        assert explore.status_code == 200
        assert b'page-index.js' in explore.data, '/explore 必须还是原来的搜索页'
        assert b'lightbox.js' in explore.data
