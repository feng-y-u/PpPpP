# ── 图库 / 详情 / 图片 / 收藏 路由 ──
# /thumb、/api/image、/detail、/gallery、/api/gallery*、/api/favorite、
# /api/open-dir、/api/illust/<pid>/collections 路由。
from __future__ import annotations

import hashlib
import logging
import os
import platform
import threading
import time
from base64 import urlsafe_b64decode

import requests
from flask import Blueprint, Response, abort, jsonify, render_template, request, send_file
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import fetcher
from fetcher import PixivAuthError, build_pixiv_session
from helpers import (_build_orphan_dicts, _delete_illust_files, _extract_ext,
                     _fetch_original_urls, _fmt_num, _get_download_dir,
                     _next_collection_position, _original_to_resized,
                     _page_sort_key, _proxy_thumb, _scan_local_downloads)
from middleware import _csrf_required, _get_csrf_token, _get_json_body
from models import (BlockedTag, Collection, CollectionItem, DownloadLog,
                    Illust, get_favorite_pids, get_session, safe_commit)
from runtime import (_DB_PIDS_CACHE_TTL, _THUMB_FAIL_COOLDOWN, _db_pids_cache,
                     _thumb_failed, _thumb_sem)

logger = logging.getLogger(__name__)

bp = Blueprint('gallery', __name__)

# 缩略图代理磁盘缓存目录（app.py 中同名定义路径一致：instance/image_cache）
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'image_cache')


# ── 图片服务 / 详情页 ──

@bp.route('/thumb/<path:url_b64>')
def thumb_proxy(url_b64: str) -> Response:
    """代理 Pixiv 图片，绕过 Referer 检查。带磁盘缓存。"""
    try:
        padding = 4 - len(url_b64) % 4
        if padding != 4:
            url_b64 += '=' * padding
        url = urlsafe_b64decode(url_b64.encode()).decode()
    except Exception:
        return abort(400)

    if not url.startswith('https://i.pximg.net/'):
        return abort(403)

    cache_key = hashlib.md5(url.encode()).hexdigest()
    ext = _extract_ext(url)
    cache_path = os.path.join(CACHE_DIR, f'{cache_key}.{ext}')
    meta_path = cache_path + '.meta'
    if os.path.isfile(cache_path):
        mimetype = 'image/jpeg'
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                mimetype = f.read().strip()
        return send_file(cache_path, mimetype=mimetype, max_age=86400 * 7)

    # 实时拉取分流：限制并发数 + 失败 URL 冷却，防止刷新/返回时把 Pixiv 图床打爆
    #（大量未命中缓存的缩略图同时实时拉 → 超时/限流 → 整批 502 → 前端"全消失"）
    now = time.time()
    if now - _thumb_failed.get(url, 0.0) < _THUMB_FAIL_COOLDOWN:
        return abort(502)  # 冷却期内直接失败，不重复发起网络请求

    try:
        with _thumb_sem:
            session = build_pixiv_session()
            try:
                resp = session.get(url, timeout=(10, 30))
                resp.raise_for_status()
            finally:
                session.close()
    except requests.RequestException:
        _thumb_failed[url] = now
        # 顺手清理过期失败记录，防止集合无限增长
        for failed_url in [k for k, v in _thumb_failed.items() if now - v >= _THUMB_FAIL_COOLDOWN]:
            _thumb_failed.pop(failed_url, None)
        return abort(502)

    _thumb_failed.pop(url, None)

    mimetype = resp.headers.get('Content-Type', 'image/jpeg')
    try:
        # 原子写：先写唯一临时文件再 rename，避免并发/半程中断留下损坏缓存
        tmp_path = f'{cache_path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        os.replace(tmp_path, cache_path)
        with open(meta_path, 'w') as f:
            f.write(mimetype)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return Response(resp.iter_content(chunk_size=8192), mimetype=mimetype)

    return send_file(cache_path, mimetype=mimetype, max_age=86400 * 7)


