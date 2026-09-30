# 搜索预览可查看 实施计划

**Spec:** `docs/superpowers/specs/2026-09-30-search-preview-viewable-design.md`

**Goal:** 搜索运行中已经筛选出的预览作品，点开 `/detail/<pid>` 就能看图，而不是只弹「该作品仍在筛选中」。不碰事务边界（不做增量落库）。

**Architecture:** `runtime.find_running_preview(pixiv_id)` 在 `_search_tasks_lock` 内查运行中任务的快照并返回 dict 副本；`routes_gallery.detail_page` 查库未命中时用它兜底渲染，并把 `preview=True` 传给模板；`templates/detail.html` 预览态显示提示条 + 禁用下载按钮；`static/page-index.js` 让预览卡点击跳详情并更新文案。

**涉及文件:** `runtime.py`、`routes_gallery.py`、`templates/detail.html`、`static/page-index.js`、`tests/test_app.py`

**状态：已实现并验证（2026-09-30）**

---

## Task 1：`runtime.find_running_preview`

- [x] **Step 1**：在 `runtime.py` 的「异步搜索任务」段末尾（`_search_tasks` / `_search_tasks_lock` / `SEARCH_TASK_TTL` 之后）加：

  ```python
  def find_running_preview(pixiv_id: int) -> dict | None:
      """在**运行中**搜索任务的快照里找一条已确认但尚未落库的预览；找不到返回 None。"""
      best_item: dict | None = None
      best_created: float = -1.0
      with _search_tasks_lock:
          for task in _search_tasks.values():
              if task.get('status') != 'running':
                  continue
              created = task.get('created_at') or 0
              if created <= best_created:
                  continue
              for item in task.get('results') or []:
                  if item.get('pixiv_id') == pixiv_id:
                      best_item = item
                      best_created = created
                      break
          return dict(best_item) if best_item is not None else None
  ```

  实现细节（按 spec 的取舍）：
  - 只认 `status == 'running'`。
  - 多个 running 任务含同一 pid 时取 `created_at` 最新的那个（`best_created` 初值 `-1.0`，`created_at=0` 的字典也要能被选中）。
  - **返回 `dict(item)` 副本**：详情页会往这个 dict 里塞 `local_urls` 等展示字段，不能污染任务快照。
  - 遍历与拷贝都在锁内（并发约定：容器遍历要加锁）。

- [x] **Step 2**：`tests/test_app.py` 加 `TestFindRunningPreview`：
  - 非 running（`done` / `partial` / `cancelled` / `error`）的任务不返回；
  - 返回的是副本（改动返回值不影响 `_search_tasks` 里的快照）；
  - 同一 pid 在多个 running 任务里取最新的；
  - 未知 pid 返回 `None`。

## Task 2：`routes_gallery.detail_page` 兜底

- [x] **Step 1**：重构 `detail_page`，让"数据来源"成为分支，其余渲染逻辑（原图地址惰性拉取、中图/原图代理、相关作品）逐字复用：

  ```python
  preview_mode = False
  with get_session() as db:
      illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
      if illust is not None:
          data = illust.to_dict()
          paths = illust.local_paths_list or []
          file_size = illust.file_size or None
          owner_id = illust.user_id
          stored_urls = illust.original_urls_list or []
      else:
          snapshot = runtime.find_running_preview(pixiv_id)
          if snapshot is None:
              abort(404)
          preview_mode = True
          data = snapshot
          paths = []
          file_size = None
          owner_id = data.get('user_id')
          stored_urls = [u for u in (data.get('original_urls') or []) if isinstance(u, str)]

      local_urls = [f'/api/image/{pixiv_id}/{n}' for n in range(len(paths))]
      related = db.query(Illust).filter(Illust.user_id == owner_id, ...).all()
      need_fetch_urls = not stored_urls
  ```

  **陷阱**：原实现把 `illust.original_urls_list` 放在 `with` 块**之后**读（`urls = illust.original_urls_list or []`），一旦走快照分支就没有 `illust` 对象了 —— 必须把"已存的原图地址"提前算成局部变量 `stored_urls`。

- [x] **Step 2**：`render_template(..., preview=preview_mode)`。

