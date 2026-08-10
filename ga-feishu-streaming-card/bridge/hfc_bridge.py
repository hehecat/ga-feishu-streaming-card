# -*- coding: utf-8 -*-
"""hfc_bridge.py — GA 插件：GA hooks → 飞书流式卡片桥。

设计约定：
- 单文件、无第三方依赖（仅标准库）；安装时复制到 GA plugins/，import 名 plugins.hfc_bridge。
- 顶层执行：读 env HFC_ENABLED 与 .hfc_config.json；停用则直接 return（无副作用）。
- sys.path 注入 DELIVERY/src（engine_root：.hfc_config.json 或 env HFC_ENGINE_ROOT）。
- `from ga_feishu_streaming_card import bridge, config`；`from plugins import hooks as _ph`。
- 注册 7 事件回调（turn_before 注册但不产生事件；llm_before 不注册）；
  回调内只调 bridge.emit_from_ga_locals_threadsafe(ctx, cfg)，全部 try/except（fail-open）。
- `_uninstall()`：unregister 全部回调 + 停线程桥（供运行时停用/测试）。
"""
import json
import os
import sys

_REGISTERED = []  # [(event, callback)]
_CFG = None
_bridge = None


def is_active():
    """Return whether the HFC bridge is fully initialized in this process."""
    return _CFG is not None and _bridge is not None


def send_command_result_card(chat_id, command, content, reply_to=None, metadata=None, cfg=None):
    """宿主命令路径出口：转发到 engine bridge 的命令结果卡（fsapp._reply 契约）。

    与 engine bridge.send_command_result_card 同签名；_bridge 未就绪时返回
    False（fsapp 回退纯文本），绝不抛异常。cfg 缺省时由 engine 侧按
    懒创建/宿主 transport 兜底（冷启动命令如重启后直接 /new）。
    """
    if _bridge is None:
        return False
    try:
        return _bridge.send_command_result_card(
            chat_id, command, content, reply_to=reply_to, metadata=metadata, cfg=cfg
        )
    except Exception as e:
        sys.stderr.write(f"[hfc_bridge] send_command_result_card error: {e}\n")
        return False


def _read_json_config():
    candidates = []
    env_path = os.environ.get("HFC_CONFIG_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.append(os.path.join(os.getcwd(), ".hfc_config.json"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), ".hfc_config.json"))  # plugins/..
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def _env_disabled():
    v = os.environ.get("HFC_ENABLED", "").strip().lower()
    return v in ("0", "false", "no", "off", "disabled")


def _uninstall():
    """运行时停用：unregister 全部回调 + 停线程桥。"""
    try:
        from plugins import hooks as _ph
        for _ev, _cb in list(_REGISTERED):
            try:
                _ph.unregister(_ev, _cb)
            except Exception:
                pass
        _REGISTERED.clear()
    except Exception as e:
        sys.stderr.write(f"[hfc_bridge] uninstall unregister failed: {e}\n")
    if _bridge is not None:
        try:
            _bridge.shutdown()
        except Exception:
            pass


# ---------------- 顶层执行（import 时） ----------------
if not _env_disabled():
    _conf = _read_json_config()
    if _conf.get("enabled", True):
        engine_root = _conf.get("engine_root") or os.environ.get("HFC_ENGINE_ROOT")
        if engine_root and engine_root not in sys.path:
            sys.path.insert(0, engine_root)
        try:
            from ga_feishu_streaming_card import bridge as _bridge
            from ga_feishu_streaming_card import config as _hfc_config
            _cfg_obj = _hfc_config.load_config()
            if getattr(_cfg_obj, "enabled", True):
                _CFG = _cfg_obj
                _chat = _conf.get("chat_id") or os.environ.get("HFC_CHAT_ID")
                _skey = str(_conf.get("session_key", "default"))
                if _chat:
                    try:
                        _bridge.register_session_chat(_skey, _chat)
                    except Exception:
                        pass
            else:
                _CFG = None
        except Exception as e:
            sys.stderr.write(f"[hfc_bridge] engine import failed: {e} (fail-open, 插件停用)\n")
            _CFG = None

if _CFG is not None:
    try:
        from plugins import hooks as _ph

        def _make_callback(event_name):
            def _cb(ctx):
                try:
                    _ctx = dict(ctx)
                    _ctx["_hfc_event"] = event_name
                    # fsapp 为每条用户消息设置唯一 task_id；同一任务的
                    # agent/tool hooks 均可经 handler.parent 取得同一 parent。
                    _handler = _ctx.get("handler") or _ctx.get("self")
                    _parent = getattr(_handler, "parent", None)
                    _task_id = getattr(_parent, "_fs_active_task_id", None)
                    if _task_id:
                        _ctx["_hfc_conversation_id"] = f"{_skey}:{_task_id}"
                        # fsapp exposes the real per-message receiver. Prefer it
                        # over an optional static chat_id from install config.
                        _receiver = getattr(_parent, "_fs_active_receive_id", None)
                        if _receiver or _chat:
                            _ctx["_hfc_chat_id"] = _receiver or _chat
                    _bridge.emit_from_ga_locals_threadsafe(_ctx, _CFG)
                except Exception as e:
                    sys.stderr.write(f"[hfc_bridge] {event_name} callback error: {e}\n")
            return _cb

        for _ev in ("agent_before", "tool_before", "tool_after", "turn_before",
                    "llm_after", "turn_after", "agent_after"):
            if hasattr(_ph, "register"):
                _cb = _make_callback(_ev)
                try:
                    _ph.register(_ev)(_cb)
                    _REGISTERED.append((_ev, _cb))
                except Exception as e:
                    sys.stderr.write(f"[hfc_bridge] register {_ev} failed: {e}\n")
    except Exception as e:
        sys.stderr.write(f"[hfc_bridge] hooks import failed: {e}\n")
