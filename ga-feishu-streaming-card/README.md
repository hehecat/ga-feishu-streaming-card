# ga-feishu-streaming-card

版本历史见 [CHANGELOG.md](CHANGELOG.md)。

GA（GenericAgent）→ 飞书流式卡片桥（独立实现）。

把 GA 的一次对话过程（思考、工具调用、最终答案）收束为一张**持续更新的流式卡片**：
`message.started → thinking.delta → tool.updated（同 id 原地更新）→ answer.delta → message.completed`。
默认以 **fake 传输**离线运行（不真发飞书），真实飞书发送（`transport: http`）为预留字段。

## 项目定位

- **面向 GenericAgent**：通过 GA 插件面（`plugins/hooks.py` 的 8 个钩子）接入，不改动 GA 任何源码。
- `CardEngine.handle_event` 在单个引擎实例内串行执行；调用方可以并发提交，但状态变更、投递与终态去重按事件顺序逐一完成。不同引擎实例彼此独立。
- 长文本按 `MAX_TEXT_CHARS=200_000`（thinking/answer 分别累计）截断；单事件处理与渲染耗时随事件文本量近似线性增长，生产接入应避免在单事件中无界累积内容。
- **离线可验**：`transport: fake` 为默认传输，安装 → fake-e2e → 停用 → 卸载全流程无需任何飞书凭据。

## 架构

```
GA (GenericAgent) 进程内
  plugins/hfc_bridge.py        ← 单文件插件（hfc install 复制；7 钩子注册——agent_before/tool_before/tool_after/turn_before/llm_after/turn_after/agent_after，llm_before 不注册；HFC_ENABLED 停用即无副作用）
      │  ctx=locals() 快照
      ▼
  bridge.py                     ← GA 事件 → CardEvent 映射（agent_before→message.started / tool_before|after→tool.updated /
  │                                llm_after→answer.delta / turn_after→system.notice / agent_after→message.completed|failed）
  ▼
  CardEngine（engine.py）       ← 会话状态机（session/status/text/lifecycle/limits）→ 渲染（render）→ 投递策略（delivery_policy）
      │
      ▼
  Transport（transport.py）     ← fake（默认，离线断言）/ http（预留，真实飞书未启用）
```

组件全景（`src/ga_feishu_streaming_card/`，14 模块）：`events`（事件协议）、`session`/`status`/`text`/`lifecycle`/`limits`（会话与内容）、`render`（渲染）、`config`/`delivery_policy`/`transport`（配置与投递）、`engine`/`interaction`（引擎与交互）、`bridge`（GA 桥）、`cli`（hfc 命令）。

## 真实飞书接入

接入真实飞书（事件订阅 / Encrypt Key / Verification Token / 回调地址 / 验收清单）见 [docs/OPEN_PLATFORM_SETUP.md](docs/OPEN_PLATFORM_SETUP.md)。

## 快速上手（全新用户）

```bash
# 0) 准备（Python ≥3.10；推荐 uv）
uv sync --extra dev
#    以下 uv run hfc 命令请在项目自身 venv / 干净 shell 中执行
#    （残留 VIRTUAL_ENV 等环境变量可能使入口解析到旧安装，行为与本文档不一致；
#     复用 venv 环境变量残留时先 `unset VIRTUAL_ENV`，或整行前缀 `env -i PATH=$PATH HOME=$HOME`）

# 1) 安装到 GA 根：复制插件到 <GA_ROOT>/plugins/hfc_bridge.py + 写 .hfc_config.json
#    推荐同时安装引擎 YAML；未带 --config 时引擎以默认配置降级运行（stderr 有警告属预期）
uv run hfc install --ga-root <GA_ROOT> --config ./config.example.yaml   # 无 uv 环境：scripts/install_hfc.sh [GA_ROOT]
#    GA 根解析优先级：显式 --ga-root > HFC_GA_ROOT > GA_ROOT/GA_HOME（需根标志）
#                    > cwd（AGENTS.md 或 plugins/）> cwd 上级链；全部失败则报错并非零退出

# 2) 状态校验（插件存在 / enabled=True / 配置可解析 / HFC_ENABLED 未禁用 / 引擎可导入）
uv run hfc status --ga-root <GA_ROOT>

# 3) 离线端到端演练：协议事件流 → CardEngine(FakeTransport) → 会话完成（不真发）
uv run hfc fake-e2e --ga-root <GA_ROOT>
#    HFC_CONFIG=<GA_ROOT>/config.yaml 可显式使用步骤 1 安装的引擎配置（enabled/transport/limits/coalesce 等）；
#    未设置时引擎按加载顺序（见"配置字段"节）降级默认配置，stderr 输出 "[hfc-config] 未找到配置…" 警告属预期
#    输出示例（实测）：
#    OK: GA 帧 8 个 -> 协议事件 9 个: message.started, thinking.delta, thinking.delta, answer.delta, tool.updated, tool.updated, answer.delta, system.notice, message.completed
#    OK: 投递调用 9 次（同 message_id 原地更新，不真发）: send -> update -> ...（9 次）
#    OK: 最终卡片 header='已完成'

# 4) 停用（写 enabled=false，插件 import 后不再注册/触发）
uv run hfc stop --ga-root <GA_ROOT>

# 5) 卸载（删除插件与配置，目录还原）
uv run hfc uninstall --ga-root <GA_ROOT>
```