- [x] **Step 3**：`tests/test_app.py` 加 `TestDetailPagePreviewFallback`（`/detail/<pid>` 此前**没有**任何用例覆盖，这组用例同时把 DB 命中路径与 404 路径钉住）：
  - 库里无行 + running 任务里有该 pid → 200，HTML 含标题/画师/提示条/禁用按钮，且**不发任何网络请求**（`_fetch_original_urls` 被换成会抛异常的 patch；夹具预置 `original_urls` —— 与仓库"详情类用例必须预置 original_urls"的约定一致）；
  - 库里无行 + 任务处于 `done`/`partial`/`cancelled`/`error` → 404；没有任务也 404；
  - 库里有行 + running 任务里同 pid 的快照标题不同 → 渲染的是 **DB** 的标题，且无预览提示条；
  - **端到端**：真实 `_make_search_publisher` 发布的预览（只补丁 `app.search_by_tag`）→ 详情页 200。这条是必须的：夹具手工构造任务字典时，"发布侧存哪个键"与"读取侧读哪个键"各写一份约定，两边一起错也能通过；只有真实 publisher 对接过才算接线成立。

## Task 3：`templates/detail.html` 预览态

- [x] **Step 1**：在统计区下方、`.info-actions` 之前插提示条（仅 `preview` 为真时渲染），文案点明"来自进行中的搜索、还没写入数据库、图与标签可看、下载要等本次筛选结束"。
- [x] **Step 2**：`.info-actions` 的下载按钮加 `{% if preview %}` 分支：`disabled` + `title` 说明 + 文案「⬇ 下载原图（筛选中）」，避免点出必然 404 的请求（`page-detail.js` 的下载处理器本身也有 `if (this.disabled) return`）。
- [x] **Step 3**：模板变量与路由传参同名 `preview`（一个变量、一个名字）。

## Task 4：`static/page-index.js`

- [x] **Step 1**：`renderCard` 的卡片点击分支删掉"预览卡弹提示"的特例 → 预览卡与已提交卡一样跳 `/detail/<pid>`。
- [x] **Step 2**：预览卡操作区文案 `筛选中…` → `可预览 · 暂不可下载`（徽标与虚线描边保留）。
- [x] **Step 3**：捕获阶段那条"预览卡不许下载"的兜底守卫保留，提示改为 `该作品仍在筛选中，等本次筛选完成后再下载`；注释补一句"拦的只是下载，点开详情是允许的"。
- [x] **Step 4**：更新文件顶部与 `renderCard` 上方那段注释（原文写的是"预览卡不跳详情"，已与代码不符）。
- [x] 语法自检：`node --check static/page-index.js` 通过。

## Task 5：文档回写

- [x] spec / plan 标记"已实现"并写验证结果；
- [x] `AGENTS.md` 的「API 行为」搜索条目补一句：预览未落库，`/detail/<pid>` 对运行中搜索的预览 pid 走内存快照兜底，但下载仍要求行已落库。

## 验收

| 验收项 | 命令 / 方式 | 结果 |
|---|---|---|
| 预览兜底渲染详情页 | `tests/test_app.py -k Preview` | ✅ |
| 非 running 不兜底、DB 优先、端到端发布侧对接 | 同上 | ✅ |
| 快照副本与查找选择 | `tests/test_app.py::TestFindRunningPreview` | ✅ 4 passed |
| 新增用例数 | 694 - 682 = 12 | ✅ 12 passed |
| **变异验证**（用例不是恒绿） | 临时插件把 `runtime.find_running_preview` 打成 `lambda pid: None` 后跑 `-k Preview` | ✅ **4 failed / 14 passed** —— 两条 200 路径与两条查找用例确实依赖兜底 |
| 无回归 | `scripts\run_tests.ps1 -q` | ✅ **688 passed / 2 skipped / 4 failed(env)** 18.16s |
| 前端语法自检 | `node --check static/page-index.js` | ✅ |

基线（删除收藏夹功能后的实测）：682 条 = 676 passed / 2 skipped / 4 failed。**4 个失败全部是 `tests/test_test_setup.py::test_temp_root_*`**：用例在测试进程里再起 `powershell.exe` 子进程并捕获输出，受限执行环境下退出码非 0（`subprocess.CalledProcessError`），与本次改动无关（这 4 条不 import 本次触碰的任何模块）。

## 执行勘误（留档）

1. **断言踩了"恒真字符串"**：计划里写的 `'preview-notice' in html` 无法区分预览态 —— 该字符串在 `<style>` 块里恒在，"DB 优先"用例的 `not in` 断言因此必失败。改为断言标记本身 `<div class="preview-notice">`。
2. **测试夹具的 pid 必须与其它用例错开**：`_search_tasks` 是进程级内存且不在 `clean_db` 覆盖范围内，残留的 running 任务会让后续用例的 `/detail` 兜底命中。本批用 `preview_task` 夹具在收尾摘掉自己装的任务，并把 pid 集中在 91xxx / 92xxx 段。
3. **Task 1 的初版草图有 bug**：先写的是"遍历两遍、以 `best is not None` 比较创建时间"，`created_at` 为 0（假值）时判定会走偏；最终实现用独立的 `best_created = -1.0` 哨兵。
