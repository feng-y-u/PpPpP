# 收藏数分级刷新实施计划（2026-11-13）

状态：**已实现并验证**。

1. 在 `config.py` 增加三档间隔：`PREFETCH_REFRESH_HOME/NORMAL/OLD`，老作品年龄阈值 `PREFETCH_REFRESH_OLD_AGE`。
2. 在 `background._refresh_bookmarks_pass` 中：
   - 构造未下载条件（`download_status` / `local_paths`）。
   - 构造首页候选与老作品条件。
   - 用 `COALESCE(bookmark_updated_at, prefetch_refresh_at, created_at)` 判断到期，按档位 + 最久未更新排序。
   - 已下载/下载中在候选查询与逐条写入前双重排除。
3. `fetcher._process_items` 已有记录优先用列表 `bookmarkCount` 更新（defer/非 defer 两条路径）。
4. 保留失败退避、全局失败熔断、14 天兜底；兜底改为推迟到下一档周期。
5. `reset_prefetch_refresh` 同时清空 `bookmark_updated_at`。
6. 更新设置页文案与 API 文档：`pending_refresh` 表示未首次刷新数。
7. 回归：
   - `tests/test_prefetch.py::TestPrefetchRefreshBookmarks` 覆盖三档顺序/间隔、下载跳过、失败状态机。
   - `tests/test_fetcher.py` 覆盖列表计数优先与 gate 熔断。
   - 全量：`488 passed / 2 skipped / 0 failed`。