GA 重启后插件自动加载（`plugins.discover_and_load`）。诊断命令：

```bash
uv run hfc diagnose --ga-root <GA_ROOT>   # 引擎包导入 + map_ga_ctx 事件映射 + engine 模块自检
```

## 离线安装（wheel 分发）

> 前提：`dist/` 为构建产物（已 gitignore，clone 后不存在）——**先执行 `uv build` 生成 dist/ 与 wheel**。

`dist/` 下的 wheel（`uv build` 产物）包含完整可安装面：包代码 + 插件源
`ga_feishu_streaming_card/_bridge/hfc_bridge.py`（源码树 `bridge/` 的打包副本）+
`ATTRIBUTION.md` / `README.md`（wheel 根与 `dist-info/licenses/` 双份，METADATA 经
`License-File` 引用）。在**目标环境**（无需源码树）安装并接入 GA：

```bash
uv venv /opt/hfc-venv
uv pip install --python /opt/hfc-venv/bin/python dist/ga_feishu_streaming_card-0.1.0-py3-none-any.whl
/opt/hfc-venv/bin/hfc install --ga-root <GA_ROOT>    # 插件源取自包内 _bridge/，源码树缺失亦可安装
/opt/hfc-venv/bin/hfc status --ga-root <GA_ROOT>     # 引擎包可导入项指向 site-packages 实际路径
```

## 生产部署与自动回滚

在已运行的 GenericAgent 根目录上部署当前交付仓构建版本：

```bash
scripts/deploy_prod.sh <GA_ROOT>
# 端口不是默认 8898 时：HFC_PROD_PORT=<port> scripts/deploy_prod.sh <GA_ROOT>
```

脚本会先读取 GA 虚拟环境中已安装的 `ga-feishu-streaming-card` 版本，并将当前 `dist/` 原子移动到带时间戳的 `dist.bak.<timestamp>.<pid>/`。备份目录中必须有与已安装版本完全匹配的 wheel；不满足时脚本会在修改虚拟环境之前拒绝部署。随后脚本重建 `dist/`、安装新 wheel、精确重启 fsapp，并执行监听端口、WebSocket 新连接和 `/llms` 模型按钮三项冒烟。

新 wheel 开始安装后任一步失败，脚本都会用已校验的旧 wheel 自动恢复包版本、重启 fsapp，并再次验证监听端口：

- `RECOVERY=ROLLED_BACK`：旧版本与 fsapp 服务均已恢复。
- `RECOVERY=ROLLBACK_FAILED`：自动回滚失败；现场、旧 wheel 路径、报告/日志路径和可复制执行的 `ROLLBACK_ACTION` 会保留在输出中，按该命令手工恢复。

成功与失败报告均写入 `<GA_ROOT>/temp/hfc_deploy_report_<timestamp>.txt`。`dist.bak.*` 默认被 git 忽略，确认新版本稳定后可人工清理历史备份。

## 获取源码

