# Pixiv 个人自用维护整改实施计划

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

目标：在单用户、单 Worker、SQLite 的使用场景下，修复测试环境不稳定、依赖不可复现、数据库升级脆弱和清理脚本不可移植等问题，保持现有功能和预取容量清理逻辑不变。

架构：保留 Flask 单体、SQLite WAL、进程内后台线程和预取缓存上限，不引入 Redis、Celery、多 Worker 协调或数据库替换。按小步提交，每一步都可独立验证和回滚。

技术栈：Python 3.9+、Flask、SQLAlchemy、SQLite、pytest、PowerShell、Bash。

---

## 已确认无需重复处理

- 预取缓存默认上限 PREFETCH_MAX_ILLUSTS=10000 已实现。
- _prefetch_capacity_cleanup() 会按收藏数从低到高清理未下载、未收藏的预取作品。
- 已下载、下载中和已收藏作品会被保护。
- 删除时会同步移除 SearchCache 引用。
- tests/test_prefetch.py 已覆盖上述规则。
- 公网安全、多用户、横向扩展不属于本计划。

## 当前基线

- 默认 pytest -q：163 项通过、29 项 setup error。
- setup error 来自 Windows pytest 临时目录权限，不是业务断言。
- app.py 约 2476 行，混合路由、下载、预取、设置和收藏夹逻辑。
- models.py:init_db() 使用 create_all() 和手写 ALTER TABLE，没有版本化迁移。
- requirements.txt 只有最低版本约束。
- scripts/pixiv-cleanup.sh 使用硬编码路径，并写入不存在的 deleted_records 表。

---

### 任务 1：稳定默认测试入口

文件：

- 修改：tests/conftest.py
- 修改：scripts/run_tests.ps1
- 创建：pytest.ini

- [ ] 创建 pytest.ini，内容包含 testpaths=tests、addopts=-ra，并注册 integration 标记。

- [ ] 将 scripts/run_tests.ps1 的临时目录改为 LOCALAPPDATA 下的 pixiv-viewer-test-tmp，并允许环境变量 PIXIV_TEST_TMP 覆盖。每次运行使用进程 ID 和时间戳创建独立目录。

- [ ] 删除 tests/conftest.py 中对全局 os.mkdir 的替换。测试数据库路径从 PIXIV_TEST_TMP 或系统临时目录生成，并在导入 models、app 前覆盖 config.DATABASE_PATH。

- [ ] 运行：
  
  powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1 -q

  预期：不再出现 tmp_path、os.scandir 或 PermissionError setup error。

- [ ] 提交：
  
  git add pytest.ini tests/conftest.py scripts/run_tests.ps1
  git commit -m "test: make pytest temp directories deterministic"

### 任务 2：明确默认测试与联网测试边界

文件：

- 修改：tests/conftest.py
- 修改：pytest.ini
- 修改：AGENTS.md

- [ ] 检查当前测试。现有 tests/test_fetcher.py 主要使用 mock 或 monkeypatch，不要把离线单元测试错误标为集成测试。

- [ ] 在 tests/conftest.py 增加 live_pixiv_required fixture。缺少 config.COOKIE_PATH 时使用 pytest.skip。

- [ ] 规定未来真实网络测试必须同时使用 @pytest.mark.integration 和 live_pixiv_required；默认命令保持离线。

- [ ] 在 AGENTS.md 记录默认测试命令和集成测试命令。

- [ ] 提交：
  
  git add pytest.ini tests/conftest.py AGENTS.md
  git commit -m "test: document offline and live test boundaries"

### 任务 3：锁定依赖版本

文件：

- 修改：requirements.txt
- 创建：requirements-lock.txt
- 创建：requirements-dev.txt
- 修改：AGENTS.md

- [ ] 使用当前虚拟环境生成 requirements-lock.txt：
  
  venv\Scripts\python.exe -m pip freeze | Set-Content -Encoding UTF8 requirements-lock.txt

- [ ] 将 requirements.txt 改为以下兼容范围：
  
  Flask>=3.1,<3.2
  SQLAlchemy>=2.0,<2.1
  requests>=2.32,<3
  gunicorn>=23,<27

- [ ] 创建 requirements-dev.txt，包含：
  
  -r requirements.txt
  pytest>=8,<10

- [ ] 文档说明开发安装使用 requirements-dev.txt，可复现环境使用 requirements-lock.txt。

- [ ] 验证安装和测试，再提交：
  
  git add requirements.txt requirements-lock.txt requirements-dev.txt AGENTS.md
  git commit -m "build: lock Python dependency versions"

### 任务 4：增加 SQLite 版本迁移和备份

文件：

- 创建：migrations/__init__.py
- 创建：migrations/runner.py
- 创建：migrations/versions/__init__.py
- 创建：migrations/versions/v001_current_schema.py
- 修改：models.py
- 创建：tests/test_migrations.py

