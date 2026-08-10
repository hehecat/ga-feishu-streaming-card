# COMPONENT_DECISIONS.md — 组件决策表

> 文档：ga-feishu-streaming-card 组件决策表（独立实现）
> 对比基准：上游 `hermes-feishu-streaming-card` v4.2.8（只读深潜与测试测绘见 ATTRIBUTION.md）
> 决策口径：**调用**（直接复用上游实现）/ **重写**（独立实现，行为对齐上游公开契约）/ **舍弃**（不引入，附范围理由）；本插件总体策略 = **零复制 + 协议兼容 + 独立实现**。

## 0. 总体策略声明

1. **零复制**：本交付树未复制上游任何源码文件、函数体或注释；所有模块均为针对 GA（GenericAgent）插件面重新编写。
2. **协议兼容**：事件协议（`message.started / thinking.delta / tool.updated / answer.delta / message.completed / message.failed / system.notice / interaction.requested / interaction.completed / interaction.failed`，schema_version=1）与行为规格（会话状态机、展示态派生、内容安全限额、投递策略、fail-open 语义）作为**公开契约**被对齐，而非代码依赖。
3. **独立实现**：GA 的事件源是 `plugins/hooks.py` 的 8 个钩子（`tool_before/tool_after/agent_before/turn_before/llm_before/llm_after/turn_after/agent_after`），与上游 Hermes hook 完全不同，故桥接/引擎/CLI 均为面向 GA 的新实现。
4. **许可合规**：上游 MIT 许可允许参考协议与行为规格；归属声明见 `ATTRIBUTION.md`，许可文本见 `LICENSE`。

## 1. 组件决策表（上游 30 模块 + install/ 子包 + legacy/）

| 上游组件（v4.2.8） | 上游职责 | 本插件对应 | 决策 | 理由 |
|---|---|---|---|---|
| `events.py` | `SidecarEvent` 数据类 + `SUPPORTED_EVENTS`（10 种）+ 协议字段解析 | `src/.../events.py`（`CardEvent`） | **重写** | 协议字段/事件名对齐上游公开契约；GA 事件源不同，数据类与解析为独立实现 |
| `session.py` | `CardSession` 状态机：正文/思考/工具/交互/序列号/状态 | `src/.../session.py` | **重写** | 状态迁移语义（thinking 累积、answer 收束、sequence 守卫）按行为规格独立实现 |
| `status.py` | 展示态归一：`DISPLAY_STATUSES`、active markers | `src/.../status.py` | **重写** | 派生规则（explicit 优先、active+future 推断）按规格对齐 |
| `text.py` | 文本规整：`normalize_stream_text`、think 标签剥离、markdown 分块 | `src/.../text.py` | **重写** | 行为规格对齐（剥离 `<think>/<thinking>`、分块/冲刷判断） |
| `render.py` | 卡片渲染：工具标题/详情/模型标签转义、Markdown→lark | `src/.../render.py` | **重写** | 转义点位与 lark 结构按行为规格独立实现 |
| `card_limits.py` | 内容安全限额：200 元素 / 5 表格 / 28KB、`CardLimitExceeded` | `src/.../limits.py` | **重写** | 限额常量与超限语义对齐（FEISHU_MAX_* → card_limits.* 配置） |
| `lifecycle.py` | `CleanupPolicy(session_retention_seconds=3600, zombie_grace_seconds=120, history_limit=50)` | `src/.../lifecycle.py` | **重写** | 保留/zombie/history 清理策略按规格实现，默认参数一致 |
| `delivery_policy.py` | 每 chat 投递决策 card/native、通配 `*?[]`、policy_unavailable 回退 | `src/.../delivery_policy.py` | **重写** | 决策逻辑（card 默认、native 白名单、fail-safe 回 card）按行为规格实现 |
| `config.py` | 配置发现与加载（profile 等） | `src/.../config.py`（`EngineConfig`） | **重写** | GA 侧配置形态为 `.hfc_config.json` + YAML 示例，profile 体系不引入（范围外） |
| `feishu_client.py` | 飞书 API：send/update、delivery_uuid、`FeishuAPIError` | `src/.../transport.py` | **重写** | 传输层抽象为 Transport（`fake` 默认 / `http` 预留）；真实飞书调用不在本任务启用范围 |
| `hook_runtime.py` | Hermes hook 侧事件抽取与转发 | `bridge/hfc_bridge.py` | **重写** | GA 插件面完全不同：从 GA 8 钩子的 `ctx=locals()` 快照抽取事件 |
| `server.py` | aiohttp sidecar 主服务：路由/分发/锁/卡片收发/交互回调 | `src/.../engine.py` | **重写** | GA 无 sidecar 架构：引擎在 GA 进程内以线程+队列 fail-open 桥运行（`bridge.py`） |
| `cli.py`（~202KB，12 子命令） | sidecar 生命周期/诊断/管理 | `src/.../cli.py`（`hfc` 6 子命令） | **重写** | GA 场景精简：install/uninstall/status/stop/diagnose/fake-e2e |
| `card_timeline.py` | 工具/通知时间线（按 id 索引更新、terminal 状态） | 合入 `src/.../session.py` | **重写** | 工具时间线能力保留（同 id 原地更新），实现合入会话模型 |
| `interaction.py`（上游无独立模块；对应 `operations.py`） | 卡上操作 token/transport proof 校验 | `src/.../interaction.py` | **重写** | 交互令牌与传输证明校验按规格独立实现 |
| `event_auth.py` | 事件 HMAC 防伪/nonce 防重放 | — | **舍弃** | GA 插件为进程内本机信任边界，无外部 HTTP 事件口；防伪面不适用（范围外） |
| `native_handoff.py` | 原生输出交接状态机 | — | **舍弃** | GA 无 Hermes 原生输出通道，交接语义不适用 |
| `subscription_usage.py` | Codex 订阅配额 footer | — | **舍弃** | GA 无对应配额源 |
| `bots.py` | 多 bot 支持 | — | **舍弃** | 单 bot 范围（本任务不引入多 bot 配置面） |
| `runner.py` / `process.py` | sidecar 启动/进程管理 | — | **舍弃** | 无 sidecar 进程；生命周期由 `hfc install/stop/uninstall` 管理 |
| `flush.py` | 聚合冲刷控制器 | 合入 `src/.../bridge.py`（`_Bridge` 分发循环，coalesce） | **重写** | 冲刷/合并策略（delta_ms/delta_chars/max_pending）在桥内重写实现：按会话暂存增量、时间/字符阈值冲刷、终态立即冲刷、异常退化直发 |
| `metrics.py` | `SidecarMetrics` 计数器 | 简化内嵌 `engine.py`（calls 计数） | **重写** | 保留关键计数（transport.calls 等），全量指标体系不引入 |
| `diagnostics.py` | 诊断报告/脱敏 | `hfc diagnose`（cli.py） | **重写** | 面向 GA 场景的导入+映射自检 |
| `integrity.py` / `runtime_control.py` | 完整性围栏/运行时心跳 | — | **舍弃** | Hermes 升级恢复体系专属，GA 无对应机制（范围外） |
| `profile_sources.py` | profile 来源管理 | — | **舍弃** | 同 config.py 条目：profile 不引入 |
| `maintenance_*.py`（4 模块） | 维护期/升级恢复 | — | **舍弃** | Hermes 升级体系专属 |
| `install/`（detect/envfile/manifest/patcher/integrity/recovery） | Hermes 安装器与恢复 | `scripts/install_hfc.sh` + `hfc install` | **重写** | GA 插件安装面极简：复制插件 + 写 `.hfc_config.json`，无需 patch 框架 |
| `legacy/`（V2 归档） | 历史实现 | — | **舍弃** | 非 active runtime |
| `docs/`（event-protocol/architecture/e2e-verification 等） | 协议/架构/验证文档 | 本 README + 本决策表 + tests | **舍弃** | 上游文档仅作行为契约来源参照，不复制文本；契约表达落地为本 README + 本决策表 + tests |

