# Pixiv Viewer — 源码级技术文档生成任务计划

目标：对 E:\pixiv（Pixiv Viewer，Flask 自部署应用）完成系统性源码级逆向分析，并生成一套按 32 节结构组织的完整技术文档（含 mermaid、源码引用、问题清单、技术债务、路线图）。

## 阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| 0 | 规划文件 + 项目盘点（目录结构/技术栈/依赖/配置清单） | complete |
| 1 | 核心后端源码精读（app/config/models/runtime/helpers/middleware/background/fetcher/routes_*） | complete |
| 2 | 架构分析：模块依赖、数据流、调用链、请求生命周期 | complete |
| 3 | 外围分析汇总：测试体系 / 前端 / 脚本迁移运维（子代理产出 + 实测 309 passed） | complete |
| 4 | 问题审查：Bug/安全/性能/工程化/测试/技术债务 | complete |
| 5 | 文档生成：docs/technical-documentation.md（1461 行，32 节 + 自检表；UTF-8 校验通过） | complete |
| 6 | 最终自检（28 项检查表，含 N/A 1 项） | complete |

## 关键决策

- 核心后端由主代理亲自精读（保证引用准确）；tests/、templates+static/、scripts+migrations+docs/ 委托三个后台子代理产出结构化报告（已全部回收）。
- 敏感内容（instance/.secret_key、.cursor_secret、cookies.txt、settings.json 中的密码类值）只标注存在性，绝不在文档中输出原文。
- 所有结论标注来源（文件+符号名）；无法确认的内容标「未确认/推测」。
- 最终文档落盘位置：docs/technical-documentation.md（新增文件，不修改现有 architecture.md/maintenance.md）。

## 已产出文件

- task_plan.md / findings.md / progress.md — 规划与记录
- docs/technical-documentation.md — 最终 32 节技术文档（1461 行）