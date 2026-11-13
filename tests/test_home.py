"""首页「最近 48 小时热门」的查询口径与页面路由。

不变式只有一条主线：**首页是本地库的只读聚合** —— 时间窗按 JST 口径、
只认已补全的收藏数、R18 与屏蔽标签恒定生效、48 小时不够时如实降级。
每条用例钉一个不变式，不合并（降级与排序是两件事，缓存与清缓存是第三件）。
"""
from datetime import datetime, timedelta, timezone
from base64 import urlsafe_b64decode
import pathlib
import re

import pytest

from pixivviewer import helpers
from pixivviewer.routes import home as routes_home
from pixivviewer.models import BlockedTag, Illust


def _jst_now() -> datetime:
    """Pixiv 的 upload_date 是 JST 墙面时间（偏移在写库时丢了），测试必须同口径造数据。"""
    return (datetime.now(timezone.utc) + timedelta(hours=9)).replace(tzinfo=None)


def _illust(db, pixiv_id, *, hours_ago=1.0, bookmarks=100, tags=(), title='作品', user='画师',
            originals=None, prefetch=False):
    """造一行作品；hours_ago=None 表示没有发布时间。

    `originals=None` 用默认原图地址（首页只收带原图的行，见 `query_home_feed`），
    传 `[]` 可造"没有原图地址"的行。
    `prefetch=True` 标成预取缓存来源（`prefetch_source=1`），第二屏随机池只认这类行。
    """
    illust = Illust(
        pixiv_id=pixiv_id,
        title=title,
        user_id=pixiv_id,
        user_name=user,
        bookmark_count=bookmarks,
        thumb_url=f'https://i.pximg.net/x/{pixiv_id}.jpg',
        prefetch_source=1 if prefetch else 0,
        upload_date=None if hours_ago is None else _jst_now() - timedelta(hours=hours_ago),
    )
    illust.tags_list = list(tags)
    illust.original_urls_list = originals if originals is not None else [
        f'https://i.pximg.net/img-original/img/2026/11/09/00/00/00/{pixiv_id}_p0.jpg',
    ]
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
    def test_excludes_downloaded_from_first_screen(self, clean_db):
        """已下载的作品不再出现在首页第一屏。

        `download_status='done'` 是主信号；历史/半状态行可能只有 local_paths，
        也要排除，避免"下载过的图又回到首页"。
        """
        keep = _illust(clean_db, 4801, bookmarks=100)
        done = _illust(clean_db, 4802, bookmarks=9999)
        done.download_status = 'done'
        done.local_paths_list = ['/home/azure/pixiv-viewer/downloads/4802/1.jpg']
        legacy = _illust(clean_db, 4803, bookmarks=8888)
        legacy.local_paths_list = ['/home/azure/pixiv-viewer/downloads/4803/1.jpg']
        clean_db.commit()

        results, _ = helpers.query_home_feed(24, '48')
        ids = {r['pixiv_id'] for r in results}

        assert 4801 in ids
        assert 4802 not in ids, 'download_status=done 的作品不得出现在首页'
        assert 4803 not in ids, 'local_paths 非空的历史下载行也不得出现'
        assert keep.pixiv_id == 4801

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

    def test_medium_url_is_master1200_not_the_square_thumb(self, client, clean_db):
        """瀑布流用中图：`medium_url` 必须是 master1200 的代理地址，不是 250px 方形缩略图。

        这条链路与详情页同源（原图地址 → `_original_to_resized` → `/thumb`），
        所以扩展名恒为 `.jpg`（原图是 PNG 也拼 `.jpg`，否则图床 404）。
        """
        with_original = _illust(clean_db, 4401, bookmarks=900)
        with_original.original_urls_list = [
            'https://i.pximg.net/img-original/img/2026/11/09/00/00/00/4401_p0.png',
        ]
        clean_db.commit()

        data = client.get('/api/home').get_json()
        by_id = {r['pixiv_id']: r for r in data['results']}

        medium = by_id[4401]['medium_url']
        assert medium.startswith('/thumb/'), '中图必须走 /thumb 代理（带 Referer 与缓存）'
        raw = medium[len('/thumb/'):]
        decoded = urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode()
        assert decoded.endswith('/4401_p0_master1200.jpg'), \
            f'中图地址不对（扩展名必须 .jpg）：{decoded}'
        assert '/c/' in decoded and 'img-master' in decoded, decoded

    def test_rows_without_original_urls_are_excluded(self, client, clean_db):
        """首页承诺"每张卡都是中图"：没有原图地址的行**不进候选**。

        中图地址是从原图地址拼的，没有原图就只能退回 250px 方形缩略图 —— 大图瀑布流里
        很显眼。实测生产库 `bookmark_count > 0` 与"有原图地址"完全同步（收藏数只在拉详情
        时写入，拉详情同时写原图），所以这条不额外排除任何作品，只是把保证写进查询。
        """
        _illust(clean_db, 4501, bookmarks=900)
        _illust(clean_db, 4502, bookmarks=800, originals=[])

        data = client.get('/api/home').get_json()

        assert [r['pixiv_id'] for r in data['results']] == [4501], \
            '没有原图地址的行不该出现在首页（否则只能显示缩略图）'

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
            # 带原图地址：首页只收中图候选，这里测的是 tags 降级，不是候选资格
            original_urls='["https://i.pximg.net/img-original/img/2026/11/09/00/00/00/4202_p0.jpg"]',
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


