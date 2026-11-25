"""两会场离线对照：最早的已证分歧 + 缺段导致的未证结果。

对齐方式不依赖任何会场内部索引或导入先后：以 *批准清单的批准版本号*
为共同逻辑序列。顺序无关性保证"相同已应用批次集合 => 相同根"，因此
对照器只在两会场都见证了同一个 *批准前缀 {1..k}* 时做根对齐。

结论分三类
- equivalent  ：所有可证前缀逐一证毕一致；
- divergent   ：发现至少一个 *已证* 分歧，报告取最早者；
- inconclusive：存在缺段，缺段及之后的历史一致性无法证明
                （缺段之前若已出现分歧，仍属 divergent，并同时列出未证区间）。

关键判定原则
1. 缺段一旦在某版本出现，版本链即断裂——其后不再做根对齐，一律计入未证。
2. 逆序导入的会场，只有其已应用批次集合恰好构成连续批准前缀时，该信封
   才是该前缀的"见证信封"，可与另一会场同前缀见证信封语义对齐。
3. 同时用严格按批准顺序的 *规范前缀重放* 复核两个见证信封，任何一方
   与规范重放不符即为已证分歧。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import (
    KIND_BATCH,
    BatchManifest,
    VersionEnvelope,
    digest,
)
from .venue import Venue

STATUS_EQUIVALENT = "equivalent"
STATUS_DIVERGENT = "divergent"
STATUS_INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Gap:
    """缺段：某批准批次在一个或两个会场未收齐（或清单缺失）。"""

    approved_version: int
    batch_id: str
    missing_in_a: tuple[int, ...]
    missing_in_b: tuple[int, ...]
    manifest_missing_in_a: bool
    manifest_missing_in_b: bool
    rejected_in_a: tuple[dict[str, str], ...]
    rejected_in_b: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_version": self.approved_version,
            "batch_id": self.batch_id,
            "missing_in_a": list(self.missing_in_a),
            "missing_in_b": list(self.missing_in_b),
            "manifest_missing_in_a": self.manifest_missing_in_a,
            "manifest_missing_in_b": self.manifest_missing_in_b,
            "rejected_in_a": list(self.rejected_in_a),
            "rejected_in_b": list(self.rejected_in_b),
        }


@dataclass(frozen=True)
class Divergence:
    """最早的已证分歧。证据全部为可独立复核的摘要。"""

    approved_version: int
    batch_id: str
    kind: str  # manifest / fragment_tamper / root / screen
    detail: str
    evidence_a: dict[str, Any]
    evidence_b: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_version": self.approved_version,
            "batch_id": self.batch_id,
            "kind": self.kind,
            "detail": self.detail,
            "evidence_a": self.evidence_a,
            "evidence_b": self.evidence_b,
        }


@dataclass(frozen=True)
class ComparisonReport:
    venue_a: str
    venue_b: str
    status: str
    earliest_divergence: Divergence | None
    gaps: tuple[Gap, ...]
    proven_through: int | None          # 最后一个证毕一致的批准前缀长度
    unproven_from: int | None           # 从该批准版本起无法证明
    compared_versions: tuple[int, ...]  # 实际参与前缀比对的批准版本
    head_screen_equal: bool | None      # 当前画面对比（可能为 None：无法取得）
    head_evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue_a": self.venue_a,
            "venue_b": self.venue_b,
            "status": self.status,
            "earliest_divergence": (
                self.earliest_divergence.to_dict()
                if self.earliest_divergence is not None
                else None
            ),
            "gaps": [g.to_dict() for g in self.gaps],
            "proven_through": self.proven_through,
            "unproven_from": self.unproven_from,
            "compared_versions": list(self.compared_versions),
            "head_screen_equal": self.head_screen_equal,
            "head_evidence": self.head_evidence,
        }

    def content_digest(self) -> str:
        return digest(self.to_dict())

    def render_text(self) -> str:
        lines = [
            f"对照报告（报告摘要 {self.content_digest()[:16]}…）",
            f"  会场 A={self.venue_a}  B={self.venue_b}",
            f"  结论：{self.status}",
            f"  已逐前缀证毕一致至批准版本：{self.proven_through}",
            f"  未证区间起始批准版本：{self.unproven_from}",
            f"  当前画面是否相同：{self.head_screen_equal}",
        ]
        if self.earliest_divergence is not None:
            d = self.earliest_divergence
            lines.append(
                f"  最早已证分歧：批准版本 {d.approved_version} 批次 {d.batch_id}"
                f" 类型={d.kind}"
            )
            lines.append(f"    说明：{d.detail}")
            lines.append(f"    A 证据：{d.evidence_a}")
            lines.append(f"    B 证据：{d.evidence_b}")
        if self.gaps:
            lines.append("  缺段（该版本及其后的历史一致性未证）：")
            for g in self.gaps:
                notes = []
                if g.manifest_missing_in_a:
                    notes.append("A 缺清单")
                if g.manifest_missing_in_b:
                    notes.append("B 缺清单")
                lines.append(
                    f"    版本 {g.approved_version} 批次 {g.batch_id}："
                    f"A 缺分片 {list(g.missing_in_a)}，B 缺分片 {list(g.missing_in_b)}"
                    + (f"（{'，'.join(notes)}）" if notes else "")
                )
        lines.append(f"  当前画面证据：{self.head_evidence}")
        return "\n".join(lines)


def _manifests_by_version(venue: Venue) -> dict[int, BatchManifest]:
    out: dict[int, BatchManifest] = {}
    for m_blob in venue.state.manifests.values():
        m = BatchManifest.from_dict(venue.store.get(m_blob))
        out[m.approved_version] = m
    return out


def _rejections_for(venue: Venue, batch_id: str) -> tuple[dict[str, str], ...]:
    return tuple(r for r in venue.state.rejected if r["batch_id"] == batch_id)


def _set_witnesses(venue: Venue) -> dict[frozenset[int], VersionEnvelope]:
    """集合见证：已应用批准版本集合 -> 产生该集合的批次信封。

    顺序无关性保证：同一已应用批次集合，无论内部到达顺序，根必相同。
    """
    applied: set[int] = set()
    witnesses: dict[frozenset[int], VersionEnvelope] = {}
    for d in venue.state.envelopes:
        env = VersionEnvelope.from_dict(venue.store.get(d))
        if env.kind != KIND_BATCH:
            continue
        manifest = venue._manifest_by_batch(env.batch_id)
        if manifest is None:
            continue
        applied.add(manifest.approved_version)
        witnesses[frozenset(applied)] = env
    return witnesses


def _canonical_screens(
    venue: Venue,
    needed_sets: set[frozenset[int]],
    manifests_by_ver: dict[int, BatchManifest],
) -> dict[frozenset[int], str]:
    """每个所需已应用集合的 *规范* 画面摘要：严格按批准版本顺序重放。

    批准顺序定义参考语义；若某会场因逆序导入对同键冲突得到不同值，
    其信封画面就会与规范结果不符，从而被正确判为已证分歧。
    前缀集合走一次线性重放缓存，避免二次方开销。
    """
    result: dict[frozenset[int], str] = {}
    # 一次批准顺序重放，缓存所有前缀 {1..k}
    prefix_cache: dict[frozenset[int], str] = {}
    screen: dict[str, object] = {}
    for ver in sorted(manifests_by_ver):
        for ev in venue._assemble_events(manifests_by_ver[ver]):
            if ev.op == "upsert":
                screen[ev.rule_id] = ev.value
            else:
                screen.pop(ev.rule_id, None)
        prefix_cache[frozenset(range(1, ver + 1))] = digest(
            [{"key": k, "value": v} for k, v in sorted(screen.items())]
        )
    for applied_set in needed_sets:
        if applied_set in prefix_cache:
            result[applied_set] = prefix_cache[applied_set]
            continue
        local: dict[str, object] = {}
        for ver in sorted(applied_set):
            for ev in venue._assemble_events(manifests_by_ver[ver]):
                if ev.op == "upsert":
                    local[ev.rule_id] = ev.value
                else:
                    local.pop(ev.rule_id, None)
        result[applied_set] = digest(
            [{"key": k, "value": v} for k, v in sorted(local.items())]
        )
    return result


def compare_venues(venue_a: Venue, venue_b: Venue) -> ComparisonReport:
    """对照两个会场，生成带证据的报告。"""
    ma_by_ver = _manifests_by_version(venue_a)
    mb_by_ver = _manifests_by_version(venue_b)
    versions = sorted(set(ma_by_ver) | set(mb_by_ver))

    gaps: list[Gap] = []
    earliest: Divergence | None = None
    unproven_from: int | None = None
    chain_broken = False

    # ---- 第一阶段：清单层与分片层，定位已证分歧与缺段 ----
    for version in versions:
        m_a = ma_by_ver.get(version)
        m_b = mb_by_ver.get(version)
        ref = m_a or m_b
        assert ref is not None
        batch_id = ref.batch_id
        manifest_missing_a = m_a is None
        manifest_missing_b = m_b is None

        if (
            not chain_broken
            and m_a is not None
            and m_b is not None
            and m_a.content_digest() != m_b.content_digest()
            and earliest is None
        ):
            earliest = Divergence(
                approved_version=version,
                batch_id=batch_id,
                kind="manifest",
                detail="两会场登记的批准清单摘要不同",
                evidence_a={"manifest_digest": m_a.content_digest()},
                evidence_b={"manifest_digest": m_b.content_digest()},
            )

        rej_a = _rejections_for(venue_a, batch_id)
        rej_b = _rejections_for(venue_b, batch_id)
        complete_a = venue_a.is_batch_complete(batch_id)
        complete_b = venue_b.is_batch_complete(batch_id)
        miss_a = tuple(venue_a.missing_fragments(batch_id))
        miss_b = tuple(venue_b.missing_fragments(batch_id))
        if manifest_missing_a:
            miss_a = tuple(range(len(ref.fragment_digests)))
        if manifest_missing_b:
            miss_b = tuple(range(len(ref.fragment_digests)))

        if not chain_broken and (rej_a or rej_b) and earliest is None:
            earliest = Divergence(
                approved_version=version,
                batch_id=batch_id,
                kind="fragment_tamper",
                detail="分片内容与清单承诺不符并被拒收（实测摘要已留痕）",
                evidence_a={"rejections": list(rej_a)},
                evidence_b={"rejections": list(rej_b)},
            )
        if manifest_missing_a or manifest_missing_b or not complete_a or not complete_b:
            gaps.append(
                Gap(
                    approved_version=version,
                    batch_id=batch_id,
                    missing_in_a=miss_a,
                    missing_in_b=miss_b,
                    manifest_missing_in_a=manifest_missing_a,
                    manifest_missing_in_b=manifest_missing_b,
                    rejected_in_a=rej_a,
                    rejected_in_b=rej_b,
                )
            )
            if unproven_from is None:
                unproven_from = version
            chain_broken = True

    # ---- 第二阶段：集合见证 + 规范重放（与导入顺序无关的历史一致性证明）----
    compared: list[int] = []
    proven_through: int | None = None
    if not chain_broken and versions:
        witnesses_a = _set_witnesses(venue_a)
        witnesses_b = _set_witnesses(venue_b)
        needed = set(witnesses_a) | set(witnesses_b)
        canonical_a = _canonical_screens(venue_a, needed, ma_by_ver)
        canonical_b = _canonical_screens(venue_b, needed, mb_by_ver)
        # 按集合规模升序考察，首个分歧即"最早"的已证分歧
        for applied_set in sorted(needed, key=lambda s: (len(s), sorted(s))):
            env_a = witnesses_a.get(applied_set)
            env_b = witnesses_b.get(applied_set)
            compared.append(len(applied_set))
            # 单方见证：用规范重放复核该方自身（它持有全部数据，只是顺序不同）
            for venue, env, canonical in (
                (venue_a, env_a, canonical_a),
                (venue_b, env_b, canonical_b),
            ):
                if env is None:
                    continue
                if earliest is None and env.screen_digest != canonical[applied_set]:
                    manifest = (
                        ma_by_ver[max(applied_set)]
                        if venue is venue_a else mb_by_ver[max(applied_set)]
                    )
                    earliest = Divergence(
                        approved_version=max(applied_set),
                        batch_id=manifest.batch_id,
                        kind="root",
                        detail="信封画面与按批准顺序的规范重放不符（历史不一致）",
                        evidence_a={"screen_digest": env.screen_digest,
                                    "canonical_screen_digest": canonical[applied_set]},
                        evidence_b={},
                    )
            # 双方共同见证的集合：直接互相比根
            if env_a is not None and env_b is not None and earliest is None:
                if (env_a.root_digest != env_b.root_digest
                        or env_a.screen_digest != env_b.screen_digest):
                    manifest = ma_by_ver[max(applied_set)]
                    earliest = Divergence(
                        approved_version=max(applied_set),
                        batch_id=manifest.batch_id,
                        kind="root",
                        detail="同一已应用批次集合的两会场信封根/画面不同",
                        evidence_a={"root_digest": env_a.root_digest,
                                    "screen_digest": env_a.screen_digest},
                        evidence_b={"root_digest": env_b.root_digest,
                                    "screen_digest": env_b.screen_digest},
                    )
        if earliest is None:
            full_set = frozenset(versions)
            if full_set in witnesses_a and full_set in witnesses_b:
                proven_through = len(versions)

    # 当前画面对比（独立于历史证明：可能相同但历史仍分歧/未证）
    head_a, head_b = venue_a.head(), venue_b.head()
    head_equal: bool | None = None
    head_evidence: dict[str, Any] = {}
    if head_a is not None and head_b is not None:
        head_equal = head_a.screen_digest == head_b.screen_digest
        head_evidence = {
            "a_head_version": head_a.version,
            "b_head_version": head_b.version,
            "a_screen_digest": head_a.screen_digest,
            "b_screen_digest": head_b.screen_digest,
            "a_root_digest": head_a.root_digest,
            "b_root_digest": head_b.root_digest,
        }

    if earliest is not None:
        status = STATUS_DIVERGENT
    elif unproven_from is not None:
        status = STATUS_INCONCLUSIVE
    else:
        status = STATUS_EQUIVALENT

    return ComparisonReport(
        venue_a=venue_a.name,
        venue_b=venue_b.name,
        status=status,
        earliest_divergence=earliest,
        gaps=tuple(gaps),
        proven_through=proven_through,
        unproven_from=unproven_from,
        compared_versions=tuple(compared),
        head_screen_equal=head_equal,
        head_evidence=head_evidence,
    )
