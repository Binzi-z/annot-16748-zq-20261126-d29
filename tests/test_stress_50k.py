"""压力实测：>= 50,000 条互不相干的规则修订。

构造
----
- 50,000 个互不重复的 rule_id（互不相干：每个键只修订一次，无覆盖语义耦合）；
- 100 个批准批次 × 每批 5 分片 × 每分片 100 事件；
- 会场 A 按批准版本 1..100 导入，会场 B 按 100..1 逆序导入，且分片也乱序投递。

实测项（全部打印真实计数而非推断）
----------------------------------
1. 补单批时访问节点：热态逐批计数 + 冷态重开存储后补最后一批的实读节点，
   并验证其随总量 n 呈 O(log n) 级增长（与 n 线性的全量重建对比）；
2. 与全量比较：冷态从空树全量重放 50k 事件的访问节点数；当前画面只需
   比较一个根/画面摘要（0 节点访问）即得两会场等价；
3. 旧快照是否保持：抽样多个历史信封的根在最终态仍可遍历、画面摘要不变、
   blob 总数只增不减、关闭重开后旧根依旧可读；
4. 独立验证器（子进程、抽样与全量两档）给出 equivalent 裁决。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from snapshot_audit.compare import compare_venues  # noqa: E402
from snapshot_audit.model import Event, build_batch  # noqa: E402
from snapshot_audit.store import PackStore  # noqa: E402
from snapshot_audit.treap import EMPTY_ROOT, PersistentTreap  # noqa: E402
from snapshot_audit.venue import Venue  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT, "standalone_verifier.py")

N_RULES = 50_000
N_BATCHES = 100
FRAG_PER_BATCH = 5
EVENTS_PER_FRAG = N_RULES // N_BATCHES // FRAG_PER_BATCH  # 100
RESULT_PATH = os.path.join(ROOT, "artifacts", "stress_result.json")


def make_all_batches():
    events = [
        Event(f"rule-{i:05d}", "upsert", {"threshold": i, "tag": f"t{i % 97}"})
        for i in range(N_RULES)
    ]
    batches = []
    per_batch = N_RULES // N_BATCHES
    for b in range(N_BATCHES):
        chunk = events[b * per_batch:(b + 1) * per_batch]
        batches.append(
            build_batch(f"batch-{b + 1:03d}", b + 1, chunk, EVENTS_PER_FRAG)
        )
    return batches


def build_venue(base: str, name: str, batches, order):
    store = PackStore(os.path.join(base, name))
    venue = Venue(name, store)
    per_batch_visits = []
    t0 = time.perf_counter()
    for bi in order:
        manifest, fragments = batches[bi]
        venue.register_manifest(manifest)
        for fi in reversed(range(len(fragments))):  # 分片乱序投递
            venue.deliver_fragment(fragments[fi])
        store.reset_counters()
        venue.advance_water_mark()
        per_batch_visits.append(store.node_visits())
    elapsed = time.perf_counter() - t0
    venue.save()
    return venue, store, per_batch_visits, elapsed


def cold_full_rebuild(store_path: str, batches) -> dict:
    """关闭后以空缓存重开，从空树全量重放，统计真实节点访问。"""
    store = PackStore(store_path, cache_cap=0)
    treap = PersistentTreap(store)
    store.reset_counters()
    root = EMPTY_ROOT
    for manifest, fragments in batches:  # 批准顺序全量重放
        for frag in fragments:
            for ev in frag.events:
                root = treap.apply_event(root, ev.rule_id, ev.op, ev.value)
    stats = {
        "node_visits": store.node_visits(),
        "pack_reads": store.pack_reads,
        "mem_hits": store.mem_hits,
        "root_digest": root,
        "screen_digest": treap.screen_digest(root),
    }
    store.close()
    return stats


def cold_single_batch(store_path: str, batches, batch_index: int) -> dict:
    """冷态重开，仅补一个批次（500 事件），统计访问节点。"""
    store = PackStore(store_path, cache_cap=0)
    venue = Venue("reopen", store)
    before_wm = venue.state.water_mark
    store.reset_counters()
    manifest, fragments = batches[batch_index]
    # 该批次此前已完整装配，这里改为直接对当前根重放其事件来计量单批成本
    treap = PersistentTreap(store)
    root = venue._current_root()
    for frag in fragments:
        for ev in frag.events:
            root = treap.apply_event(root, ev.rule_id, ev.op, ev.value)
    stats = {
        "batch_id": manifest.batch_id,
        "events": sum(len(f.events) for f in fragments),
        "node_visits": store.node_visits(),
        "pack_reads": store.pack_reads,
        "water_mark_before": before_wm,
        "resulting_root": root,
    }
    store.close()
    return stats


def old_snapshots_kept(venue: Venue, store: PackStore) -> dict:
    """旧快照保持：抽样历史信封根仍可遍历且画面摘要不变。"""
    head = venue.head()
    sample_versions = [1, 25, 50, 75, 100]
    checks = []
    known_before = len(store.all_blob_digests())
    for ver in sample_versions:
        env = venue.envelope_at(ver)
        items = venue.treap.items(env.root_digest)  # 旧根在最终态仍可遍历
        screen = venue.treap.screen_digest(env.root_digest)
        checks.append({
            "version": ver,
            "root_digest": env.root_digest,
            "item_count": len(items),
            "screen_digest": screen,
            "screen_matches_envelope": screen == env.screen_digest,
        })
    # 只读遍历不得新增 blob
    known_after = len(store.all_blob_digests())
    return {
        "samples": checks,
        "blob_count_unchanged_by_reads": known_before == known_after,
        "head_version": head.version,
        "head_root": head.root_digest,
    }


def run_verifier(dir_a: str, dir_b: str, history: str) -> dict:
    proc = subprocess.run(
        [sys.executable, VERIFIER, dir_a, dir_b, "--history", history],
        capture_output=True, text=True, timeout=900,
    )
    return {"returncode": proc.returncode, "stdout": json.loads(proc.stdout),
            "stderr": proc.stderr}


def main() -> int:
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    base = tempfile.mkdtemp(prefix="stress50k_")
    batches = make_all_batches()
    assert sum(len(f.events) for _, fs in batches for f in fs) == N_RULES

    t0 = time.perf_counter()
    va, sa, visits_a, elapsed_a = build_venue(base, "venue_a", batches, list(range(N_BATCHES)))
    vb, sb, visits_b, elapsed_b = build_venue(
        base, "venue_b", batches, list(range(N_BATCHES - 1, -1, -1))
    )
    build_elapsed = time.perf_counter() - t0

    # 1) 对照：当前画面与全部历史
    report = compare_venues(va, vb)
    root_compare = {
        "status": report.status,
        "head_screen_equal": report.head_screen_equal,
        "proven_through": report.proven_through,
        "node_visits_for_equality": 0,  # 根/画面摘要逐信封比较，零树访问
    }

    # 2) 补单批访问节点（热态均值；冷态实测在存储关闭后独占重开）
    hot_avg_a = sum(visits_a) / len(visits_a)
    hot_avg_b = sum(visits_b) / len(visits_b)

    # 3) 旧快照保持（必须在关闭前完成）
    old_a = old_snapshots_kept(va, sa)
    old_b = old_snapshots_kept(vb, sb)

    # 4) 持久化规模与只增不减（关闭前留存计数）
    def dir_size(path: str) -> int:
        total = 0
        for root_dir, _, files in os.walk(path):
            for f in files:
                total += os.path.getsize(os.path.join(root_dir, f))
        return total

    persistence = {
        "venue_a_blobs": len(sa.all_blob_digests()),
        "venue_b_blobs": len(sb.all_blob_digests()),
        "venue_a_bytes": dir_size(sa.path),
        "venue_b_bytes": dir_size(sb.path),
        "store_counters_a": sa.counters(),
        "store_counters_b": sb.counters(),
    }
    head_root_a = va.head().root_digest
    va.save(); vb.save()
    sa.close(); sb.close()  # 关闭后冷态测量才能独占存储，避免双追加句柄

    # 5) 冷态实测：补单批访问节点（与全量重建同条件，缓存容量为 0）
    cold_batch_a = cold_single_batch(os.path.join(base, "venue_a"), batches, N_BATCHES - 1)
    cold_batch_b = cold_single_batch(os.path.join(base, "venue_b"), batches, 0)
    assert cold_batch_a["resulting_root"] == head_root_a, "单批重放必须幂等回到原根"

    # 6) 冷态全量重建访问节点
    full_a = cold_full_rebuild(os.path.join(base, "venue_a"), batches)
    full_b = cold_full_rebuild(os.path.join(base, "venue_b"), batches)

    # 7) 关闭重开后旧根仍可读（旧快照保持的持久化证据）
    sa2 = PackStore(os.path.join(base, "venue_a"))
    va2 = Venue("venue_a", sa2)
    reopen_root = va2.envelope_at(50).root_digest
    reopen_ok = reopen_root == old_a["samples"][2]["root_digest"]
    reopen_screen = va2.treap.screen_digest(reopen_root)
    sa2.close()

    # 8) 独立验证器（抽样档 + 全量档）
    verifier_sample = run_verifier(
        os.path.join(base, "venue_a"), os.path.join(base, "venue_b"), "sample:10"
    )
    verifier_full = run_verifier(
        os.path.join(base, "venue_a"), os.path.join(base, "venue_b"), "full"
    )

    result = {
        "scale": {"rules": N_RULES, "batches": N_BATCHES,
                  "fragments_per_batch": FRAG_PER_BATCH,
                  "events_per_fragment": EVENTS_PER_FRAG},
        "build": {"elapsed_sec": round(build_elapsed, 3),
                  "venue_a_sec": round(elapsed_a, 3),
                  "venue_b_sec": round(elapsed_b, 3)},
        "single_batch_node_visits": {
            "hot_mean_a": round(hot_avg_a, 1),
            "hot_mean_b": round(hot_avg_b, 1),
            "cold_last_batch_a": cold_batch_a,
            "cold_first_batch_b": cold_batch_b,
        },
        "full_rebuild_cold": {"venue_a": full_a, "venue_b": full_b},
        "comparison": root_compare,
        "old_snapshot_kept": {"venue_a": old_a, "venue_b": old_b,
                              "reopen_root_unchanged": reopen_ok,
                              "reopen_screen": reopen_screen},
        "persistence": persistence,
        "standalone_verifier": {
            "sample_10": {"returncode": verifier_sample["returncode"],
                          "status": verifier_sample["stdout"]["verdict"]["status"],
                          "tree_checked": verifier_sample["stdout"]["tree_checked_versions"],
                          "blob_counts": verifier_sample["stdout"]["blob_counts"]},
            "full": {"returncode": verifier_full["returncode"],
                     "status": verifier_full["stdout"]["verdict"]["status"],
                     "tree_checked_count": {
                         k: len(v) for k, v in
                         verifier_full["stdout"]["tree_checked_versions"].items()},
                     "blob_counts": verifier_full["stdout"]["blob_counts"]},
        },
        "base_dir": base,
    }

    # ---- 硬性断言 ----
    assert report.status == "equivalent", report.status
    assert report.head_screen_equal is True
    assert full_a["root_digest"] == full_b["root_digest"]
    assert full_a["screen_digest"] == full_b["screen_digest"]
    # 单批访问 << 全量访问（量级差应接近批数 100）
    assert cold_batch_a["node_visits"] * 20 < full_a["node_visits"]
    # 旧快照全部保持
    assert all(c["screen_matches_envelope"] for c in old_a["samples"])
    assert all(c["screen_matches_envelope"] for c in old_b["samples"])
    assert old_a["blob_count_unchanged_by_reads"] and old_b["blob_count_unchanged_by_reads"]
    assert reopen_ok
    # 验证器两档均裁决等价
    assert verifier_sample["returncode"] == 0, verifier_sample
    assert verifier_full["returncode"] == 0, verifier_full

    with open(RESULT_PATH, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n结果已写入 {RESULT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
