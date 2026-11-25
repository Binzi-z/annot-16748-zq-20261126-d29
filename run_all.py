"""一键运行：AST 自检 -> 全枚举测试 -> 5 万条压测 -> 端到端演示。

用法：python run_all.py
任一步失败立即以非零码退出，便于在审查环境中复现。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def step(title: str, cmd: list[str], timeout: int | None = None) -> None:
    print("\n" + "#" * 80)
    print(f"# {title}")
    print("#" * 80)
    print("$", " ".join(cmd))
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT, timeout=timeout)
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        raise SystemExit(f"步骤失败（退出码 {proc.returncode}）：{title}，耗时 {dt:.1f}s")
    print(f"[OK] {title}，耗时 {dt:.1f}s")


def main() -> int:
    py = sys.executable
    step("1/4 独立验证器 AST 自检（不依赖引擎与内部索引）",
         [py, "standalone_verifier.py", "--self-check"])
    step("2/4 小输入全枚举（事件顺序/缺段/篡改/回滚/水位）",
         [py, "-m", "unittest", "tests.test_exhaustive", "-v"], timeout=900)
    step("3/4 五万条规则修订压力实测",
         [py, os.path.join("tests", "test_stress_50k.py")], timeout=1800)
    step("4/4 端到端四场景演示",
         [py, "run_demo.py"], timeout=600)
    result = os.path.join(ROOT, "artifacts", "stress_result.json")
    if os.path.exists(result):
        with open(result, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        print("\n===== 压测关键实测值 =====")
        print(json.dumps({
            "scale": data["scale"],
            "single_batch_cold_visits": data["single_batch_node_visits"],
            "full_rebuild_cold_visits": {
                k: {"node_visits": v["node_visits"], "pack_reads": v["pack_reads"]}
                for k, v in data["full_rebuild_cold"].items()
            },
            "comparison": data["comparison"],
            "old_snapshot_reopen_unchanged":
                data["old_snapshot_kept"]["reopen_root_unchanged"],
            "standalone_verifier": data["standalone_verifier"],
        }, ensure_ascii=False, indent=2))
    print("\n全部步骤通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
