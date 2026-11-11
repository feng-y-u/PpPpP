# ── 登录 / 设置 / 屏蔽标签 / 自动关注控制 ──
# /login(GET/POST)、/settings、/api/settings、/api/settings/unlock、
# /api/blocked-tags、/api/auto-follow/* 路由 + 私有辅助
# _SETTINGS_PATH/_SETTINGS_DEFAULTS/_load_settings/_settings_locked。
# ACCESS_PASSWORD/SETTINGS_PASSWORD 均经 app 命名空间延迟读取：
# tests monkeypatch('app.ACCESS_PASSWORD'/'app.SETTINGS_PASSWORD')，
# 本模块 from config import 的独立绑定看不到补丁。
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time

from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, session)

from .. import config as config_module
from .. import pixiv_client
from ..fetcher import clear_search_cache
from ..background import get_background_health
from ..helpers import _atomic_write_json, _atomic_write_text
from ..middleware import (_credential_version, _csrf_required, _get_csrf_token,
                        _get_json_body, _is_authed, _rate_limit, _safe_next,
                        _session_authed)
from .prefetch import _PREFETCH_SETTINGS_KEYS
from .home import clear_home_cache
from ..models import BlockedTag, get_session, safe_commit
from ..runtime import (_CREDENTIAL_HEALTH_TTL, _auto_follow_state,
                     _credential_health_cache, _prefetch_state, _snapshot_state)

logger = logging.getLogger(__name__)

bp = Blueprint('settings', __name__)


@bp.route('/login', methods=['GET'])
def login_page():
    if _is_authed():
        return redirect(_safe_next(request.args.get('next', '')))
    return render_template('login.html', csrf_token=_get_csrf_token())


@bp.route('/login', methods=['POST'])
@_csrf_required
@_rate_limit(max_attempts=5, window=60)
def login_submit():
    import app  # 延迟导入读取 app.ACCESS_PASSWORD：tests monkeypatch('app.ACCESS_PASSWORD')
    #             （test_auth.py auth_enabled 夹具），from config import 绑定看不到补丁
    body = _get_json_body()
    password = str(body.get('password', ''))
    if app.ACCESS_PASSWORD and hmac.compare_digest(password.encode(), app.ACCESS_PASSWORD.encode()):
        session.clear()
        session['authed'] = True
        session['credential_version'] = _credential_version()
        session.permanent = True
        return jsonify({'ok': True, 'next': _safe_next(str(body.get('next', '')))})
    time.sleep(1)  # 失败延迟，减缓爆破
    return jsonify({'error': '密码错误'}), 403


# ── 自动关注控制 ──

@bp.route('/api/auto-follow/status')
def auto_follow_status() -> Response:
    """自动关注的运行态（供设置页展示）。

    `alive` 取自 `background.get_background_health()` —— 线程引用的唯一持有者在
    `background`，这里与 `/api/prefetch/status` 的同名字段**同源**，两个路由只是视角
    不同（这里按功能聚合，那里是后台线程总览）。

    刻意返回**副本**：`alive` 是派生值，直接塞进 `_auto_follow_state` 会污染运行态
    （该 dict 同时被自动关注线程本身和 `/api/auto-follow/config` 读写）。

    字段语义提醒：`last_check` 只在**成功拉到关注列表并处理完一轮**时更新（拉不到
    任何作品的那一轮直接 continue），所以它陈旧既可能是"关注列表里没有新作品"、
    也可能是"每一轮都在失败"；`last_count` 是那一轮新增入库的作品数（可以是 0）。
    界面文案必须照此措辞，不能写成"上次轮询时间"。

    这正是 `last_error` 存在的理由（审计 S20 遗留，与预取那套同名同义）：它只在
    成功跑完一轮时清空，所以**非空 = 最近一轮就失败了** —— 把上面那两种"陈旧"
    分开。注意"拉不到任何作品"不写这个字段（Cookie 失效时 Pixiv 静默返回空结果，
    写进去就是假告警），那种轮次也不清它（会抹掉真正的证据）。
    """
    data = dict(_auto_follow_state)
    data['alive'] = get_background_health()['auto_follow_alive']
    return jsonify(data)


