# Pixiv 响应样本（脱敏）

这套样本是 `tests/test_pixiv_contract.py` 的输入，用来把"我们的代码依赖 Pixiv 响应的
哪些**字段名与嵌套路径**"变成可执行断言。

## 为什么需要
Pixiv 用的是非官方内部 Ajax API，字段改名/移动不会提前通知 —— 以前只能等用户报障
（搜索突然空了、原图拉不到、收藏数恒为 0）。现在字段一变，契约测试立刻红，且失败
信息里带 JSON 路径。

## 脱敏规则（**样本里绝不能出现真实账号数据**）
- 身份字段（`id` / `userId` / `userName` / `userAccount` / `title` / `message` …）
  统一替换为 `見本…` / `90000xx` 之类的占位值；
- 所有图片 URL 收敛为 `https://i.pximg.net/...`，只保留 `_pN.<ext>` 尾巴 ——
  **页序信息必须留**，因为原图地址的解析正是靠它；
- 标签名替换为 `サンプル` / `R-18` 这类无害占位（`R-18` 要留，`R18_TAGS` 判定靠它）；
- 不含任何 Cookie / `PHPSESSID`；
- 保留**结构**：字段名、嵌套层级、类型（字符串列表 vs `[{tag: …}]` 两种 tags 形态、
  `metaPages` vs `metaSinglePage` vs 旧 `urls.original` 三条原图路径都在样本里各占一份）。

## 刷新样本
```bash
# 用真实 Cookie 抓一次并**就地脱敏**后覆盖本目录
venv\Scripts\python.exe scripts\pixiv_capture.py --illust-id <pid> --user-id <uid> --tag <关键词>
```
抓完请自查 `git diff tests/fixtures/pixiv/`：**只应该看到字段名/结构的变化**，
出现任何真实昵称、标题、URL 路径即说明脱敏漏了，不要提交。

## 文件清单
| 文件 | 覆盖的契约 |
|---|---|
| `illust_detail_meta_pages.json` | 多图详情：`metaPages[].urls.original` + `tags.tags[].tag` |
| `illust_detail_meta_single.json` | 单图详情：`metaSinglePage.originalImageUrl` |
| `illust_detail_legacy_urls.json` | 旧形态：无 meta*，靠 `urls.original` + `pageCount` 推导 `_pN` |
| `search_illustrations.json` | `/ajax/search/illustrations`：`body.illust.data` / `body.illust.total`、字符串 tags |
| `discovery_artworks.json` | `/ajax/discovery/artworks`：`body.thumbnails.illust`、按 `type` 过滤、`body.total` |
| `user_profile_all.json` | `/ajax/user/<uid>/profile/all`：`body.illusts` 的**键**即作品 id |
| `follow_latest.json` | `/ajax/follow_latest/illust`：`body.thumbnails.illust` + `body.page.isLastPage` |
| `error_envelope_auth.json` | `error:true` + 认证类 message → `PixivAuthError` |
| `error_envelope_deleted.json` | `error:true` + 删除类 message → `DEAD_DETAIL` |
