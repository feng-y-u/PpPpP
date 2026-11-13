"""`app.js` 懒加载 observer 的真实 JS 回归测试。

首页无限加载时每批都会调用 `lazyLoad()`。旧实现每调用一次就 new 一个
IntersectionObserver，并重新 observe 所有未加载图片；批次越多 observer 越多，
同一张图被重复回调，滚动明显变卡。这里用 Node 执行真实 `app.js`，只 mock
DOM/IntersectionObserver，验证 observer 复用与 reset。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

NODE = shutil.which('node')


def test_lazy_load_reuses_one_observer_and_does_not_duplicate_targets():
    if not NODE:
        pytest.skip('node 不可用，跳过 lazyLoad 运行时测试')

    app_js = pathlib.Path(__file__).resolve().parents[1] / 'pixivviewer/static/app.js'

    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.env.APP_JS, 'utf8');

const images = [
  { dataset: { src: 'a' }, removeAttribute(name) { if (name === 'data-src') delete this.dataset.src; } },
  { dataset: { src: 'b' }, removeAttribute(name) { if (name === 'data-src') delete this.dataset.src; } },
];
const observers = [];

class FakeObserver {
  constructor(cb, opts) {
    this.cb = cb;
    this.opts = opts;
    this.targets = [];
    observers.push(this);
  }
  observe(target) { this.targets.push(target); }
  unobserve(target) { this.targets = this.targets.filter(t => t !== target); }
  disconnect() { this.targets = []; }
}

const document = {
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll() { return images.filter(img => img.dataset.src); },
};

const context = {
  document,
  IntersectionObserver: FakeObserver,
  setInterval() { return 0; },
  setTimeout() { return 0; },
  clearInterval() {},
  console,
};
vm.createContext(context);
vm.runInContext(
  source + '\n;globalThis.__lazyLoad = lazyLoad;globalThis.__resetLazyLoad = resetLazyLoad;',
  context,
);

context.__lazyLoad();
context.__lazyLoad();
if (observers.length !== 1) {
  throw new Error(`observer 应复用 1 个，实际 ${observers.length} 个`);
}
if (observers[0].targets.length !== 2) {
  throw new Error(`两张图应各 observe 一次，实际 ${observers[0].targets.length} 次`);
}

// 第三批只新增一张图，不得重复观察旧图。
images.push({
  dataset: { src: 'c' },
  removeAttribute(name) { if (name === 'data-src') delete this.dataset.src; },
});
context.__lazyLoad();
if (observers.length !== 1 || observers[0].targets.length !== 3) {
  throw new Error(`新增图片后应仍是 1 个 observer / 3 个 target，实际 ${observers.length}/${observers[0].targets.length}`);
}

// 模拟首张图进入视口：加载 src 并从 observer 移除。
observers[0].cb([{ isIntersecting: true, target: images[0] }]);
if (images[0].src !== 'a' || images[0].dataset.src) {
  throw new Error('进入视口后应设置 src 并删除 data-src');
}
if (observers[0].targets.includes(images[0])) {
  throw new Error('加载后应从 observer 移除');
}

// 清空列表时必须断开 observer，避免它继续持有已移除节点。
context.__resetLazyLoad();
if (observers[0].targets.length !== 0) {
  throw new Error('resetLazyLoad 应断开并清空 observer targets');
}

process.stdout.write(JSON.stringify({ observers: observers.length, targets: observers[0].targets.length }));
"""

    env = os.environ.copy()
    env['APP_JS'] = str(app_js.resolve())
    proc = subprocess.run(
        [NODE, '-e', script],
        capture_output=True, text=True, timeout=30, env=env,
    )

    assert proc.returncode == 0, f'node 执行失败：{proc.stderr}'
    assert json.loads(proc.stdout) == {'observers': 1, 'targets': 0}
