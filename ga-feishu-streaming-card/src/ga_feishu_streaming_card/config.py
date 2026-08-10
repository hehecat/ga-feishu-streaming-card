"""EngineConfig 与配置加载（独立实现）。

设计约定：
- EngineConfig 字段：limits(retention/zombie/history)、card_limits(200/5/28000)、
  delivery(DeliveryPolicy 规则)、transport(fake|http)、http(base_url,timeout_ms=800,
  app_id,app_secret 仅存引用不读取)、enabled(bool, env HFC_ENABLED 默认1)、
  coalesce(delta_ms=250, delta_chars=600, max_pending=128)。
- load_config(path|env)：YAML 文件加载 + 环境变量覆盖。
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
from urllib.parse import urlsplit

import yaml

from .delivery_policy import DeliveryPolicy

# 默认飞书开放平台 base_url（凭证仅存引用，不读取）
DEFAULT_BASE_URL = "https://open.feishu.cn"
DEFAULT_HTTP_TIMEOUT_MS = 800
_ALLOWED_SCHEMES = ("https", "http")


def valid_base_url(url: str) -> bool:
    """base_url 校验：仅 http/https 且带 host（防 file:// 等伪协议/SSRF 拉偏）。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in _ALLOWED_SCHEMES and bool(parts.netloc)


def _coerce_field(f_type: Type, value: Any) -> Any:
    """按 dataclass 字段类型安全强转；失败/非法（NaN/Inf）→ None（调用方跳过用默认）。

    from __future__ import annotations 下 f.type 是字符串（如 "Optional[int]"），
    需解析为真实类型后比较。
    """
    if value is None:
        return None
    # 字符串类型注解 → 真实类型（取 Optional/Union 内层）
    if isinstance(f_type, str):
        inner = f_type[f_type.find("[") + 1 : f_type.rfind("]")] if "[" in f_type else f_type
        f_type = {"int": int, "float": float, "str": str, "bool": bool}.get(inner.strip(), f_type)
    if f_type is int:
        if isinstance(value, bool):
            return None
        if isinstance(value, float):
            return int(value) if math.isfinite(value) else None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if f_type is float:
        if isinstance(value, bool):
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None
    if f_type is str:
        return str(value)
    if f_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return value


@dataclass
class LimitsConfig:
    """会话清理/历史保留参数（配合 lifecycle 使用）。"""

    retention_seconds: float = 3600.0
    zombie_grace_seconds: float = 120.0
    history_limit: int = 50


@dataclass
class CardLimitsConfig:
    """卡片内容安全限额（对应上游 FEISHU_MAX_* 规格）。"""

    max_elements: int = 200
    max_tables: int = 5
    safe_bytes: int = 28000


@dataclass
class HttpConfig:
    """HTTP 投递配置。app_id/app_secret 仅保存引用（字符串），本模块不读取、不落日志。"""

    base_url: str = DEFAULT_BASE_URL
    timeout_ms: int = DEFAULT_HTTP_TIMEOUT_MS
    app_id: str = ""
    app_secret: str = ""


@dataclass
class CoalesceConfig:
    """delta 合并参数（对应上游 coalesce 250ms/600 字符/128 队列）。"""

    delta_ms: int = 250
    delta_chars: int = 600
    max_pending: int = 128


