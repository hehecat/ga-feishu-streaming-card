"""文本处理（引擎核心，独立实现）。

设计约定：normalize_stream_text / strip_think_tags / split_markdown_blocks /
count_markdown_tables；strip_think_tags 必须处理**跨 chunk 拆分**的 think 标签
（开标签/闭合标签被切成残片分布在多个 chunk，任一 chunk 单独处理后不得泄漏
thinking 内容到可见文本）。

strip_think_tags 处理顺序（自洽定义）：
1. 完整块 ```think ... ```：整块删除（含内容）；
2. 未闭合开标签 ```think ... EOF：删除到文本末尾；
3. 行尾开标签前缀残片（` 、`` 、```t / ```th / ```thi / ```thin / ```think）：
   只删除残片本身，保留前缀可见文本（如 "abc ```thi" -> "abc "）；
4. 孤立闭合围栏残片：剩余文本中 ``` 出现奇数次时，判定为 thinking 残留的
   闭合残片，删除最后一个 ``` 及其之前的所有内容（如 "secret``` rest" -> " rest"）。
"""

from __future__ import annotations

import re
from typing import List

_THINK_BLOCK = re.compile(r"(?:^|\n)?```think\b.*?```(?=\n|$)", re.DOTALL)
_UNCLOSED_THINK = re.compile(r"```think\b[^\n]*(?:\n(?!```).*)*$", re.DOTALL)
# 行首开标签前缀残片（含 ` / `` / ```t / ```th / ```thi / ```thin / ```think）：整行删除
_OPEN_FRAGMENT_LINE = re.compile(
    r"(?:^|\n)(?:`{1,2}|```(?:t(?:h(?:i(?:n(?:k)?)?)?)?))\s*$", re.MULTILINE
)
# 行中开标签前缀残片：只删残片本身（如 "abc ```thi" -> "abc "）；
# (?!\s) 保证不误伤后随空格/换行的完整开标签（```think\n / ```think x）
_OPEN_FRAGMENT_AT_EOL = re.compile(r"```(?:t(?:h(?:i(?:n(?:k)?)?)?)?)(?!\s)", re.MULTILINE)
_FENCE = re.compile(r"```")

# D3: HTML 形式 <think> 标签（大小写不敏感，容忍属性）
_HTML_THINK_FULL_RE = re.compile(
    r"<think\b[^>]*>.*?</think\s*>",
    re.DOTALL | re.IGNORECASE,
)
# 未闭合块：开标签后直到文本末尾（防泄漏优先）
_HTML_THINK_UNCLOSED_RE = re.compile(
    r"<think\b[^>]*>.*$",
    re.DOTALL | re.IGNORECASE,
)
# 孤立标签残片（闭合标签 / 自闭合）
_HTML_THINK_TAG_RE = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)


def strip_think_tags(text: str) -> str:
    """剥离 think 标签（含跨 chunk 残片），返回可见文本。

    处理顺序（自洽定义）：
    1. 完整块 ```think ... ```（行首块连同其独占行，行中块只删块体+尾随换行）；
    2. 未闭合开标签 ```think ... EOF：删除到文本末尾；
    3. 行首/行尾开标签前缀残片（` `` ```t..```think）：行首残片整行删，
       行中残片只删残片本身；
    4. 孤立闭合围栏残片：剩余 ``` 为奇数个时判定为 think 残留的闭合端，
       删除最后一个 ``` 及其之前的内容；
    5. 收尾：去除因删除产生的首尾空行。
    """
    if not text:
        return text
    # 1) 完整 think 块（块体 + 块后的换行；行首块的前导换行也一并删除）
    out = _THINK_BLOCK.sub("", text)
    # 2) 未闭合 think 块（```think 之后直到结尾）
    out = _UNCLOSED_THINK.sub("", out)
    # 3) 开标签残片：先整行（行首），后行中
    out = _OPEN_FRAGMENT_LINE.sub("", out)
    out = _OPEN_FRAGMENT_AT_EOL.sub("", out)
    # 4) 孤立闭合围栏残片（奇数个 ``` 视为 thinking 残留的闭合端）
    fences = _FENCE.findall(out)
    if len(fences) % 2 == 1:
        last = out.rfind("```")
        out = out[last + 3 :]
        out = out.lstrip("\n")
    # 5) 因删除产生的首尾空行
    return out.strip("\n")


