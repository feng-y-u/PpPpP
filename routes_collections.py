# ── 收藏夹（Collections）路由 ──
# /api/collections 全部路由（含 items / batch / move）。
# 注：/api/illust/<pid>/collections 已在 Task 4 随 routes_gallery 迁移，不在此处。
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import text

from helpers import _compute_move_position, _next_collection_position
from middleware import _csrf_required, _get_json_body
from models import Collection, CollectionItem, get_session, safe_commit

bp = Blueprint('collections', __name__)


# ── 收藏夹 ──


@bp.route('/api/collections', methods=['GET'])
def list_collections() -> Response:
    with get_session() as db:
        collections = db.query(Collection).order_by(Collection.created_at).all()
        # 一次 GROUP BY 取全部计数，别在循环里逐条 COUNT——那是 N+1，
        # 实测 20 个收藏夹 5.0ms → 0.1ms，收藏夹越多差距越大。
        counts = dict(db.execute(text(
            'SELECT collection_id, COUNT(*) FROM collection_items GROUP BY collection_id'
        )).all())
        return jsonify([
            {**c.to_dict(), 'item_count': counts.get(c.id, 0)}
            for c in collections
        ])


@bp.route('/api/collections', methods=['POST'])
@_csrf_required
def create_collection() -> Response:
    body = _get_json_body()
    name = body.get('name', '').strip()
    if not name or len(name) > 50:
        return jsonify({'error': '收藏夹名称不能为空且不超过50字'}), 400
    with get_session() as db:
        if db.query(Collection).filter(Collection.name == name).first():
            return jsonify({'error': '收藏夹名称已存在'}), 409
        c = Collection(name=name, description=body.get('description', ''))
        db.add(c)
        safe_commit(db)
        return jsonify(c.to_dict()), 201


@bp.route('/api/collections/<int:collection_id>', methods=['PUT'])
@_csrf_required
def update_collection(collection_id: int) -> Response:
    body = _get_json_body()
    name = body.get('name', '').strip()
    if not name or len(name) > 50:
        return jsonify({'error': '收藏夹名称不能为空且不超过50字'}), 400
    with get_session() as db:
        c = db.query(Collection).filter(Collection.id == collection_id).first()
        if not c:
            return jsonify({'error': '收藏夹不存在'}), 404
        if c.name != name and db.query(Collection).filter(Collection.name == name).first():
            return jsonify({'error': '收藏夹名称已存在'}), 409
        c.name = name
        if 'description' in body:
            c.description = body.get('description', '')
        safe_commit(db)
        return jsonify(c.to_dict())


@bp.route('/api/collections/<int:collection_id>', methods=['DELETE'])
@_csrf_required
def         delete_collection(collection_id: int) -> Response:
    with get_session() as db:
        c = db.query(Collection).filter(Collection.id == collection_id).first()
        if not c:
            return jsonify({'error': '收藏夹不存在'}), 404
        db.query(CollectionItem).filter(CollectionItem.collection_id == collection_id).delete()
        db.delete(c)
        safe_commit(db)
        return jsonify({'status': 'deleted'})


@bp.route('/api/collections/<int:collection_id>/items', methods=['GET'])
def list_collection_items(collection_id: int) -> Response:
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        total = db.query(CollectionItem).filter(CollectionItem.collection_id == collection_id).count()
        items = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id
        ).order_by(CollectionItem.position.asc()).offset(offset).limit(limit).all()
        return jsonify({
            'data': [item.to_dict() for item in items],
            'total': total,
            'has_more': offset + limit < total,
        })


@bp.route('/api/collections/<int:collection_id>/items', methods=['POST'])
@_csrf_required
def add_collection_item(collection_id: int) -> Response:
    body = _get_json_body()
    pixiv_id = body.get('pixiv_id')
    if not pixiv_id:
        return jsonify({'error': '请提供作品ID'}), 400
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        existing = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if existing:
            return jsonify({'error': '作品已在收藏夹中'}), 409
        item = CollectionItem(
            collection_id=collection_id, pixiv_id=pixiv_id,
            position=_next_collection_position(db, collection_id),
        )
        db.add(item)
        safe_commit(db)
        data = item.to_dict()
    return jsonify(data), 201


