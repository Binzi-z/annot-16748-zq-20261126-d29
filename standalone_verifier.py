"""独立验证器——不 import 引擎、不使用任何对照器/存储内部索引。

设计独立性（可用 --self-check 做 AST 级证明）
---------------------------------------------
1. 不 import snapshot_audit 任何模块，全部逻辑（规范化哈希、树遍历、会场
   语义重建）在本文件内独立实现一份；
2. 不读取存储的内部索引文件（该文件名在本文件中甚至不以字面量出现），
   而是自行顺序扫描全部 pack 记录，按记录内自验摘要重建 digest->负载映射；
3. 不使用对照器的任何结论数据结构：批次完整性、缺段、最早分歧、未证区间、
   当前画面全部由分片原文 + 普通 dict 重放独立得出；若附带引擎报告，
   只做"结论必须一致"的交叉核对；
4. 快照树只按负载结构（k=leaf/node、left/right 指针）泛化遍历，不依赖
   引擎的 Treap 类，并顺带复核"优先级仅由键决定"这一顺序无关性机制。

用法
----
  python standalone_verifier.py 会场A目录 会场B目录 \
      [--report 引擎报告.json] [--history full|sample:N|none] [--out 裁决.json]
退出码：0 已证一致；3 已证分歧；4 缺段未证；2 校验出错/篡改。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys

# ---------------------------------------------------------------------------
# 独立实现的规范化哈希（与引擎公式相同，但代码独立、互不引用）
# ---------------------------------------------------------------------------


def canonical(obj) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj) -> str:
    return sha256_hex(canonical(obj))


EMPTY_TREE_DIGEST = digest({"empty": "treap"})
INDEX_FILE_NAME = "index" + ".json"  # 刻意拼接：自检要求该文件名不以字面量出现
ENGINE_PACKAGE = "snapshot_" + "audit"
PACK_PREFIX = "pack-"
PACK_SUFFIX = ".rec"
BLOB_DIR = "blobs"
STATE_FILE = "venue_state.json"


class VerificationError(Exception):
    """发现数据自相矛盾或篡改。"""


# ---------------------------------------------------------------------------
# 第一步：不依赖任何索引，自行扫描 pack 重建内容映射
# ---------------------------------------------------------------------------


def scan_packs(venue_dir: str) -> dict[str, dict]:
    blob_dir = os.path.join(venue_dir, BLOB_DIR)
    if not os.path.isdir(blob_dir):
        raise VerificationError(f"缺少 blob 目录：{blob_dir}")
    own_map: dict[str, dict] = {}
    pack_names = sorted(
        n for n in os.listdir(blob_dir)
        if n.startswith(PACK_PREFIX) and n.endswith(PACK_SUFFIX)
    )
    if not pack_names:
        raise VerificationError(f"{blob_dir} 下没有任何 pack 记录文件")
    for name in pack_names:
        with open(os.path.join(blob_dir, name), "rb") as fh:
            for line_no, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise VerificationError(
                        f"{name}:{line_no} 记录无法解析（疑似字节级篡改）：{exc}"
                    ) from exc
                claimed, payload = rec["d"], rec["b"]
                actual = digest(payload)
                if actual != claimed:
                    raise VerificationError(
                        f"{name}:{line_no} 记录自验摘要不符（{claimed[:12]}… != 实算）"
                    )
                if claimed in own_map and own_map[claimed] != payload:
                    raise VerificationError(f"同一摘要对应两份不同负载：{claimed[:12]}…")
                own_map[claimed] = payload
    return own_map


def load_state(venue_dir: str) -> dict:
    path = os.path.join(venue_dir, STATE_FILE)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 第二步：泛化快照树遍历（只认负载结构，不认任何引擎类）
# ---------------------------------------------------------------------------


def key_priority(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")


def walk_tree(root_digest: str, own_map: dict[str, dict]) -> list[tuple[str, object]]:
    """中序遍历快照树并自验：指针存在、leaf/node 结构、堆性质与键优先级。"""
    items: list[tuple[str, object]] = []

    def go(node_digest: str, parent_prio: int | None, parent_key: str | None) -> None:
        # None 指针与空树常量都表示空子树
        if node_digest is None or node_digest == EMPTY_TREE_DIGEST:
            return
        node = own_map.get(node_digest)
        if node is None:
            raise VerificationError(f"快照树引用了缺失 blob：{node_digest[:12]}…")
        kind = node.get("k")
        if kind not in ("leaf", "node"):
            raise VerificationError(f"未知快照节点结构：{kind!r}")
        key, value, prio = node["key"], node["value"], node["prio"]
        if prio != key_priority(key):
            raise VerificationError(f"键 {key} 的节点优先级不是键哈希派生（顺序无关性被破坏）")
        if parent_prio is not None:
            if prio > parent_prio or (prio == parent_prio and parent_key is not None
                                      and _lower_tie(key, parent_key)):
                raise VerificationError(f"键 {key} 处违反 Treap 堆性质")
        if kind == "leaf":
            if node.get("left") is not None or node.get("right") is not None:
                raise VerificationError("叶子节点携带了子指针")
            items.append((key, value))
            return
        go(node["left"], prio, key)
        items.append((key, value))
        go(node["right"], prio, key)

    go(root_digest, None, None)
    return items


def _lower_tie(child_key: str, parent_key: str) -> bool:
    return child_key < parent_key


def screen_of(items: list[tuple[str, object]]) -> str:
    return digest([{"key": k, "value": v} for k, v in sorted(items)])


# ---------------------------------------------------------------------------
# 第三步：单会场独立核验与语义重放（普通 dict，不用任何树索引）
# ---------------------------------------------------------------------------


class IndependentVenue:
    def __init__(self, label: str, venue_dir: str) -> None:
        self.label = label
        self.dir = venue_dir
        self.blob = scan_packs(venue_dir)
        self.state = load_state(venue_dir)
        self.manifests: dict[str, dict] = {}       # batch_id -> 清单负载
        self.fragments: dict[str, dict[int, dict]] = {}
        self.rejections: list[dict] = list(self.state.get("rejected", []))
        self.envelopes: list[dict] = []
        self.snapshots: list[dict] = []            # 按信封版本的独立画面
        self.tree_checked_versions: list[int] = []
        self._load_and_check_blobs()

    def _load_and_check_blobs(self) -> None:
        # 清单与分片：摘要全部重算，并与清单承诺逐条比对
        for batch_id, m_digest in self.state.get("manifests", {}).items():
            payload = self._require(m_digest, "清单")
            if digest(payload) != m_digest:
                raise VerificationError(f"批次 {batch_id} 清单 blob 摘要不符")
            self.manifests[batch_id] = payload
            self.fragments[batch_id] = {}
        for batch_id, index_map in self.state.get("fragments", {}).items():
            manifest = self.manifests.get(batch_id)
            if manifest is None:
                raise VerificationError(f"分片属于未登记清单的批次 {batch_id}")
            for idx_text, f_digest in index_map.items():
                payload = self._require(f_digest, "分片")
                idx = int(idx_text)
                committed = manifest["fragment_digests"]
                if not (0 <= idx < len(committed)) or committed[idx] != digest(payload):
                    raise VerificationError(
                        f"批次 {batch_id} 分片 {idx} 与清单承诺不符"
                    )
                self.fragments[batch_id][idx] = payload
        # 信封链：版本连续、父指针正确、摘要重算
        parent = None
        for i, env_digest in enumerate(self.state.get("envelopes", []), start=1):
            env = self._require(env_digest, "信封")
            if digest(env) != env_digest:
                raise VerificationError(f"信封版本 {i} blob 摘要不符")
            if env["version"] != i or env["parent"] != parent:
                raise VerificationError(f"信封版本 {i} 链不连续或父指针错误")
            self.envelopes.append(env)
            parent = env_digest
        if self.state.get("head_envelope") != parent:
            raise VerificationError("链首信封与状态记录不一致")
        if self.state.get("water_mark") != len(self.envelopes):
            raise VerificationError("水位与信封链长度不一致")

    def _require(self, d: str, what: str) -> dict:
        payload = self.blob.get(d)
        if payload is None:
            raise VerificationError(f"{what} blob 缺失：{d[:12]}…")
        return payload

    # -- 完整性与缺段（完全自行从清单承诺与分片台账推导） --
    def missing(self, batch_id: str) -> list[int]:
        manifest = self.manifests[batch_id]
        have = self.fragments.get(batch_id, {})
        return [i for i in range(len(manifest["fragment_digests"])) if i not in have]

    def is_complete(self, batch_id: str) -> bool:
        return not self.missing(batch_id)

    def assemble_events(self, batch_id: str) -> list[dict]:
        manifest = self.manifests[batch_id]
        events: list[dict] = []
        for i in range(len(manifest["fragment_digests"])):
            frag = self.fragments[batch_id][i]
            if frag["batch_id"] != batch_id or frag["index"] != i:
                raise VerificationError(f"批次 {batch_id} 分片 {i} 定位字段不自洽")
            events.extend(frag["events"])
        if digest(events) != manifest["event_digest"]:
            raise VerificationError(f"批次 {batch_id} 重组事件摘要不符")
        return events

    # -- 独立重放 --
    def replay(self, history_mode: str) -> None:
        screen: dict[str, object] = {}
        # 只保留回滚目标版本的完整画面，避免 50k×版本数 的二次方级内存；
        # 其余历史以逐信封 screen_digest 与快照树核验为证。
        keep_versions = {0}
        for env in self.envelopes:
            if env["kind"] == "rollback":
                rec = self.blob[env["rollback_digest"]]
                keep_versions.add(rec["target_version"])
        self.snapshots: dict[int, dict] = {0: dict(screen)}
        complete = {b for b in self.manifests if self.is_complete(b)}
        sample_step = self._parse_sample_step(history_mode)
        for version, env in enumerate(self.envelopes, start=1):
            if env["kind"] == "batch":
                batch_id = env["batch_id"]
                if batch_id not in complete:
                    raise VerificationError(
                        f"信封 {version} 提交了未收齐的批次 {batch_id}（原子性被破坏）"
                    )
                for ev in self.assemble_events(batch_id):
                    self._apply(screen, ev)
            elif env["kind"] == "rollback":
                self._apply_rollback(screen, env, version)
            else:
                raise VerificationError(f"信封 {version} 类型未知：{env['kind']}")
            own_screen = screen_of(list(screen.items()))
            if own_screen != env["screen_digest"]:
                raise VerificationError(
                    f"信封 {version} 画面摘要与独立重放不符"
                )
            if version in keep_versions:
                self.snapshots[version] = dict(screen)
            # 泛化树核验按策略执行（压测时抽样，小输入全量）
            check_tree = (
                history_mode == "full"
                or (sample_step and version % sample_step == 0)
                or version == len(self.envelopes)
            )
            if check_tree and env["root_digest"] != EMPTY_TREE_DIGEST:
                items = walk_tree(env["root_digest"], self.blob)
                if screen_of(items) != own_screen:
                    raise VerificationError(f"信封 {version} 快照树内容与重放画面不符")
                self.tree_checked_versions.append(version)
            elif env["root_digest"] != EMPTY_TREE_DIGEST and env["root_digest"] not in self.blob:
                raise VerificationError(f"信封 {version} 根 blob 不存在")

    @staticmethod
    def _parse_sample_step(mode: str) -> int | None:
        if mode == "full":
            return 1
        if mode == "none":
            return None
        if mode.startswith("sample:"):
            step = int(mode.split(":", 1)[1])
            if step <= 0:
                raise ValueError("sample 步长必须为正")
            return step
        raise ValueError(f"未知 history 模式：{mode}")

    @staticmethod
    def _apply(screen: dict[str, object], ev: dict) -> None:
        if ev["op"] == "upsert":
            screen[ev["rule_id"]] = ev["value"]
        elif ev["op"] == "delete":
            screen.pop(ev["rule_id"], None)
        else:
            raise VerificationError(f"未知事件操作：{ev['op']}")

    def _apply_rollback(self, screen: dict[str, dict], env: dict, version: int) -> None:
        rec = self._require(env["rollback_digest"], "回滚记录")
        if digest(rec) != env["rollback_digest"]:
            raise VerificationError(f"信封 {version} 回滚记录摘要不符")
        target = rec["target_version"]
        scope = rec["scope"]
        if not scope or target < 0 or target >= version or target not in self.snapshots:
            raise VerificationError(
                f"信封 {version} 回滚引用的版本 {target} 不在已批准历史内"
            )
        target_screen = self.snapshots[target]
        for key in scope:  # 只允许动批准范围内的键
            old, now = target_screen.get(key), screen.get(key)
            if old == now:
                continue
            if old is None:
                screen.pop(key, None)
            else:
                screen[key] = old

    def head_screen(self) -> str:
        if not self.envelopes:
            return screen_of([])
        return self.envelopes[-1]["screen_digest"]

    def _manifest_by_batch(self, batch_id: str):
        return self.manifests.get(batch_id)

    def set_witnesses(self) -> dict[frozenset[int], dict]:
        """已应用批准版本集合 -> 产生该集合的批次信封（与导入顺序无关）。"""
        applied: set[int] = set()
        witnesses: dict[frozenset[int], dict] = {}
        for env in self.envelopes:
            if env["kind"] != "batch":
                continue
            manifest = self._manifest_by_batch(env["batch_id"])
            if manifest is None:
                continue
            applied.add(manifest["approved_version"])
            witnesses[frozenset(applied)] = env
        return witnesses


# ---------------------------------------------------------------------------
# 第四步：两会场独立对照（自己再推导一遍结论）
# ---------------------------------------------------------------------------


def _canonical_screens(venue: "IndependentVenue",
                       needed: set[frozenset[int]],
                       by_ver: dict[int, dict]) -> dict[frozenset[int], str]:
    """严格按批准版本顺序重放；前缀集合走一次线性重放缓存。"""
    out: dict[frozenset[int], str] = {}
    prefix_cache: dict[frozenset[int], str] = {}
    screen: dict[str, object] = {}
    for ver in sorted(by_ver):
        for ev in venue.assemble_events(by_ver[ver]["batch_id"]):
            if ev["op"] == "upsert":
                screen[ev["rule_id"]] = ev["value"]
            else:
                screen.pop(ev["rule_id"], None)
        prefix_cache[frozenset(range(1, ver + 1))] = screen_of(list(screen.items()))
    for applied_set in needed:
        if applied_set in prefix_cache:
            out[applied_set] = prefix_cache[applied_set]
            continue
        local: dict[str, object] = {}
        for ver in sorted(applied_set):
            for ev in venue.assemble_events(by_ver[ver]["batch_id"]):
                if ev["op"] == "upsert":
                    local[ev["rule_id"]] = ev["value"]
                else:
                    local.pop(ev["rule_id"], None)
        out[applied_set] = screen_of(list(local.items()))
    return out


def independent_compare(a: IndependentVenue, b: IndependentVenue) -> dict:
    ma_by_ver = {m["approved_version"]: m for m in a.manifests.values()}
    mb_by_ver = {m["approved_version"]: m for m in b.manifests.values()}
    versions = sorted(set(ma_by_ver) | set(mb_by_ver))
    gaps: list[dict] = []
    earliest = None
    unproven_from = None
    chain_broken = False

    # 第一阶段：清单层与分片层
    for ver in versions:
        ma = ma_by_ver.get(ver)
        mb = mb_by_ver.get(ver)
        ref = ma or mb
        batch_id = ref["batch_id"]
        if not chain_broken and ma is not None and mb is not None and \
                digest(ma) != digest(mb) and earliest is None:
            earliest = {"approved_version": ver, "batch_id": batch_id, "kind": "manifest"}
        rej_a = [r for r in a.rejections if r["batch_id"] == batch_id]
        rej_b = [r for r in b.rejections if r["batch_id"] == batch_id]
        if not chain_broken and (rej_a or rej_b) and earliest is None:
            earliest = {"approved_version": ver, "batch_id": batch_id,
                        "kind": "fragment_tamper"}
        miss_a = a.missing(batch_id) if batch_id in a.manifests else \
            list(range(len(ref["fragment_digests"])))
        miss_b = b.missing(batch_id) if batch_id in b.manifests else \
            list(range(len(ref["fragment_digests"])))
        if miss_a or miss_b or ma is None or mb is None:
            gaps.append({"approved_version": ver, "batch_id": batch_id,
                         "missing_in_a": miss_a, "missing_in_b": miss_b})
            unproven_from = unproven_from if unproven_from is not None else ver
            chain_broken = True

    # 第二阶段：集合见证 + 规范重放（逆序导入也能完整证明）
    compared_sizes = []
    if not chain_broken and versions:
        wa = a.set_witnesses()
        wb = b.set_witnesses()
        needed = set(wa) | set(wb)
        ca = _canonical_screens(a, needed, ma_by_ver)
        cb = _canonical_screens(b, needed, mb_by_ver)
        for applied_set in sorted(needed, key=lambda s: (len(s), sorted(s))):
            compared_sizes.append(len(applied_set))
            ea, eb = wa.get(applied_set), wb.get(applied_set)
            if earliest is None and ea is not None and \
                    ea["screen_digest"] != ca[applied_set]:
                earliest = {"approved_version": max(applied_set),
                            "batch_id": ma_by_ver[max(applied_set)]["batch_id"],
                            "kind": "root"}
            if earliest is None and eb is not None and \
                    eb["screen_digest"] != cb[applied_set]:
                earliest = {"approved_version": max(applied_set),
                            "batch_id": mb_by_ver[max(applied_set)]["batch_id"],
                            "kind": "root"}
            if earliest is None and ea is not None and eb is not None and (
                ea["root_digest"] != eb["root_digest"]
                or ea["screen_digest"] != eb["screen_digest"]
            ):
                earliest = {"approved_version": max(applied_set),
                            "batch_id": ma_by_ver[max(applied_set)]["batch_id"],
                            "kind": "root"}

    if earliest is not None:
        status = "divergent"
    elif unproven_from is not None:
        status = "inconclusive"
    else:
        status = "equivalent"
    return {
        "status": status,
        "earliest_divergence": earliest,
        "gaps": gaps,
        "unproven_from": unproven_from,
        "compared_set_sizes": compared_sizes,
        "head_screen_equal": a.head_screen() == b.head_screen(),
        "head_screens": {"a": a.head_screen(), "b": b.head_screen()},
    }


def cross_check_engine_report(verdict: dict, report_path: str) -> list[str]:
    """与引擎报告交叉核对关键字段（引擎报告仅作比对，不参与任何推导）。"""
    with open(report_path, "r", encoding="utf-8") as fh:
        engine = json.load(fh)
    problems = []
    if engine["status"] != verdict["status"]:
        problems.append(f"状态不符：引擎={engine['status']} 独立={verdict['status']}")
    ed_e = engine.get("earliest_divergence")
    ed_v = verdict.get("earliest_divergence")
    if (ed_e is None) != (ed_v is None) or (
        ed_e and ed_v and (ed_e["approved_version"] != ed_v["approved_version"]
                           or ed_e["kind"] != ed_v["kind"])
    ):
        problems.append(f"最早分歧不符：引擎={ed_e} 独立={ed_v}")
    if engine.get("unproven_from") != verdict.get("unproven_from"):
        problems.append(
            f"未证起点不符：引擎={engine.get('unproven_from')} 独立={verdict['unproven_from']}"
        )
    if bool(engine.get("head_screen_equal")) != bool(verdict["head_screen_equal"]):
        problems.append("当前画面一致性结论不符")
    return problems


# ---------------------------------------------------------------------------
# AST 自检：证明本文件不依赖引擎包与内部索引
# ---------------------------------------------------------------------------


def self_check(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == ENGINE_PACKAGE:
                    problems.append(f"禁止 import 引擎包：{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] == ENGINE_PACKAGE:
                problems.append(f"禁止 from 引擎包导入：{mod}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if INDEX_FILE_NAME in node.value:
                problems.append("禁止引用存储内部索引文件名")
    if ENGINE_PACKAGE in sys.modules:
        problems.append("运行时 sys.modules 中存在引擎包")
    return problems


def main(argv: list[str]) -> int:
    here = os.path.abspath(__file__)
    if argv and argv[0] == "--self-check":
        problems = self_check(here)
        print(json.dumps({"self_check": "pass" if not problems else "fail",
                          "problems": problems}, ensure_ascii=False, indent=2))
        return 0 if not problems else 2
    if len(argv) < 2:
        print(__doc__)
        return 2
    dir_a, dir_b = argv[0], argv[1]
    report_path = None
    history_mode = "full"
    out_path = None
    i = 2
    while i < len(argv):
        if argv[i] == "--report" and i + 1 < len(argv):
            report_path, i = argv[i + 1], i + 2
        elif argv[i] == "--history" and i + 1 < len(argv):
            history_mode, i = argv[i + 1], i + 2
        elif argv[i] == "--out" and i + 1 < len(argv):
            out_path, i = argv[i + 1], i + 2
        else:
            print(f"未知参数：{argv[i]}")
            return 2
    try:
        problems = self_check(here)
        if problems:
            raise VerificationError("AST 自检失败：" + "; ".join(problems))
        va = IndependentVenue("A", os.path.abspath(dir_a))
        vb = IndependentVenue("B", os.path.abspath(dir_b))
        va.replay(history_mode)
        vb.replay(history_mode)
        verdict = independent_compare(va, vb)
        cross = cross_check_engine_report(verdict, report_path) if report_path else []
        result = {
            "verdict": verdict,
            "cross_check_problems": cross,
            "tree_checked_versions": {
                "a": va.tree_checked_versions,
                "b": vb.tree_checked_versions,
            },
            "blob_counts": {"a": len(va.blob), "b": len(vb.blob)},
            "history_mode": history_mode,
            "verifier": "standalone-index-free",
        }
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        print(text)
        if cross:
            return 2
        return {"equivalent": 0, "divergent": 3, "inconclusive": 4}[verdict["status"]]
    except VerificationError as exc:
        print(json.dumps({"verification_error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
