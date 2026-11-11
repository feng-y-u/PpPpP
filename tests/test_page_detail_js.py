"""`page-detail.js` 页大小的真实 JS 执行测试。

为什么不在 Python 里做字符串断言：页大小是浏览器运行时从
`<meta name="items-per-page">` 读取的，只有真正执行那段 IIFE 才能证明
"读 meta / 缺省回退 / 0 值回退"三条路径。这里用 Node 的 `vm` 执行源文件里
**原样的** `PAGE_SIZE` 代码块，只 mock `document.querySelector`。

Node 不是项目运行依赖；没有 node 的环境 skip 而不是失败。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

NODE = shutil.which('node')


def test_page_detail_page_size_actually_reads_meta():
    if not NODE:
        pytest.skip('node 不可用，跳过 page-detail.js 运行时测试')

    source_path = pathlib.Path(__file__).resolve().parents[1] / 'pixivviewer/static/page-detail.js'
    source = source_path.read_text(encoding='utf-8')
    assert 'const PAGE_SIZE = (() => {' in source, '源文件里找不到 PAGE_SIZE 代码块'

    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.env.PAGE_DETAIL_JS, 'utf8');
const match = source.match(/const PAGE_SIZE = \(\(\) => \{[\s\S]*?\}\)\(\);/);
if (!match) throw new Error('PAGE_SIZE block not found');

function run(meta) {
  const document = {
    querySelector(selector) {
      if (!selector.includes('items-per-page')) return null;
      return meta === null ? null : { getAttribute: () => meta };
    },
  };
  const context = { document, Number, parseInt };
  vm.createContext(context);
  vm.runInContext(match[0] + '\n__result = PAGE_SIZE;', context);
  return context.__result;
}

process.stdout.write(JSON.stringify({
  meta24: run('24'),
  zero: run('0'),
  missing: run(null),
}));
"""

    env = os.environ.copy()
    env['PAGE_DETAIL_JS'] = str(source_path.resolve())
    proc = subprocess.run(
        [NODE, '-e', script],
        capture_output=True, text=True, timeout=30, env=env,
    )

    assert proc.returncode == 0, f'node 执行失败：{proc.stderr}'
    data = json.loads(proc.stdout)
    assert data == {'meta24': 24, 'zero': 50, 'missing': 50}, data
