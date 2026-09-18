"""AstrBot 指令自动同步到 QQ 官方「指令面板」。

参考 telegram/discord 适配器的指令注册实现：从 star_handlers_registry 收集
系统与所有已激活插件的指令（含别名），按官方 /v2/panels 约束（name ≤14 字符
宽度、desc ≤30、单面板 ≤20 元素）分片生成面板，以 remark 标记（MARKER）识别
本插件托管的面板，做幂等差异同步（创建缺失/更新不符/删除多余）。

同步由协调循环驱动：refresh() 里 request_sync() 排队后台任务；指令签名不变
且实例集合不变时零网络调用，插件装卸导致的指令集变化在一个巡检周期内收敛。
失败按实例/场景隔离并记录，下一周期自动重试。本模块零 astrbot 顶层依赖
（collect_commands 惰性导入），可直接离线测试。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "MARKER",
    "MAX_ITEMS_PER_PANEL",
    "NAME_WIDTH_LIMIT",
    "DESC_WIDTH_LIMIT",
    "visual_len",
    "normalize_commands",
    "build_panels",
    "collect_commands",
    "collect_command_entries",
    "OverrideStore",
    "CommandPanelSyncer",
]

MARKER = "astrbot-cmdpanel"
"""托管面板 remark 前缀：「astrbot-cmdpanel {序号}/{总数}」，同步时按此前缀识别。"""

MAX_PANELS = 20            # 官方：单机器人最多 20 个指令面板
MAX_ITEMS_PER_PANEL = 20   # 官方：单面板最多 20 个元素
NAME_WIDTH_LIMIT = 14      # 官方：name 最多 14 字符（中文按 2 计）
DESC_WIDTH_LIMIT = 30      # 官方：desc 最多 30 字符（中文按 2 计）

SCOPES = ("c2c", "group", "channel", "dm")
DEFAULT_SCOPES = ["c2c", "group"]


def visual_len(s: str) -> int:
    """官方字符宽度：东亚宽字符按 2 计，其余按 1 计。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _truncate(s: str, limit: int) -> str:
    out, width = [], 0
    for ch in s:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if width + w > limit:
            break
        out.append(ch)
        width += w
    return "".join(out)


def normalize_commands(raw: list[tuple[str, str, bool]]) -> list[dict]:
    """(指令名, 描述, 仅管理员) 原始列表 → 符合官方约束的 PanelItem 列表。

    面板项 name 会填入聊天输入框，统一带 "/" 前缀以命中默认唤醒前缀；超宽或
    含空白字符的指令无法进面板。同名先去重（先注册优先），输出按名称排序。
    """
    items: dict[str, dict] = {}
    for name, desc, only_admin in raw:
        if not name or any(ch.isspace() for ch in name):
            continue
        item_name = f"/{name}"
        if visual_len(item_name) > NAME_WIDTH_LIMIT:
            continue
        text = _truncate((desc or "").strip() or f"指令: {name}", DESC_WIDTH_LIMIT)
        items.setdefault(item_name, {
            "type": "command",
            "name": item_name,
            "desc": text,
            "only_admin": bool(only_admin),
        })
    return [items[k] for k in sorted(items)]


def build_panels(items: list[dict]) -> list[dict]:
    """按单面板 20 元素分片；remark 带 MARKER 与序号，供同步时识别托管面板。"""
    chunks = [
        items[i:i + MAX_ITEMS_PER_PANEL]
        for i in range(0, len(items), MAX_ITEMS_PER_PANEL)
    ][:MAX_PANELS]
    total = len(chunks)
    return [
        {"items": chunk, "remark": f"{MARKER} {i + 1}/{total}"}
        for i, chunk in enumerate(chunks)
    ]