@bp.route('/api/pixiv/health')
def pixiv_health() -> Response:
    """凭据体检：本地 Cookie 事实 + 一次上游自检。

    为什么要有这个接口：Cookie 失效时 Pixiv 对搜索类请求常常**静默返回空页**
    （`docs/maintenance.md` 有说明），界面只能显示"没有找到作品" —— 用户没法把
    "真的没结果"与"凭据失效"分开。前端在空结果时调它，然后按 `advice` 给具体建议。

    `probe.result` 的取值与含义：`cookie_missing`（本地就没有文件，不联网）/
    `auth`（上游明确报认证问题）/ `rate_limited`（403，限流而非失效）/
    `network`（连不上，网络或代理）/ `error`（上游非认证错误）/ `ok`（上游接受了请求）。
    注意 `ok` **不等于**"一定有数据"：关注列表本来没有新作品时同样是空结果。
    结论有 30s TTL 缓存，避免用户连点搜索时反复打上游。
    """
    cookie_path = pixiv_client.COOKIE_PATH
    present = os.path.isfile(cookie_path)
    cookie = {
        'path': cookie_path,
        'present': present,
        'age_seconds': (int(time.time() - os.path.getmtime(cookie_path))
                        if present else None),
    }

    now = time.time()
    cache = _credential_health_cache
    cached = cache['data']
    if cached is not None and now - cache['ts'] < _CREDENTIAL_HEALTH_TTL:
        probe = dict(cached)
    elif not present:
        # 本地事实就足够定性，没必要为了"证明没有 Cookie"去打一次上游
        probe = {'result': 'cookie_missing', 'detail': f'没有找到 Cookie 文件 {cookie_path}'}
        cache['data'] = dict(probe)      # 先写数据
        cache['ts'] = now                # 再写时间戳
    else:
        session = pixiv_client.build_pixiv_session()
        try:
            probe = pixiv_client.probe_credentials(session)
        except Exception as e:           # 探针自己炸了不该 500：如实报 network
            probe = {'result': 'network', 'detail': f'{type(e).__name__}: {e}'}
        finally:
            session.close()
        cache['data'] = dict(probe)
        cache['ts'] = now

    return jsonify({
        'ok': probe['result'] == 'ok',
        'cookie': cookie,
        'probe': probe,
        'advice': _credential_advice(probe, cookie),
    })


def _credential_advice(probe: dict, cookie: dict) -> str:
    """把体检结论翻成一句可执行的建议；`ok` 时返回空串（没有可建议的）。"""
    result = probe.get('result')
    if result == 'ok':
        return ''
    if result == 'cookie_missing':
        return ('没有找到 Cookie 文件：在浏览器登录 Pixiv 后把 PHPSESSID 写入 cookies.txt'
                '（或在设置页填写），否则搜索**一定是**空的。')
    if result == 'auth':
        return 'Cookie 已失效（上游明确报认证失败）：请更新 PHPSESSID 后重试。'
    if result == 'rate_limited':
        return '上游返回 403：这是限流/风控，不是凭据失效 —— 等一会儿再试。'
    if result == 'network':
        return f'连不上 Pixiv（网络或代理问题），当前结果不代表"没有作品"。详情：{probe.get("detail", "")}'
    if result == 'error':
        return f'上游返回错误：{probe.get("detail", "")}'
    return ''


@bp.route('/api/backups')
def backups_status() -> Response:
    """数据库滚动快照的观测面：运行态 + 磁盘上的快照文件。

    `state` 里 `enabled` 是**派生值**（`interval>0`）：备份线程即使被禁用也会启动并
    空转，所以它自己的 `snapshot_alive`（见 `/api/prefetch/status`）只说明"线程活着"，
    回答不了"到底有没有在留快照" —— 那要看 `enabled` 与 `last_at`。

    `files` 只列 `pixiv.db.snapshot-*.bak`（本机制自己写的那些）：迁移前备份与用户
    手工放进 `instance/backups/` 的文件不在这里，也不会被保留策略清理。
    """
    from ..backups import backup_dir_for, snapshot_paths   # 局部导入：仅本接口需要
    db_path = config_module.DATABASE_PATH
    files = []
    for path in snapshot_paths(db_path):
        stat = path.stat()
        files.append({
            'name': path.name,
            'size': stat.st_size,
            'mtime': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(stat.st_mtime)),
        })
    return jsonify({
        'state': dict(_snapshot_state),
        'db_path': db_path,
        'backup_dir': str(backup_dir_for(db_path)),
        'files': files,
    })


@bp.route('/api/auto-follow/config', methods=['POST'])
@_csrf_required
def auto_follow_config() -> Response:
    body = _get_json_body()
    if 'interval' in body:
        try:
            _auto_follow_state['interval'] = max(0, int(body['interval']))
        except (ValueError, TypeError):
            return jsonify({'error': 'interval must be integer seconds'}), 400
    if 'auto_download' in body:
        val = body['auto_download']
        _auto_follow_state['auto_download'] = val if isinstance(val, bool) else str(val).lower() == 'true'
    return jsonify(_auto_follow_state)


# ── 屏蔽标签 ──

@bp.route('/api/blocked-tags', methods=['GET'])
def list_blocked_tags() -> Response:
    with get_session() as db:
        tags = db.query(BlockedTag).order_by(BlockedTag.created_at.desc()).all()
        return jsonify([t.tag for t in tags])