@bp.route('/api/collections/<int:collection_id>/items/<int:pixiv_id>', methods=['DELETE'])
@_csrf_required
def remove_collection_item(collection_id: int, pixiv_id: int) -> Response:
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        item = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if not item:
            return jsonify({'error': '作品不在收藏夹中'}), 404
        db.delete(item)
        safe_commit(db)
    return jsonify({'status': 'deleted'})


@bp.route('/api/collections/<int:collection_id>/items/batch', methods=['POST'])
@_csrf_required
def batch_add_collection_items(collection_id: int) -> Response:
    body = _get_json_body()
    pixiv_ids = body.get('pixiv_ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400
    pixiv_ids = [int(pid) for pid in pixiv_ids
                 if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        next_pos = _next_collection_position(db, collection_id)
        added = 0
        for pid in pixiv_ids:
            existing = db.query(CollectionItem).filter(
                CollectionItem.collection_id == collection_id,
                CollectionItem.pixiv_id == pid,
            ).first()
            if not existing:
                db.add(CollectionItem(collection_id=collection_id, pixiv_id=pid, position=next_pos))
                next_pos += 1000.0
                added += 1
        safe_commit(db)
    return jsonify({'added': added, 'total': len(pixiv_ids)})


@bp.route('/api/collections/<int:collection_id>/items/batch', methods=['DELETE'])
@_csrf_required
def batch_remove_collection_items(collection_id: int) -> Response:
    body = _get_json_body()
    pixiv_ids = body.get('pixiv_ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400
    pixiv_ids = [int(pid) for pid in pixiv_ids
                 if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        removed = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id.in_(pixiv_ids),
        ).delete(synchronize_session='fetch')
        safe_commit(db)
    return jsonify({'removed': removed})


@bp.route('/api/collections/<int:collection_id>/items/<int:pixiv_id>/move', methods=['POST'])
@_csrf_required
def move_collection_item(collection_id: int, pixiv_id: int) -> Response:
    body = _get_json_body()
    direction = body.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction 必须是 up 或 down'}), 400

    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        current = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if not current:
            return jsonify({'error': '作品不在收藏夹中'}), 404

        rows = db.execute(text(
            'SELECT id, position FROM collection_items WHERE collection_id = :cid ORDER BY position ASC, id ASC'
        ), {'cid': collection_id}).fetchall()
        items = [(r[0], r[1]) for r in rows]
        idx = next((i for i, it in enumerate(items) if it[0] == current.id), None)
        if idx is None:
            return jsonify({'error': '作品不在收藏夹中'}), 404

        new_pos, needs_rebalance, err = _compute_move_position(items, idx, direction)
        if err == 400:
            return jsonify({'error': '已在边界位置'}), 400

        rebalanced = False
        if needs_rebalance:
            rebalanced = True
            for i, it_tuple in enumerate(items):
                db.execute(text('UPDATE collection_items SET position=:p WHERE id=:id'),
                           {'p': (i + 1) * 1000.0, 'id': it_tuple[0]})
            safe_commit(db)
            rows = db.execute(text(
                'SELECT id, position FROM collection_items WHERE collection_id = :cid ORDER BY position ASC, id ASC'
            ), {'cid': collection_id}).fetchall()
            items = [(r[0], r[1]) for r in rows]
            idx = next((i for i, it in enumerate(items) if it[0] == current.id), None)
            new_pos, _, _ = _compute_move_position(items, idx, direction)

        # 用重查后的 items 里的 position 做乐观锁基准：rebalance 分支已重排
        # 并 commit，current 的 ORM 缓存仍是重排前的旧值（否则误报 409）。
        old_pos = items[idx][1]
        result = db.execute(text(
            'UPDATE collection_items SET position=:np '
            'WHERE collection_id=:cid AND pixiv_id=:pid AND position=:op'
        ), {'np': new_pos, 'cid': collection_id, 'pid': pixiv_id, 'op': old_pos})
        if result.rowcount == 0:
            db.rollback()
            return jsonify({'error': '位置已被修改，请刷新后重试'}), 409
        safe_commit(db)
        return jsonify({'position': new_pos, 'rebalanced': rebalanced})