## 2. 协议兼容对照（本插件保有的上游公开契约）

| 契约点 | 上游规格 | 本插件实现 |
|---|---|---|
| 事件集 | 10 种事件（§1 events.py） | `events.py` 同语义事件（GA 侧由 bridge 映射产出） |
| schema_version | "1" | 事件序列/字段对齐（conversation_id/message_id/chat_id/thread_id/platform/sequence/turn_id） |
| 卡片序列 | message.started → thinking.delta → tool.updated（同 id 原地更新）→ answer.delta → message.completed | `session.py` + `render.py` 全链路对齐（fake-e2e 断言，见 `tests/`） |
| 展示态 | thinking/in_progress/waiting/completed/failed | `status.py` 同集合 |
| 内容安全 | 200 元素 / 5 表格 / 28KB | `limits.py` 同限额 |
| 生命周期 | retention=3600 / zombie=120 / history=50 | `lifecycle.py` 同默认值 |
| 投递 | card/native + 通配 `*?[]` + fail-safe 回 card | `delivery_policy.py` 同语义 |
| fail-open | 发送异常不阻塞主流程 | `bridge.py` 桥 fail-open + transport 异常隔离 |

## 2.1 有意差异声明（与上游对照探针中的 DECLARED 项）

以下差异为**有意决策**（内容安全/交付质量优先），不视为行为回归；验收判据按
上游对照探针分类为 DECLARED。交付内可核验证据：`tests/unit/test_t11_behavior.py`
（D3/D4-S7 断言）与 `tests/e2e/test_hard_cases.py`；完整比对记录位于外部归档，
交付内仅保留结论摘要。

| 项 | 上游行为 | 本插件行为（有意差异） | 决策理由 |
|---|---|---|---|
| D3 `<think>` 剥离 | 剥标签**留内容**（`<think>secret</think>vis` → `secret vis`） | **整块剥除含内容**（→ `vis`） | 思考过程内容不泄漏到卡片/原生消息，内容安全更优 |
| D3 未闭合标签 | 未闭合 `<think>` 剥离到结尾 | 同上游：未闭合标签内容剥除到结尾 | 流式到达时避免思考残片泄漏；策略与上游一致并显式说明 |
| D4-S7 短后缀保护（无工具事件） | 无工具事件时 `_answer_archive_index` 为 None，短完成保护不触发，正文被 1 字后缀覆盖 | **保护仍生效**：≥64 字流式正文 + ≤240 字短后缀（≤3:1）合并保留 | 交付质量超越上游：避免无工具事件场景下流式答案被无意义短后缀覆盖 |
| D6 降级 disposition | `render_card_result` 返回 `deferred_native`（非终态）/ `native`（终态） | `render_card_result()` 同语义：超限降级返回 `deferred_native`/`native`，`render_card()` 兼容返回 dict | 交付语义对齐上游，卡片/原生交接可编程决策 |

## 3. 边界与风险

- 本任务 **不调用** 上游任何代码（无 import 上游包、无文件复制、无 patch）。
- `transport: http`（`HttpFeishuTransport`，httpx 客户端）为预留实现，**默认未启用真实发送**；fake-e2e/单元测试全部在离线 fake 传输上验证。
- GA 侧仅以 `plugins/hooks.py` 公开钩子面接入，**不改动 GA 源码**。
