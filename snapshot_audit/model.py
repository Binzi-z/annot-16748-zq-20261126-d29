"""数据模型与规范化编码。

所有哈希都对 *规范化 JSON* 取 SHA-256：sort_keys、无空白、保留 Unicode、
布尔/整数/字符串/数组/对象/None 的语义稳定。这样相同语义的对象无论由谁、
以何种顺序构造，摘要必然相同——这是"不同导入顺序得到同一根哈希"的基础。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# 支持的事件操作
OP_UPSERT = "upsert"
OP_DELETE = "delete"


def canonical(obj: Any) -> bytes:
    """返回对象的规范化 JSON 字节串（跨进程、跨导入顺序稳定）。"""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(obj: Any) -> str:
    """对规范化编码取 SHA-256，返回十六进制摘要。"""
    return hashlib.sha256(canonical(obj)).hexdigest()


# ---------------------------------------------------------------------------
# 事件、批次、分片
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """一条规则修订事件。

    rule_id  规则标识；同 key 的事件按版本顺序决定最终画面。
    op       upsert / delete
    value    upsert 时的规则载荷（delete 时为 None）
    """

    rule_id: str
    op: str
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"rule_id": self.rule_id, "op": self.op}
        if self.op == OP_UPSERT:
            d["value"] = self.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Event":
        return Event(rule_id=d["rule_id"], op=d["op"], value=d.get("value"))


@dataclass(frozen=True)
class Fragment:
    """批次的一个可拆分传输分片。

    分片只承载事件原文与定位信息；会场必须收齐 batch_size 个分片、
    且每个分片的摘要与批次清单一致，才允许把批次视为完整。
    """

    batch_id: str
    index: int
    batch_size: int
    events: tuple[Event, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "index": self.index,
            "batch_size": self.batch_size,
            "events": [e.to_dict() for e in self.events],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Fragment":
        return Fragment(
            batch_id=d["batch_id"],
            index=d["index"],
            batch_size=d["batch_size"],
            events=tuple(Event.from_dict(e) for e in d["events"]),
        )

    def content_digest(self) -> str:
        """分片内容摘要（由批次清单预先承诺，会场据此识别篡改/缺段）。"""
        return digest(self.to_dict())


@dataclass(frozen=True)
class BatchManifest:
    """批次的批准清单：批准版本、分片摘要承诺、影响范围。

    approved_version  该批次在全局批准序列中的版本号（与导入顺序无关）。
    scope             受影响 rule_id 的有序范围，回滚必须原样引用。
    fragment_digests  对每个分片内容摘要的承诺。
    """

    batch_id: str
    approved_version: int
    scope: tuple[str, ...]
    fragment_digests: tuple[str, ...]
    event_digest: str  # 对完整事件序列（按分片顺序拼接）的摘要

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "approved_version": self.approved_version,
            "scope": list(self.scope),
            "fragment_digests": list(self.fragment_digests),
            "event_digest": self.event_digest,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "BatchManifest":
        return BatchManifest(
            batch_id=d["batch_id"],
            approved_version=d["approved_version"],
            scope=tuple(d["scope"]),
            fragment_digests=tuple(d["fragment_digests"]),
            event_digest=d["event_digest"],
        )

    def content_digest(self) -> str:
        return digest(self.to_dict())


def build_batch(
    batch_id: str,
    approved_version: int,
    events: list[Event] | tuple[Event, ...],
    fragment_size: int,
) -> tuple[BatchManifest, list[Fragment]]:
    """把一批事件切成分片，并生成带摘要承诺的批准清单。"""
    if fragment_size <= 0:
        raise ValueError("fragment_size 必须为正整数")
    events = tuple(events)
    chunks: list[tuple[Event, ...]] = [
        events[i : i + fragment_size] for i in range(0, len(events), fragment_size)
    ]
    if not chunks:  # 空批次也给出一个空分片，保证装配协议统一
        chunks = [()]
    fragments = [
        Fragment(batch_id=batch_id, index=i, batch_size=len(chunks), events=chunk)
        for i, chunk in enumerate(chunks)
    ]
    digests = tuple(f.content_digest() for f in fragments)
    full_events = [e.to_dict() for e in events]
    scope = tuple(sorted({e.rule_id for e in events}))
    manifest = BatchManifest(
        batch_id=batch_id,
        approved_version=approved_version,
        scope=scope,
        fragment_digests=digests,
        event_digest=digest(full_events),
    )
    return manifest, fragments


# ---------------------------------------------------------------------------
# 版本信封（批次提交与回滚共用同一条只追加版本链）
# ---------------------------------------------------------------------------

KIND_BATCH = "batch"
KIND_ROLLBACK = "rollback"


@dataclass(frozen=True)
class VersionEnvelope:
    """版本信封：不可变版本链上的一环。

    kind          batch / rollback
    version       本会场内严格递增的信封版本号
    parent        上一信封的摘要（创世父节点为 None）
    root_digest   该版本对应的快照树根哈希
    screen_digest 该版本"当前画面"（存活键值集合）的规范化摘要
    batch_id      kind=batch 时为提交的批次；rollback 时为引发回滚的批次
    manifest_digest / rollback_digest  对应负载摘要，供外部独立核验
    """

    kind: str
    version: int
    parent: str | None
    root_digest: str
    screen_digest: str
    batch_id: str
    manifest_digest: str | None = None
    rollback_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "parent": self.parent,
            "root_digest": self.root_digest,
            "screen_digest": self.screen_digest,
            "batch_id": self.batch_id,
            "manifest_digest": self.manifest_digest,
            "rollback_digest": self.rollback_digest,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "VersionEnvelope":
        return VersionEnvelope(**d)

    def content_digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True)
class RollbackRecord:
    """回滚记录——必须引用 *批准的版本* 与 *范围*，二者缺一不可。

    target_version  批准回滚到的信封版本（必须是已存在信封）。
    scope           批准的回滚范围（rule_id 集合的有序元组）；
                    只允许该范围内的键被补偿事件改写。
    reason          批准说明（仅审计用途）。
    """

    target_version: int
    scope: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_version": self.target_version,
            "scope": list(self.scope),
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RollbackRecord":
        return RollbackRecord(
            target_version=d["target_version"],
            scope=tuple(d["scope"]),
            reason=d["reason"],
        )

    def content_digest(self) -> str:
        return digest(self.to_dict())
