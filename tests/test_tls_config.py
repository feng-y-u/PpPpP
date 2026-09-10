"""审计 S7a：`SSL_VERIFY` 默认值 与 TLS 拦截诊断脚本的判定逻辑。

两条关注点分开测：
  - 默认值本身（模块级表达式，没有可 patch 的 seam → 源码级断言，与 S6 同款做法）
  - 判定逻辑（纯函数 `evaluate`，可完全离线构造探测结果）
"""
import ast
import pathlib

import config
from scripts import check_tls


def _config_tree():
    return ast.parse(pathlib.Path(config.__file__).read_text(encoding='utf-8'))


def _assigned_source(name: str) -> str:
    """取出 config.py 里某个模块级赋值的源码片段。"""
    for node in _config_tree().body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.unparse(node.value)
    raise AssertionError(f'config.py 里找不到模块级赋值 {name}')


def test_ssl_verify_defaults_to_true():
    """默认必须是"开启校验"：关闭校验等于允许链路上任何人读改流量。

    旧实现是字面量 `SSL_VERIFY = False`；新实现只允许通过环境变量
    `SSL_VERIFY=false` 显式关闭（TLS 拦截型代理的逃生门）。
    """
    source = _assigned_source('SSL_VERIFY')
    assert "os.environ.get('SSL_VERIFY', 'true')" in source
    assert "!= 'false'" in source


def test_image_host_allowlist_contains_official_host():
    assert 'i.pximg.net' in config.IMAGE_HOST_ALLOWLIST


# ── 诊断脚本判定逻辑 ──

def _probe(ok=True, fingerprint='aaaa', issuer='CN=Real CA', error=None):
    return {'ok': ok, 'fingerprint': fingerprint if ok else None,
            'issuer': issuer if ok else None, 'error': error}


def test_verdict_passes_when_fingerprints_match():
    probes = {
        ('i.pximg.net', 'direct', True): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'proxy', True): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'direct', False): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='abc123'),
    }
    code, lines = check_tls.evaluate(probes, hosts=('i.pximg.net',))
    assert code == 0
    assert any('指纹相同' in line for line in lines)


def test_verdict_flags_mitm_when_fingerprints_differ():
    """直连与代理的叶证书不同 = 代理替换了证书（决定性判据）。"""
    probes = {
        ('i.pximg.net', 'direct', True): _probe(fingerprint='real1234'),
        ('i.pximg.net', 'proxy', True): _probe(fingerprint='fake9999'),
        ('i.pximg.net', 'direct', False): _probe(fingerprint='real1234'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='fake9999'),
    }
    code, lines = check_tls.evaluate(probes, hosts=('i.pximg.net',))
    assert code == 1
    assert any('指纹不一致' in line for line in lines)


def test_verdict_flags_untrusted_ca_when_only_unverified_connects():
    """verify=True 全失败而 verify=False 能连通：不可信 CA / 缺 CA 证书。"""
    probes = {
        ('i.pximg.net', 'direct', True): _probe(ok=False, error='SSLCertVerificationError'),
        ('i.pximg.net', 'proxy', True): _probe(ok=False, error='SSLCertVerificationError'),
        ('i.pximg.net', 'direct', False): _probe(fingerprint='self1234'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='self1234'),
    }
    code, lines = check_tls.evaluate(probes, hosts=('i.pximg.net',))
    assert code == 1
    assert any('疑似拦截' in line for line in lines)


def test_verdict_passes_with_single_reachable_path():
    """只走代理出网（直连不可达）不算拦截：该链路本身已通过系统 CA 校验。"""
    probes = {
        ('i.pximg.net', 'direct', True): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'direct', False): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'proxy', True): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='abc123'),
    }
    code, lines = check_tls.evaluate(probes, hosts=('i.pximg.net',))
    assert code == 0
    assert any('无法交叉印证' in line for line in lines)


def test_verdict_inconclusive_when_network_unreachable():
    probes = {
        ('i.pximg.net', 'direct', True): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'proxy', True): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'direct', False): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'proxy', False): _probe(ok=False, error='TimeoutError'),
    }
    code, lines = check_tls.evaluate(probes, hosts=('i.pximg.net',))
    assert code == 2
    assert any('无法判定' in line for line in lines)


def test_interception_wins_over_inconclusive():
    """一台主机疑似拦截、另一台连不上：结论必须是"疑似拦截"而不是"无法判定"。"""
    probes = {
        ('a.example', 'direct', True): _probe(fingerprint='real'),
        ('a.example', 'proxy', True): _probe(fingerprint='fake'),
        ('a.example', 'direct', False): _probe(fingerprint='real'),
        ('a.example', 'proxy', False): _probe(fingerprint='fake'),
        ('b.example', 'direct', True): _probe(ok=False, error='TimeoutError'),
        ('b.example', 'proxy', True): _probe(ok=False, error='TimeoutError'),
        ('b.example', 'direct', False): _probe(ok=False, error='TimeoutError'),
        ('b.example', 'proxy', False): _probe(ok=False, error='TimeoutError'),
    }
    code, _ = check_tls.evaluate(probes, hosts=('a.example', 'b.example'))
    assert code == 1
