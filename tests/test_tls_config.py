"""审计 S7a：`SSL_VERIFY` 默认值 与 TLS 拦截诊断脚本的判定逻辑。

两条关注点分开测：
  - 默认值本身（模块级表达式，没有可 patch 的 seam → 源码级断言，与 S6 同款做法）
  - 判定逻辑（纯函数 `evaluate`，可完全离线构造探测结果）
"""
import ast
import pathlib

from pixivviewer import config
from scripts import check_tls


def _config_tree():
    return ast.parse(pathlib.Path(config.__file__).read_text(encoding='utf-8'))


def _assigned_source(name: str) -> str:
    """取出 config.py 里某个模块级赋值的源码片段。"""
    for node in _config_tree().body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.unparse(node.value)
    raise AssertionError(f'pixivviewer/config.py 里找不到模块级赋值 {name}')


def test_config_security_defaults():
    """默认必须是"开启校验 + 只信官方主机"（两件都属「默认值本身」这一关注点）。

    校验默认开启：关闭校验等于允许链路上任何人读改流量。旧实现是字面量
    `SSL_VERIFY = False`；新实现只允许通过环境变量 `SSL_VERIFY=false` 显式关闭
    （TLS 拦截型代理的逃生门）。

    白名单是配套的另一半：它决定"取图时是否携带凭据"，官方图床必须在内，否则
    `/thumb` 与下载会退化成无凭据请求。
    """
    source = _assigned_source('SSL_VERIFY')
    assert "os.environ.get('SSL_VERIFY', 'true')" in source
    assert "!= 'false'" in source
    assert 'i.pximg.net' in config.IMAGE_HOST_ALLOWLIST


# ── 诊断脚本判定逻辑 ──

def _probe(ok=True, fingerprint='aaaa', issuer='CN=Real CA', error=None):
    return {'ok': ok, 'fingerprint': fingerprint if ok else None,
            'issuer': issuer if ok else None, 'error': error}


def _verdict(probes):
    """单主机探测结果 → (退出码, 输出行)。"""
    return check_tls.evaluate(probes, hosts=('i.pximg.net',))


def test_verdict_passes_without_interception_evidence():
    """判「通过」（code 0）的两种证据形状：双链路指纹相同，或只有一条链路可达。

    后者（例如本机只能走代理出网、直连不可达）无法交叉印证指纹，但该链路本身已由
    系统 CA 校验通过 —— 判成拦截会让"代理是唯一出口"的部署收到误导性结论。
    """
    same_fingerprint = {
        ('i.pximg.net', 'direct', True): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'proxy', True): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'direct', False): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='abc123'),
    }
    single_reachable_path = {
        ('i.pximg.net', 'direct', True): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'direct', False): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'proxy', True): _probe(fingerprint='abc123'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='abc123'),
    }

    for label, probes, evidence in (
            ('双链路指纹相同', same_fingerprint, '指纹相同'),
            ('只有代理可达', single_reachable_path, '无法交叉印证')):
        code, lines = _verdict(probes)
        assert code == 0, f'{label} 应判通过（0），实际 {code}：{lines}'
        assert any(evidence in line for line in lines), f'{label} 缺少判据说明：{lines}'


def test_verdict_flags_interception():
    """判「疑似拦截」（code 1）的两种证据：双链路叶证书指纹不一致（决定性判据），
    或 verify=True 全失败而关闭校验能连通（链路上有不可信 CA / 本机缺 CA 证书）。"""
    replaced_cert = {
        ('i.pximg.net', 'direct', True): _probe(fingerprint='real1234'),
        ('i.pximg.net', 'proxy', True): _probe(fingerprint='fake9999'),
        ('i.pximg.net', 'direct', False): _probe(fingerprint='real1234'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='fake9999'),
    }
    untrusted_ca = {
        ('i.pximg.net', 'direct', True): _probe(ok=False, error='SSLCertVerificationError'),
        ('i.pximg.net', 'proxy', True): _probe(ok=False, error='SSLCertVerificationError'),
        ('i.pximg.net', 'direct', False): _probe(fingerprint='self1234'),
        ('i.pximg.net', 'proxy', False): _probe(fingerprint='self1234'),
    }

    for label, probes, evidence in (
            ('代理替换了证书', replaced_cert, '指纹不一致'),
            ('链路上有不可信 CA', untrusted_ca, '疑似拦截')):
        code, lines = _verdict(probes)
        assert code == 1, f'{label} 应判疑似拦截（1），实际 {code}：{lines}'
        assert any(evidence in line for line in lines), f'{label} 缺少判据说明：{lines}'


def test_verdict_inconclusive_when_network_unreachable():
    """直连与代理都连不上：没有证据可判，必须如实说"无法判定"（code 2），
    而不是把"探测不出来"冒充成"没问题"。"""
    probes = {
        ('i.pximg.net', 'direct', True): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'proxy', True): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'direct', False): _probe(ok=False, error='TimeoutError'),
        ('i.pximg.net', 'proxy', False): _probe(ok=False, error='TimeoutError'),
    }
    code, lines = _verdict(probes)
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