- [ ] 先写测试，覆盖版本号记录、重复运行幂等，以及旧数据库升级前生成时间戳备份。

- [ ] migrations/runner.py 提供 current_version、backup_database、migrate 三个函数。使用 PRAGMA user_version；迁移按版本升序、每个版本只执行一次；迁移在事务中完成；备份失败时不得继续迁移。

- [ ] 将当前 init_db() 中的列补齐、废弃列删除、collection_items.position 回填和索引逻辑整理到 v001_current_schema.py，最终 schema 保持不变。

- [ ] 验证：
  
  venv\Scripts\python.exe -m pytest tests/test_migrations.py tests/test_models.py -q

- [ ] 提交：
  
  git add migrations models.py tests/test_migrations.py
  git commit -m "refactor: version SQLite schema changes"

### 任务 5：减少导入副作用并拆分 app.py

文件：

- 创建：application.py
- 创建：routes/auth.py
- 创建：routes/collections.py
- 创建：routes/settings.py
- 创建：services/download_service.py
- 创建：services/prefetch_service.py
- 修改：app.py
- 修改：tests/conftest.py

- [ ] 添加 create_app(overrides=None)，测试模式支持 START_BACKGROUND_SERVICES=False。

- [ ] 将测试 fixture 改为通过应用工厂创建测试应用，确保导入测试模块不会自动启动预取和自动关注线程。

- [ ] 抽取下载服务，保持下载状态、取消状态、现有 URL、状态码和 JSON 响应兼容。

- [ ] 抽取预取服务，必须保留 10000 条容量上限、低收藏优先删除、已下载/下载中/已收藏保护和 SearchCache 引用同步移除。

- [ ] 将认证、设置和收藏夹路由移入 Blueprint，URL 保持不变。

- [ ] 每完成一个模块运行相关测试，全部通过后提交：
  
  git add application.py app.py routes services tests
  git commit -m "refactor: split app routes and background services"

### 任务 6：修复独立磁盘清理脚本

文件：

- 修改：scripts/pixiv-cleanup.sh
- 创建：tests/test_cleanup_script.ps1
- 修改：AGENTS.md

- [ ] 先写测试：使用临时 SQLite 和下载目录，插入 31 天前、收藏数 10、状态为 done 的作品，验证文件删除、空目录删除、download_status=cleaned 和 local_paths=NULL。

- [ ] 使用脚本相对路径，并支持 PIXIV_DB 和 PIXIV_DOWNLOADS 环境变量覆盖；不要继续使用 /home/ubuntu/pixiv-viewer 硬编码路径。

- [ ] 删除对不存在的 deleted_records 表的写入。

- [ ] 删除文件前规范化路径，只允许删除 DOWNLOADS 目录下的文件。

- [ ] 验证 bash -n scripts/pixiv-cleanup.sh 和 PowerShell 测试脚本。

- [ ] 在 AGENTS.md 明确该脚本只清理已下载原图，不参与预取缓存的 10000 条容量控制。

- [ ] 提交：
  
  git add scripts/pixiv-cleanup.sh tests/test_cleanup_script.ps1 AGENTS.md
  git commit -m "fix: make optional disk cleanup script portable"

### 任务 7：维护文档和最终验收

文件：

- 创建：docs/maintenance.md
- 修改：AGENTS.md

- [ ] 文档包含开发启动、默认测试、联网测试、数据库备份、预取容量检查和下载清理命令。

- [ ] 明确区分三类存储：预取数据库缓存、instance/image_cache/ 缩略图缓存、downloads/ 原图下载文件。

- [ ] 运行：
  
  powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1 -q
  venv\Scripts\python.exe -m compileall app.py application.py fetcher.py models.py routes services migrations
  bash -n scripts/pixiv-cleanup.sh
  git diff --check

- [ ] 在 AGENTS.md 记录：预取容量清理已完成；单 Worker 进程内状态是个人自用的明确取舍。

- [ ] 提交：
  
  git add docs/maintenance.md AGENTS.md
  git commit -m "docs: add personal maintenance guide"

---

## 验收标准

1. 默认测试不再因 pytest 临时目录权限产生 setup error。
2. 默认测试不依赖真实 Pixiv Cookie 或外部网络。
3. 依赖可以通过锁定文件复现。
4. 旧 SQLite 数据库可以版本化升级，并在升级前备份。
5. 测试模式不会在导入时启动后台线程。
6. 拆分模块后 URL、状态码、JSON 字段和预取清理行为不变。
7. 磁盘清理脚本不依赖硬编码路径或不存在的数据库表。

## 不纳入本计划

- Redis、Celery 或外部任务队列。
- 多 Worker、多实例、多用户扩展。
- 更换 SQLite。
- 公网安全整改。
- 前端视觉重做。
