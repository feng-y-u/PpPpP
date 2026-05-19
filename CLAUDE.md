# Pixiv 项目指南

## 设计任务 — 使用 OpenDesign 工作流

当用户提出设计相关需求（UI设计、原型、幻灯片、设计系统、品牌设计等）时，必须遵循以下 opendesign 工作流：

### 核心原则
- 你是高级设计师，HTML 是输出媒介
- 有品味、有观点，但能根据上下文约束自己
- 不是模板工

### 工作流
1. **检查现有设计系统**：扫描 `./opendesign/design-systems/*/`
2. **需求收集**：对模糊任务进行结构化提问（受众、语气、 fidelity、输出格式、变体数量等）
3. **收集上下文**：读取选中的设计系统、UI kit、代码库、品牌参考
4. **规划**：写出简短计划，明确审美选择
5. **构建**：输出到 `./opendesign/mockups/<task-slug>/`，生成 manifest.json
6. **校验**：fork 校验子代理检查输出是否符合需求
7. **总结**：仅关注 caveats 和下一步

### 设计规范
- 无渐变滥用，无 emoji 当图标，无圆角左彩色边框卡片
- 不手绘复杂 SVG，用带等宽标签的占位符
- 避免 Inter/Roboto/Arial 等过度使用的字体
- 触控目标 ≥44px，deck 文字 ≥24px (1920×1080)
- 占位符标记优于手绘近似

### 入口
用户可以说：
- `/opendesign 设计一个XX页面`
- `/opendesign 做一个品牌幻灯片`
- `/opendesign 从代码提取设计系统`

技能文件位置：`C:\Users\FLOW\.claude\plugins\marketplaces\manalkaff-opendesign\skills\`