def collect_command_entries(disabled: set | frozenset | None = None) -> list[dict]:
    """收集系统与已激活插件的全部指令条目（含插件归属与开关态）。

    页面展示与面板同步共用的单一事实来源；disabled 为 "module:指令名"
    键集合（见 OverrideStore.disabled_set）。条目字段：plugin/module/name/
    desc/only_admin/is_alias/panel_ok/enabled。

    与 telegram 适配器 collect_commands 同策略：跳过未激活/停用处理器与子指令；
    指令组登记组名；带管理员权限过滤器的指令标记 only_admin。
    """
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.filter.permission import (
        PermissionType,
        PermissionTypeFilter,
    )
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    admin_types = _admin_permission_types(PermissionType)
    disabled = disabled or frozenset()
    entries: list[dict] = []
    for handler_md in star_handlers_registry:
        module = handler_md.handler_module_path
        meta = star_map.get(module)
        if meta is None or not meta.activated or not handler_md.enabled:
            continue
        only_admin = any(
            isinstance(f, PermissionTypeFilter) and f.permission_type in admin_types
            for f in handler_md.event_filters
        )
        plugin = meta.display_name or meta.name or module
        desc = handler_md.desc or ""
        for f in handler_md.event_filters:
            if isinstance(f, CommandFilter) and f.command_name:
                if f.parent_command_names and f.parent_command_names != [""]:
                    continue   # 子指令不单独占面板位（同 telegram/discord）
                names = [(f.command_name, False)] + [(a, True) for a in sorted(f.alias)]
            elif isinstance(f, CommandGroupFilter) and not f.parent_group:
                names = [(f.group_name, False)]
            else:
                continue
            for name, is_alias in names:
                if not name or any(ch.isspace() for ch in name):
                    continue
                entries.append({
                    "plugin": plugin,
                    "module": module,
                    "name": name,
                    "desc": desc,
                    "only_admin": only_admin,
                    "is_alias": is_alias,
                    "panel_ok": visual_len(f"/{name}") <= NAME_WIDTH_LIMIT,
                    "enabled": f"{module}:{name}" not in disabled,
                })
    return entries


def collect_commands(disabled: set | frozenset | None = None) -> list[dict]:
    """收集应注册到指令面板的指令：启用且符合官方约束，同名先去重。"""
    raw: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for e in collect_command_entries(disabled):
        if not e["enabled"] or not e["panel_ok"] or e["name"] in seen:
            continue
        seen.add(e["name"])
        raw.append((e["name"], e["desc"], e["only_admin"]))
    return normalize_commands(raw)


class OverrideStore:
    """指令开关覆盖表：data_dir 下 JSON 持久化。

    只记录被关闭的指令（键 "module:指令名"，缺省启用）；文件不存在或损坏
    时按空表处理，落盘失败不影响内存态（下次变更重试）。
    """

    FILE_NAME = "cmdpanel_overrides.json"

    def __init__(self, data_dir=None):
        self._path = Path(data_dir) / self.FILE_NAME if data_dir else None
        self._disabled: set[str] = set()
        self.load()

    def load(self) -> None:
        self._disabled = set()
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._disabled = {str(k) for k, v in data.items() if v is False}
        except Exception:
            self._disabled = set()

    def save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: False for k in sorted(self._disabled)}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    def disabled_set(self) -> frozenset:
        return frozenset(self._disabled)

    def set_enabled(self, key: str, enabled: bool) -> None:
        if enabled:
            self._disabled.discard(key)
        else:
            self._disabled.add(key)
        self.save()


def _admin_permission_types(permission_type_cls) -> tuple:
    """提取「管理员类」权限成员，兼容旧版枚举（v4.27 仅有 ADMIN/MEMBER）。"""
    return tuple(
        m for n in ("ADMIN", "GROUP_ADMIN", "SHARED_GROUP_ADMIN")
        if (m := getattr(permission_type_cls, n, None)) is not None
    )


def _item_key(item: dict) -> tuple:
    return (
        item.get("type") or "command",
        item.get("name") or "",
        item.get("desc") or "",
        bool(item.get("only_admin", False)),
    )


def _panel_seq(record: dict) -> int:
    """托管面板 remark 序号（「MARKER i/n」），无法解析的排最前。"""
    remark = str((record.get("panel") or {}).get("remark") or "")
    tail = remark[len(MARKER):].strip().split("/")[0] if remark.startswith(MARKER) else ""
    return int(tail) if tail.isdigit() else 0