```bash
git clone <repo-url>
cd ga-feishu-streaming-card
# 或从发布页下载 release archive 后解压进入项目目录
```

将 `<repo-url>` 替换为实际发布仓库地址；离线部署也可直接使用 `dist/` 中的 wheel/sdist。

## CLI 命令一览

| 命令 | 行为 | 退出码 |
| --- | --- | --- |
| `hfc install [--ga-root ROOT] [--config PATH]` | 复制 `bridge/hfc_bridge.py` → `ROOT/plugins/`；写 `ROOT/.hfc_config.json`；指定 `--config` 时复制 YAML → `ROOT/config.yaml` | 0=成功 |
| `hfc status [--ga-root ROOT]` | 校验插件文件/配置 enabled/配置可解析/HFC_ENABLED env/引擎可导入，逐项 `[OK]/[FAIL]` | 0=全 OK |
| `hfc stop [--ga-root ROOT]` | 写 `enabled=false`（运行时停用）；已加载进程需重启完全生效；未安装时 FAIL | 0（未安装时 1） |
| `hfc uninstall [--ga-root ROOT]` | 删除插件与配置；未安装时提示 INFO | 0 |
| `hfc diagnose [--ga-root ROOT]` | 引擎导入 + 事件映射自检 + engine 模块可用性 | 0（引擎缺时 1） |
| `hfc fake-e2e [--ga-root ROOT]` | 8 帧 GA 快照离线全链路：映射为 9 个协议事件（started→thinking×2→answer→tool×2→answer→notice→completed），输出事件序列与投递调用数 | 0 |

`ROOT` 的解析顺序是：显式 `--ga-root` → `HFC_GA_ROOT` → `GA_ROOT` / `GA_HOME`（目录须含 `AGENTS.md` 或 `plugins/` 根标志）→ 当前工作目录 → 当前目录的上级链。后两步同样按根标志识别；全部未命中时命令向 stderr 输出可操作提示并以非零码退出，不再依赖交付源码目录层级，因此 wheel 安装形态也可用。入口：`uv run hfc`（pyproject scripts）或 `python -m ga_feishu_streaming_card.cli`；无 uv 环境用 `scripts/install_hfc.sh`（自动走 `uv run` 或 `python3` + PYTHONPATH）。

## Python API（包顶层导出）

核心对象可直接从包顶层导入（模块级路径如 `ga_feishu_streaming_card.session.CardSession` 保持可用）：

```python
from ga_feishu_streaming_card import (
    CardSession, ToolState, InteractionState,                          # 会话状态机
    DisplayStatus, resolve_display_status,                             # 展示态（4 种）
    CardRenderResult, render_card, render_card_result, html_escape_card_text,  # 渲染
    CleanupPolicy, cleanup_expired,                                    # 生命周期
    EventType, CardEvent, parse_event,                                 # 协议事件（10 种，schema_version=1）
    CardEngine, EngineConfig, load_config,                             # 引擎与配置
    FakeTransport, HttpFeishuTransport,                                # 传输（fake 默认 / http 预留）
)

# 事件解析示例（wire dict → CardEvent；非法结构抛 ValueError）
ev = parse_event({
    "type": EventType.MESSAGE_STARTED.value,
    "conversation_id": "c1", "chat_id": "ch1",
    "sequence": 1, "created_at": 1.0, "data": {},
})
assert ev.type is EventType.MESSAGE_STARTED
assert ev.to_dict()["type"] == "message.started"

# 渲染示例（会话 → 卡片 JSON；超限自动降级 disposition，永不抛异常）
result = render_card_result(session)          # CardRenderResult(card=..., disposition="card")
card_json = render_card(session)              # 兼容旧接口，直接返回 dict
```

## 配置字段

`hfc install` 生成的状态文件 `.hfc_config.json`（GA 根下）：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 插件开关（`hfc stop` 写 false；顶层环境变量 `HFC_ENABLED=0/false/...` 亦可停用） |
| `engine_root` | 引擎源码路径（CLI 注入 PYTHONPATH 用） |
| `installed_at` | 安装时间（ISO8601） |

引擎运行参数对照 [config.example.yaml](config.example.yaml)（当前引擎按默认 fake 运行，无需改动即可跑通全流程）。推荐安装时显式复制：

