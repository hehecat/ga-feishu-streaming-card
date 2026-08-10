"""投递策略（独立实现）。

设计约定：
- DeliveryPolicy / decide_disposition(chat_id, event) -> 'card' | 'native'
- policy_unavailable 时回退 'card'（fail-open 到卡片：策略不可判时优先保证
  用户能看到卡片内容，而不是静默降级为 native 文本）。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .events import CardEvent


@dataclass
class DeliveryPolicy:
    """投递策略：default('card'|'native') + native_chats（glob 模式，支持 * ? []）。"""

    default: str = "card"
    native_chats: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 非法 default 视为策略不可用 → 回退 'card'
        if self.default not in ("card", "native"):
            self.default = "card"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeliveryPolicy":
        d = d or {}
        chats = d.get("native_chats") or []
        # 防御：非 str 元素转字符串（fnmatch 对非 str 模式会炸）；上限防超长名单
        norm = []
        for c in chats[:100]:
            if c is None:
                continue
            norm.append(c if isinstance(c, str) else str(c))
        return cls(
            default=str(d.get("default", "card")).lower(),
            native_chats=norm,
        )

    def decide_disposition(
        self, chat_id: Optional[str], event: Optional[CardEvent] = None
    ) -> str:
        """判定事件投递形态：'card' 或 'native'。

        规则：
        1. 存在 native_chats 且 chat_id 命中任一模式 → 'native'；
        2. 无 chat_id（策略不可判）→ 'card'（回退）；
        3. 其余 → self.default。
        event 参数保留用于未来按事件类型细分规则；当前仅校验类型合法性。
        """
        if not isinstance(event, CardEvent):
            return "card"  # 事件缺失/非法 → 策略不可用 → 回退 card
        if not self.native_chats:
            return self.default
        if not chat_id:
            # 有 native 名单但缺 chat_id，无法可靠判定 → 回退 card
            return "card"
        for pattern in self.native_chats:
            if fnmatch.fnmatch(str(chat_id), pattern):
                return "native"
        return self.default