@bp.route('/api/image/<int:pixiv_id>/<int:index>')
def serve_image(pixiv_id: int, index: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if illust and illust.download_status == 'done' and illust.local_paths_list:
            paths = illust.local_paths_list
            if 0 <= index < len(paths) and os.path.isfile(paths[index]):
                return send_file(paths[index])

    # 不在 DB（或状态不对）→ 直接从 downloads 目录读
    ddir = _get_download_dir(pixiv_id)
    if not os.path.isdir(ddir):
        abort(404)
    files = sorted(
        (os.path.join(ddir, f) for f in os.listdir(ddir)
         if os.path.isfile(os.path.join(ddir, f))),
        key=_page_sort_key,
    )
    if 0 <= index < len(files) and os.path.isfile(files[index]):
        return send_file(files[index])
    abort(404)


@bp.route('/detail/<int:pixiv_id>')
def detail_page(pixiv_id: int) -> str:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            abort(404)

        data = illust.to_dict(favorite=(pixiv_id in get_favorite_pids(db)))
        paths = illust.local_paths_list or []
        local_urls = [f'/api/image/{pixiv_id}/{n}' for n in range(len(paths))]

        file_size = illust.file_size or None

        # 相关作品：同一画师，排除自身
        related = db.query(Illust).filter(
            Illust.user_id == illust.user_id,
            Illust.pixiv_id != pixiv_id,
            Illust.download_status == 'done',
        ).order_by(Illust.created_at.desc()).limit(6).all()
        related = [r.to_dict() for r in related]

        need_fetch_urls = not illust.original_urls_list

    # 网络请求放到 DB session 之外（避免事务随网络往返长时间占用连接）
    if need_fetch_urls:
        try:
            urls = _fetch_original_urls(pixiv_id)
        except fetcher.PixivAuthError as e:
            # 认证失效：不 500，按"未拉到原图"降级（仍可看缩略图等）
            logger.warning(f'详情原图拉取认证失效 {pixiv_id}: {e}')
            urls = []
        except Exception as e:
            logger.warning(f'详情原图拉取失败 {pixiv_id}: {e}')
            urls = []
        if urls:
            with get_session() as db:
                row = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
                if row:
                    row.original_urls_list = urls
                    safe_commit(db)
    else:
        urls = illust.original_urls_list or []

    medium_urls = []
    original_proxied = []
    for url in urls:
        medium_urls.append(_proxy_thumb(_original_to_resized(url)))
        original_proxied.append(_proxy_thumb(url))

    return render_template(
        'detail.html',
        illust=data,
        local_urls=local_urls,
        medium_urls=medium_urls,
        original_proxied=original_proxied,
        file_size=file_size,
        related=related,
        proxy_thumb=_proxy_thumb,
        fmt_num=_fmt_num,
        csrf_token=_get_csrf_token(),
    )


@bp.route('/api/detail/<int:pixiv_id>')
def detail_api(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        d = illust.to_dict()
        paths = illust.local_paths_list or []
        d['local_urls'] = [f'/api/image/{pixiv_id}/{n}' for n in range(len(paths))]
        d['medium_urls'] = [_proxy_thumb(_original_to_resized(u)) for u in (illust.original_urls_list or [])]
        d['file_count'] = len(paths)
        return jsonify(d)


@bp.route('/gallery')
def gallery() -> str:
    return render_template('gallery.html', csrf_token=_get_csrf_token())


# ── 图库查询与删除 ──

@bp.route('/api/gallery')
def api_gallery() -> Response:
    tag_filter = request.args.get('tag', '').strip()
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    favorites_only = request.args.get('favorites', '').lower() == 'true'
    collection_id = request.args.get('collection_id', type=int)
    sort = request.args.get('sort', 'created')
    if sort not in ('created', 'downloaded'):
        sort = 'created'
    limit = max(1, min(200, limit))
    offset = max(0, offset)

    is_collection_view = collection_id is not None

    # 扫描本地 downloads 目录
    local_items = _scan_local_downloads()
    local_pids = sorted(local_items.keys(), reverse=True)

    with get_session() as db:
        blocked = {t.tag for t in db.query(BlockedTag).all()}

        default_cid = None
        default_fav_set: set[int] = set()
        if not collection_id:
            dc = db.query(Collection).filter(Collection.name == '我的收藏').first()
            if dc:
                default_cid = dc.id
                pids = db.query(CollectionItem.pixiv_id).filter(
                    CollectionItem.collection_id == dc.id
                ).all()
                default_fav_set = {p[0] for p in pids}

        if local_pids:
            # 本地 pid 可能数千：分片拼 IN，避免超过 SQLite 绑定变量上限
            #（旧版本默认 999，现代版本 32766；分片后对两者都安全）。
            or_clauses = ["illusts.download_status = 'done'"]
            params = {}
            for ci, chunk in enumerate(local_pids[i:i + 500] for i in range(0, len(local_pids), 500)):
                phs = ','.join(f':local_pid_{ci}_{j}' for j in range(len(chunk)))
                or_clauses.append(f'illusts.pixiv_id IN ({phs})')
                for j, pid in enumerate(chunk):
                    params[f'local_pid_{ci}_{j}'] = pid
            wheres = ['(' + ' OR '.join(or_clauses) + ')']
        else:
            wheres = ["illusts.download_status = 'done'"]
            params = {}
        if blocked:
            blk_list = list(blocked)
            phs = ','.join(f':blk_{i}' for i in range(len(blk_list)))
            wheres.append(f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) AS je WHERE je.value IN ({phs}))')
            for i, t in enumerate(blk_list):
                params[f'blk_{i}'] = t
        if tag_filter:
            wheres.append('EXISTS (SELECT 1 FROM json_each(illusts.tags) AS je WHERE je.value = :tag_filter)')
            params['tag_filter'] = tag_filter
        if favorites_only:
            if default_cid is not None:
                wheres.append('illusts.pixiv_id IN (SELECT pixiv_id FROM collection_items WHERE collection_id = :default_cid)')
                params['default_cid'] = default_cid
            else:
                wheres.append('0 = 1')

        where_clause = ' AND '.join(wheres)

        total: int = 0
        fav_total: int = 0
        pk_ids: list[int] = []

        # 查询执行闭包：COUNT + 本页 pk_ids。
        # json_each(illusts.tags) 遇到单条非法 JSON 会抛 OperationalError，
        # 外层捕获后降级去掉标签相关过滤重试（数据损坏兜底，不让整页 500）。
        def _run_gallery_queries(wc: str, p: dict) -> None:
            nonlocal total, fav_total, pk_ids
            page_params = {**p, 'lim': limit, 'off': offset}
            if is_collection_view:
                p['collection_id'] = collection_id
                page_params['collection_id'] = collection_id
                row = db.execute(
                    text(f'SELECT COUNT(*) FROM illusts '
                         f'JOIN collection_items ON collection_items.pixiv_id = illusts.pixiv_id '
                         f'WHERE collection_items.collection_id = :collection_id AND {wc}'),
                    p
                ).one()
                total = row[0] or 0
                fav_total = 0
                pk_ids = db.execute(
                    text(f'SELECT illusts.id FROM illusts '
                         f'JOIN collection_items ON collection_items.pixiv_id = illusts.pixiv_id '
                         f'WHERE collection_items.collection_id = :collection_id AND {wc} '
                         f'ORDER BY collection_items.position ASC '
                         f'LIMIT :lim OFFSET :off'),
                    page_params
                ).scalars().all()
            else:
                row = db.execute(
                    text(f'SELECT COUNT(*) AS total FROM illusts WHERE {wc}'),
                    p
                ).one()
                total = row[0] or 0
                fav_total = 0
                order_col = 'downloaded_at DESC' if sort == 'downloaded' else 'created_at DESC'
                pk_ids = db.execute(
                    text(f'SELECT id FROM illusts WHERE {wc} ORDER BY {order_col} LIMIT :lim OFFSET :off'),
                    page_params
                ).scalars().all()

        try:
            _run_gallery_queries(where_clause, params)
        except OperationalError:
            logger.warning('图库查询因 tags 数据异常失败，降级跳过标签过滤重试')
            wc = ' AND '.join(w for w in wheres if 'json_each' not in w)
            _run_gallery_queries(wc, {k: v for k, v in params.items()
                                      if not k.startswith('blk_') and k != 'tag_filter'})

        # 获取完整 ORM 对象并保持排序
        illusts = db.query(Illust).filter(Illust.id.in_(pk_ids)).all()
        id_order = {id_: i for i, id_ in enumerate(pk_ids)}
        illusts.sort(key=lambda x: id_order.get(x.id, 0))

        results = []
        fill_ids: list[int] = []
        for i in illusts:
            paths = local_items.get(i.pixiv_id) or i.local_paths_list or []
            if not i.file_size and paths:
                total_size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
                if total_size:
                    i.file_size = total_size
            d = i.to_dict(favorite=(i.pixiv_id in default_fav_set))
            d['file_count'] = len(paths)
            d['local_urls'] = [f'/api/image/{i.pixiv_id}/{n}' for n in range(len(paths))]
            results.append(d)
            if i.bookmark_count == 0 and not i.original_urls_list:
                fill_ids.append(i.pixiv_id)

        if fill_ids:
            fetcher._kick_background_fill(fill_ids)

        # 补充真正不在 DB 的本地文件（孤儿：磁盘有文件但 DB 无记录）。
        # 注意：必须排除【全部】DB 记录而非仅当前页（seen_pids）——否则其他页
        # 或未通过过滤条件的 DB 作品会被误判为孤儿，生成"只有作品号"的简陋卡片，
        # 与正常卡片重复展示（同一作品两张卡片），且 total 被重复计算。
        # 全表 pid 集合带 TTL 缓存：illusts 表总量通常远大于已下载数，避免每请求全表加载。
        if not collection_id and not favorites_only:
            now = time.time()
            if now - _db_pids_cache['ts'] >= _DB_PIDS_CACHE_TTL:
                with get_session() as cache_db:
                    _db_pids_cache['ts'] = now
                    _db_pids_cache['data'] = {
                        r[0] for r in cache_db.query(Illust.pixiv_id).all()
                    }
            orphan_pids = sorted(set(local_pids) - _db_pids_cache['data'], reverse=True)
            orphan_results = _build_orphan_dicts(orphan_pids, local_items)
            total += len(orphan_results)
            results.extend(orphan_results[:max(0, limit - len(results))])

        fav_total = total if favorites_only else sum(1 for r in results if r.get('pixiv_id') in default_fav_set)

        safe_commit(db)

        return jsonify({
            'data': results,
            'total': total,
            'favorite_total': fav_total,
            'has_more': offset + limit < total,
        })


@bp.route('/api/gallery/tags')
def api_gallery_tags() -> Response:
    with get_session() as db:
        rows = db.execute(text("""
            SELECT DISTINCT j.value AS tag
            FROM illusts, json_each(illusts.tags) AS j
            WHERE illusts.download_status = 'done'
            ORDER BY tag
            LIMIT 1000
        """)).all()
        return jsonify([row[0] for row in rows])


@bp.route('/api/gallery/<int:pixiv_id>', methods=['DELETE'])
@_csrf_required
def delete_gallery(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404

        deleted = _delete_illust_files(illust)
        db.add(DownloadLog(pixiv_id=pixiv_id, action='deleted', message=f'已删除 {deleted} 个文件'))
        safe_commit(db)
        return jsonify({'status': 'deleted', 'message': f'已删除 {deleted} 个文件'})


@bp.route('/api/gallery/batch-delete', methods=['POST'])
@_csrf_required
def batch_delete_gallery() -> Response:
    body = _get_json_body()
    ids = body.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400

    with get_session() as db:
        pixiv_ids = [int(pid) for pid in ids if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
        illusts = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
        deleted_count = 0
        total_files = 0
        for illust in illusts:
            n = _delete_illust_files(illust)
            total_files += n
            db.add(DownloadLog(pixiv_id=illust.pixiv_id, action='deleted', message=f'已删除 {n} 个文件'))
            deleted_count += 1
        safe_commit(db)

    failed = len(pixiv_ids) - deleted_count
    return jsonify({
        'status': 'done',
        'deleted': deleted_count,
        'failed': failed,
        'total_files': total_files,
        'message': f'已删除 {deleted_count} 个作品 ({total_files} 个文件)' + (f', {failed} 个失败' if failed else ''),
    })


# ── 收藏与打开目录 ──

@bp.route('/api/illust/<int:pixiv_id>/collections')
def illust_collections(pixiv_id: int) -> Response:
    with get_session() as db:
        items = db.query(CollectionItem).filter(CollectionItem.pixiv_id == pixiv_id).all()
        return jsonify([item.collection_id for item in items])


@bp.route('/api/open-dir', methods=['POST'])
@_csrf_required
def api_open_dir() -> Response:
    """打开本地文件夹（仅限本机浏览器访问时有效）。"""
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': '该功能仅本机可用'}), 403
    body = _get_json_body()
    path = body.get('path', '')
    if not path or not os.path.isdir(path):
        return jsonify({'error': '目录不存在'}), 404
    try:
        if platform.system() == 'Windows':
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(['xdg-open', path])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/favorite/<int:pixiv_id>', methods=['GET'])
def api_favorite_get(pixiv_id: int) -> Response:
    with get_session() as db:
        default = db.query(Collection).filter(Collection.name == '我的收藏').first()
        if not default:
            return jsonify({'is_favorite': False})
        exists = db.query(CollectionItem).filter(
            CollectionItem.collection_id == default.id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first() is not None
        return jsonify({'is_favorite': exists})


@bp.route('/api/favorite/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def api_favorite_post(pixiv_id: int) -> Response:
    """切换'我的收藏'收藏夹中的归属。"""
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        default = db.query(Collection).filter(Collection.name == '我的收藏').first()
        if not default:
            return jsonify({'error': '默认收藏夹不存在'}), 500
        existing = db.query(CollectionItem).filter(
            CollectionItem.collection_id == default.id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if existing:
            db.delete(existing)
            safe_commit(db)
            return jsonify({'is_favorite': False})
        else:
            db.add(CollectionItem(
                collection_id=default.id, pixiv_id=pixiv_id,
                position=_next_collection_position(db, default.id),
            ))
            safe_commit(db)
            return jsonify({'is_favorite': True})