```bash
uv run hfc install --ga-root <GA_ROOT> --config ./config.example.yaml
# 结果：<GA_ROOT>/config.yaml
```

引擎加载顺序：调用 `load_config(path)` 的显式路径 → 环境变量 `HFC_CONFIG` → 当前工作目录 `./config.yaml`。无候选命中时仍返回可用默认配置，但会向 stderr 输出 `[hfc-config] 未找到配置...` 警告，避免 CWD 耦合被静默掩盖。若 GA 不是从 `<GA_ROOT>` 启动，应设置 `HFC_CONFIG=<GA_ROOT>/config.yaml`；`.hfc_config.json` 是插件状态文件，不等同于引擎 YAML。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `transport` | `fake` | `fake`=离线测试；`http`=真实飞书（预留，默认不启用） |
| `http.base_url` / `http.timeout_ms` | `https://open.feishu.cn` / `800` | http 传输预留参数 |
| `delivery.default` | `card` | 默认卡片投递；`native_chats` 支持通配 `*?[]` |
| `delivery.native_chats` | `[]` | 原生消息白名单 chat |
| `limits.retention_seconds` | `3600` | 会话保留时长 |
| `limits.zombie_grace_seconds` | `120` | 空会话回收宽限 |
| `limits.history_limit` | `50` | 会话历史上限 |
| `card_limits.max_elements` | `200` | 卡片元素上限 |
| `card_limits.max_tables` | `5` | 卡片表格上限 |
| `card_limits.safe_bytes` | `28000` | 卡片 JSON 安全体积 |
| `coalesce.delta_ms` | `250` | 冲刷合并窗口 |
| `coalesce.delta_chars` | `600` | 合并字符阈值 |
| `coalesce.max_pending` | `128` | 待冲刷上限 |

## 测试与验收

```bash
uv run pytest tests/unit -q     # 313/313 passed（unit 28 文件）
uv run pytest tests/e2e -q      # 27/27 passed（e2e 2 文件）
```

11 能力类别覆盖（每文件只计一次，unit 合计 313）：

| # | 能力类别 | 测试文件（测例数） |
| --- | --- | --- |
| 1 | 事件协议 | `test_events`(18) |
| 2 | 会话状态机、生命周期与回收 | `test_session`(19) + `test_lifecycle`(16) + `test_gc`(7) |
| 3 | 文本规整与展示态 | `test_text`(23) + `test_status`(8) |
| 4 | 渲染与内容安全限额 | `test_render`(10) + `test_limits`(11) + `test_t11_behavior`(14) |
| 5 | 配置 / 投递策略 / 增量合并 / 传输 | `test_config`(10) + `test_delivery_policy`(5) + `test_coalescing`(9) + `test_transport`(14) |
| 6 | 引擎与交互 | `test_engine`(19) + `test_interaction`(13) |
| 7 | 桥与 CLI | `test_bridge`(30) + `test_cli`(4) + `test_cli_edge`(21) |
| 8 | 安全与输入校验（含 fail-open） | `test_security_cli_paths`(5) + `test_security_concurrency`(4) + `test_security_config`(9) + `test_security_frames`(12) + `test_security_http_paths`(5) + `test_security_payload`(8) + `test_failopen`(6) |
| 9 | 对抗与边界 | `test_adversarial_edge`(6) |
| 10 | 元数据与文档一致性 | `test_metadata_docs`(3) + `test_public_api`(4) |
| 11 | 端到端（离线 fake + 硬测例） | `test_fake_e2e`(17) + `test_hard_cases`(10) |

验收证据：`uv run pytest -q` → 340 passed（unit 313 + e2e 27，30 文件）；`hfc fake-e2e` → 退出码 0；CLI 安装/停用/卸载闭环由 `test_cli.py` 覆盖（fake GA 根，不污染真实环境）。

## 与上游对比

- **组件决策表**（调用/重写/舍弃 逐项 + 理由）：[COMPONENT_DECISIONS.md](COMPONENT_DECISIONS.md)
- **设计依据与上游测绘**：见 [ATTRIBUTION.md](ATTRIBUTION.md)（协议/行为规格参照来源）
- **对比矩阵（摘要）**：