class CommandPanelSyncer:
    """指令面板幂等同步器：协调循环驱动，签名不变零网络调用。

    - 键控按机器人身份（robot prefix）而非实例：同 AppID 多实例共享同一
      机器人的面板，只同步一次；
    - 配置关闭时摘除全部托管面板并清空签名缓存；
    - 单实例/场景失败不影响其他目标，下一巡检周期按签名差异自动重试。
    """

    def __init__(self, svc, *, collect: Callable[[], list[dict]] = collect_commands,
                 logger=None):
        self._svc = svc
        self._collect = collect
        self._logger = logger
        self._task: asyncio.Task | None = None
        self._synced: dict[tuple[str, str], str] = {}   # (robot_prefix, scope) -> 签名
        self.last_result = ""

    def _log(self, level: str, msg: str) -> None:
        log = self._logger
        if log is not None:
            getattr(log, level)(f"[qqoffice_expand] {msg}")

    # -- 配置 --

    def enabled(self) -> bool:
        return bool(self._svc.config.get("command_panel_sync", True))

    def scopes(self) -> list[str]:
        raw = self._svc.config.get("command_panel_scopes") or DEFAULT_SCOPES
        return [s for s in raw if s in SCOPES] or DEFAULT_SCOPES

    # -- 调度 --

    def request_sync(self) -> None:
        """由 refresh()（同步上下文）调用；无在途任务时排队一个后台同步。"""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    def force_sync(self) -> None:
        """清空签名缓存，下一任务无条件全量比对（网络调用发生在任务内）。"""
        self._synced.clear()
        self.request_sync()

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "scopes": self.scopes(),
            "synced": sorted(f"{p}|{s}" for p, s in self._synced),
            "running": bool(self._task and not self._task.done()),
            "last_result": self.last_result,
        }

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # -- 同步主流程 --

    async def _run(self) -> None:
        try:
            if not self.enabled():
                await self._cleanup()
                return
            items = self._collect()
            scopes = self.scopes()
            sig = hashlib.sha1(
                repr((scopes, [_item_key(i) for i in items])).encode()
            ).hexdigest()
            panels = build_panels(items)
            done, failed = [], []
            seen: set[str] = set()
            for route in list(self._svc.routes.routes.values()):
                prefix = route.robot_key.prefix()
                if prefix in seen:
                    continue   # 同身份多实例共享同一机器人面板
                seen.add(prefix)
                for scope in scopes:
                    if self._synced.get((prefix, scope)) == sig:
                        continue
                    try:
                        view = self._svc.instance(route.platform_id)
                        await self._sync_scope(view, scope, panels)
                    except Exception as exc:
                        failed.append(f"{prefix}/{scope}")
                        self._log("warning", f"指令面板同步失败 {prefix}/{scope}: {exc!r}")
                        continue
                    self._synced[(prefix, scope)] = sig
                    done.append(f"{prefix}/{scope}")
            # 已下线身份的签名记录清理（面板留在官方侧，本地不再跟踪）
            for key in list(self._synced):
                if key[0] not in seen:
                    self._synced.pop(key, None)
            if done or failed:
                self.last_result = (
                    f"同步 {len(done)} 个目标（{len(items)} 条指令/{len(panels)} 面板）"
                    + (f"，失败 {len(failed)}: {', '.join(failed)}" if failed else "")
                )
                self._log("info", f"指令面板{self.last_result}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_result = f"同步异常: {exc!r}"
            self._log("error", f"指令面板{self.last_result}")

    async def _list_managed(self, view, scope: str) -> list[dict]:
        """分页拉取该场景下本插件托管的面板（remark 前缀识别），按序号排序。"""
        managed: list[dict] = []
        cursor = ""
        while True:
            resp = await view.manage.panel_list(scope, cursor=cursor)
            for rec in resp.get("records") or []:
                remark = str((rec.get("panel") or {}).get("remark") or "")
                if remark.startswith(MARKER):
                    managed.append(rec)
            cursor = resp.get("next_cursor") or ""
            if not cursor or resp.get("is_end", True):
                break
        managed.sort(key=_panel_seq)
        return managed

    async def _sync_scope(self, view, scope: str, panels: list[dict]) -> None:
        """单实例单场景幂等对齐：更新不符、创建缺失、删除多余。"""
        existing = await self._list_managed(view, scope)
        for i, desired in enumerate(panels):
            if i < len(existing):
                cur = existing[i]
                cur_items = [_item_key(x)
                             for x in (cur.get("panel") or {}).get("items") or []]
                if cur_items != [_item_key(x) for x in desired["items"]]:
                    await view.manage.panel_update(cur["panel_id"], desired)
            else:
                await view.manage.panel_create(scope, desired, target_type="all")
        for extra in existing[len(panels):]:
            await view.manage.panel_delete(extra["panel_id"])

    async def _cleanup(self) -> None:
        """开关关闭：摘除已知身份的全部托管面板，清空签名缓存。"""
        if not self._synced:
            return
        targets: dict[str, Any] = {}
        for route in list(self._svc.routes.routes.values()):
            prefix = route.robot_key.prefix()
            if any(p == prefix for p, _ in self._synced) and prefix not in targets:
                try:
                    targets[prefix] = self._svc.instance(route.platform_id)
                except Exception:
                    continue
        for prefix, view in targets.items():
            for scope in {s for p, s in self._synced if p == prefix}:
                try:
                    for rec in await self._list_managed(view, scope):
                        await view.manage.panel_delete(rec["panel_id"])
                except Exception as exc:
                    self._log("warning", f"指令面板清理失败 {prefix}/{scope}: {exc!r}")
                    continue
                self._synced.pop((prefix, scope), None)
        self._log("info", "指令面板同步已关闭，托管面板已清理")
