"""包装关系审计:按输入顺序执行装入 / 取出操作,维护实物间的直接父子关系。

实物由 GTIN + AI 21 序列号唯一标识。操作按输入顺序逐条执行,
只有成功操作才改变包装关系,失败操作按原下标记录原因:

- 父件 / 子件扫描非法或缺少 AI 21 序列号:操作不执行;
- 装入:子件须尚无父件,且不能是父件本身或父件的祖先(防止循环嵌套);
- 取出:指定的父件须为子件当前的直接父件。
"""

from __future__ import annotations

from .gs1 import ScanParseError, parse_scan
from .models import (
    PackingAuditRequest,
    PackingAuditResponse,
    PackingItem,
    PackingItemRef,
    PackingOperationError,
)

_ItemKey = tuple[str, str]  # (gtin, serial)


def _fmt_item(key: _ItemKey) -> str:
    gtin, serial = key
    return f"(GTIN '{gtin}',序列号 '{serial}')"


def _parse_item(
    scan: str, role: str, index: int, errors: list[PackingOperationError]
) -> _ItemKey | None:
    """解析一端扫描串为实物标识;失败时按原下标记录原因并返回 None。"""
    try:
        parsed = parse_scan(scan)
    except ScanParseError as exc:
        errors.append(PackingOperationError(index=index, reason=f"{role}扫描非法: {exc.reason}"))
        return None
    if parsed.serial is None:
        errors.append(
            PackingOperationError(
                index=index, reason=f"{role}扫描串缺少 AI 21 序列号,无法识别实物"
            )
        )
        return None
    return (parsed.gtin, parsed.serial)


def _is_ancestor(ancestor: _ItemKey, key: _ItemKey, parent_of: dict[_ItemKey, _ItemKey]) -> bool:
    """ancestor 是否为 key 的祖先(沿直接父件链向上;不含 key 本身)。"""
    current = parent_of.get(key)
    while current is not None:
        if current == ancestor:
            return True
        current = parent_of.get(current)
    return False


def audit_packing(request: PackingAuditRequest) -> PackingAuditResponse:
    """按输入顺序执行装入 / 取出操作,返回成功操作涉及实物的最终包装关系。"""
    parent_of: dict[_ItemKey, _ItemKey] = {}  # 子件 -> 直接父件
    children_of: dict[_ItemKey, set[_ItemKey]] = {}  # 父件 -> 直接子件集合
    involved: set[_ItemKey] = set()  # 成功操作涉及的实物
    errors: list[PackingOperationError] = []

    for index, operation in enumerate(request.operations):
        parent = _parse_item(operation.parent_scan, "父件", index, errors)
        if parent is None:
            continue
        child = _parse_item(operation.child_scan, "子件", index, errors)
        if child is None:
            continue

        if operation.action == "pack":
            if child == parent:
                errors.append(PackingOperationError(index=index, reason="子件不能是父件本身"))
                continue
            if child in parent_of:
                errors.append(
                    PackingOperationError(
                        index=index,
                        reason=f"子件已有直接父件 {_fmt_item(parent_of[child])},装入前须先取出",
                    )
                )
                continue
            if _is_ancestor(child, parent, parent_of):
                errors.append(
                    PackingOperationError(
                        index=index, reason="子件是父件的祖先容器,装入会形成循环"
                    )
                )
                continue
            parent_of[child] = parent
            children_of.setdefault(parent, set()).add(child)
        else:  # unpack
            current = parent_of.get(child)
            if current is None:
                errors.append(PackingOperationError(index=index, reason="子件当前无父件,无法取出"))
                continue
            if current != parent:
                errors.append(
                    PackingOperationError(
                        index=index,
                        reason=f"子件当前直接父件为 {_fmt_item(current)},与指定父件不符",
                    )
                )
                continue
            del parent_of[child]
            children_of[parent].discard(child)

        involved.add(parent)
        involved.add(child)

    def outermost_of(key: _ItemKey) -> _ItemKey:
        """沿父件链向上找到最外层容器;无父件时为自身。"""
        while key in parent_of:
            key = parent_of[key]
        return key

    items: list[PackingItem] = []
    for gtin, serial in sorted(involved):
        key = (gtin, serial)
        parent_key = parent_of.get(key)
        outermost = outermost_of(key)
        items.append(
            PackingItem(
                gtin=gtin,
                serial=serial,
                parent=(
                    PackingItemRef(gtin=parent_key[0], serial=parent_key[1])
                    if parent_key is not None
                    else None
                ),
                children=[
                    PackingItemRef(gtin=child_gtin, serial=child_serial)
                    for child_gtin, child_serial in sorted(children_of.get(key, ()))
                ],
                outermost=PackingItemRef(gtin=outermost[0], serial=outermost[1]),
            )
        )
    errors.sort(key=lambda error: error.index)
    return PackingAuditResponse(items=items, errors=errors)