class TestHomeRandom:
    """第二屏起：从预取缓存里随机抽收藏数 > 500 的作品。

    与第一屏的分界是"口径"而不是"接口"：第一屏是时间窗 + 收藏数排序，第二屏是
    不限时间窗的随机推荐。两条口径各自钉住，防止随机部分悄悄退化成"又一个排行"。
    """

    def _token(self, client):
        return client.get('/csrf-token').get_json()['token']

    def _more(self, client, exclude=None, limit=24):
        return client.post('/api/home/more',
                           json={'exclude': exclude or [], 'limit': limit},
                           headers={'X-CSRF-Token': self._token(client)})

    def test_pool_excludes_downloaded_works(self, client, clean_db):
        """随机池同样不返回已下载作品；否则第二屏会把刚下载的图又推回来。"""
        keep = _illust(clean_db, 5201, bookmarks=600, prefetch=True)
        done = _illust(clean_db, 5202, bookmarks=9000, prefetch=True)
        done.download_status = 'done'
        done.local_paths_list = ['/home/azure/pixiv-viewer/downloads/5202/1.jpg']
        legacy = _illust(clean_db, 5203, bookmarks=8000, prefetch=True)
        legacy.local_paths_list = ['/home/azure/pixiv-viewer/downloads/5203/1.jpg']
        clean_db.commit()

        data = self._more(client).get_json()
        ids = {r['pixiv_id'] for r in data['results']}

        assert ids == {5201}
        assert keep.pixiv_id == 5201
        assert data['has_more'] is False, '排除已下载后只剩 1 条，不应再声称还有更多'

    def test_pool_is_prefetch_with_strict_bookmark_threshold(self, client, clean_db):
        """池子只认：预取来源 + 收藏数**严格大于** 500 + 带原图地址。

        500 本身不进池（用户口径是 ">500"）；`prefetch_source=0`（手搜临时入库）
        也不进池 —— "缓存里"只指预取缓存。中图地址由原图拼出，没有原图的行走不了
        中图，随机推荐同样排除。
        """
        _illust(clean_db, 5001, bookmarks=501, prefetch=True)
        _illust(clean_db, 5002, bookmarks=500, prefetch=True)
        _illust(clean_db, 5003, bookmarks=9000, prefetch=True, originals=[])
        _illust(clean_db, 5004, bookmarks=9999, prefetch=False)

        data = self._more(client).get_json()

        assert [r['pixiv_id'] for r in data['results']] == [5001], \
            f'500 本身与手搜行都该被排除，实际 {[r["pixiv_id"] for r in data["results"]]}'
        assert data['min_bookmarks'] == helpers.HOME_RANDOM_MIN_BOOKMARKS
        assert data['has_more'] is False
        assert data['results'][0]['medium_url'].startswith('/thumb/'), \
            '随机推荐同样要给中图地址（与第一屏同一链路）'

    def test_exclude_prevents_duplicates_across_pages(self, client, clean_db):
        """随机序不能用 offset 分页：跨屏去重靠 exclude，池子抽空时返回空。

        `has_more` 是"排除已展示后还剩多少"的信号：剩 10 条要 10 条时为 False，
        前端据此收尾，不必多打一次空请求。
        """
        for i in range(30):
            _illust(clean_db, 5100 + i, bookmarks=600 + i, prefetch=True)

        first = self._more(client, limit=10).get_json()
        shown = [r['pixiv_id'] for r in first['results']]
        assert len(shown) == 10 and first['has_more'] is True

        second = self._more(client, exclude=shown, limit=10).get_json()
        second_ids = [r['pixiv_id'] for r in second['results']]
        assert len(second_ids) == 10
        assert not (set(second_ids) & set(shown)), '已展示的作品不允许重复出现'

        third = self._more(client, exclude=shown + second_ids, limit=10).get_json()
        third_ids = [r['pixiv_id'] for r in third['results']]
        assert len(third_ids) == 10
        assert third['has_more'] is False, '池子恰好抽满时 has_more 必须为 False'

        all_shown = shown + second_ids + third_ids
        exhausted = self._more(client, exclude=all_shown, limit=10).get_json()
        assert exhausted['results'] == [], '池子抽空后返回空列表而不是重复作品'
        assert exhausted['has_more'] is False

    def test_random_hides_r18_and_blocked_and_ignores_time_window(self, client, clean_db):
        """恒定过滤照旧生效，但随机部分**不限时间窗**：缓存里再老的高收藏作品也要能翻出来。

        再加时间窗等于只在"最近 48 小时且 >500"的极少数里抽，第二屏会立刻抽空。
        """
        clean_db.add(BlockedTag(tag='巨人'))
        clean_db.commit()
        _illust(clean_db, 5201, bookmarks=900, prefetch=True, tags=['R-18'])
        _illust(clean_db, 5202, bookmarks=900, prefetch=True, tags=['巨人'])
        _illust(clean_db, 5203, bookmarks=900, prefetch=True, hours_ago=24 * 365)

        data = self._more(client).get_json()

        assert [r['pixiv_id'] for r in data['results']] == [5203], \
            'R18 与被屏蔽标签必须隐藏，老作品必须能出现'

    def test_more_is_not_written_to_the_60s_cache(self, client, clean_db):
        """随机接口不能写首页的 60 秒缓存：写进去"随机"就会在一分钟内定住。"""
        _illust(clean_db, 5301, bookmarks=900, prefetch=True)

        self._more(client)

        assert routes_home._home_cache == {}, '随机推荐绝不能进 /api/home 的进程内缓存'

    def test_bad_body_or_dirty_exclude_does_not_500(self, client, clean_db):
        """body 不是对象 / exclude 里混了非整数：忽略脏值，不许把首页打成 500。"""
        _illust(clean_db, 5401, bookmarks=900, prefetch=True)
        token = self._token(client)

        bad = client.post('/api/home/more', data='not json',
                          content_type='application/json',
                          headers={'X-CSRF-Token': token})
        assert bad.status_code == 200, '非法 JSON 应退化成空 body'
        assert [r['pixiv_id'] for r in bad.get_json()['results']] == [5401]

        dirty = client.post('/api/home/more',
                            json={'exclude': ['abc', None, 5401, {}], 'limit': 'x'},
                            headers={'X-CSRF-Token': token})
        assert dirty.status_code == 200
        assert dirty.get_json()['results'] == [], \
            '合法的那条 exclude（5401）仍要生效，脏值忽略'

    def test_home_page_wires_scroll_loading(self, client):
        """页面要把滚动加载接上：CSRF/页大小 meta + 哨兵 + POST /api/home/more。

        注：`_home_page_card_wiring` 钉的是"卡片挂进网格"；这里钉的是"往下滚有东西"。
        """
        html = client.get('/').get_data(as_text=True)
        assert 'name="csrf-token"' in html, '滚动加载用 POST，页面必须下发 CSRF token'
        assert 'name="items-per-page"' in html, '页大小必须读服务端下发的值'

        js = pathlib.Path('pixivviewer/static/page-home.js').read_text(encoding='utf-8')
        assert '/api/home/more' in js, '滚动加载必须调用随机接口'
        assert 'IntersectionObserver' in js, '滚动到底要自动加载'
        assert 'feed-divider' in js, '随机段之前要有分隔条'
        assert "'X-CSRF-Token'" in js, 'POST 必须带 CSRF 头'


