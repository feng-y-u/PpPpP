"""TLS 拦截诊断：判断当前网络/代理是否在做 TLS 中间人解密。

**只读诊断**：只做 TLS 握手并读取对端叶证书，不发送任何应用层数据、不写任何文件。

背景：`config.SSL_VERIFY` 默认已开启。只有"你的代理确实在做 TLS 拦截（用自签根
证书解密流量）"时才允许设 `SSL_VERIFY=false` —— 那种情况下开启校验会让所有 Pixiv
请求失败。本脚本就是判断"到底该不该关"的工具，网络或代理变更后可重跑。

用法：
    python scripts/check_tls.py                 # 探测 PIXIV_BASE_URL 与 i.pximg.net
    python scripts/check_tls.py --timeout 15    # 网络慢时放宽
    python scripts/check_tls.py --host img.example.net

判定规则与退出码：
    0  可安全开启校验：至少一条链路（直连或代理）verify=True 握手成功；
       若直连与代理都成功，两者叶证书指纹必须一致
    1  疑似 TLS 拦截／本机缺 CA：所有 verify=True 都失败而 verify=False 能连通，
       或直连与代理的叶证书指纹不一致（= 代理替换了证书）
    2  无法判定：两种方式都连不上（网络不可达），先修网络再跑

注意：直连不可达（例如本机只能走代理出网）**不算**拦截 —— 只要代理这条链路
verify=True 成功且 issuer 是公共 CA 即可判 0。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
import ssl
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402  （只为读取 PROXY / PIXIV_BASE_URL / SSL_VERIFY）

VIAS = ('direct', 'proxy')


def _issuer_text(cert: dict) -> str:
    """把 `getpeercert()` 的 issuer 结构拍平成可读文本。"""
    parts = []
    for rdn in cert.get('issuer', ()):  # ((('commonName', 'WR1'),), ...)
        for key, value in rdn:
            parts.append(f'{key}={value}')
    return ', '.join(parts) if parts else '（未取得：verify=False 时不返回证书字段）'


def _tls_handshake(host: str, port: int, *, proxy: str | None, verify: bool,
                   timeout: float) -> dict:
    """做一次 TLS 握手，返回 `{ok, error, fingerprint, issuer}`。

    `proxy` 非空时先向代理发 CONNECT 建立隧道再握手（与 requests 的行为一致）。
    """
    raw = None
    try:
        if proxy:
            parts = urlsplit(proxy if '://' in proxy else f'http://{proxy}')
            raw = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
            raw.sendall(f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n'.encode())
            head = b''
            while b'\r\n\r\n' not in head:
                chunk = raw.recv(4096)
                if not chunk:
                    raise OSError('代理在 CONNECT 阶段断开连接')
                head += chunk
            status = head.split(b'\r\n', 1)[0].decode('latin-1', 'replace')
            if ' 200' not in status:
                raise OSError(f'代理 CONNECT 失败：{status}')
        else:
            raw = socket.create_connection((host, port), timeout=timeout)

        context = ssl.create_default_context() if verify else ssl._create_unverified_context()
        with context.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
            cert = tls.getpeercert() or {}
        return {
            'ok': True,
            'error': None,
            'fingerprint': hashlib.sha256(der).hexdigest()[:16],
            'issuer': _issuer_text(cert),
        }
    except Exception as e:  # 任何失败都只是诊断信息，不抛出
        return {'ok': False, 'error': f'{type(e).__name__}: {e}',
                'fingerprint': None, 'issuer': None}
    finally:
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass


def _ok(probes: dict, host: str, via: str, verify: bool) -> bool:
    return bool(probes.get((host, via, verify), {}).get('ok'))


def evaluate(probes: dict, *, hosts: tuple[str, ...]) -> tuple[int, list[str]]:
    """根据探测结果给出判定。`probes` 键为 `(host, via, verify)`，via ∈ VIAS。"""
    lines: list[str] = []
    codes: list[int] = []
    for host in hosts:
        trusted = [via for via in VIAS if _ok(probes, host, via, True)]
        reachable = [via for via in VIAS if _ok(probes, host, via, False)]
        if not trusted:
            if reachable:
                codes.append(1)
                lines.append(
                    f'[疑似拦截] {host}: verify=True 全部失败，但关闭校验能连通 '
                    f'（{"、".join(reachable)}）→ 链路上有不可信 CA，或本机缺 CA 证书。'
                    f'查明原因前保持 SSL_VERIFY=false。')
            else:
                codes.append(2)
                lines.append(f'[无法判定] {host}: 直连与代理都连不上（网络不可达）。')
            continue

        issuer = probes[(host, trusted[0], True)].get('issuer') or '?'
        lines.append(f'[通过] {host}: verify=True 成功（{"、".join(trusted)}），issuer={issuer}')

        if len(trusted) == len(VIAS):
            fp_direct = probes[(host, 'direct', True)]['fingerprint']
            fp_proxy = probes[(host, 'proxy', True)]['fingerprint']
            if fp_direct != fp_proxy:
                codes.append(1)
                lines.append(
                    f'[疑似拦截] {host}: 直连与代理的叶证书指纹不一致 '
                    f'（{fp_direct} vs {fp_proxy}）→ 代理替换了证书。')
            else:
                codes.append(0)
                lines.append(f'[一致] {host}: 直连与代理叶证书指纹相同（{fp_direct}）→ 纯透传。')
        else:
            # 只有一条链路可达（例如本机只能走代理出网）：无法交叉印证，但已由
            # 系统 CA 库验证过，可判安全。
            codes.append(0)
            lines.append(f'[说明] {host}: 只有 {"、".join(trusted)} 可达，无法交叉印证指纹；'
                         f'该链路已通过系统 CA 校验。')

    if 1 in codes:
        return 1, lines
    if 2 in codes:
        return 2, lines
    return 0, lines


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url if '://' in url else f'https://{url}').hostname or '').lower()
    except ValueError:
        return ''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='TLS 拦截诊断（只读）')
    parser.add_argument('--timeout', type=float, default=8.0, help='单次握手超时秒数（默认 8）')
    parser.add_argument('--host', action='append', default=None,
                        help='附加探测主机，可重复（默认含 PIXIV_BASE_URL 与 i.pximg.net）')
    args = parser.parse_args(argv)

    hosts = tuple(dict.fromkeys(
        h for h in [_host_of(config.PIXIV_BASE_URL), 'i.pximg.net'] + (args.host or []) if h))
    vias = ('direct', 'proxy') if config.PROXY else ('direct',)

    print(f'探测主机: {", ".join(hosts)}')
    print(f'代理: {config.PROXY or "（未配置）"}    当前 SSL_VERIFY={config.SSL_VERIFY}\n')

    probes: dict = {}
    for host in hosts:
        for via in vias:
            for verify in (True, False):
                result = _tls_handshake(host, 443,
                                        proxy=config.PROXY if via == 'proxy' else None,
                                        verify=verify, timeout=args.timeout)
                probes[(host, via, verify)] = result
                status = f'OK   {result["fingerprint"]}  {result["issuer"]}' if result['ok'] \
                    else f'FAIL {result["error"]}'
                print(f'  {host:<24} {via:<7} verify={str(verify):<5} → {status}')

    code, lines = evaluate(probes, hosts=hosts)

    print()
    for line in lines:
        print(line)
    conclusion = {
        0: '可安全开启校验（SSL_VERIFY=true，默认值）',
        1: '疑似 TLS 拦截／缺 CA：保留 SSL_VERIFY=false，并查明是不可信 CA 还是本机缺证书',
        2: '无法判定：网络不可达，先修网络再跑本脚本',
    }[code]
    print(f'\n结论（退出码 {code}）：{conclusion}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
