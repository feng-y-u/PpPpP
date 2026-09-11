# ── 下载触发 / 状态 / 取消 / 批量 / 下载管理 ──
# /download、/api/download/batch、/download/cancel、/download/reset、
# /download_status、/api/download/status/batch、/download_file、
# /downloads（页面）、/api/downloads 路由 + 私有函数 _cancel_download_internal。
from __future__ import annotations

import logging
import os
import re
import tempfile
import zipfile
from io import BytesIO

from flask import Blueprint, Response, jsonify, render_template, request, send_file
from sqlalchemy import update

from background import _download_illust
from config import ZIP_MEMORY_THRESHOLD_BYTES
from helpers import _fetch_original_urls, _get_download_dir
from middleware import _csrf_required, _get_csrf_token, _get_json_body
from models import DownloadLog, Illust, get_session, safe_commit
from runtime import (_download_progress, _queued_downloads,
                     _download_queue_lock, is_queued_download,
                     queued_download_snapshot,
                     download_cancellations, download_executor)

logger = logging.getLogger(__name__)

bp = Blueprint('download', __name__)


@bp.route('/download/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def trigger_download(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404

        if illust.download_status == 'done':
            return jsonify({'status': 'done', 'message': '已下载'})

        if illust.download_status == 'downloading':
            if pixiv_id in _download_progress or is_queued_download(pixiv_id):
                return jsonify({'status': 'downloading', 'message': '下载中'})
            # 幽灵 downloading：worker 已不在（终态写入提交失败、进程异常退出，
            # 或上次运行遗留）。运行期没有别的自愈路径 —— 这里直接返回"下载中"
            # 会让 UI 永远挂着一张下不动的卡片。用条件更新只清仍是 downloading
            # 的行：并发新任务抢先把状态改回 downloading 时 rowcount=0，照常按
            # "下载中"返回，不覆盖它的状态。
            cleared = db.execute(
                update(Illust)
                .where(Illust.pixiv_id == pixiv_id,
                       Illust.download_status == 'downloading')
                .values(download_status=None)
            ).rowcount
            if not cleared:
                return jsonify({'status': 'downloading', 'message': '下载中'})
            db.add(DownloadLog(pixiv_id=pixiv_id, action='failed',
                               message='检测到残留 downloading 状态，已自动复位'))
            safe_commit(db)
            # 复位后继续走下面的正常下载流程

        # 已在队列中（worker 还没轮到，状态还没写成 downloading）→ 不再重复提交。
        # 这里只是"快速路径"，省掉下面一次原图地址网络请求；权威判定在下面加锁那段。
        if is_queued_download(pixiv_id):
            return jsonify({'status': 'queued', 'message': '已在下载队列中'})

        if not illust.original_urls_list:
            urls = _fetch_original_urls(pixiv_id)
            if not urls:
                return jsonify({'error': '无法获取原图链接'}), 400
            illust.original_urls_list = urls
            safe_commit(db)

    # 判定与入队必须在同一把锁内：拆成"先判定、后入队"时，两个并发请求会各自
    # 判定"不在队列"然后各自入队 —— 同一作品排两个任务，第二个会在第一个跑完后
    # 重下整份原图，失败时还会删掉第一个已成功下载的文件（审计 S9）。
    with _download_queue_lock:
        already_queued = pixiv_id in _queued_downloads
        if not already_queued:
            _queued_downloads.add(pixiv_id)
    if already_queued:
        return jsonify({'status': 'queued', 'message': '已在下载队列中'})
    try:
        download_executor.submit(_download_illust, pixiv_id)
    except RuntimeError:
        # 线程池已关闭（进程退出中）：必须把 pid 撤出队列。否则该作品此后一直
        # 返回"已在下载队列中"，却永远不会被下载 —— 幻影排队，比重复下载更难查。
        with _download_queue_lock:
            _queued_downloads.discard(pixiv_id)
        raise
    return jsonify({'status': 'accepted', 'message': '已加入下载队列'})


@bp.route('/api/download/batch', methods=['POST'])
@_csrf_required
def batch_download() -> Response:
    body = _get_json_body()
    pixiv_ids = body.get('ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400

    accepted, skipped = 0, 0
    with get_session() as db:
        ids = [int(pid) for pid in pixiv_ids if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
        existing_list = db.query(Illust).filter(Illust.pixiv_id.in_(ids)).all()
        existing_map = {i.pixiv_id: i for i in existing_list}

        for pid in ids:
            illust = existing_map.get(pid)
            if not illust or not illust.original_urls_list:
                skipped += 1
                continue
            if illust.download_status in ('done', 'downloading'):
                skipped += 1
                continue
            # 已在队列中的也算 skipped：同一 pid 排两个任务会在第一个跑完后重下
            # 一遍（失败时删掉成功文件）。判定与入队同锁原子完成，理由同 trigger_download。
            with _download_queue_lock:
                if pid in _queued_downloads:
                    skipped += 1
                    continue
                _queued_downloads.add(pid)
            try:
                download_executor.submit(_download_illust, pid)
            except RuntimeError:
                with _download_queue_lock:
                    _queued_downloads.discard(pid)
                raise
            accepted += 1

    return jsonify({'accepted': accepted, 'skipped': skipped, 'message': f'已加入 {accepted} 个下载任务'})


def _cancel_download_internal(pixiv_id: int, reset: bool = False) -> Response:
    """标记下载为取消状态，可选清理已下载的部分文件。"""
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        is_queued = is_queued_download(pixiv_id)
        if illust.download_status != 'downloading' and not is_queued:
            return jsonify({'error': '该作品未在下载中'}), 400

        if reset:
            # 先原子地"抢下"这次重置：只有仍是 downloading 的行才归本次处置。
            # 守卫读取与这里之间 worker 可能刚好把状态固化为 done —— 旧实现无条件
            # 删文件 + 置空会造成两种不一致：删掉刚下载成功的文件，或（反向）worker
            # 随后把 done 写回已删文件的幻影状态。
            claimed = db.execute(
                update(Illust)
                .where(Illust.pixiv_id == pixiv_id,
                       Illust.download_status == 'downloading')
                .values(download_status=None)
            ).rowcount
            if not claimed and not is_queued:
                # worker 已提交终态：不删文件、不加取消标记（标记加了没人清，会静默
                # 吞掉用户的下一次下载触发），让前端刷新看到真实状态。
                return jsonify({'error': '下载已结束，未执行重置', 'status': 'finished'}), 409

            with _download_queue_lock:
                _queued_downloads.discard(pixiv_id)
            download_cancellations.add(pixiv_id)

            work_dir = _get_download_dir(pixiv_id)
            if os.path.isdir(work_dir):
                for f in os.listdir(work_dir):
                    try:
                        os.remove(os.path.join(work_dir, f))
                    except OSError:
                        pass
                try:
                    os.rmdir(work_dir)
                except OSError:
                    pass
            illust.download_status = None
            db.add(DownloadLog(pixiv_id=pixiv_id, action='failed', message='下载已手动重置'))
            safe_commit(db)
            # 不在此处清除取消标记：若 worker 仍在下载，须让它感知取消并自行
            # 清理（_download_illust 的 finally 会 discard）；若任务仅 queued 未
            # 启动，worker 启动时的取消检查也会走 finally 清理。
            return jsonify({'status': 'reset', 'message': '已重置'}), 200

        # 普通取消：尽力而为（worker 在下一个检查点感知），不动文件与状态
        with _download_queue_lock:
            _queued_downloads.discard(pixiv_id)
        download_cancellations.add(pixiv_id)
        return jsonify({'status': 'cancelling', 'message': '正在取消...'}), 200


@bp.route('/download/cancel/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def cancel_download(pixiv_id: int) -> Response:
    return _cancel_download_internal(pixiv_id, reset=False)


@bp.route('/download/reset/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def reset_download(pixiv_id: int) -> Response:
    return _cancel_download_internal(pixiv_id, reset=True)


@bp.route('/download_status/<int:pixiv_id>')
def download_status(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        return jsonify({
            'status': illust.download_status or 'none',
            'local_paths': illust.local_paths_list,
        })


@bp.route('/api/download/status/batch')
def download_status_batch() -> Response:
    ids_str = request.args.get('ids', '')
    if not ids_str:
        return jsonify({'error': '请提供作品ID'}), 400
    pixiv_ids = [int(pid) for pid in ids_str.split(',') if pid.strip().isdigit()]
    if not pixiv_ids:
        return jsonify({'error': '无效的作品ID'}), 400
    with get_session() as db:
        illusts = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
        statuses = {i.pixiv_id: i.download_status or 'none' for i in illusts}
        for pid in pixiv_ids:
            statuses.setdefault(pid, 'none')
        return jsonify({'statuses': statuses})


def _write_zip_entries(zf: zipfile.ZipFile, paths: list[str], safe_title: str) -> int:
    """把 paths 逐个写进已打开的 zip，返回**实际写入**的条目数。

    每个文件单独 `except OSError: continue`（审计 S15）：`valid_paths` 是校验时刻的
    快照，此后文件仍可能被删除或替换（清理脚本、用户删目录、重新下载），打包到一半
    抛 OSError 会让整个下载 500 —— 少一张图远好过整个包失败。
    """
    written = 0
    for i, p in enumerate(paths):
        try:
            zf.write(p, f'{safe_title}_p{i}{os.path.splitext(p)[1]}')
        except OSError as e:
            logger.warning(f'打包跳过不可读文件 {p}: {e}')
            continue
        written += 1
    return written


def _total_bytes(paths: list[str]) -> int:
    """可用文件总大小（跳过打包瞬间已消失的文件，与 `_write_zip_entries` 同口径）。"""
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            continue
    return total


def _remove_temp_zip(path: str) -> None:
    """删除打包用的临时文件；失败只记日志（响应已经发完，不能再报错）。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass  # 已经删过：读完/close 两条清理路径都可能触发，必须幂等
    except OSError as e:
        logger.warning(f'临时 zip 清理失败（可手工删除）: {path}: {e}')


def _close_body_chain(body) -> None:
    """关闭 `send_file()` 产出的 body 链上第一个可关闭对象（由此关掉底层文件句柄）。

    `FileWrapper.close()` 会连它持有的文件一起关；但 Range 请求的 body 是
    `_RangeWrapper`，它自己没有 close（靠 `__getattr__` 委托或包着 `.iterable`）——
    不往下走一层，底层文件句柄就一直开着，Windows 上 `os.remove` 会因文件被占用而
    失败（WinError 32），临时文件照样漏。链很短：`_RangeWrapper` → `FileWrapper` → file。
    """
    obj = body
    for _ in range(4):
        if obj is None:
            return
        close = getattr(obj, 'close', None)
        if close is not None:
            close()
            return
        obj = getattr(obj, 'iterable', None) or getattr(obj, 'file', None)


class _DeletingBody:
    """转发 `send_file()` 的 body，并在读完/关闭时删除临时 zip（审计 S15）。

    为什么清理挂在 body 上，而不是 `after_this_request` / `Response.call_on_close`
    （实测确认，不是推测）：
      - `send_file()` 产出的响应是 `direct_passthrough`，Werkzeug 的 `get_app_iter()`
        在该模式下**直接返回 body（文件包装器）本身**，服务器全程不会调用
        `Response.close()` —— 挂在响应对象上的 close 回调永远不会执行，大包每下一份
        就漏一份几百 MB 的临时文件。
      - `after_this_request` 执行得更早：响应体还没发，文件句柄还开着，Windows 上
        `os.remove` 必然因占用失败。
    会被服务器 `close()` 的就是这个 body，所以清理只能挂它。两条路径都覆盖：正常读完
    （`__next__` 撞 StopIteration 时主动清理）与 `close()`（客户端断开 / HEAD / 416 空
    body）。转发用迭代而不是 `read()`：`FileWrapper` 与 `_RangeWrapper` 都只保证可迭代。
    """

    def __init__(self, body, tmp_path: str):
        self._body = body
        self._iter = iter(body)
        self._tmp_path = tmp_path

    def __iter__(self) -> '_DeletingBody':
        return self

    def __next__(self) -> bytes:
        try:
            return next(self._iter)
        except StopIteration:
            self.close()
            raise

    def close(self) -> None:
        try:
            _close_body_chain(self._body)
        finally:
            _remove_temp_zip(self._tmp_path)


@bp.route('/download_file/<int:pixiv_id>')
def download_file(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust or illust.download_status != 'done' or not illust.local_paths_list:
            return jsonify({'error': '文件未下载'}), 404

        paths = illust.local_paths_list
        # 验证文件存在
        valid_paths = [p for p in paths if os.path.isfile(p)]
        if not valid_paths:
            return jsonify({'error': '文件已丢失，请重新下载'}), 404

        title = illust.title or str(pixiv_id)
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]

        # 单文件直接返回
        if len(valid_paths) == 1:
            return send_file(
                valid_paths[0],
                as_attachment=True,
                download_name=f'{safe_title}{os.path.splitext(valid_paths[0])[1]}',
            )

        # 多文件打包 zip（ZIP_STORED 不压缩）。两种落地方式（审计 S15）：
        #   小包 → 内存缓冲：一次系统调用就能发完，且不留临时文件；
        #   大包 → 临时文件：整包拼在内存里会同时持有 zip 与逐张读入的字节，
        #          在 `-w 1` 单进程下这份峰值会跟正在跑的下载/缩略图抢内存。
        if _total_bytes(valid_paths) <= ZIP_MEMORY_THRESHOLD_BYTES:
            buf = BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
                written = _write_zip_entries(zf, valid_paths, safe_title)
            if not written:
                return jsonify({'error': '文件已丢失，请重新下载'}), 404
            buf.seek(0)
            return send_file(
                buf,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f'{safe_title}.zip',
            )

        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix='.zip', prefix=f'pixiv_{pixiv_id}_')
        tmp_path = tmp.name
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as zf:
                written = _write_zip_entries(zf, valid_paths, safe_title)
        except BaseException:
            tmp.close()
            _remove_temp_zip(tmp_path)
            raise
        tmp.close()
        if not written:
            _remove_temp_zip(tmp_path)
            return jsonify({'error': '文件已丢失，请重新下载'}), 404

        try:
            resp = send_file(
                tmp_path,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f'{safe_title}.zip',
            )
        except BaseException:
            # send_file 自己也会抛：Range 不可满足时它抛 RequestedRangeNotSatisfiable
            # （由 Flask 转成 416）。这时 body 根本没建起来，清理挂不上 → 当场删再上抛。
            _remove_temp_zip(tmp_path)
            raise
        if resp.status_code not in (200, 206):
            # 不发送文件内容的响应（如 ETag 命中返回 304）：body 为空，清理挂不上，
            # 只能当场删；删之前必须先关掉 send_file 自己打开的文件句柄，否则
            # Windows 上会因占用失败（WinError 32）。206 必须排除在外 —— 它会发送
            # 内容，且此刻句柄还开着，这里删会失败并让下载拿不到数据。
            _close_body_chain(resp.response)
            _remove_temp_zip(tmp_path)
            return resp
        # 清理挂在 body 上，不挂在响应对象上（原因见 _DeletingBody 注释）。
        resp.response = _DeletingBody(resp.response, tmp_path)
        return resp


# ── 下载管理 ──

@bp.route('/downloads')
def downloads_page() -> str:
    return render_template('downloads.html', csrf_token=_get_csrf_token())


@bp.route('/api/downloads')
def api_downloads() -> Response:
    with get_session() as db:
        active = db.query(Illust).filter(Illust.download_status == 'downloading').order_by(Illust.created_at.desc()).all()
        queued_ids = queued_download_snapshot()
        queued = db.query(Illust).filter(Illust.pixiv_id.in_(queued_ids)).order_by(Illust.created_at.desc()).all() if queued_ids else []
        completed = db.query(Illust).filter(Illust.download_status == 'done').order_by(Illust.downloaded_at.desc().nullslast()).limit(30).all()
        logs = (
            db.query(DownloadLog)
            .order_by(DownloadLog.created_at.desc())
            .limit(50).all()
        )
        def _with_progress(i):
            d = i.to_dict()
            p = _download_progress.get(i.pixiv_id)
            if p and p['total'] > 0:
                d['progress'] = {'current': p['current'], 'total': p['total']}
            return d

        def _with_dir(i):
            d = i.to_dict()
            paths = i.local_paths_list or []
            d['local_dir'] = os.path.abspath(_get_download_dir(i.pixiv_id)) if paths else None
            return d

        return jsonify({
            'active': [_with_progress(i) for i in active],
            'queued': [i.to_dict() for i in queued],
            'completed': [_with_dir(i) for i in completed],
            'logs': [l.to_dict() for l in logs],
        })