class TestHomePages:
    def test_home_page_card_wiring(self, client):
        """首页脚本必须把卡片挂进网格，并接上"跳详情页"与信息牌。

        回归背景：`renderInChunks` 只分帧调用渲染函数、**不负责插入**（搜索页也是
        调用方自己 `appendChild`）。第一版 `page-home.js` 漏了这一步，症状是
        计数与时间窗说明条都正常、网格永远空白 —— 所以这条用源码断言钉住。
        卡片点击曾经开共享灯箱，灯箱整体移除后改为跳详情页（2026-11-10）。
        """
        page = client.get('/')
        assert page.status_code == 200
        assert b'page-home.js' in page.data

        js = pathlib.Path('pixivviewer/static/page-home.js').read_text(encoding='utf-8')
        assert 'placeIntoColumns(bandHot, node)' in js and 'placeIntoColumns(bandMore, node)' in js, \
            '卡片必须显式挂进列容器（renderInChunks 不负责插入）'
        assert 'lightbox' not in js, '灯箱已移除'
        assert 'window.location.href = `/detail/${r.pixiv_id}`' in js, \
            '点卡片要跳详情页'
        assert 'card-meta-title' in js and 'card-meta-artist' in js, \
            '信息牌只放名称与作者'

    def test_home_cards_do_not_promote_every_card_to_compositor_layer(self, client):
        """`will-change: transform` 不能常驻在每张卡片上。

        首页无限加载后卡片数量很大；每张 `.photo-card` 都声明 will-change 会让
        浏览器为每张卡建立合成层，GPU 内存和滚动合成成本随卡片数线性增长。
        只允许在 hover/focus 时临时提升，不能全局常驻。
        """
        html = client.get('/').get_data(as_text=True)
        card_rule = re.search(r'\.photo-card\s*\{(.*?)\}', html, re.S)
        assert card_rule, '找不到 .photo-card 样式块'
        assert 'will-change' not in card_rule.group(1), \
            '.photo-card 常驻 will-change 会让每张卡片各占一个合成层'

        hover_rule = re.search(
            r'\.photo-card:hover,\s*\.photo-card:focus-visible\s*\{(.*?)\}', html, re.S)
        assert hover_rule, '找不到 .photo-card hover/focus 样式块'
        assert 'will-change: transform' in hover_rule.group(1), \
            '只允许 hover/focus 时临时提升合成层'

    def test_home_masonry_uses_js_columns_not_css_multicol(self, client):
        """首页瀑布流必须是 JS 分列，不能退回 CSS 多列（`column-count`）。

        回归背景：CSS 多列是"列优先 + 自动平衡"，每追加一批就要重算断列点，
        已经看过的卡片会被搬到别的列 —— 用户反馈"后两列看着看着就刷新"
        （2026-11-11）。JS 分列按"最短列优先"追加，已落位的卡片不再移动。
        """
        html = client.get('/').get_data(as_text=True)
        # 只认"声明"（`column-count:` / `column-span:`），注释里提到这两个词是允许的
        assert not re.search(r'column-count\s*:', html), \
            '首页不能用 CSS 多列（追加会跨列重排）'
        assert not re.search(r'column-span\s*:', html), '分隔条不再需要跨列'

        js = pathlib.Path('pixivviewer/static/page-home.js').read_text(encoding='utf-8')
        assert "className = 'masonry-col'" in js, '列容器由脚本创建（个数必须与列数一致）'
        assert 'offsetHeight' in js, '最短列优先要读实时列高'
        assert 'matchMedia' in js, '列数（3/2）按视口断点由脚本判定'

    def test_home_masonry_falls_back_to_round_robin_when_columns_have_no_height(self, client):
        """列高不可用时必须按序号轮询兜底，不能全塞进第一列。

        回归背景（2026-11-11）：卡片图片还是占位 SVG、尚未解码时，所有列的
        `offsetHeight` 都是 0，`0 < 0` 恒为 false —— 首屏卡片全堆进第 1 列、
        右边整列空白（手机截图实测）。兜底按 `placed % 列数` 轮询，保证一定分散。
        """
        js = pathlib.Path('pixivviewer/static/page-home.js').read_text(encoding='utf-8')
        assert 'heights.some((h) => h > 0)' in js, '要先判断列高是否可用'
        assert 'band.placed % band.cols.length' in js, '不可用时按序号轮询兜底'
        assert 'placed: 0' in js, '列段要记录已放置张数（轮询游标）'

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
        assert b'lightbox' not in explore.data, '灯箱已移除，不再引入 lightbox.js'
