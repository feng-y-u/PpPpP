# ── 搜索预取管理 API ──
# _PREFETCH_SETTINGS_KEYS + /api/prefetch/config、/api/prefetch/tags、
# /api/prefetch/status、/api/prefetch/refresh 路由。
# 设置读写经 app 命名空间延迟访问：tests(test_prefetch_api._isolate_settings 夹具)
# monkeypatch('app._SETTINGS_PATH')，从 routes_settings 的独立绑定取不到补丁。
from __future__ import annotations

import json
import threading

from flask import Blueprint, Response, jsonify, request

from ..background import (_collect_other_tag_pids, _find_alias_group,
                        _search_cache_guard, get_background_health)
from ..fetcher import get_detail_error_samples
from ..helpers import _atomic_write_json
from ..middleware import _csrf_required, _get_json_body
from ..models import Illust, SearchCache, get_session, iso_utc, safe_commit
from ..runtime import _prefetch_state

bp = Blueprint('prefetch', __name__)


_PREFETCH_SETTINGS_KEYS = {
    'interval': 'prefetch_interval',
    'pages': 'prefetch_pages',
    'max_illusts': 'prefetch_max_illusts',
}


@bp.route('/api/prefetch/config', methods=['GET'])
def prefetch_config_get() -> Response:
    return jsonify({
        'interval': _prefetch_state['interval'],
        'pages': _prefetch_state['pages'],
        'max_illusts': _prefetch_state['max_illusts'],
    })


@bp.route('/api/prefetch/config', methods=['POST'])
@_csrf_required
def prefetch_config_post() -> Response:
    import app  # 延迟导入经 app 命名空间读设置路径/加载函数：
    #             tests monkeypatch('app._SETTINGS_PATH')（test_prefetch_api.py
    #             _isolate_settings 夹具重定向 settings.json），routes_settings 的
    #             独立绑定看不到补丁
    body = _get_json_body()
    updates: dict[str, int] = {}
    for key in _PREFETCH_SETTINGS_KEYS:
        if key in body:
            try:
                updates[key] = max(0, int(body[key]))
            except (ValueError, TypeError):
                return jsonify({'error': f'{key} must be integer'}), 400

    # 写配置：先全部校验并持久化 settings.json，成功后一次性提交到内存，避免校验/写盘失败时状态漂移
    if updates:
        current = app._load_settings()
        for key, val in updates.items():
            current[_PREFETCH_SETTINGS_KEYS[key]] = val
        try:
            # 原子写（审计 S16）：直接 open('w') 会在写盘失败/进程被杀时留下截断的
            # settings.json，读取侧遇到损坏只能整体回退默认，用户那份配置全丢。
            _atomic_write_json(app._SETTINGS_PATH, current)
        except Exception as e:
            return jsonify({'error': f'保存失败: {e}'}), 500
        _prefetch_state.update(updates)

    return jsonify({
        'interval': _prefetch_state['interval'],
        'pages': _prefetch_state['pages'],
        'max_illusts': _prefetch_state['max_illusts'],
    })


@bp.route('/api/prefetch/tags', methods=['GET'])
def prefetch_tags_get() -> Response:
    with get_session() as db:
        rows = db.query(SearchCache).order_by(SearchCache.cached_at.desc()).all()
        return jsonify([{
            'tag': r.tag,
            'cached_at': iso_utc(r.cached_at),
            'status': r.status,
            'total': r.total,
            'error': r.error,
            # 组名之外的额外搜索词（同义词）：缓存页据此在 meta 里展示，
            # 设置页据此显示 +N 与 title
            'aliases': r.alias_tags_list,
        } for r in rows])


@bp.route('/api/prefetch/tags', methods=['POST'])
@_csrf_required
def prefetch_tags_post() -> Response:
    tag = _get_json_body().get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        if db.query(SearchCache).filter(SearchCache.tag == tag).first():
            return jsonify({'error': '标签已存在'}), 409
        # 已被别组当作同义词的标签不能再建独立行：否则同一批作品被两个条目
        # 各存一份，正是同义词合并要消除的重复
        owner = _find_alias_group(db, tag)
        if owner:
            return jsonify({'error': f'该标签已是「{owner}」的同义词'}), 409
        db.add(SearchCache(tag=tag))
        safe_commit(db)
        return jsonify({'tag': tag}), 201


