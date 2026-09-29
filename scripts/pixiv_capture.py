"""抓一份 Pixiv 响应样本并**就地脱敏**，覆盖 `tests/fixtures/pixiv/`（维护工具）。

为什么要它：`tests/test_pixiv_contract.py` 的断言建立在"真实响应的结构"上。Pixiv 改了
字段/嵌套时，正确的处理顺序是「重抓样本 → 看契约测试哪条红 → 只改 `pixiv_client` 的
解析」。这个脚本负责第一步，并且**不许把真实账号数据带进仓库**。

用法（需要可用的 `cookies.txt`，即 `config.COOKIE_PATH`）：
    venv\\Scripts\\python.exe scripts\\pixiv_capture.py \\
        --illust-id 100000001 --user-id 9000001 --tag サンプル
    # 只想刷新某一部分时，省略不需要的参数（对应文件保持不动）

抓完**必须自查**：`git diff tests/fixtures/pixiv/` 只应看到字段名/结构的变化。
出现真实昵称、作品标题、图片 URL 路径、Cookie 即说明脱敏漏了 —— 不要提交。

脱敏规则（与 fixtures/pixiv/README.md 一致）：
- 身份字段 → 占位值；图片 URL → `https://i.pximg.net/redacted/_pN.ext`（**保留 `_pN`
  页序与扩展名**，原图地址解析正是靠它）；标签名 → `サンプル` 类占位（`R-18`/`R-18G`
  原样保留，下游按它过滤）；
- 只写**结构**：字段名、嵌套层级、类型全都保留。

不抓错误信封（`error_envelope_*.json`）：那种响应要在 Cookie 失效/作品被删时才出现，
无法按需复现，故由人工维护。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402  （触发 .env / settings.json 装载，拿到 COOKIE_PATH）
import pixiv_client  # noqa: E402

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tests', 'fixtures', 'pixiv')

# 身份类字段：一律换成占位值（值本身对契约毫无意义，只留类型）
_IDENTITY_KEYS = {
    'id', 'illustId', 'userId', 'userName', 'userAccount', 'authorId',
    'title', 'illustTitle', 'description', 'illustComment', 'message',
    'translation', 'romaji', 'illustComment',
}
# URL 类字段：收敛成 canonical 占位（保留 _pN 与扩展名）
_URL_KEYS = {
    'url', 'thumb', 'small', 'regular', 'original', 'mini',
    'profileImageUrl', 'originalImageUrl', 'imageUrls', 'nextUrl',
}
# 标签：只保留"是否 R18"这一位信息，其余换成占位
_KEEP_TAGS = {'R-18', 'R-18G'}

_URL_TAIL_RE = re.compile(r'(_p\d+(?:_[A-Za-z0-9]+)?\.(?:jpg|jpeg|png|gif|webp))')


def _redact_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        return value
    m = _URL_TAIL_RE.search(value)
    tail = m.group(1) if m else '.jpg'
    return f'https://i.pximg.net/redacted/{tail}'


def _redact_tag_name(name: str) -> str:
    return name if name in _KEEP_TAGS else 'サンプル'


def _redact(value, key: str = ''):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _IDENTITY_KEYS:
                out[k] = _placeholder(k, v)
            elif k == 'tags':
                out[k] = _redact_tags(v)
            elif k in _URL_KEYS:
                out[k] = _redact_url(v) if isinstance(v, str) else _redact(v, k)
            else:
                out[k] = _redact(v, k)
        return out
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


def _redact_tags(tags):
    """tags 有三种形态（字符串列表 / `[{tag: …}]` / `{tags: [{tag: …}]}`），全保留形态。"""
    if isinstance(tags, list):
        return [
            _redact_tag_name(t) if isinstance(t, str)
            else {**t, 'tag': _redact_tag_name(str(t.get('tag', '')))}
            if isinstance(t, dict) else t
            for t in tags
        ]
    if isinstance(tags, dict):
        inner = tags.get('tags')
        if isinstance(inner, list):
            return {**tags, 'tags': _redact_tags(inner)}
    return tags


def _placeholder(key: str, value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(str(value)[:6] or 1)
    if isinstance(value, str):
        if key in ('userName', 'userAccount', 'title', 'illustTitle'):
            return '見本サンプル'
        if key in ('id', 'illustId', 'userId', 'authorId'):
            return '9000' + str(abs(hash(value)) % 10000).zfill(4)[:4]
        return ''
    return value


def _write(out_dir: str, name: str, payload: dict) -> None:
    path = os.path.join(out_dir, name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'wrote {path}')


def main() -> int:
    parser = argparse.ArgumentParser(description='抓取并脱敏 Pixiv 响应样本')
    parser.add_argument('--illust-id', type=int, help='用于详情样本的作品 id')
    parser.add_argument('--user-id', help='用于 profile/all 样本的画师 id')
    parser.add_argument('--tag', help='用于搜索样本的关键词')
    parser.add_argument('--out', default=DEFAULT_OUT, help='输出目录')
    parser.add_argument('--skip-discovery', action='store_true',
                        help='不抓发现页（它在没有标签时返回 403/R18 混合内容，可选）')
    parser.add_argument('--skip-follow', action='store_true',
                        help='不抓关注页（需要账号确实关注了画师）')
    args = parser.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    session = pixiv_client.build_pixiv_session()
    written = 0

    if args.illust_id:
        body = session.get(
            pixiv_client.endpoint_illust_detail(args.illust_id),
            timeout=config.DETAIL_TIMEOUT).json()
        _write(out_dir, 'illust_detail_meta_pages.json',
               _redact({'error': body.get('error', False), 'message': '',
                        'body': body.get('body', {})}))
        written += 1
    else:
        print('skip detail（未传 --illust-id）')

    if args.tag:
        url = pixiv_client.endpoint_search_illustrations(
            args.tag, order='date_d', mode='all', page=1)
        _write(out_dir, 'search_illustrations.json', _redact(session.get(url).json()))
        written += 1
    else:
        print('skip search（未传 --tag）')

    if args.user_id:
        url = pixiv_client.endpoint_user_profile_all(args.user_id)
        _write(out_dir, 'user_profile_all.json', _redact(session.get(url).json()))
        written += 1
    else:
        print('skip profile（未传 --user-id）')

    if not args.skip_discovery:
        url = pixiv_client.endpoint_discovery_artworks(
            mode='all', page=1, limit=60, order='date_d')
        _write(out_dir, 'discovery_artworks.json', _redact(session.get(url).json()))
        written += 1

    if not args.skip_follow:
        url = pixiv_client.endpoint_follow_latest_illust(mode='all', page=1)
        _write(out_dir, 'follow_latest.json', _redact(session.get(url).json()))
        written += 1

    print(f'\n完成 {written} 个样本。请自查 git diff：只应看到字段名/结构变化。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
