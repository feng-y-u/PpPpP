"""X 抓图可行性探针（只读、脱敏）。

在服务器上先跑这个，再决定要不要开抓取：

    python scripts/x_probe.py              # 用默认探针标签
    python scripts/x_probe.py '#原神'      # 指定标签

它只做三件事：
1. 读 `instance/x_cookies.txt`（auth_token + ct0）与 `instance/x_api.json`（query_id）；
2. 跑一次 SearchTimeline，打印 HTTP 结论 / 条数 / 首条媒体地址；
3. 按错误类型给出下一步动作（补 Cookie / 改 query_id / 查网络）。

输出里的 Cookie 与完整 URL 一律脱敏，可以安全贴进 issue。
退出码：0 成功；1 Cookie/query_id/网络问题；2 无法判定。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pixivviewer import config, x_client  # noqa: E402


def _mask(value: str) -> str:
    if len(value) <= 8:
        return '*' * len(value)
    return f'{value[:4]}…{value[-4:]}'


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else '#テスト'
    print(f'[probe] 实例目录：{config._instance_dir}')
    print(f'[probe] Cookie 文件：{config.X_COOKIE_PATH}（存在：{os.path.exists(config.X_COOKIE_PATH)}）')
    print(f'[probe] API 配置：{config.X_API_CONFIG_PATH}（存在：{os.path.exists(config.X_API_CONFIG_PATH)}）')
    print(f'[probe] 代理：{config.PROXY or "未配置（直连）"}')
    print(f'[probe] 探测标签：{tag!r}')

    try:
        auth_token, ct0 = x_client.load_cookie()
        print(f'[probe] Cookie：auth_token={_mask(auth_token)} ct0={_mask(ct0)}')
    except x_client.XAuthError as e:
        print(f'[probe] ❌ {e}')
        print('[probe] 处理：把浏览器里 x.com 的 auth_token 与 ct0 写进 instance/x_cookies.txt')
        return 1

    try:
        url = x_client.build_search_url(tag)
        print(f'[probe] 请求：{url.split("?")[0]}')
    except x_client.XQueryStale as e:
        print(f'[probe] ❌ {e}')
        print('[probe] 处理：浏览器打开 x.com → F12 → Network 里搜 "SearchTimeline"，'
              '把 URL 中 /graphql/<query_id>/ 的那一段写进 instance/x_api.json 的 query_id')
        return 1

    try:
        tweets, cursor = x_client.search_timeline(tag)
    except x_client.XAuthError as e:
        print(f'[probe] ❌ {e}')
        return 1
    except x_client.XRateLimited as e:
        print(f'[probe] ❌ {e}')
        print('[probe] 处理：等 15 分钟再试；抓取间隔建议 ≥ 3600 秒')
        return 1
    except x_client.XQueryStale as e:
        print(f'[probe] ❌ {e}')
        print('[probe] 处理：更新 instance/x_api.json 的 query_id（以及必要时 features）')
        return 1
    except x_client.XNetworkError as e:
        print(f'[probe] ❌ {e}')
        print('[probe] 处理：确认服务器能直连 x.com / 代理配置是否正确')
        return 2
    except x_client.XError as e:
        print(f'[probe] ❌ {e}')
        return 2

    with_media = [t for t in tweets if t.media]
    print(f'[probe] ✅ 返回 {len(tweets)} 条推文，其中 {len(with_media)} 条带图')
    if cursor:
        print(f'[probe] 下一页游标：{cursor[:24]}…')
    if with_media:
        sample = with_media[0]
        print(f'[probe] 样本：@{sample.author_screen_name} {sample.tweet_id}')
        print(f'[probe] 媒体：{sample.media[0].url} ({sample.media[0].width}x{sample.media[0].height})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