| 维度 | 上游 v4.2.8 | 本插件 |
| --- | --- | --- |
| 事件源 | Hermes hook → sidecar HTTP | GA 8 钩子 locals 快照 → 进程内桥 |
| 运行时 | aiohttp sidecar 进程（30 模块） | GA 进程内线程+队列（14 模块） |
| 传输 | 飞书 SDK（lark-oapi） | `fake` 默认 / `http` 预留 |
| 安装 | 安装器 patch/恢复体系 | 复制插件 + `.hfc_config.json` |
| 事件协议 | 10 种事件 schema_version=1 | 同协议（独立实现） |
| 内容安全/生命周期/投递 | 200/5/28KB；3600/120/50；card/native | 同规格（独立实现） |

> **有意差异（DECLARED，详见 [COMPONENT_DECISIONS.md §2.1](COMPONENT_DECISIONS.md)）**：D3 `<think>` 标签**整块剥除含内容**（思考过程不泄漏，比上游留内容更安全；未闭合标签同样剥到结尾）；D4-S7 短后缀保护在**无工具事件**场景仍生效（≥64 字正文 + ≤240 字后缀合并保留，超越上游 1 字覆盖）。

## 安全与信任边界

1. **base_url 无 host allowlist**：`http.base_url` 仅做格式校验（`valid_base_url`），不维护 host 白名单。建议：部署层限制配置文件写权限（仅授权管理员可改 `.hfc_config.json` / `config.yaml`）；生产启用 HTTPS；`transport: http` 为预留实现，默认不启用。
2. **CLI `--ga-root` 是授权安装目标**：`hfc install/stop/uninstall/status` 会写/删 `<GA_ROOT>/plugins/hfc_bridge.py` 与 `<GA_ROOT>/.hfc_config.json`，无 sandbox——请仅对可信目录执行安装类命令，并将 `--ga-root` 视为特权参数。
3. **降级卡不泄漏内部异常**：渲染异常时卡片内容只含固定错误码（`render_error: code=RC01`），不拼接原始异常文本/类型；异常详情仅进 `logger.debug` 受控诊断通道。

## 边界声明

1. **不改 GA 源码**：仅以 `plugins/hooks.py` 公开钩子面接入；安装/卸载只操作 GA 根 `plugins/` 与 `.hfc_config.json`。
2. **不真发飞书**：`transport: fake` 为默认且唯一启用传输；`http`（`HttpFeishuTransport`，httpx 客户端）为预留实现，不注入凭据、不在默认路径启用，默认不进行真实发送。
3. **零复制**：未复制上游源码，仅参照公开协议与行为规格（见 ATTRIBUTION.md / COMPONENT_DECISIONS.md）。

## 目录结构

```
src/ga_feishu_streaming_card/   # 引擎（14 模块：events/session/status/text/lifecycle/limits/
  bridge.py                     #   render/config/delivery_policy/transport/engine/interaction/bridge/cli）
  cli.py                        # hfc CLI（install/uninstall/status/stop/diagnose/fake-e2e）
bridge/hfc_bridge.py            # GA 单文件插件（复制到 GA plugins/）
config.example.yaml             # 示例配置
scripts/install_hfc.sh          # 无 uv 环境安装脚本
tests/unit/                     # 313 测例（28 文件，11 能力类别）
tests/e2e/                      # 离线 fake 端到端 + 硬测例（27 测例）
dist/                           # 构建产物（wheel/sdist，`uv build` 生成）
COMPONENT_DECISIONS.md          # 组件决策表
ATTRIBUTION.md                  # 上游归属声明
LICENSE                         # MIT
```

## 安装脚本说明

`scripts/install_hfc.sh` 等价于 `hfc install`（供无 uv 环境使用）：自动探测 `uv run` 或 `python3`；不传 GA_ROOT 时交给 hfc 完整探测链（`--ga-root` → `HFC_GA_ROOT` → `GA_ROOT`/`GA_HOME`（需 AGENTS.md 或 plugins/ 标志）→ cwd 标志 → cwd 上级链首个标志目录，全部未命中则报错并以非零码退出）。`dist/` 为 `uv build` 构建产物（wheel + sdist），可离线安装到目标环境。
