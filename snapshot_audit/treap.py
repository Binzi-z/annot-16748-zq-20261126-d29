"""持久化哈希 Treap——快照树的纯函数式实现。

为什么选择哈希 Treap
--------------------
普通 BST 的形状取决于插入顺序：同样的最终键值集合，两个会场若以不同顺序
导入事件，根哈希会不同，无法作为"历史一致"的证据。哈希 Treap 的节点优先级
仅由键的哈希决定（与导入顺序无关），且堆性质唯一确定树形：*相同键值集合
必然得到相同的根哈希*，而更新又是纯函数式的（写新节点、复用旧子树），
天然保留全部历史快照。

节点负载（存入 PackStore 的 blob）
- 叶子：{"k":"leaf","key":str,"value":...,"prio":int}
- 内部：{"k":"node","prio":int,"key":str,"value":...,"left":digest|None,
         "right":digest|None}
None 表示空树，其"根摘要"由空树摘要常量表示。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

from .model import OP_DELETE, OP_UPSERT, digest
from .store import PackStore

EMPTY_ROOT = digest({"empty": "treap"})  # 空树的固定摘要


def priority_of(key: str) -> int:
    """键的确定性优先级：仅取决于键本身，与事件到达顺序无关。"""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")


def _higher(prio_a: int, key_a: str, prio_b: int, key_b: str) -> bool:
    """大顶堆；优先级相同按键名决胜，保证跨进程完全确定。"""
    if prio_a != prio_b:
        return prio_a > prio_b
    return key_a < key_b


class PersistentTreap:
    def __init__(self, store: PackStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------
    def _write_leaf(self, key: str, value: Any) -> str:
        return self.store.put(
            {"k": "leaf", "key": key, "value": value, "prio": priority_of(key)}
        )

    def _write_node(
        self, key: str, value: Any, prio: int, left: str | None, right: str | None
    ) -> str:
        return self.store.put(
            {
                "k": "node",
                "key": key,
                "value": value,
                "prio": prio,
                "left": left,
                "right": right,
            }
        )

    def _is_leaf(self, blob: dict[str, Any]) -> bool:
        return blob["k"] == "leaf"

    # ------------------------------------------------------------------
    # 纯函数式旋转（复制节点，绝不改写历史 blob）
    # ------------------------------------------------------------------
    def _rotate_right(self, node: dict[str, Any]) -> str:
        left = self.store.get(node["left"])
        new_right = self._write_node(
            node["key"], node["value"], node["prio"], left["right"], node["right"]
        )
        return self._write_node(
            left["key"], left["value"], left["prio"], left["left"], new_right
        )

    def _rotate_left(self, node: dict[str, Any]) -> str:
        right = self.store.get(node["right"])
        new_left = self._write_node(
            node["key"], node["value"], node["prio"], node["left"], right["left"]
        )
        return self._write_node(
            right["key"],
            right["value"],
            right["prio"],
            new_left,
            right["right"],
        )

    # ------------------------------------------------------------------
    # 插入 / 更新
    # ------------------------------------------------------------------
    def upsert(self, root: str, key: str, value: Any) -> str:
        prio = priority_of(key)
        return self._upsert(root, key, value, prio)

    def _upsert(self, root: str | None, key: str, value: Any, prio: int) -> str:
        if root is None or root == EMPTY_ROOT:
            return self._write_leaf(key, value)
        node = self.store.get(root)
        if key == node["key"]:
            # 键已存在：仅替换值，形状与优先级不变（复用两个子树）
            if self._is_leaf(node):
                return self._write_leaf(key, value)
            return self._write_node(
                key, value, node["prio"], node["left"], node["right"]
            )
        if key < node["key"]:
            new_left = self._upsert(node.get("left"), key, value, prio)
            new_node = self._write_node(
                node["key"], node["value"], node["prio"], new_left, node.get("right")
            )
            child = self.store.get(new_left)
            if _higher(child["prio"], child["key"], node["prio"], node["key"]):
                return self._rotate_right(self.store.get(new_node))
            return new_node
        new_right = self._upsert(node.get("right"), key, value, prio)
        new_node = self._write_node(
            node["key"], node["value"], node["prio"], node.get("left"), new_right
        )
        child = self.store.get(new_right)
        if _higher(child["prio"], child["key"], node["prio"], node["key"]):
            return self._rotate_left(self.store.get(new_node))
        return new_node

    # ------------------------------------------------------------------
    # 删除（输出结构等价于用剩余键从零重建，保证两会场删除后仍同根）
    # ------------------------------------------------------------------
    def delete(self, root: str | None, key: str) -> str:
        if root is None or root == EMPTY_ROOT:
            return EMPTY_ROOT
        node = self.store.get(root)
        if key < node["key"]:
            new_left = self.delete(node.get("left"), key)
            if new_left == node.get("left"):
                return root
            return self._write_node(
                node["key"], node["value"], node["prio"], new_left, node.get("right")
            )
        if key > node["key"]:
            new_right = self.delete(node.get("right"), key)
            if new_right == node.get("right"):
                return root
            return self._write_node(
                node["key"], node["value"], node["prio"], node.get("left"), new_right
            )
        # 命中待删节点
        if self._is_leaf(node) or node.get("left") is None:
            return node["right"] if node.get("right") is not None else EMPTY_ROOT
        if node.get("right") is None:
            return node["left"]
        right = self.store.get(node["right"])
        left = self.store.get(node["left"])
        if _higher(left["prio"], left["key"], right["prio"], right["key"]):
            new_right = self._write_node(
                node["key"], node["value"], node["prio"], left.get("right"), node["right"]
            )
            return self._write_node(
                left["key"], left["value"], left["prio"], left.get("left"), new_right
            )
        new_left = self._write_node(
            node["key"], node["value"], node["prio"], node["left"], right.get("left")
        )
        return self._write_node(
            right["key"], right["value"], right["prio"], new_left, right.get("right")
        )

    def apply_event(self, root: str, event_op: str, key: str, value: Any) -> str:
        if event_op == OP_UPSERT:
            return self.upsert(root, key, value)
        if event_op == OP_DELETE:
            return self.delete(root, key)
        raise ValueError(f"未知事件操作：{event_op}")

    # ------------------------------------------------------------------
    # 查询与遍历
    # ------------------------------------------------------------------
    def get(self, root: str | None, key: str) -> Any | None:
        cur = root
        while cur is not None and cur != EMPTY_ROOT:
            node = self.store.get(cur)
            if key == node["key"]:
                return node["value"]
            cur = node.get("left") if key < node["key"] else node.get("right")
        return None

    def contains(self, root: str | None, key: str) -> bool:
        cur = root
        while cur is not None and cur != EMPTY_ROOT:
            node = self.store.get(cur)
            if key == node["key"]:
                return True
            cur = node.get("left") if key < node["key"] else node.get("right")
        return False

    def iter_items(self, root: str) -> Iterator[tuple[str, Any]]:
        """按键升序产生 (key, value)；全程只通过摘要读取 blob，不产生写入。"""
        if root == EMPTY_ROOT:
            return
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            cur, expanded = stack.pop()
            if cur == EMPTY_ROOT:
                continue
            node = self.store.get(cur)
            if expanded or node["k"] == "leaf":
                yield node["key"], node["value"]
                continue
            if node["right"] is not None:
                stack.append((node["right"], False))
            stack.append((cur, True))
            if node["left"] is not None:
                stack.append((node["left"], False))

    def items(self, root: str) -> list[tuple[str, Any]]:
        return list(self.iter_items(root))

    def size(self, root: str) -> int:
        return sum(1 for _ in self.iter_items(root))

    def screen_digest(self, root: str) -> str:
        """当前画面摘要：存活键值对按 key 排序后规范化哈希（与树形无关）。"""
        return digest([{"key": k, "value": v} for k, v in self.items(root)])
