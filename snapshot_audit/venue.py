"""测试会场：分片装配、水位、版本信封链、历史读者与受控回滚。

会场协议
--------
1. 先登记 *批准清单*（BatchManifest），再投递分片。分片可以乱序、重复投递，
   也可以只到一部分——未收齐的批次永远不会进入快照。
2. 每个分片先做摘要自验，再与清单承诺比对；任何不符都拒绝并记入拒收记录。
3. 只有清单承诺的全部分片到齐且全部通过校验，批次才"完整"。完整批次按
   批准版本号排序后，在 advance_water_mark 时原子提交：一次信封、一个
   根哈希，历史读者绝不会看到半个批次。
4. 历史读者（ReaderAt）绑定一个水位，只能看到水位线以内的完整批次，
   批次内事件整体可见或整体不可见。
5. 回滚必须引用已批准的信封版本与明确范围（RollbackRecord），会场校验
   两者后以补偿事件生成新信封；历史信封与旧快照一律保留。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .model import (
    KIND_BATCH,
    KIND_ROLLBACK,
    BatchManifest,
    Event,
    Fragment,
    RollbackRecord,
    VersionEnvelope,
    digest,
)
from .store import PackStore
from .treap import EMPTY_ROOT, PersistentTreap


class VenueError(Exception):
    """会场协议违规（未登记清单、分片篡改、回滚引用无效等）。"""


@dataclass
class RejectedFragment:
    batch_id: str
    index: int
    reason: str
    observed_digest: str


@dataclass
class VenueState:
    """可持久化的会场状态（信封链与装配台账之外的一切都在 blob 存储里）。"""

    name: str
    water_mark: int = 0  # 已提交信封版本的高水位
    head_envelope: str | None = None  # 最新信封摘要
    envelopes: list[str] = field(default_factory=list)  # 信封摘要的有序链
    # 装配台账（批次未完整时只存内存/状态文件，不进快照树）
    manifests: dict[str, str] = field(default_factory=dict)  # batch_id -> 清单 blob
    fragments: dict[str, dict[int, str]] = field(default_factory=dict)  # -> 分片 blob
    rejected: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "water_mark": self.water_mark,
            "head_envelope": self.head_envelope,
            "envelopes": list(self.envelopes),
            "manifests": dict(self.manifests),
            "fragments": {
                b: {str(i): d for i, d in m.items()}
                for b, m in self.fragments.items()
            },
            "rejected": list(self.rejected),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "VenueState":
        return VenueState(
            name=d["name"],
            water_mark=d["water_mark"],
            head_envelope=d["head_envelope"],
            envelopes=list(d["envelopes"]),
            manifests=dict(d["manifests"]),
            fragments={
                b: {int(i): d2 for i, d2 in m.items()}
                for b, m in d["fragments"].items()
            },
            rejected=list(d["rejected"]),
        )


class Venue:
    """一个离线测试会场。"""

    STATE_FILENAME = "venue_state.json"

    def __init__(self, name: str, store: PackStore) -> None:
        self.name = name
        self.store = store
        self.treap = PersistentTreap(store)
        self.state = VenueState(name=name)
        self._state_path = os.path.join(store.path, self.STATE_FILENAME)
        if os.path.exists(self._state_path):
            with open(self._state_path, "r", encoding="utf-8") as fh:
                self.state = VenueState.from_dict(json.load(fh))

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def save(self) -> None:
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state.to_dict(), fh, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, self._state_path)
        self.store.flush_index()

    # ------------------------------------------------------------------
    # 清单登记与分片投递（可拆分、可乱序、可重复、可缺失）
    # ------------------------------------------------------------------
    def register_manifest(self, manifest: BatchManifest) -> str:
        if manifest.batch_id in self.state.manifests:
            # 同内容重复登记幂等；内容不同则拒绝
            existing = self.state.manifests[manifest.batch_id]
            if existing != manifest.content_digest():
                raise VenueError(f"批次 {manifest.batch_id} 已登记不同清单")
            return existing
        d = self.store.put(manifest.to_dict())
        self.state.manifests[manifest.batch_id] = d
        self.state.fragments.setdefault(manifest.batch_id, {})
        return d

    def deliver_fragment(self, fragment: Fragment) -> bool:
        """投递一个分片；返回 True 表示接受，False 表示拒收（同时留痕）。"""
        m_blob = self.state.manifests.get(fragment.batch_id)
        if m_blob is None:
            raise VenueError(
                f"批次 {fragment.batch_id} 未登记批准清单，拒绝分片 {fragment.index}"
            )
        manifest = BatchManifest.from_dict(self.store.get(m_blob))
        if not (0 <= fragment.index < fragment.batch_size) or \
                fragment.batch_size != len(manifest.fragment_digests):
            return self._reject(fragment, "分片定位与清单不符")
        observed = fragment.content_digest()
        if observed != manifest.fragment_digests[fragment.index]:
            return self._reject(fragment, "分片摘要与清单承诺不符", observed)
        # 内容寻址：重复投递直接幂等
        d = self.store.put(fragment.to_dict())
        self.state.fragments.setdefault(fragment.batch_id, {})[fragment.index] = d
        return True

    def _reject(
        self, fragment: Fragment, reason: str, observed: str | None = None
    ) -> bool:
        self.state.rejected.append(
            {
                "batch_id": fragment.batch_id,
                "index": str(fragment.index),
                "reason": reason,
                "observed_digest": observed or fragment.content_digest(),
            }
        )
        return False

    def is_batch_complete(self, batch_id: str) -> bool:
        m_blob = self.state.manifests.get(batch_id)
        if m_blob is None:
            return False
        manifest = BatchManifest.from_dict(self.store.get(m_blob))
        have = self.state.fragments.get(batch_id, {})
        return len(have) == len(manifest.fragment_digests) and all(
            i in have for i in range(len(manifest.fragment_digests))
        )

    def missing_fragments(self, batch_id: str) -> list[int]:
        """返回批次尚缺的分片序号（对照器用它报告"缺段未证"）。"""
        m_blob = self.state.manifests.get(batch_id)
        if m_blob is None:
            return []
        manifest = BatchManifest.from_dict(self.store.get(m_blob))
        have = self.state.fragments.get(batch_id, {})
        return [i for i in range(len(manifest.fragment_digests)) if i not in have]

    def _assemble_events(self, manifest: BatchManifest) -> list[Event]:
        """收齐后按序号重组事件，并对完整事件序列做二次摘要核验。"""
        parts = self.state.fragments[manifest.batch_id]
        events: list[Event] = []
        for i in range(len(manifest.fragment_digests)):
            frag = Fragment.from_dict(self.store.get(parts[i]))
            events.extend(frag.events)
        if digest([e.to_dict() for e in events]) != manifest.event_digest:
            raise VenueError(
                f"批次 {manifest.batch_id} 重组事件摘要与清单不符"
            )
        return events

    # ------------------------------------------------------------------
    # 水位推进与原子提交
    # ------------------------------------------------------------------
    def complete_batches(self) -> list[BatchManifest]:
        out = []
        for batch_id, m_blob in self.state.manifests.items():
            if self.is_batch_complete(batch_id):
                out.append(BatchManifest.from_dict(self.store.get(m_blob)))
        return sorted(out, key=lambda m: m.approved_version)

    def advance_water_mark(self) -> list[str]:
        """把所有已完整但尚未提交的批次按批准版本顺序原子提交，返回新信封摘要。

        每个批次是一个不可分割的提交单元：要么整批事件应用后生成一个信封，
        要么完全不动。历史读者按信封水位读取，因此只可能见到完整批次。
        """
        submitted_versions = {
            VersionEnvelope.from_dict(self.store.get(d)).batch_id
            for d in self.state.envelopes
            if VersionEnvelope.from_dict(self.store.get(d)).kind == KIND_BATCH
        }
        pending = [
            m for m in self.complete_batches()
            if m.batch_id not in submitted_versions
        ]
        new_envelopes: list[str] = []
        for manifest in pending:
            events = self._assemble_events(manifest)
            root = self._current_root()
            for ev in events:
                root = self.treap.apply_event(root, ev.rule_id, ev.op, ev.value)
            envelope = VersionEnvelope(
                kind=KIND_BATCH,
                version=self.state.water_mark + 1,
                parent=self.state.head_envelope,
                root_digest=root,
                screen_digest=self.treap.screen_digest(root),
                batch_id=manifest.batch_id,
                manifest_digest=manifest.content_digest(),
            )
            d = self.store.put(envelope.to_dict())
            self._append_envelope(d, envelope)
            new_envelopes.append(d)
        return new_envelopes

    def _append_envelope(self, d: str, envelope: VersionEnvelope) -> None:
        # 信封链自洽性校验：版本连续、父指针正确
        if envelope.version != self.state.water_mark + 1:
            raise VenueError("信封版本不连续")
        if envelope.parent != self.state.head_envelope:
            raise VenueError("信封父指针与当前链首不符")
        self.state.envelopes.append(d)
        self.state.head_envelope = d
        self.state.water_mark = envelope.version

    def _current_root(self) -> str:
        if self.state.head_envelope is None:
            return EMPTY_ROOT
        return VersionEnvelope.from_dict(
            self.store.get(self.state.head_envelope)
        ).root_digest

    # ------------------------------------------------------------------
    # 回滚：必须引用批准的版本与范围
    # ------------------------------------------------------------------
    def rollback(self, record: RollbackRecord) -> str:
        """按批准记录回滚，返回新信封摘要。

        校验：
        - target_version 必须是链上已存在的信封版本（批准的版本）；
        - scope 必须非空，且只处理范围内的键；
        - 目标快照与当前画面在范围内的差异，用补偿事件重放到当前根上，
          生成新版本而非删除历史——旧信封、旧根全部保留。
        """
        if record.target_version < 0 or record.target_version > self.state.water_mark:
            raise VenueError(
                f"回滚目标版本 {record.target_version} 不在已批准版本链上"
            )
        if not record.scope:
            raise VenueError("回滚必须引用明确的非空范围")
        scope = tuple(sorted(set(record.scope)))
        target_root = self._root_at_version(record.target_version)
        current_root = self._current_root()
        # 仅在批准范围内计算补偿事件
        compensation: list[Event] = []
        for key in scope:
            old_val = self.treap.get(target_root, key)
            new_val = self.treap.get(current_root, key)
            if old_val == new_val:
                continue
            if old_val is None:
                compensation.append(Event(key, "delete"))
            else:
                compensation.append(Event(key, "upsert", old_val))
        root = current_root
        for ev in compensation:
            root = self.treap.apply_event(root, ev.rule_id, ev.op, ev.value)
        rb_blob = self.store.put(record.to_dict())
        envelope = VersionEnvelope(
            kind=KIND_ROLLBACK,
            version=self.state.water_mark + 1,
            parent=self.state.head_envelope,
            root_digest=root,
            screen_digest=self.treap.screen_digest(root),
            batch_id=f"rollback-v{record.target_version}",
            rollback_digest=rb_blob,
        )
        d = self.store.put(envelope.to_dict())
        self._append_envelope(d, envelope)
        return d

    def _root_at_version(self, version: int) -> str:
        if version == 0:
            return EMPTY_ROOT
        d = self.state.envelopes[version - 1]
        return VersionEnvelope.from_dict(self.store.get(d)).root_digest

    # ------------------------------------------------------------------
    # 历史读者：只按自己的水位见到完整批次
    # ------------------------------------------------------------------
    def reader(self, water_mark: int) -> "ReaderAt":
        if water_mark < 0 or water_mark > self.state.water_mark:
            raise VenueError(
                f"读者水位 {water_mark} 超出会场高水位 {self.state.water_mark}"
            )
        return ReaderAt(self, water_mark)

    def envelope_at(self, version: int) -> VersionEnvelope:
        if version <= 0 or version > self.state.water_mark:
            raise VenueError(f"版本 {version} 不存在")
        return VersionEnvelope.from_dict(
            self.store.get(self.state.envelopes[version - 1])
        )

    def applied_versions_at(self, water_mark: int) -> tuple[int, ...]:
        """返回某本地水位之前，各批次信封对应的批准版本号（按本地信封顺序）。"""
        out = []
        for d in self.state.envelopes[:water_mark]:
            env = VersionEnvelope.from_dict(self.store.get(d))
            if env.kind != KIND_BATCH:
                continue
            manifest = self._manifest_by_batch(env.batch_id)
            if manifest is not None:
                out.append(manifest.approved_version)
        return tuple(out)

    def _manifest_by_batch(self, batch_id: str) -> BatchManifest | None:
        m_blob = self.state.manifests.get(batch_id)
        if m_blob is None:
            return None
        return BatchManifest.from_dict(self.store.get(m_blob))

    def head(self) -> VersionEnvelope | None:
        if self.state.head_envelope is None:
            return None
        return VersionEnvelope.from_dict(self.store.get(self.state.head_envelope))


class ReaderAt:
    """绑定固定水位的历史读者；批次原子性保证其只见完整批次。"""

    def __init__(self, venue: Venue, water_mark: int) -> None:
        self.venue = venue
        self.water_mark = water_mark
        self.root = venue._root_at_version(water_mark)

    def get(self, key: str) -> Any:
        return self.venue.treap.get(self.root, key)

    def items(self) -> list[tuple[str, Any]]:
        return self.venue.treap.items(self.root)

    def screen_digest(self) -> str:
        return self.venue.treap.screen_digest(self.root)

    def visible_envelopes(self) -> list[VersionEnvelope]:
        return [
            VersionEnvelope.from_dict(self.venue.store.get(d))
            for d in self.venue.state.envelopes[: self.water_mark]
        ]
