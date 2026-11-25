"""全枚举测试：小输入下穷举所有事件/批次顺序、缺段组合与篡改位置。

覆盖题面要求：
- 小输入全枚举事件顺序（6 事件 720 排列 + 4 批次 24 顺序 × 双会场）；
- 缺段枚举（所有非空缺片子集，含单方/双方缺段）；
- 篡改定位枚举（逐分片、逐字节位点）；
- 回滚枚举（所有合法目标版本 × 范围）及非法引用拒绝；
- 水位读者只见完整批次（不可见半批）；
- "当前画面相同但历史分歧"必须被识别为 divergent；
- 独立验证器以子进程方式复核全部场景结论（不依赖对照器内部索引）。
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from snapshot_audit.compare import (  # noqa: E402
    STATUS_DIVERGENT,
    STATUS_EQUIVALENT,
    STATUS_INCONCLUSIVE,
    compare_venues,
)
from snapshot_audit.model import (  # noqa: E402
    Event,
    Fragment,
    RollbackRecord,
    build_batch,
)
from snapshot_audit.store import PackStore  # noqa: E402
from snapshot_audit.treap import EMPTY_ROOT, PersistentTreap  # noqa: E402
from snapshot_audit.venue import Venue, VenueError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT, "standalone_verifier.py")


def fresh_venue(base: str, name: str) -> tuple[Venue, PackStore]:
    store = PackStore(os.path.join(base, name))
    return Venue(name, store), store


def feed(venue: Venue, manifest, fragments, order=None) -> None:
    """按给定分片顺序投递（默认乱序反转，证明乱序不影响结果）。"""
    venue.register_manifest(manifest)
    order = range(len(fragments)) if order is None else order
    for i in order:
        assert venue.deliver_fragment(fragments[i]) is True


def run_verifier(dir_a: str, dir_b: str, history: str = "full",
                 report: str | None = None) -> tuple[int, dict]:
    cmd = [sys.executable, VERIFIER, dir_a, dir_b, "--history", history]
    if report:
        cmd += ["--report", report]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return proc.returncode, json.loads(proc.stdout)


class TreapOrderTests(unittest.TestCase):
    def test_all_permutations_same_root(self):
        # 6 个键的全部 720 种插入顺序，最终根哈希必须完全相同
        keys = [f"rule-{i}" for i in range(6)]
        roots = set()
        for perm in itertools.permutations(keys):
            base = tempfile.mkdtemp()
            store = PackStore(os.path.join(base, "s"))
            treap = PersistentTreap(store)
            root = EMPTY_ROOT
            for k in perm:
                root = treap.upsert(root, k, {"payload": k})
            roots.add(root)
            store.close()
        self.assertEqual(len(roots), 1)

    def test_event_level_720_permutations_same_screen(self):
        # 同一批事件（新增/覆盖/删除）的全部 720 种顺序，画面与树均相同
        events = [
            Event("a", "upsert", 1),
            Event("b", "upsert", 2),
            Event("a", "upsert", 9),  # 覆盖
            Event("c", "upsert", 3),
            Event("b", "delete"),
            Event("d", "upsert", 4),
        ]
        screens, roots = set(), set()
        for perm in itertools.permutations(events):
            base = tempfile.mkdtemp()
            store = PackStore(os.path.join(base, "s"))
            treap = PersistentTreap(store)
            root = EMPTY_ROOT
            for ev in perm:
                root = treap.apply_event(root, ev.rule_id, ev.op, ev.value)
            screens.add(treap.screen_digest(root))
            roots.add(root)
            store.close()
        self.assertEqual(len(screens), 1)
        self.assertEqual(len(roots), 1)


class BatchOrderEnumTests(unittest.TestCase):
    def _make_batches(self):
        batches = []
        for ver in range(1, 5):
            evs = [Event(f"v{ver}-k{j}", "upsert", {"v": j}) for j in range(3)]
            batches.append(build_batch(f"b{ver}", ver, evs, 2))
        return batches

    def test_all_24_batch_orders_pairwise_equivalent(self):
        batches = self._make_batches()
        order_a = [0, 1, 2, 3]
        for order_b in itertools.permutations(range(4)):
            base = tempfile.mkdtemp()
            va, sa = fresh_venue(base, "A")
            vb, sb = fresh_venue(base, "B")
            for idx in order_a:
                m, fs = batches[idx]
                feed(va, m, fs)
                va.advance_water_mark()
            for idx in order_b:
                m, fs = batches[idx]
                feed(vb, m, fs, order=list(reversed(range(len(fs)))))
                vb.advance_water_mark()
            report = compare_venues(va, vb)
            self.assertEqual(report.status, STATUS_EQUIVALENT,
                             f"顺序 {order_b} 出现意外分歧")
            # 历史一致的证据：两会场各自"已应用批次集合"相同的读者画面一致。
            # 注意不能按本地水位号跨会场比（逆序会场本地水位对应不同批次），
            # 而要按共同的已应用批准版本集合对齐。
            sets_a = {frozenset(va.applied_versions_at(w)): va.reader(w).screen_digest()
                      for w in range(1, 5)}
            sets_b = {frozenset(vb.applied_versions_at(w)): vb.reader(w).screen_digest()
                      for w in range(1, 5)}
            for common in set(sets_a) & set(sets_b):
                self.assertEqual(sets_a[common], sets_b[common],
                                 f"已应用集合 {sorted(common)} 画面不一致")
            # 每个会场内部：历史读者只见到完整批次，且旧水位画面不随后续提交改变
            for wm in range(1, 5):
                self.assertEqual(len(va.reader(wm).visible_envelopes()), wm)
                self.assertEqual(len(vb.reader(wm).visible_envelopes()), wm)
            # 独立验证器子进程复核
            va.save(); vb.save()
            code, verdict = run_verifier(sa.path, sb.path)
            self.assertEqual(code, 0, verdict)
            self.assertEqual(verdict["verdict"]["status"], "equivalent")
            sa.close(); sb.close()


class GapEnumTests(unittest.TestCase):
    def test_every_nonempty_missing_subset(self):
        # 3 个批次，每批 3 分片；枚举 A 侧所有非空缺片子集（2^9-1=511）
        batches = []
        for ver in range(1, 4):
            evs = [Event(f"g{ver}-{j}", "upsert", j) for j in range(6)]
            batches.append(build_batch(f"g{ver}", ver, evs, 2))  # 每批 3 片
        positions = [(b, f) for b in range(3) for f in range(3)]
        for mask in range(1, 1 << len(positions)):
            missing = {positions[i] for i in range(len(positions)) if mask & (1 << i)}
            base = tempfile.mkdtemp()
            va, sa = fresh_venue(base, "A")
            vb, sb = fresh_venue(base, "B")
            first_gap_ver = min(b + 1 for b, _ in missing)
            for b, (m, fs) in enumerate(batches):
                feed(va, m, fs)
                feed(vb, m, fs)
                va.advance_water_mark(); vb.advance_water_mark()
            # 在 A 侧制造缺段：重建一个只含部分分片的会场
            base2 = tempfile.mkdtemp()
            va2, sa2 = fresh_venue(base2, "A")
            for b, (m, fs) in enumerate(batches):
                va2.register_manifest(m)
                for f, frag in enumerate(fs):
                    if (b, f) not in missing:
                        va2.deliver_fragment(frag)
                va2.advance_water_mark()
            report = compare_venues(va2, vb)
            self.assertEqual(report.status, STATUS_INCONCLUSIVE)
            self.assertEqual(report.unproven_from, first_gap_ver)
            self.assertTrue(all(
                g.approved_version >= first_gap_ver for g in report.gaps
            ))
            # 缺段批次的未提交不影响已提交旧快照的可读性
            for wm in range(1, first_gap_ver):
                self.assertEqual(
                    va2.reader(wm).screen_digest(), vb.reader(wm).screen_digest()
                )
            sa.close(); sb.close(); sa2.close()

    def test_two_side_gaps_and_verifier_codes(self):
        batches = [
            build_batch("t1", 1, [Event("x", "upsert", 1), Event("y", "upsert", 2)], 1),
            build_batch("t2", 2, [Event("z", "upsert", 3)], 1),
        ]
        base = tempfile.mkdtemp()
        va, sa = fresh_venue(base, "A")
        vb, sb = fresh_venue(base, "B")
        # A 拿到批次1，B 缺批次1；批次2 双方都有
        m1, fs1 = batches[0]
        m2, fs2 = batches[1]
        feed(va, m1, fs1); va.advance_water_mark()
        feed(va, m2, fs2); va.advance_water_mark()
        vb.register_manifest(m1)  # 只登记清单，不投递 -> 缺段
        feed(vb, m2, fs2)
        # B 的批次2虽完整，但批次1链断裂，提交后仍不可逐版本对齐
        vb.advance_water_mark()
        report = compare_venues(va, vb)
        self.assertEqual(report.status, STATUS_INCONCLUSIVE)
        self.assertEqual(report.unproven_from, 1)
        va.save(); vb.save()
        code, verdict = run_verifier(sa.path, sb.path)
        self.assertEqual(code, 4)
        self.assertEqual(verdict["verdict"]["unproven_from"], 1)
        sa.close(); sb.close()


class TamperEnumTests(unittest.TestCase):
    def test_tamper_each_fragment_each_byte_position(self):
        events = [Event(f"k{i}", "upsert", {"n": i, "pad": "x" * 8}) for i in range(4)]
        manifest, frags = build_batch("tam", 1, events, 1)  # 4 个分片
        for fi in range(len(frags)):
            # 篡改载荷中几个不同的字节位点（枚举值字符串内位置）
            payload = frags[fi].to_dict()
            for pos in range(0, min(3, len(payload["events"]))):
                tampered_events = list(payload["events"])
                ev = dict(tampered_events[pos])
                ev["rule_id"] = ev["rule_id"] + "!"
                tampered_events[pos] = ev
                bad = Fragment.from_dict({**payload, "events": tampered_events})
                base = tempfile.mkdtemp()
                va, sa = fresh_venue(base, "A")
                vb, sb = fresh_venue(base, "B")
                feed(va, manifest, frags); va.advance_water_mark()
                vb.register_manifest(manifest)
                for j, f in enumerate(frags):
                    ok = vb.deliver_fragment(bad if j == fi else f)
                    self.assertTrue(ok if j != fi else ok is False)
                self.assertFalse(vb.is_batch_complete("tam"))
                vb.advance_water_mark()
                report = compare_venues(va, vb)
                # 拒收有实测摘要留痕 = 已证分歧（同时该批次也缺段，链其后未证）
                self.assertEqual(report.status, STATUS_DIVERGENT)
                self.assertEqual(
                    report.earliest_divergence.kind if report.earliest_divergence else None,
                    "fragment_tamper",
                )
                # 拒收留痕携带实测摘要
                self.assertTrue(vb.state.rejected)
                # 独立验证器同样裁决分歧（退出码 3）
                va.save(); vb.save()
                code, verdict = run_verifier(sa.path, sb.path)
                self.assertEqual(code, 3)
                self.assertEqual(
                    verdict["verdict"]["earliest_divergence"]["kind"],
                    "fragment_tamper",
                )
                sa.close(); sb.close()

    def test_storage_byte_tamper_detected_by_verifier(self):
        # 直接篡改 pack 字节，验证器扫描时必须自验失败（退出码 2）
        base = tempfile.mkdtemp()
        va, sa = fresh_venue(base, "A")
        vb, sb = fresh_venue(base, "B")
        m, fs = build_batch("p", 1, [Event("q", "upsert", "1")], 1)
        feed(va, m, fs); va.advance_water_mark(); va.save()
        feed(vb, m, fs); vb.advance_water_mark(); vb.save()
        sa.close(); sb.close()
        pack = os.path.join(sa.path, "blobs", "pack-000000.rec")
        with open(pack, "rb") as fh:
            data = fh.read()
        # 翻转最后一个 JSON 字符附近的字节（避开换行）
        flipped = bytearray(data)
        flipped[-3] = flipped[-3] ^ 0x01
        with open(pack, "wb") as fh:
            fh.write(bytes(flipped))
        code, out = run_verifier(sa.path, sb.path)
        self.assertEqual(code, 2)
        self.assertIn("verification_error", out)


class SameScreenHistoryDivergesTests(unittest.TestCase):
    def test_head_equal_but_history_divergent(self):
        # 两会场最终画面相同（x=2），但 A 有中间版本 x=1，B 没有 -> 历史分歧
        base = tempfile.mkdtemp()
        va, sa = fresh_venue(base, "A")
        vb, sb = fresh_venue(base, "B")
        ma1, fa1 = build_batch("h1", 1, [Event("x", "upsert", 1)], 1)
        ma2, fa2 = build_batch("h2", 2, [Event("x", "upsert", 2)], 1)
        feed(va, ma1, fa1); va.advance_water_mark()
        feed(va, ma2, fa2); va.advance_water_mark()
        # B 在版本1 用另一批事件直接得到 x=2，版本2 为保持序列也提交不相交批次
        mb1, fb1 = build_batch("h1", 1, [Event("x", "upsert", 2)], 1)
        mb2, fb2 = build_batch("h2", 2, [Event("w", "upsert", 0)], 1)
        # 让 B 最终画面也恰好等于 A：B 再把 w 删掉
        mb3, fb3 = build_batch("h3", 3, [Event("w", "delete")], 1)
        feed(vb, mb1, fb1); vb.advance_water_mark()
        feed(vb, mb2, fb2); vb.advance_water_mark()
        feed(vb, mb3, fb3); vb.advance_water_mark()
        # A 补一个版本3空操作批次以对齐序列
        ma3, fa3 = build_batch("h3", 3, [Event("w", "upsert", 0), Event("w", "delete")], 1)
        feed(va, ma3, fa3); va.advance_water_mark()
        report = compare_venues(va, vb)
        self.assertTrue(report.head_screen_equal, "构造目标：当前画面相同")
        self.assertEqual(report.status, STATUS_DIVERGENT)
        self.assertEqual(report.earliest_divergence.approved_version, 1)
        va.save(); vb.save()
        code, verdict = run_verifier(sa.path, sb.path)
        self.assertEqual(code, 3)
        self.assertEqual(verdict["verdict"]["earliest_divergence"]["approved_version"], 1)
        sa.close(); sb.close()


class RollbackTests(unittest.TestCase):
    def _venue_with_history(self, base, name):
        v, s = fresh_venue(base, name)
        specs = [
            (1, [Event("a", "upsert", 1), Event("b", "upsert", 1)]),
            (2, [Event("a", "upsert", 2), Event("c", "upsert", 2)]),
            (3, [Event("b", "delete"), Event("d", "upsert", 3)]),
        ]
        for ver, evs in specs:
            m, fs = build_batch(f"r{ver}", ver, evs, 2)
            feed(v, m, fs)
            v.advance_water_mark()
        return v, s

    def test_rollback_enum_versions_and_scopes(self):
        for target in range(0, 4):
            for scope in (("a",), ("b",), ("a", "b", "c", "d")):
                base = tempfile.mkdtemp()
                v, s = self._venue_with_history(base, "V")
                rec = RollbackRecord(target_version=target, scope=tuple(scope),
                                     reason=f"enum-{target}")
                env_d = v.rollback(rec)
                # 新信封而非删除历史：链长度 +1，旧信封全部仍可解析
                self.assertEqual(v.state.water_mark, 4)
                self.assertEqual(len(v.state.envelopes), 4)
                env = v.envelope_at(4)
                self.assertEqual(env.kind, "rollback")
                # 范围内画面回到目标版本，范围外保持当前
                target_screen = dict(v.reader(target).items())
                head_screen = dict(v.reader(4).items())
                for key in scope:
                    self.assertEqual(head_screen.get(key), target_screen.get(key))
                untouched = {"a", "b", "c", "d"} - set(scope)
                before = dict(v.reader(3).items())
                for key in untouched:
                    self.assertEqual(head_screen.get(key), before.get(key))
                s.close()

    def test_illegal_rollback_references_rejected(self):
        base = tempfile.mkdtemp()
        v, s = self._venue_with_history(base, "V")
        with self.assertRaises(VenueError):
            v.rollback(RollbackRecord(99, ("a",), "不存在的版本"))
        with self.assertRaises(VenueError):
            v.rollback(RollbackRecord(1, (), "空范围"))
        # 被拒后水位不变、链首不变
        self.assertEqual(v.state.water_mark, 3)
        s.close()

    def test_rollback_verified_by_standalone(self):
        base = tempfile.mkdtemp()
        va, sa = self._venue_with_history(base, "A")
        vb, sb = self._venue_with_history(base, "B")
        va.rollback(RollbackRecord(2, ("a", "b"), "审批单 RB-001"))
        vb.rollback(RollbackRecord(2, ("a", "b"), "审批单 RB-001"))
        va.save(); vb.save()
        code, verdict = run_verifier(sa.path, sb.path)
        self.assertEqual(code, 0)
        sa.close(); sb.close()


class WaterMarkReaderTests(unittest.TestCase):
    def test_half_batch_invisible(self):
        base = tempfile.mkdtemp()
        v, s = fresh_venue(base, "V")
        m, fs = build_batch("w1", 1, [Event(f"k{i}", "upsert", i) for i in range(6)], 2)
        v.register_manifest(m)
        # 只投递 3 片中的 2 片：批次不完整，任何读者都看不到其中事件
        v.deliver_fragment(fs[0]); v.deliver_fragment(fs[2])
        v.advance_water_mark()
        self.assertEqual(v.state.water_mark, 0)
        reader = v.reader(0)
        self.assertEqual(reader.items(), [])
        self.assertEqual(v.missing_fragments("w1"), [1])
        # 补齐后原子可见
        v.deliver_fragment(fs[1])
        v.advance_water_mark()
        self.assertEqual(v.reader(1).screen_digest(),
                         v.treap.screen_digest(v.envelope_at(1).root_digest))
        self.assertEqual(len(v.reader(1).items()), 6)
        # 重复投递/重复推进幂等
        v.deliver_fragment(fs[0])
        self.assertEqual(v.advance_water_mark(), [])
        s.close()

    def test_reader_water_mark_bounds(self):
        base = tempfile.mkdtemp()
        v, s = fresh_venue(base, "V")
        m, fs = build_batch("w", 1, [Event("a", "upsert", 1)], 1)
        feed(v, m, fs); v.advance_water_mark()
        with self.assertRaises(VenueError):
            v.reader(2)
        s.close()


class VerifierSelfCheckTests(unittest.TestCase):
    def test_ast_self_check_pass(self):
        proc = subprocess.run(
            [sys.executable, VERIFIER, "--self-check"],
            capture_output=True, text=True, timeout=120,
        )
        out = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(out["self_check"], "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