@bp.route('/api/blocked-tags', methods=['POST'])
@_csrf_required
def add_blocked_tag() -> Response:
    tag = _get_json_body().get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        if db.query(BlockedTag).filter(BlockedTag.tag == tag).first():
            return jsonify({'error': '标签已存在'}), 409
        db.add(BlockedTag(tag=tag))
        safe_commit(db)
        clear_search_cache()
        clear_home_cache()   # 首页也有 60 秒缓存：屏蔽集合一变必须立刻生效
        return jsonify({'status': 'added', 'tag': tag})


@bp.route('/api/blocked-tags/<path:tag>', methods=['DELETE'])
@_csrf_required
def remove_blocked_tag(tag: str) -> Response:
    with get_session() as db:
        entry = db.query(BlockedTag).filter(BlockedTag.tag == tag).first()
        if not entry:
            return jsonify({'error': '标签不存在'}), 404
        db.delete(entry)
        safe_commit(db)
        clear_search_cache()
        clear_home_cache()   # 同上：取消屏蔽后首页要立刻把作品放回来
        return jsonify({'status': 'deleted', 'tag': tag})


# ── 设置 ──

_SETTINGS_PATH = os.path.join(config_module._instance_dir, 'settings.json')

# 设置页可编辑键与默认值：由 config.SETTINGS_KEYS（唯一来源）派生，
# 排除密码类与 cookie_secure（这些只通过 settings.json/环境变量管理）。
_SETTINGS_DEFAULTS = {
    k: v for k, (_, v) in config_module.SETTINGS_KEYS.items()
    if not k.endswith('_password') and k != 'cookie_secure'
}


def _sanitize_settings(data: dict) -> dict:
    """把 settings.json 的原始值收敛到当前允许范围。

    为什么不在读取时直接信任文件：旧版本设置页允许保存 0/越界值，升级后
    `config.py` 会在启动时忽略它们并回退默认，但设置页若仍显示 0，用户点一次
    保存就会被新的边界校验 400 拒绝 —— 页面从此无法保存任何配置。这里按
    `SETTINGS_BOUNDS` 清洗：非法数值回退默认，合法数值保留，非数值键原样透传
    （密码/Cookie 等仍由调用方处理）。
    """
    result = dict(_SETTINGS_DEFAULTS)
    for key, value in data.items():
        bounds = config_module.SETTINGS_BOUNDS.get(key)
        if bounds is not None:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            low, high = bounds
            if number < low or (high is not None and number > high):
                continue
            result[key] = number
        elif key == 'auto_follow_download':
            result[key] = bool(value)
        else:
            result[key] = value
    return result


