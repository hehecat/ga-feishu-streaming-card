# GA Feishu Streaming Card — 飞书流式卡片桥（开源版）

将 LLM 流式输出实时渲染为飞书交互卡片的 Python 桥接组件，含模型切换下拉、2×2 按钮布局、Goal Hive 状态入口等实战打磨功能。

## 仓库结构
- `ga-feishu-streaming-card/` — 项目本体（MIT，基于 hermes-feishu-streaming-card v4.2.8 移植适配，归因见 ATTRIBUTION.md）
- `sop/` — Goal Hive 多 Agent 协作机制的 Linux 适配操作手册（脱敏公开版）

## 快速开始
见 `ga-feishu-streaming-card/README.md`（安装 `hfc install`、配置 `config.example.yaml`、开放平台指引 `docs/OPEN_PLATFORM_SETUP.md`）。

## 说明
- 所有密钥均"只引用键名，不写明文值"；请自行配置飞书开放平台应用凭据。
- SOP 为脱敏版：内部路径/UID 已泛化，保留完整技术流程。
