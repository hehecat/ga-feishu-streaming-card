# 最终验收清单（真实飞书接入）

> 面向真实用户的逐项验收交付物。配套「一键验收脚本」：`scripts/verify_production.py`（只读探测）。
> 配置细节以 [OPEN_PLATFORM_SETUP.md](./OPEN_PLATFORM_SETUP.md) 为准；本文只给**检查清单 + 预期现象 + 失败排查指引**。
> 全程不落密钥明文：所有密钥只存放在 GA 根目录 `mykey.py`（键名小写：`fs_app_id` / `fs_app_secret` / `fs_verification_token` / `fs_encrypt_key`）。

## 0. 前置条件（未满足先补，否则后续项无意义）

- [ ] 已 `hfc install`，`hfc status` 输出 5/5 OK
- [ ] fsapp 生产进程在跑：`ss -tlnp | grep 8898` 有监听；PID **以 ss 实测为准**（勿依赖旧 pid 文件；verify 脚本 PORT_LISTEN 已做监听 PID + /proc cwd/cmdline 双重身份校验）
- [ ] 有公网可达服务器（或内网穿透/反代）指向 `127.0.0.1:8898`

## A. 配置前检查清单（改动开放平台之前逐项核对）

| # | 检查项 | 预期 | 失败排查指引 |
|---|--------|------|--------------|
| A1 | 三处配置位置已定位：①「凭证与基础信息」（App ID/Secret）②「事件与回调→事件订阅」（长连接）③「事件与回调→卡片回调」（独立区域）| 三个页面都能打开且位置明确 | 对照 OPEN_PLATFORM_SETUP.md §1/§3.1/§3.2 逐段定位；开放平台后台「开发者后台→应用详情」 |
| A2 | `mykey.py` 四个小写键已存在（值可先留空）| `fs_app_id`/`fs_app_secret`/`fs_verification_token`/`fs_encrypt_key` 四键在文件内 | 参考 OPEN_PLATFORM_SETUP.md §1.4：键名必须**精确小写**，否则 fsapp 启动即退出；密钥值**不要写进文档/仓库** |
| A3 | 事件订阅已选**长连接模式**（不填请求地址），待勾事件 `im.message.receive_v1` | 订阅方式为长连接；事件列表含 `im.message.receive_v1` | 若误选「请求地址模式」会导致收不到消息；事件列表**不存在** `card.action.trigger`（卡片按钮回调走 A4 的独立区域） |
| A4 | 卡片回调地址已确定：`http://<公网地址>:8898/card/actions`，且**保存前公网可达自检通过** | 服务器上 `curl http://<公网地址>:8898/card/actions` 返回 JSON 响应 | 按 OPEN_PLATFORM_SETUP.md §3.3 四步排查：①服务器防火墙（ufw/firewalld）②云安全组入站 ③端口映射/内网穿透 ④Nginx/Caddy 反代 |
| A5 | 加密策略：先「开启」再「生成 Encrypt Key」，页面展示 Verification Token 与 Encrypt Key | 两项凭据可见并可复制 | 加密策略默认关闭、页面不展示 Encrypt Key；先开启再生成（OPEN_PLATFORM_SETUP.md §3.4） |
| A6 | `mykey.py` 凭据回填：Verification Token 与 Encrypt Key **至少配置其一** | `fs_verification_token` 或 `fs_encrypt_key` 非空 | 两者都为空时 fsapp 卡片回调端点**拒绝启动**（防配置漂移），启动日志可见原因；填完必须**重启 fsapp**（配置启动时加载）|

## B. 配置后逐项验收走查（在真实飞书中操作）

> 每项：**操作** → **预期现象**；未达预期按「排查」逐条查。

### B1 长连接消息通道
- 操作：私聊/群聊 @机器人 发送 `/help`
- 预期：收到 help 说明卡片（长连接链路通）
- 排查：①事件订阅是否长连接 + 已勾 `im.message.receive_v1` ②「版本管理与发布」已创建版本并发布 ③权限管理已申请 `im:message` 系列 ④`hfc status` 5/5 OK ⑤fsapp 日志出现 `connected to wss://msg-frontier.feishu.cn`

### B2 六命令逐一可用
- 操作：依次发送 `/new` `/reset` `/undo` `/llm` `/llms` `/model`
- 预期：每条命令都有对应卡片回执（`/new` 会话已重置、`/reset` 重置、`/undo` 已撤销上一步、`/llm` 流式会话卡、`/llms` 模型选择卡、`/model` 当前模型）
- 排查：①命令白名单缺失 → 见 verify_production.py WHITELIST 项（应含全部六命令+`/settings` `/status`）②消息事件未生效（见 B1）③fsapp 未重启导致配置未加载

