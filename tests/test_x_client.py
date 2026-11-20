"""X 适配层（`pixivviewer/x_client.py`）的离线契约测试。

X 没有稳定公开接口，唯一能保证的就是"解析这一份真实结构的样本时行为正确"。
样本在 `tests/fixtures/x/search_page.json`（脱敏，结构取自真实的 SearchTimeline
响应），接口改结构时这份样本和解析函数一起更新。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from pixivviewer import config, x_client

FIXTURE = pathlib.Path(__file__).parent / 'fixtures' / 'x' / 'search_page.json'


@pytest.fixture
def isolated_x(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'X_API_CONFIG_PATH', str(tmp_path / 'x_api.json'))
    monkeypatch.setattr(config, 'X_COOKIE_PATH', str(tmp_path / 'x_cookies.txt'))
    monkeypatch.setattr(config, 'X_SEARCH_QUERY_ID', 'QID-TEST')
    x_client.clear_cookie_cache()
    yield tmp_path
    x_client.clear_cookie_cache()


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def test_search_url_contains_query_id_and_encoded_variables(isolated_x):
    url = x_client.build_search_url('#原神')
    assert '/graphql/QID-TEST/SearchTimeline?' in url
    assert 'variables=' in url and 'features=' in url
    # 中文标签必须被 URL 编码，原样拼进去会 400
    assert '%23%E5%8E%9F%E7%A5%9E' in url or 'rawQuery' in url


def test_missing_query_id_raises_query_stale(isolated_x, monkeypatch):
    monkeypatch.setattr(config, 'X_SEARCH_QUERY_ID', '')
    with pytest.raises(x_client.XQueryStale):
        x_client.build_search_url('test')


def test_parse_keeps_only_photo_tweets_and_normalizes_urls(isolated_x):
    tweets, cursor = x_client.parse_search_page(_payload())

    # 第 2 条是纯视频（应被跳过），第 1/3 条是图片
    assert [t.tweet_id for t in tweets] == ['1800000000000000001', '1800000000000000003']
    assert cursor == 'CURSOR-PAGE-2'

    first = tweets[0]
    assert first.author_screen_name == 'Artist_A'
    assert first.author_name == '画师A'
    assert first.media_count == 2
    assert first.media[0].url == 'https://pbs.twimg.com/media/AAA111.jpg?name=orig'
    assert (first.media[0].width, first.media[0].height) == (1200, 1600)
    assert 'https://www.pixiv.net/artworks/12345678' in first.expanded_urls
    # 已经是 ?name=small 的必须升级成原图（覆盖 name 参数，不能拼出两个问号）
    assert tweets[1].media[0].url == 'https://pbs.twimg.com/media/CCC333.webp?name=orig'


def test_parse_unwraps_visibility_results(isolated_x):
    payload = _payload()
    entry = payload['data']['search_by_raw_query']['search_timeline']['timeline']['instructions'][0]['entries'][1]
    entry['content']['itemContent']['tweet_results']['result']['tweet']['legacy']['extended_entities']['media'][0]['type'] = 'photo'
    tweets, _ = x_client.parse_search_page(payload)
    ids = [t.tweet_id for t in tweets]
    assert '1800000000000000002' in ids


def test_parse_rejects_broken_payload(isolated_x):
    with pytest.raises(x_client.XQueryStale):
        x_client.parse_search_page({})
    with pytest.raises(x_client.XQueryStale):
        x_client.parse_search_page({'data': {'search_by_raw_query': {}}})


def test_extract_pixiv_id_handles_links_and_text():
    assert x_client.extract_pixiv_id(['https://www.pixiv.net/artworks/123']) == 123
    assert x_client.extract_pixiv_id([], '见 pixiv.net/artworks/456 谢谢') == 456
    assert x_client.extract_pixiv_id(['https://www.pixiv.net/en/artworks/789']) == 789
    assert x_client.extract_pixiv_id([], '没有链接') is None


def test_parse_tweet_id_from_urls():
    for url, expected in [
        ('https://x.com/artist/status/1800000000000000001', '1800000000000000001'),
        ('https://twitter.com/a/status/42?s=20', '42'),
        ('https://x.com/artist', None),
        ('', None),
    ]:
        assert x_client.parse_tweet_id(url) == expected


def test_http_error_classification(isolated_x):
    """HTTP 状态码 → 异常类型映射（矩阵写成一条循环：同一不变式的输入变体）。"""
    cases = [
        (401, x_client.XAuthError),
        (403, x_client.XAuthError),
        (429, x_client.XRateLimited),
        (404, x_client.XQueryStale),
        (422, x_client.XQueryStale),
        (500, x_client.XError),
    ]
    for status, kind in cases:
        err = x_client._classify_http(status, 'body')
        assert isinstance(err, kind), f'{status} 应归为 {kind.__name__}'


def test_parse_created_at_is_locale_independent(isolated_x):
    """X 的 created_at 解析不依赖进程 locale（%a/%b 会按 LC_TIME 解释英文缩写）。

    标准格式、无时区尾巴的畸形输入、乱月份名三档输入钉同一个不变式：
    合法输入给准确时间，其它一律 None（绝不让异常漏出去炸掉整页解析）。
    """
    parsed = x_client._parse_created_at('Mon Nov 17 10:00:00 +0000 2026')
    assert (parsed.year, parsed.month, parsed.day) == (2026, 11, 17)
    assert (parsed.hour, parsed.minute, parsed.second) == (10, 0, 0)

    for bad in ('', None, 'not a date', 'Mon Foo 17 10:00:00 +0000 2026',
                'Mon Nov 17 10:00 +0000 2026', 'Mon Nov 17 25:00:00 +0000 2026'):
        assert x_client._parse_created_at(bad) is None, f'{bad!r} 应解析为 None'


def test_load_cookie_parses_both_formats(isolated_x):
    path = isolated_x / 'x_cookies.txt'
    path.write_text('auth_token=AAA; ct0=BBB', encoding='utf-8')
    assert x_client.load_cookie() == ('AAA', 'BBB')

    path.write_text('auth_token=CCC\nct0=DDD\n', encoding='utf-8')
    x_client.clear_cookie_cache()
    assert x_client.load_cookie() == ('CCC', 'DDD')


def test_load_cookie_missing_keys_raises(isolated_x):
    (isolated_x / 'x_cookies.txt').write_text('auth_token=AAA', encoding='utf-8')
    x_client.clear_cookie_cache()
    with pytest.raises(x_client.XAuthError):
        x_client.load_cookie()


class _FakeResp:
    """search_timeline 只用到 status_code / text / json 三样，这里够用。"""

    def __init__(self, status, payload=None):
        self.status_code = status
        self.text = ''
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


class TestTransactionHeader:
    """x-client-transaction-id：X 2026 起搜索接口的强制防爬令牌。

    2026-11-20 本地联调实测：参数全对、只缺这个头 → X 回**空 404**，
    极易误判成 query_id 过期。这里钉住三个行为：带头发请求、404 换新素材
    重试一次、未装包时单次请求并给出指向安装的报错。
    """

    @staticmethod
    def _install_fake_session(monkeypatch, responses):
        calls = []

        class _Session:
            def get(self, url, timeout=None, allow_redirects=False, headers=None):
                calls.append({'url': url, 'headers': dict(headers or {})})
                return responses[len(calls) - 1]

            def close(self):
                pass

        monkeypatch.setattr(x_client, '_session', lambda: _Session())
        return calls

    def test_search_sends_transaction_header(self, isolated_x, monkeypatch):
        monkeypatch.setattr(x_client, '_transaction_id', lambda m, p, force_refresh=False: 'TID123')
        calls = self._install_fake_session(monkeypatch, [_FakeResp(200, _payload())])

        tweets, _ = x_client.search_timeline('#tag')
        assert len(tweets) == 2
        assert calls[0]['headers']['x-client-transaction-id'] == 'TID123'

    def test_search_retries_once_with_fresh_material_on_404(self, isolated_x, monkeypatch):
        refresh_flags = []

        def _fake_tid(method, path, force_refresh=False):
            refresh_flags.append(force_refresh)
            return 'TID-NEW' if force_refresh else 'TID-OLD'

        monkeypatch.setattr(x_client, '_transaction_id', _fake_tid)
        calls = self._install_fake_session(
            monkeypatch, [_FakeResp(404), _FakeResp(200, _payload())])

        tweets, _ = x_client.search_timeline('#tag')
        assert len(tweets) == 2, '重试成功后必须正常返回推文'
        assert len(calls) == 2, '404 只重试一次'
        assert calls[0]['headers']['x-client-transaction-id'] == 'TID-OLD'
        assert calls[1]['headers']['x-client-transaction-id'] == 'TID-NEW'
        assert refresh_flags == [False, True], '第二次必须强制刷新素材'

    def test_search_404_without_package_single_attempt_and_hint(self, isolated_x, monkeypatch):
        # 未装包：令牌为空 → 裸发一次；404 报错必须指向安装，而不是让人去改 query_id
        monkeypatch.setattr(x_client, 'TRANSACTION_AVAILABLE', False)
        monkeypatch.setattr(x_client, '_transaction_id', lambda m, p, force_refresh=False: '')
        calls = self._install_fake_session(monkeypatch, [_FakeResp(404)])

        with pytest.raises(x_client.XQueryStale) as ei:
            x_client.search_timeline('#tag')
        assert 'XClientTransaction' in str(ei.value)
        assert len(calls) == 1, '没有令牌可用时不允许重试'