@dataclass
class EngineConfig:
    """引擎总配置。transport ∈ {'fake','http'}。"""

    enabled: bool = True
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    card_limits: CardLimitsConfig = field(default_factory=CardLimitsConfig)
    delivery: DeliveryPolicy = field(default_factory=DeliveryPolicy)
    transport: str = "fake"
    http: HttpConfig = field(default_factory=HttpConfig)
    coalesce: CoalesceConfig = field(default_factory=CoalesceConfig)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EngineConfig":
        """从嵌套 dict 构建（缺失字段取默认；畸形/未知字段防御：过滤+类型强转+NaN/Inf 拒绝）。"""
        if not isinstance(d, dict):
            d = {}

        def _sub(key: str, ctor):
            v = d.get(key)
            if not isinstance(v, dict):
                return ctor()
            allowed = {f.name: f.type for f in fields(ctor)}
            kwargs: Dict[str, Any] = {}
            for k, val in v.items():
                if k not in allowed:
                    continue  # 未知字段忽略（不因多余键炸 TypeError）
                coerced = _coerce_field(allowed[k], val)
                if coerced is None:
                    continue
                if isinstance(coerced, (int, float)) and not isinstance(coerced, bool) and coerced < 0:
                    continue  # 数值负数非法 → 回退默认（P3b：limits 等数值范围校验）
                kwargs[k] = coerced
            return ctor(**kwargs)

        limits = _sub("limits", LimitsConfig)
        card_limits = _sub("card_limits", CardLimitsConfig)
        dv = d.get("delivery")
        delivery = (
            DeliveryPolicy.from_dict(dv) if isinstance(dv, dict) else DeliveryPolicy()
        )
        http = _sub("http", HttpConfig)
        coalesce = _sub("coalesce", CoalesceConfig)
        if not valid_base_url(http.base_url):
            http.base_url = DEFAULT_BASE_URL

        enabled = d.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        elif not isinstance(enabled, bool):
            enabled = bool(enabled)

        transport = str(d.get("transport", "fake")).lower()
        if transport not in ("fake", "http"):
            transport = "fake"

        return cls(
            enabled=enabled,
            limits=limits,
            card_limits=card_limits,
            delivery=delivery,
            transport=transport,
            http=http,
            coalesce=coalesce,
        )

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "EngineConfig":
        """加载配置：path 为 YAML 文件；None 时尝试 env HFC_CONFIG，再尝试 ./config.yaml。

        环境变量覆盖（优先级高于文件）：
        - HFC_ENABLED: '0'/'false' 等 → disabled（默认 '1' → enabled）
        - HFC_TRANSPORT: 'fake'|'http'
        - HFC_BASE_URL / HFC_HTTP_TIMEOUT_MS: HTTP 配置
        """
        raw: Dict[str, Any] = {}
        candidates: List[Path] = []
        if path is not None:
            candidates.append(Path(path))
        else:
            env_cfg = os.environ.get("HFC_CONFIG")
            if env_cfg:
                candidates.append(Path(env_cfg))
            local = Path("config.yaml")
            if local.exists():
                candidates.append(local)

        for cand in candidates:
            if cand.exists():
                with open(cand, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                if isinstance(loaded, dict):
                    raw = loaded
                break

        # T13-E F2：无显式 path 且无任何候选文件命中 → stderr 警告（不回退静默）
        if path is None and not any(cand.exists() for cand in candidates):
            sys.stderr.write(
                "[hfc-config] 未找到配置（HFC_CONFIG / ./config.yaml 均无），使用默认配置；"
                "可用 `hfc install --config <path>` 安装配置或设置 HFC_CONFIG。\n"
            )

        cfg = cls.from_dict(raw)

        # --- 环境变量覆盖 ---
        if "HFC_ENABLED" in os.environ:
            v = os.environ["HFC_ENABLED"].strip().lower()
            cfg.enabled = v in ("1", "true", "yes", "on")
        if "HFC_TRANSPORT" in os.environ:
            v = os.environ["HFC_TRANSPORT"].strip().lower()
            if v in ("fake", "http"):
                cfg.transport = v
        if "HFC_BASE_URL" in os.environ and os.environ["HFC_BASE_URL"].strip():
            url = os.environ["HFC_BASE_URL"].strip().rstrip("/")
            if valid_base_url(url):
                cfg.http.base_url = url
            else:
                cfg.http.base_url = DEFAULT_BASE_URL
        if "HFC_HTTP_TIMEOUT_MS" in os.environ:
            try:
                cfg.http.timeout_ms = int(os.environ["HFC_HTTP_TIMEOUT_MS"])
            except ValueError:
                pass
        return cfg


def load_config(path: Optional[str | Path] = None) -> EngineConfig:
    """配置入口：load_config(path|env)。"""
    return EngineConfig.load(path)
