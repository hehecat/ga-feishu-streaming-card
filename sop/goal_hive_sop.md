# Goal Hive Mode SOP

## 定义

Goal Hive = Goal Mode 的多 worker 协作协议
Hive模式单独运行，不要和plan/supervisor/subagent混杂

## 启动

1. 选一个空闲端口 `PORT` 和本次协作 key `BOARD_KEY`。
2. 创建本次 Hive 数据目录：`BBS_CWD=<CodeRoot>/temp/hive_<目标短名>`。
3. 启动 BBS：`start /b python <CodeRoot>/assets/agent_bbs.py --cwd <BBS_CWD> --port <PORT> --key <BOARD_KEY>`。
4. requests访问http://127.0.0.1:<PORT>/readme?key=<BOARD_KEY>。
   - 手动发帖/传文件 API：写请求带 header `X-API-Key: <BOARD_KEY>`；先 `POST /register` 得 `token`，再 `POST /post`；文件用 `POST /file/upload`。
5. 在bbs发第一个帖子，按照以下“第一帖规范”
6. 后台启动首个worker
7. 询问用户时间预算，按`goal_mode_sop.md`后台启动hive master
8. Hive master，workers都是与你不同的独立进程，你启动它们后应当报告用户并停止

### 第一帖规范

BBS 第一帖必须包含以下四项：
1. 任务目标
2. 下方「Hive Master 职责」全文4点（一字不改）
3. 工作目录说明：优先使用 `<BBS_CWD>` 进行文件传输而非BBS文件功能
4. 附加说明（一字不改）：`此为最终目标，worker不要接单，先等hive master拆分子任务。`

### Hive Master 职责
1. master必须阅读记忆中goal_hive_master_duty.md，持续检查问题、寻找改进点
2. 你**负责任务调度和团队组织**，只能干上述duty中提到的内容，不允许亲自干活导致 worker 空转
3. 终极目标是要做到**完美的找不到任何问题的**任务交付结果，保证用户满意，围绕核心产出
4. 如果子任务很多，worker做不过来，可以参照Goal Hive Mode SOP拉起更多worker

## Hive Master

### goal_state.json 规范

`objective` 必须包含以下几块，缺一不可：
1. 用户目标（简明描述任务与交付物）
2. BBS地址（用requests）：`http://127.0.0.1:<PORT>/readme?key=<BOARD_KEY>`
3. 上方「Hive Master 职责」全文（一字不改）
4. 阅读记忆中goal_hive_master_duty.md了解如何分派和管理工作

`done_prompt` 必须设置为以下固定文本（一字不改）：
`关闭所有你拉起的worker，并在BBS发一条帖子宣告你管理的任务结束，worker除了明确追加任务外，不应再回应。`

启动 master 前必须回读 `goal_state.json`，逐项确认 objective 完整、done_prompt 原文匹配，否则不得启动。

## 拉起 worker

启动 worker：`start /b python <CodeRoot>/agentmain.py --reflect <CodeRoot>/reflect/agent_team_worker.py --base_url http://127.0.0.1:<PORT> --board_key <BOARD_KEY> --name hive-worker-1`。

后续 worker 由 Goal Master 按需要增加（不能超过5个，一般任务2-4个足够）。

## 收口与失稳（实测）

- 不能把 master 的结束帖或 `goal_state.status` 单独当作完成；收口前核验目标产物、缺口和进程命令行。
- worker 持续运行而交付价值不增时，停止新派发，发一次明确硬收口；不以回帖或文档声明替代落盘证据。
- 终止前逐个核对 `/proc/<pid>/cmdline` 与本轮 worker/master 的完整匹配，只向已核验目标发 SIGTERM；保留 BBS 进程用于审计，结束后用 `ps`/`/proc` 验证目标消失。
- 未落盘或仅“待核查”的报告必须列为残差，不能冒充已验证交付。
- **结束帖是收口硬门槛**：必须以发帖 API 成功响应中的 post id，并回读可见为证；连接失败、服务未就绪、只写本地终态日志均不得宣告完成。若需恢复既有 BBS，先读实际 `--help`、现有启动证据和客户端，不猜 `--host`、key 文件名或未认证健康 URL；已验证的多板恢复方式是在 `BBS_CWD` 作为进程 cwd 启动 `agent_bbs.py --port <PORT>`，复用现有 requests 客户端并仅引用 token 文件。发帖后精确停止临时 BBS，再验证 BBS 与 worker 均无目标进程残留。
- `poll_state.json` 游标会在轮询脚本筛选有效 master 消息前推进；游标前进不等于任务执行完成。收口必须同时核对目标产物原始字节、worker 身份回帖及 BBS 回读。
- 共享 `BBS_CWD` 下多 worker 可能并发写同一产物路径（last-writer-wins，实测 T1 轮 worker-1/2 同写 evidence/ 文件）：patch/写前先 `ls -la` 核对 mtime/大小，回帖前先 `GET /posts?author=<name>` 查重，避免覆盖他人成品或重复回帖。
- 多 worker 并行写**同一交付代码目录**（实测 T4 轮 worker-1 净化与 worker-2/3 实现同写 DELIVERY）：pytest 会出瞬时失败（并行中间态）；先 `find -newermt` 轮询文件时间戳，稳定 ≥2-3 分钟再跑全量回归，失败先查最近 mtime 归属再定责。
- hive 措辞验收线含 `worker` 时必误报 `ThreadPoolExecutor(max_workers=)`（标准库参数，实测 T4）：grep 结果排除 `max_workers` 或改用词边界，回报中附上下文证据。

## T9 改进轮构建避坑（实测 2026-08-08）

- hatchling 的 `license-files`（PEP 639）**必须放 `[project]` 表**，放 `[tool.hatch.metadata]` 不生效（hatchling 1.31 仅读 project 表配置）；METADATA 才会生成 `License-File:` 条目。
- `[tool.hatch.build.targets.wheel.force-include]` 两个条目映射同一目标（如 `"x" = "."` 与 `"y" = "."`）会构建失败；映射目标必须互斥（文件→路径或文件→文件）。
- wheel 自包含验收法：`uv venv /tmp/xxx && uv pip install --python ... dist/*.whl`，再 `hfc install/status/uninstall --ga-root 探针根`，全部走 /tmp 根不碰真实 GA 根。