def _load_settings() -> dict:
    import app  # 延迟导入读取 app._SETTINGS_PATH：
    #             tests monkeypatch('app._SETTINGS_PATH')（test_prefetch_api.py
    #             _isolate_settings 夹具），本模块模块级绑定看不到补丁
    if os.path.exists(app._SETTINGS_PATH):
        try:
            with open(app._SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return _sanitize_settings(data)
        except Exception:
            logger.warning('设置文件读取失败，使用默认设置')
    return dict(_SETTINGS_DEFAULTS)


def _settings_locked() -> bool:
    """设置页门禁：当前凭据代次已全局登录则直通；否则按旧 SETTINGS_PASSWORD 流程。"""
    import app  # 延迟导入读取 app.SETTINGS_PASSWORD：tests monkeypatch('app.SETTINGS_PASSWORD')
    #             （test_auth.py TestSettingsCompat），from config import 绑定看不到补丁
    if _session_authed():
        return False
    return bool(app.SETTINGS_PASSWORD) and not session.get('settings_unlocked')


@bp.route('/settings')
def settings_page() -> str:
    if _settings_locked():
        return render_template('settings_unlock.html', csrf_token=_get_csrf_token())
    return render_template('settings.html', csrf_token=_get_csrf_token())


@bp.route('/api/settings/unlock', methods=['POST'])
@_csrf_required
@_rate_limit(max_attempts=5, window=60)
def settings_unlock() -> Response:
    import app  # 延迟导入读取 app.SETTINGS_PASSWORD：tests monkeypatch('app.SETTINGS_PASSWORD')
    #             （test_auth.py TestSettingsCompat），from config import 绑定看不到补丁
    if _session_authed() or not app.SETTINGS_PASSWORD:
        return jsonify({'ok': True})
    body = _get_json_body()
    if hmac.compare_digest(str(body.get('password', '')).encode(), app.SETTINGS_PASSWORD.encode()):
        session['settings_unlocked'] = True
        return jsonify({'ok': True})
    time.sleep(1)  # 失败延迟，减缓爆破（与 login_submit 对齐）
    return jsonify({'error': '密码错误'}), 403


@bp.route('/api/settings', methods=['GET'])
def api_settings_get() -> Response:
    if _settings_locked():
        return jsonify({'error': '需要密码访问'}), 403
    data = _load_settings()
    # 脱敏：密码类与 Cookie 字段不回传明文（纵深防御，前端 FIELD_MAP 不消费这些键）
    for k in list(data):
        if k.endswith('_password') or k == 'cookie':
            data[k] = ''
    return jsonify(data)


@bp.route('/api/settings', methods=['POST'])
@_csrf_required
def api_settings_post() -> Response:
    import app  # 延迟导入经 app 命名空间读写 _SETTINGS_PATH：本函数读侧经
    #             _load_settings()（app._SETTINGS_PATH）已走 app 命名空间，写侧若用
    #             模块级绑定，在 tests monkeypatch('app._SETTINGS_PATH') 下会读临时路径
    #             却写坏生产 instance/settings.json（test_prefetch_api._isolate_settings 夹具）
    if _settings_locked():
        return jsonify({'error': '需要密码访问'}), 403

    body = _get_json_body()
    current = _load_settings()

    # 先只校验 Cookie 值，不落盘：后面的数值校验失败时不能留下"Cookie 已改、
    # 设置没保存"的半成品状态（审计 P2-1）。
    cookie_val = body.pop('cookie', '').strip()
    clean_val = ''
    if cookie_val:
        # 剔除换行/控制字符，防止向 cookies.txt 注入多行破坏鉴权
        clean_val = re.sub(r'[\r\n\t\x00-\x1f\x7f]', '', cookie_val).strip()
        if not clean_val:
            return jsonify({'error': 'Cookie 内容无效'}), 400

    # 仅合并已知的配置键；全部校验通过后才产生任何磁盘副作用。
    for key in _SETTINGS_DEFAULTS:
        if key in body:
            val = body[key]
            if key in ('auto_follow_download',):
                val = bool(val)
            elif key in config_module.SETTINGS_BOUNDS:
                # 数值键必须落在 config.SETTINGS_BOUNDS 区间内：设置页的
                # `<input min/max>` 不会在 fetch 保存时生效，清空输入框还会把
                # 空串变成 0。0 个下载线程 / 0 个详情线程 / 0 上限预取缓存
                # 都会在保存后（或下次启动时）造成实际故障，见审计 P1-2。
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    continue
                low, high = config_module.SETTINGS_BOUNDS[key]
                if val < low or (high is not None and val > high):
                    limit = f'{low}~{high}' if high is not None else f'≥{low}'
                    return jsonify({'error': f'{key} 取值需满足 {limit}'}), 400
            current[key] = val

    # 校验全部通过，再按"先 Cookie 后设置"的历史顺序落盘：
    # Cookie 写失败时 settings.json 一个字节都不写（已有用例钉住）。
    if clean_val:
        cookie_path = app.COOKIE_PATH
        try:
            # 原子写：读侧在其它线程读同一路径（--threads 8），先截断再写会让它读到
            # 空串并把空值连同 mtime 一起缓存住（此后一直用空 Cookie，重启才恢复）。
            _atomic_write_text(cookie_path, f'PHPSESSID={clean_val}\n')
        except OSError as e:
            return jsonify({'error': f'cookies.txt 写入失败（{cookie_path}）: {e}'}), 500
        # Cookie 缓存归适配层所有（见 pixiv_client.set_cookie_cache）；mtime 必须取自
        # **刚写入的那个路径**，否则与连接池失效戳不是同一把尺子。
        pixiv_client.set_cookie_cache(clean_val, os.path.getmtime(cookie_path))
        logger.info('cookies.txt 已通过设置页更新: %s', cookie_path)

    try:
        # 原子写（审计 S16）：直接 open('w') 在写盘失败/进程被杀时会留下截断的
        # settings.json，而读取侧遇到损坏只能整体回退默认 —— 用户那份配置全丢。
        _atomic_write_json(app._SETTINGS_PATH, current)
        # prefetch_* 键与 /api/prefetch/config 保持同构：保存成功后同步内存态
        #（interval 即时生效，不再需要重启）。
        # 键名必须经 _PREFETCH_SETTINGS_KEYS 映射：内存态用短键（interval/pages/
        # max_illusts，background 的预取循环读的就是它），settings.json 用长键
        # （prefetch_*）。此前直接把长键塞进 _prefetch_state，等于写进三个没人读的
        # 键 —— 设置页保存"立即生效"实际从未生效（审计 S18 补测时发现）。
        _prefetch_state.update({
            short: current[long]
            for short, long in _PREFETCH_SETTINGS_KEYS.items()
            if long in current
        })
        return jsonify(current)
    except Exception as e:
        return jsonify({'error': f'保存失败: {e}'}), 500