def strip_html_think_tags(text: str) -> str:
    """剥离模型 HTML 形式 `<think>` thinking 标签（D3 防泄漏）。

    处理顺序：完整块 `<think>...</think>` 整块删除；未闭合开标签连同其后内容删除；
    孤立闭合/自闭合标签仅删标签本身。大小写不敏感，开标签容忍属性。
    """
    out = _HTML_THINK_FULL_RE.sub("", text)
    out = _HTML_THINK_UNCLOSED_RE.sub("", out)
    out = _HTML_THINK_TAG_RE.sub("", out)
    return out


def normalize_stream_text(text: str) -> str:
    """流式文本规范化：统一换行、剥离零宽/控制字符、去首尾空白、
    压缩连续空行（>=3 个 \\n 压为 \\n\\n），不破坏 markdown 表格等语义。"""
    if not text:
        return ""
    # D3: 先剥离 HTML 形式 thinking 标签（防泄漏），再走既有规范化
    out = strip_html_think_tags(text).replace("\r\n", "\n").replace("\r", "\n")
    # 剥离零宽字符与 BOM
    out = out.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    # 剥离 NUL 等控制字符（保留 \n \t）
    out = "".join(ch for ch in out if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    out = out.strip()
    # 连续 3+ 空行压为 2（保留段落分隔，去除流式重复空行）
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


_CODE_FENCE = re.compile(r"```")


def _split_paragraphs(text: str) -> List[str]:
    """按空行切段落；代码围栏整体视为一个块（不被空行切开）。"""
    lines = text.split("\n")
    blocks: List[str] = []
    buf: List[str] = []
    in_code = False

    def flush() -> None:
        if buf:
            blocks.append("\n".join(buf))
            buf.clear()

    for line in lines:
        stripped = line.strip()
        if _CODE_FENCE.search(line):
            buf.append(line)
            if stripped.startswith("```"):
                in_code = not in_code
            continue
        if not stripped and not in_code:
            flush()
            continue
        buf.append(line)
    flush()
    return blocks


def split_markdown_blocks(
    text: str,
    max_bytes: int = 28000,
    max_blocks: int = 200,
) -> List[str]:
    """把长文本切成不超过 max_bytes 的块（UTF-8 字节计），最多 max_blocks 块。

    切分策略：先按段落（\n\n / 空行）切；单个超长段落再按行切；代码围栏块
    保持原子（不切开，避免 think/代码块跨块）。空输入返回 []。
    """
    if not text:
        return []
    paragraphs = _split_paragraphs(text)
    blocks: List[str] = []
    for para in paragraphs:
        if len(para.encode("utf-8")) <= max_bytes:
            blocks.append(para)
            continue
        # 超长段落：按行重组，每行追加直到超限即收块
        cur: List[str] = []
        cur_bytes = 0
        for line in para.split("\n"):
            line_bytes = len(line.encode("utf-8")) + 1  # 含换行
            if cur and cur_bytes + line_bytes > max_bytes:
                blocks.append("\n".join(cur))
                cur = []
                cur_bytes = 0
            cur.append(line)
            cur_bytes += line_bytes
        if cur:
            blocks.append("\n".join(cur))
    # 极限情况：单行仍超限则强制截断（保语义优先，极少发生）
    final: List[str] = []
    for b in blocks:
        if len(b.encode("utf-8")) > max_bytes:
            raw = b.encode("utf-8")
            cut = raw[: max_bytes - 3]
            # 避免切断多字节字符
            while cut and (cut[-1] & 0xC0) == 0x80:
                cut = cut[:-1]
            final.append(cut.decode("utf-8", errors="ignore") + "...")
        else:
            final.append(b)
    return final[:max_blocks]


_TABLE_HEADER = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def count_markdown_tables(text: str) -> int:
    """统计 markdown 表格块数量。

    表格 = 至少两行连续的 '|' 行，其中第二行是分隔行（--- / :---: 等）。
    """
    n = 0
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if _TABLE_HEADER.match(lines[i]) and i + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[i + 1]):
            n += 1
            i += 2
            # 跳过表格数据行
            while i < len(lines) and _TABLE_HEADER.match(lines[i]):
                i += 1
            continue
        i += 1
    return n