def _load_cached_ids(row: SearchCache) -> list[int]:
    """安全解析行上的 illust_ids（损坏 JSON 当空列表，与查询侧口径一致）。"""
    try:
        ids = json.loads(row.illust_ids) if row.illust_ids else []
    except (json.JSONDecodeError, TypeError):
        ids = []
    return [pid for pid in ids if isinstance(pid, int)]


@bp.route('/api/prefetch/tags/<path:tag>/aliases', methods=['POST'])
@_csrf_required
def prefetch_alias_post(tag: str) -> Response:
    """给分组添加同义词；若该词已是独立标签行，就地合并（并集去重）后删行。

    合并存量行是"吉星排队 / 星引擎party"这类既有标签的关键路径：用户不必先删
    旧标签、再重新预取，已有缓存直接并进组里。
    """
    alias = str(_get_json_body().get('alias', '') or '').strip()
    with _search_cache_guard:
        with get_session() as db:
            group = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if group is None:
                return jsonify({'error': '标签不存在'}), 404
            if not alias:
                return jsonify({'error': '同义词不能为空'}), 400
            if alias == tag:
                return jsonify({'error': '同义词不能与标签名相同'}), 400
            if alias in group.alias_tags_list:
                return jsonify({'error': '该同义词已存在'}), 409

            existing = db.query(SearchCache).filter(SearchCache.tag == alias).first()
            if existing is not None:
                if existing.status == 'fetching':
                    # 在途预取的结果会在结束时空写/丢失，让用户稍后再合并
                    return jsonify({'error': '该标签正在预取中，请稍后再合并'}), 409
                merged = list(dict.fromkeys(
                    _load_cached_ids(group) + _load_cached_ids(existing)))
                group.illust_ids = json.dumps(merged, ensure_ascii=False)
                group.total = len(merged)
                absorbed = [a for a in existing.alias_tags_list if a and a != alias]
                group.alias_tags_list = list(dict.fromkeys(
                    group.alias_tags_list + [alias] + absorbed))
                if existing.cached_at and (
                        group.cached_at is None or existing.cached_at > group.cached_at):
                    group.cached_at = existing.cached_at
                db.delete(existing)
                safe_commit(db)
                return jsonify({'tag': tag, 'aliases': group.alias_tags_list,
                                'merged': True, 'total': group.total})

            owner = _find_alias_group(db, alias)
            if owner:
                return jsonify({'error': f'该标签已是「{owner}」的同义词'}), 409
            group.alias_tags_list = group.alias_tags_list + [alias]
            safe_commit(db)
            return jsonify({'tag': tag, 'aliases': group.alias_tags_list, 'merged': False})


@bp.route('/api/prefetch/tags/<path:tag>/aliases', methods=['DELETE'])
@_csrf_required
def prefetch_alias_delete(tag: str) -> Response:
    """摘掉一个同义词：只停止以后搜索，**已缓存作品留在组里**。

    没有逐词来源记录（illust_ids 是并集），精确拆分做不到；这些作品仍是该主题的
    有效结果，后续由容量清理按常规淘汰。
    """
    alias = str(_get_json_body().get('alias', '') or '').strip()
    with _search_cache_guard:
        with get_session() as db:
            group = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if group is None:
                return jsonify({'error': '标签不存在'}), 404
            aliases = group.alias_tags_list
            if alias not in aliases:
                return jsonify({'error': '同义词不存在'}), 404
            group.alias_tags_list = [a for a in aliases if a != alias]
            safe_commit(db)
            return jsonify({'tag': tag, 'aliases': group.alias_tags_list})


