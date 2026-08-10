# Changelog

本文件记录 ga-feishu-streaming-card 的用户可见变更，格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本语义遵循
[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

（无未发布变更）

## [0.1.0] - 2026-08-09

### Added

- 流式卡片渲染引擎：将 GA 一次对话过程（思考、工具调用、最终答案）收束为一张持续更新的流式卡片，协议事件 `message.started / thinking.delta / answer.delta / tool.updated / system.notice / message.completed`（10 种事件协议、`schema_version=1`，与上游兼容的独立实现）
- CLI 命令：`install` / `status` / `fake-e2e` / `stop` / `uninstall`（支持 `--ga-root`、`--config`、环境变量与上级链解析）
- 离线端到端演练 `hfc fake-e2e`：协议事件流 → CardEngine（FakeTransport）→ 会话完成，不真发网络
- `render_card_result()` API：超限降级返回 `disposition=deferred_native / native`，卡片/原生交接可编程决策
- 对抗性难测例：并发生命周期 / 极端 Unicode / 超大工具状态 / 配置边界
- 自动化测试套件 30 文件 340 测例（unit + e2e，含离线 fake 端到端 17 项）

### Changed

- `<think>` 标签整块剥除含内容：思考过程不泄漏到卡片/原生消息；未闭合标签同样剥除到结尾
- 短后缀保护在无工具事件场景仍生效：≥64 字流式正文 + ≤240 字短后缀合并保留，避免正文被 1 字后缀覆盖
- 引擎配置解析独立：不再依赖 GA 根目录与配置路径耦合（显式 `--ga-root` → 环境变量 → 上级链查找）
- 渲染 fail-open 加固：容器空值归一、字段类型兜底、对象类型过滤
- 协议事件支持序列化（serialize），供外部消费事件流

### Fixed

- wheel 安装：ATTRIBUTION 打包缺失、status 路径解析、配置负例处理
- Python 3.10 兼容：tomli 依赖回退（< 3.11 无标准库 tomllib 时）
- 元数据清理：移除占位作者邮箱与无效 Source URL
- 依赖锁文件（uv.lock）与 pyproject 对齐（tomli 条目）

### 兼容性与合规

- 要求 Python ≥ 3.10
- 默认 fake 传输离线运行；`transport: http` 为预留字段（不真发飞书）
- MIT 许可；组件归属与设计依据见 ATTRIBUTION.md / COMPONENT_DECISIONS.md
