"""端到端演示：复现题面场景并输出带证据的对照结论。

四个场景
1. 逆序导入但完全一致      -> equivalent（当前画面与每个历史水位都相同）
2. 当前画面相同、历史分歧  -> divergent，输出最早的已证分歧
3. 缺段                    -> inconclusive，注明缺段起哪些结果还未证
4. 分片被篡改              -> 拒收留痕 + 最早分歧证据

每个场景都同时跑独立验证器（子进程），展示不依赖对照器内部索引的复核结果。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from snapshot_audit.compare import compare_venues  # noqa: E402
from snapshot_audit.model import Event, Fragment, build_batch  # noqa: E402
from snapshot_audit.store import PackStore  # noqa: E402
from snapshot_audit.venue import Venue  # noqa: E402

ARTIFACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
VERIFIER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "standalone_verifier.py")


def open_venue(base: str, name: str) -> tuple[Venue, PackStore]:
    store = PackStore(os.path.join(base, name))
    return Venue(name, store), store


def feed(venue: Venue, manifest, fragments, reverse: bool = False) -> None:
    venue.register_manifest(manifest)
    order = range(len(fragments))
    if reverse:
        order = list(reversed(list(order)))
    for i in order:
        venue.deliver_fragment(fragments[i])
    venue.advance_water_mark()


def verifier(dir_a: str, dir_b: str, report_path: str | None = None) -> dict:
    cmd = [sys.executable, VERIFIER, dir_a, dir_b, "--history", "full"]
    if report_path:
        cmd += ["--report", report_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return {"returncode": proc.returncode, "body": json.loads(proc.stdout)}


def scenario_1_equivalent(base: str) -> dict:
    va, sa = open_venue(base, "A")
    vb, sb = open_venue(base, "B")
    # 各批键互不相交：顺序无关性保证逆序导入也得到逐集合相同的根与画面
    batches = [
        build_batch("cfg-001", 1, [Event("r1", "upsert", {"v": 1}),
                                   Event("r2", "upsert", {"v": 2})], 1),
        build_batch("cfg-002", 2, [Event("r3", "upsert", {"v": 3}),
                                   Event("r4", "upsert", {"v": 4})], 2),
        build_batch("cfg-003", 3, [Event("r5", "upsert", {"v": 5}),
                                   Event("r6", "upsert", {"v": 6})], 1),
    ]
    for m, fs in batches:
        feed(va, m, fs)
    for m, fs in reversed(batches):  # B 逆序、分片也逆序
        feed(vb, m, fs, reverse=True)
    report = compare_venues(va, vb)
    va.save(); vb.save()
    report_path = os.path.join(ARTIFACTS, "demo1_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
    v = verifier(sa.path, sb.path, report_path)
    sa.close(); sb.close()
    return {"engine": report.render_text(), "verifier_code": v["returncode"],
            "verifier_status": v["body"]["verdict"]["status"],
            "cross_check_problems": v["body"]["cross_check_problems"]}


def scenario_2_same_screen_divergent_history(base: str) -> dict:
    va, sa = open_venue(base, "A")
    vb, sb = open_venue(base, "B")
    # A：版本1 先写 x=1，版本2 改成 x=2；B：版本1 直接写 x=2
    m1a, f1a = build_batch("cfg-001", 1, [Event("x", "upsert", 1)], 1)
    m2a, f2a = build_batch("cfg-002", 2, [Event("x", "upsert", 2)], 1)
    feed(va, m1a, f1a); feed(va, m2a, f2a)
    m1b, f1b = build_batch("cfg-001", 1, [Event("x", "upsert", 2)], 1)
    m2b, f2b = build_batch("cfg-002", 2, [Event("z", "upsert", 9)], 1)
    m3b, f3b = build_batch("cfg-003", 3, [Event("z", "delete")], 1)
    m3a, f3a = build_batch("cfg-003", 3, [Event("z", "upsert", 9),
                                          Event("z", "delete")], 1)
    feed(vb, m1b, f1b); feed(vb, m2b, f2b); feed(vb, m3b, f3b)
    feed(va, m3a, f3a)
    report = compare_venues(va, vb)
    va.save(); vb.save()
    report_path = os.path.join(ARTIFACTS, "demo2_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
    v = verifier(sa.path, sb.path, report_path)
    sa.close(); sb.close()
    return {"engine": report.render_text(), "verifier_code": v["returncode"],
            "verifier_status": v["body"]["verdict"]["status"],
            "cross_check_problems": v["body"]["cross_check_problems"]}


def scenario_3_gap(base: str) -> dict:
    va, sa = open_venue(base, "A")
    vb, sb = open_venue(base, "B")
    batches = [
        build_batch("cfg-001", 1, [Event("a", "upsert", 1)], 1),
        build_batch("cfg-002", 2, [Event(f"k{i}", "upsert", i) for i in range(4)], 2),
        build_batch("cfg-003", 3, [Event("c", "upsert", 3)], 1),
    ]
    for m, fs in batches:
        feed(va, m, fs)
    # B：批次2 的两个分片只到一个 -> 缺段；批次1、3 完整
    m1, f1 = batches[0]
    m2, f2 = batches[1]
    m3, f3 = batches[2]
    feed(vb, m1, f1)
    vb.register_manifest(m2)
    vb.deliver_fragment(f2[0])  # 缺 f2[1]
    vb.advance_water_mark()
    feed(vb, m3, f3)
    report = compare_venues(va, vb)
    va.save(); vb.save()
    report_path = os.path.join(ARTIFACTS, "demo3_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
    v = verifier(sa.path, sb.path, report_path)
    sa.close(); sb.close()
    return {"engine": report.render_text(), "verifier_code": v["returncode"],
            "verifier_status": v["body"]["verdict"]["status"],
            "cross_check_problems": v["body"]["cross_check_problems"]}


def scenario_4_tamper(base: str) -> dict:
    va, sa = open_venue(base, "A")
    vb, sb = open_venue(base, "B")
    manifest, fragments = build_batch(
        "cfg-001", 1, [Event("r1", "upsert", {"v": 1}),
                       Event("r2", "upsert", {"v": 2})], 1
    )
    feed(va, manifest, fragments)
    vb.register_manifest(manifest)
    bad_payload = fragments[0].to_dict()
    bad_payload["events"][0]["value"] = {"v": 999}  # 篡改载荷
    bad = Fragment.from_dict(bad_payload)
    accepted = vb.deliver_fragment(bad)
    assert accepted is False
    vb.advance_water_mark()
    report = compare_venues(va, vb)
    va.save(); vb.save()
    report_path = os.path.join(ARTIFACTS, "demo4_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
    v = verifier(sa.path, sb.path, report_path)
    sa.close(); sb.close()
    return {"engine": report.render_text(), "verifier_code": v["returncode"],
            "verifier_status": v["body"]["verdict"]["status"],
            "cross_check_problems": v["body"]["cross_check_problems"],
            "rejected": vb.state.rejected}


def main() -> int:
    os.makedirs(ARTIFACTS, exist_ok=True)
    base = tempfile.mkdtemp(prefix="snapshot_demo_")
    scenarios = [
        ("场景1 逆序导入、逐版本等价", scenario_1_equivalent),
        ("场景2 当前画面相同但历史分歧", scenario_2_same_screen_divergent_history),
        ("场景3 缺段：未证区间显式标注", scenario_3_gap),
        ("场景4 分片篡改：拒收与最早分歧", scenario_4_tamper),
    ]
    summary = []
    for title, fn in scenarios:
        print("=" * 78)
        print(title)
        print("=" * 78)
        out = fn(base)
        print(out["engine"])
        print(f"独立验证器退出码={out['verifier_code']} 裁决={out['verifier_status']} "
              f"交叉核对问题={out['cross_check_problems']}")
        if "rejected" in out:
            print("拒收留痕：", json.dumps(out["rejected"], ensure_ascii=False))
        summary.append({"title": title,
                        "verifier_code": out["verifier_code"],
                        "verifier_status": out["verifier_status"]})
        print()
    with open(os.path.join(ARTIFACTS, "demo_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print("报告与裁决已保存到 artifacts/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
