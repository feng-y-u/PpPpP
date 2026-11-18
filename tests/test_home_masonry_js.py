"""首页瀑布流分列逻辑的真实 JS 回归测试（Node 执行 `placeIntoColumns`）。

2026-11-18 用户反馈：手机首页整屏卡片只占一边，另一边空着；两次截图左右两列
轮着出现。不是 11-11 修过的"列高全为 0"（那次整批堆第 1 列），而是更隐蔽的一种：
新追加的卡片图片还没解码，`offsetHeight` 接近 0 —— 追加后列高几乎不变，
"选最矮列"每次仍选中同一列，整批 24 张连续倒进去，另一列就空出好几屏。

这里的 fake 列容器刻意让 `appendChild` 只累加节点自身高度：给 0 高度节点就复现
"追加不改变列高"，给真实高度节点就复现正常场景。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

NODE = shutil.which('node')
HOME_JS = pathlib.Path(__file__).resolve().parents[1] / 'pixivviewer/static/page-home.js'


def _extract_placer() -> str:
    source = HOME_JS.read_text(encoding='utf-8')
    m = re.search(
        r'const UNMEASURED_MIN_HEIGHT[\s\S]*?function placeIntoColumns\(band, node\) \{[\s\S]*?\n\}',
        source,
    )
    assert m, 'page-home.js 里找不到 placeIntoColumns 与 UNMEASURED_MIN_HEIGHT'
    return m.group(0)


def _run_node(script: str) -> dict:
    env = os.environ.copy()
    env['PLACER_SRC'] = _extract_placer()
    proc = subprocess.run([NODE, '-e', script], capture_output=True, timeout=30,
                          encoding='utf-8', errors='replace', env=env)
    assert proc.returncode == 0, f'node 执行失败：{proc.stderr}'
    return json.loads(proc.stdout)


def test_place_into_columns_spreads_cards_that_have_no_height_yet():
    """追加的卡片量不出高度时，不能整批倒进同一列（用户看到"半屏空白"）。"""
    if not NODE:
        pytest.skip('node 不可用，跳过瀑布流分列测试')

    script = r"""
const vm = require('vm');
const fs = require('fs');

function makeBand(heights) {
  const cols = heights.map((h) => {
    const col = {
      h,
      children: [],
      get offsetHeight() { return this.h; },
      appendChild(node) { this.children.push(node); this.h += node.height; },
    };
    return col;
  });
  return { cols, placed: 0 };
}
function makeNode(height) {
  return { height, get offsetHeight() { return this.height; } };
}

const placeIntoColumns = vm.runInNewContext(process.env.PLACER_SRC + '\n;placeIntoColumns', {});
const band = makeBand([520, 180]);          // 第 2 列更矮
for (let i = 0; i < 24; i++) {
  placeIntoColumns(band, makeNode(0));      // 卡片图片还没解码 → 高度 0
}
const counts = band.cols.map((c) => c.children.length);
process.stdout.write(JSON.stringify({ counts, heights: band.cols.map((c) => c.h) }));
"""
    result = _run_node(script)

    counts = result['counts']
    assert min(counts) >= 8, (
        f'24 张无高度卡片应大致均分到两列，实际 {counts}（整批堆进一列 = 用户看到的半屏空白）'
    )
    assert max(counts) - min(counts) <= 2, f'两列张数不应差太多，实际 {counts}'


def test_place_into_columns_still_prefers_shortest_when_heights_are_real():
    """有真实高度时仍按最矮列落位，列高差不超过一张卡。"""
    if not NODE:
        pytest.skip('node 不可用，跳过瀑布流分列测试')

    script = r"""
const vm = require('vm');

function makeBand(heights) {
  const cols = heights.map((h) => ({
    h,
    children: [],
    get offsetHeight() { return this.h; },
    appendChild(node) { this.children.push(node); this.h += node.height; },
  }));
  return { cols, placed: 0 };
}
function makeNode(height) {
  return { height, get offsetHeight() { return this.height; } };
}

const placeIntoColumns = vm.runInNewContext(process.env.PLACER_SRC + '\n;placeIntoColumns', {});
const band = makeBand([520, 180]);
for (let i = 0; i < 24; i++) {
  placeIntoColumns(band, makeNode(240));    // 每张卡都有真实高度
}
const heights = band.cols.map((c) => c.h);
process.stdout.write(JSON.stringify({
  counts: band.cols.map((c) => c.children.length),
  heights,
  diff: Math.abs(heights[0] - heights[1]),
}));
"""
    result = _run_node(script)

    assert min(result['counts']) > 0, f'两列都应有卡片，实际 {result["counts"]}'
    assert result['diff'] <= 240 + 1, f'列高差不应超过一张卡，实际差 {result["diff"]}px'


def test_home_placeholder_image_declares_intrinsic_size():
    """占位图必须带 width/height：解码前就有内在比例，卡片插入即有余量高度。"""
    source = HOME_JS.read_text(encoding='utf-8')
    assert 'width="400" height="400"' in source, (
        '占位 <img> 必须声明 width/height，否则图片解码前列高不可测，'
        '瀑布流会把整批卡片堆进同一列'
    )