@bp.route('/api/prefetch/tags/<path:tag>', methods=['DELETE'])
@_csrf_required
def prefetch_tags_delete(tag: str) -> Response:
    with get_session() as db:
        row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not row:
            return jsonify({'error': '标签不存在'}), 404

        try:
            ids = json.loads(row.illust_ids) if row.illust_ids else []
        except (json.JSONDecodeError, TypeError):
            ids = []

        deletable: list[int] = []
        other_pids = _collect_other_tag_pids(db, tag)
        for pid in ids:
            if not isinstance(pid, int):
                continue
            # 仍被其他 SearchCache 引用时保留
            if pid in other_pids:
                continue
            illust = db.query(Illust).filter(Illust.pixiv_id == pid).first()
            if illust is None or not illust.prefetch_source:
                continue
            if illust.download_status in ('done', 'downloading') or illust.local_paths_list:
                continue
            deletable.append(pid)

        if deletable:
            db.query(Illust).filter(Illust.pixiv_id.in_(deletable)).delete(synchronize_session=False)
        db.delete(row)
        safe_commit(db)
        return jsonify({'tag': tag})


@bp.route('/api/prefetch/status', methods=['GET'])
def prefetch_status_get() -> Response:
    """预取状态 + 最终收藏数刷新的健康指标（供设置页与运维观察）。

    pending_refresh：尚未完成最终刷新的预取作品数（这批被容量清理豁免，
    长期积压说明刷新吞吐跟不上入库）；failed_backoff：带失败退避标记的数量。
    alive/stale/last_error（审计 S12）：线程存活、"很久没跑完一轮"与最近一轮的
    错误 —— 预取线程静默死掉时，光看 running/last_check 得靠人自己解读。
    """
    health = get_background_health()
    with get_session() as db:
        pending = db.query(Illust).filter(
            Illust.prefetch_source == 1,
            Illust.prefetch_refresh_at.is_(None),
        ).count()
        failed = db.query(Illust).filter(
            Illust.prefetch_source == 1,
            Illust.refresh_failed_at.isnot(None),
        ).count()
    return jsonify({
        'running': _prefetch_state['running'],
        'last_check': _prefetch_state['last_check'],
        'interval': _prefetch_state['interval'],
        'refresh': _prefetch_state.get('refresh_stats'),
        'pending_refresh': pending,
        'failed_backoff': failed,
        # 未命中删除关键词的详情报错样本（message → 次数）：据此核对/补充关键词清单
        'detail_errors': get_detail_error_samples(),
        'alive': health['prefetch_alive'],
        'auto_follow_alive': health['auto_follow_alive'],
        'stale': health['stale'],
        'last_error': health['last_error'],
    })


@bp.route('/api/prefetch/refresh-reset', methods=['POST'])
@_csrf_required
def prefetch_refresh_reset_post() -> Response:
    """把预取作品重新放回最终收藏数刷新队列（清空刷新完成与失败退避标记）。

    用途：Cookie 权限修复后救回被"14 天强制完成"或"永久失败退避"的作品；
    也可让某个标签的收藏数重新拉一遍。生效时机为下一轮预取。
    """
    import app  # 延迟导入：tests monkeypatch('app.reset_prefetch_refresh')
    body = _get_json_body()
    tag = str(body.get('tag', '') or '').strip()
    raw_pid = body.get('pixiv_id')
    if not tag and raw_pid is None:
        return jsonify({'error': '需要 tag 或 pixiv_id'}), 400
    pixiv_id = None
    if raw_pid is not None:
        try:
            pixiv_id = int(raw_pid)
        except (TypeError, ValueError):
            return jsonify({'error': 'pixiv_id 必须是整数'}), 400
    if tag:
        with get_session() as db:
            if db.query(SearchCache).filter(SearchCache.tag == tag).first() is None:
                return jsonify({'error': '标签不存在'}), 404
    count = app.reset_prefetch_refresh(tag=tag or None, pixiv_id=pixiv_id)
    return jsonify({'status': 'reset', 'count': count})


@bp.route('/api/prefetch/refresh', methods=['POST'])
@_csrf_required
def prefetch_refresh_post() -> Response:
    import app  # 延迟导入经 app 命名空间调用 _prefetch_one_tag：
    #             tests monkeypatch('app._prefetch_one_tag')（test_prefetch_api.py
    #             TestPrefetchRefreshAPI），from background import 绑定看不到补丁
    tag = _get_json_body().get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not row:
            return jsonify({'error': '标签不存在'}), 404
        if row.status == 'fetching':
            return jsonify({'error': '该标签正在刷新中'}), 409
    threading.Thread(target=app._prefetch_one_tag, args=(tag,), daemon=True).start()
    return jsonify({'tag': tag, 'status': 'refreshing'})