# ATTRIBUTION.md — 归属声明

## 上游项目

- **名称**：Hermes Feishu Streaming Card（飞书/Lark 流式卡片插件）
- **仓库**：https://github.com/baileyh8/hermes-feishu-streaming-card
- **版本**：v4.2.8（2026-08-05，CHANGELOG.md 头部）
- **许可**：MIT License；Copyright (c) 2026 Hermes Feishu Streaming Card Contributors

## 本插件（ga-feishu-streaming-card）声明

1. **独立实现**：本项目的全部源码为面向 GenericAgent（GA）插件面独立编写，未复制上游任何源码文件、函数体或注释（零复制）。
2. **协议与行为参照**：本插件参考上游**公开协议与行为规格**作为兼容契约，包括：
   - 10 种事件语义（`message.started` / `thinking.delta` / `tool.updated` / `answer.delta` / `message.completed` / `message.failed` / `system.notice` / `interaction.requested` / `interaction.completed` / `interaction.failed`，schema_version=1）；
   - 会话状态机、展示态集合（thinking/in_progress/waiting/completed/failed）、内容安全限额（200 元素 / 5 表格 / 28KB）、生命周期默认值（retention=3600 / zombie=120 / history=50）、投递策略（card/native + 通配 `*?[]` + fail-safe 回 card）与 fail-open 语义。
3. **未包含上游代码副本**：本分发物中不包含上游仓库的任何源码副本；组件取舍详见 `COMPONENT_DECISIONS.md`。
4. **许可合规**：上游为 MIT 许可，允许参考其协议与行为规格进行独立实现与衍生；按 MIT 要求，上游版权与许可声明保留于本文件，并在 `LICENSE` 中并列引用。
5. **用途边界**：本插件仅面向 GenericAgent 运行环境，不改动 GA 源码；`transport: fake` 为默认离线传输，真实飞书发送（`transport: http`）为预留字段、未启用。
6. **T4 命令结果卡**（2026-08-09）：新增 slash 命令 → 飞书命令结果卡能力（`command.py` 映射表 + `bridge.send_command_result_card`），卡片仅展示命令名/标题/安全渲染结果，不含会话与工具敏感信息；HFC 关闭或发送失败时宿主自动回退纯文本。

## 署名（Contributors）

- **W1**（2026-08-09）：T4 命令结果卡（command.py / bridge.send_command_result_card / 命令层接线 / test_command.py），以本文件 MIT 许可发布。

## 上游 MIT 许可原文（保留）

```
MIT License

Copyright (c) 2026 Hermes Feishu Streaming Card Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