### B3 模型选择卡按钮 2×2 布局
- 操作：发送 `/llms`，查看卡片底部按钮区
- 预期：**两行×两列**共四按钮：行1「新会话」「切换模型」，行2「设置」「状态」；每个按钮宽度约 50%，**不竖排、不换行**
- 排查：①若出现竖排 4 个按钮 → 部署版本未到 T27-G 飞书卡片 2.0 结构（`body.elements` 的两个**顶层 `column_set`**，每个恰 2 列、每列恰 1 个 `button`；不得出现 1.0 `action` 容器），重跑 `scripts/deploy_prod.sh` ②确认安装 wheel 版本为 T27-G 或更新：`uv pip show --python .venv/bin/python ga-feishu-streaming-card` ③`scripts/verify_production.py` 的 RENDER_2X2 项应 PASS（断言 2 个 `column_set`×每行 2 列×每列 1 按钮）

### B4 模型下拉 select_static（完整模型名防截断）
- 操作：`/llms` 卡片内点击模型下拉
- 预期：下拉 option 展示**完整模型名**（含当前模型），每个 option 文本=值，无截断省略
- 排查：①若下拉缺失 → SMOKE_SELECT 项 FAIL，确认 wheel 版本 ≥ 26502c4（T27-B）②下拉 option 文本与值不一致 → 渲染协议错误，对照 render.py select_static 输出

### B5 下拉切换模型
- 操作：下拉选择**非当前**模型
- 预期：收到「✅ 已切换到 [序号] 模型名」回执；再发 `/model` 确认当前模型已变更
- 排查：①回调无响应 → 卡片回调地址/加密配置问题（B9）②回执文本不一致 → 与 test_command.py 断言文本核对（「✅ 已切换到」前缀）

### B6 「切换模型」按钮
- 操作：点击按钮区「切换模型」
- 预期：出现模型选择卡（下拉入口）；当前模型名可见于卡片元信息行
- 排查：①按钮无响应 → 回调 value 协议（T11：`{"hfc":1,"action":"/model"}`）与 fsapp 白名单校验不符，查 fsapp 日志 ②按钮文案不是固定「切换模型」→ 版本未到 T27-G 飞书卡片 2.0 基线（重跑 `scripts/deploy_prod.sh`）；按钮区应为 `body.elements` 内两个顶层 `column_set`，不得回退为 `action` 容器

### B7 /settings 二级菜单
- 操作：发送 `/settings`
- 预期：收到 settings 卡片，含二级操作入口（可继续点击操作）
- 排查：①无卡片 → 白名单缺 `/settings`（verify_production.py WHITELIST 应含）②二级操作无响应 → 同 B9 回调链路

### B8 /status
- 操作：发送 `/status`
- 预期：收到状态卡（运行状态/会话统计等）
- 排查：白名单缺 `/status`；或消息通道未生效（B1）

### B9 加密回调（Encrypt Key 开启时必须验收）
- 操作：开启加密后，点击任意卡片按钮（如 B5 下拉切换）
- 预期：按钮回调正常生效（加密解密链路通）；fsapp 日志无 decrypt 报错
- 排查：①`fs_encrypt_key` 与开放平台「加密策略」页 Encrypt Key 完全一致 ②填后已重启 fsapp ③解密失败看 fsapp 日志（AES 解密协议，参考 memory/feishu_callback_encrypt_sop.md）

### B10 安全拒绝（白名单外用户）
- 操作：用**未授权账号**（不在 `fs_allowed_users` 中，且未开 PUBLIC_ACCESS）向机器人发送 `/status`
- 预期：**静默拒绝**——不回复任何内容（不泄露机器人存在性与命令面）；fsapp 日志有请求记录但不回发卡片
- 排查：①若返回了状态内容 → 检查 `fs_allowed_users`/`PUBLIC_ACCESS` 配置与 fsapp 日志 ②若收到「无权限」类显式回执 → 实现已改，需同步本预期（当前实现为静默 return，无响应体）

### B11 流式会话卡持续更新（补充项）
- 操作：发送 `/llm` 发起会话
- 预期：会话卡随时间更新（thinking → tool → answer），完成后 header 变「已完成」，footer 带统计信息
- 排查：同 B1 长连接；卡不更新时查 fsapp 日志的 card update 错误

## C. 一键验收脚本（只读探测）

```bash
cd <GA根目录>
.venv/bin/python temp/ga-feishu-streaming-card/scripts/verify_production.py
```

- 输出每项 `PASS/FAIL` 清单（7 项：端口监听 / WebSocket 长连接 / 回调端点 / 安全守卫 / 命令白名单 / select_static 冒烟 / 2×2 渲染结构=两个 `column_set`×每行 2 列×每列 1 按钮（schema 2.0，无 `action` 容器）），全 PASS 时退出码 0
- **只读不写**：脚本不修改任何状态、不写任何文件、不读 `mykey.py`、不打印密钥
- 安全守卫项说明：无 token 的 `url_verification` 请求必须被拒（403 token mismatch）＝ `fs_verification_token` 守卫生效；真实带 token 回显由飞书「保存」时的校验自动完成（无需人工）
- 任一项 FAIL：按脚本提示的「排查入口」对应本节 A/B 相应条目处理；渲染相关 FAIL 通常=部署版本未到 T27 系列（重跑 `scripts/deploy_prod.sh`）

## D. 验收记录

| 日期 | 验收人 | 结果（PASS/FAIL） | 备注 |
|------|--------|-------------------|------|
|      |        |                   |      |
