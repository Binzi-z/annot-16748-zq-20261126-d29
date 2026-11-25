"""仅追加打包的内容寻址不可变存储。

设计要点
--------
1. 每个 blob 是一个带自验摘要的记录：规范化 JSON 单行，键 "d" 为摘要、
   "b" 为负载。读取时重算 SHA-256 校验，任何字节级篡改都会被发现。
2. blob 按到达顺序追加进定长 pack 文件（pack-000000.rec ...），并维护
   digest -> (pack 序号, 字节偏移) 的 *本地* 索引。该索引只是读取加速，
   删掉后可通过顺序扫描 pack 完整重建——独立验证器不使用它。
3. blob 一经写入永不修改、永不删除（put 同一内容直接去重），因此旧快照
   引用的历史节点始终可读，"旧快照保持"由存储层保证。
4. 访问计量：内存命中 mem_hits 与 pack 实读 pack_reads 分开计数，
   供"补单批访问节点 / 全量重建访问节点"实测使用。
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from typing import Any

from .model import canonical, digest

PACK_RECORDS = 100_000  # 每个 pack 最多容纳的记录数
INDEX_FILENAME = "index.json"
PACK_PREFIX = "pack-"
PACK_SUFFIX = ".rec"


class BlobError(Exception):
    """blob 缺失、摘要不符等存储层错误。"""


class PackStore:
    def __init__(self, path: str, cache_cap: int = 100_000) -> None:
        self.path = path
        self.blob_dir = os.path.join(path, "blobs")
        os.makedirs(self.blob_dir, exist_ok=True)
        self.cache_cap = cache_cap
        # digest -> [pack_no, offset]
        self._index: dict[str, list[int]] = {}
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._read_handles: dict[int, Any] = {}
        self._pack_no = 0
        self._pack_count = 0
        self._append_handle = None
        # 访问/写入计量
        self.mem_hits = 0
        self.pack_reads = 0
        self.writes = 0          # 新写入的 blob 数
        self.dedup_puts = 0      # 提交时命中已有内容的次数
        self._load_index()
        self._open_append_pack()

    # ------------------------------------------------------------------
    # 初始化与持久化
    # ------------------------------------------------------------------
    def _index_path(self) -> str:
        return os.path.join(self.blob_dir, INDEX_FILENAME)

    def _pack_path(self, pack_no: int) -> str:
        return os.path.join(
            self.blob_dir, f"{PACK_PREFIX}{pack_no:06d}{PACK_SUFFIX}"
        )

    def _load_index(self) -> None:
        idx_path = self._index_path()
        if os.path.exists(idx_path):
            with open(idx_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            self._index = {k: list(v) for k, v in saved["entries"].items()}
            self._pack_no = saved["pack_no"]
            self._pack_count = saved["pack_count"]
        else:
            self.rebuild_index()

    def rebuild_index(self) -> None:
        """完全不依赖现有索引，顺序扫描全部 pack 重建（验证器思路的引擎侧版本）。"""
        self._index.clear()
        pack_no = 0
        while os.path.exists(self._pack_path(pack_no)):
            offset = 0
            with open(self._pack_path(pack_no), "rb") as fh:
                for raw in fh:
                    rec = json.loads(raw.decode("utf-8"))
                    if digest(rec["b"]) != rec["d"]:
                        raise BlobError(
                            f"pack {pack_no} 偏移 {offset} 处 blob 摘要不符"
                        )
                    self._index[rec["d"]] = [pack_no, offset]
                    offset += len(raw)
            pack_no += 1
        if pack_no:
            self._pack_no = pack_no - 1
            self._pack_count = self._count_in_pack(self._pack_path(self._pack_no))
        else:
            self._pack_no = 0
            self._pack_count = 0
        self.flush_index()

    @staticmethod
    def _count_in_pack(path: str) -> int:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)

    def flush_index(self) -> None:
        tmp = self._index_path() + ".tmp"
        payload = {
            "pack_no": self._pack_no,
            "pack_count": self._pack_count,
            "entries": self._index,
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
        os.replace(tmp, self._index_path())

    def _open_append_pack(self) -> None:
        if not os.path.exists(self._pack_path(self._pack_no)):
            self._pack_count = 0
        else:
            self._pack_count = self._count_in_pack(self._pack_path(self._pack_no))
        self._append_handle = open(
            self._pack_path(self._pack_no), "ab", buffering=0
        )

    def close(self) -> None:
        if self._append_handle is not None:
            self._append_handle.close()
            self._append_handle = None
        for fh in self._read_handles.values():
            fh.close()
        self._read_handles.clear()
        self.flush_index()

    def __enter__(self) -> "PackStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def put(self, payload: dict[str, Any]) -> str:
        """写入一个负载，返回其内容摘要；相同内容自动去重。"""
        d = digest(payload)
        if d in self._index:
            self.dedup_puts += 1
            return d
        line = canonical({"d": d, "b": payload}) + b"\n"
        if self._pack_count >= PACK_RECORDS:
            self._append_handle.close()
            self._pack_no += 1
            self._pack_count = 0
            self._append_handle = open(
                self._pack_path(self._pack_no), "ab", buffering=0
            )
        offset = self._append_handle.seek(0, os.SEEK_END)
        self._append_handle.write(line)
        self._index[d] = [self._pack_no, offset]
        self._pack_count += 1
        self.writes += 1
        self._cache_put(d, payload)
        return d

    def _cache_put(self, d: str, payload: dict[str, Any]) -> None:
        self._cache[d] = payload
        self._cache.move_to_end(d)
        while len(self._cache) > self.cache_cap:
            self._cache.popitem(last=False)

    def get(self, d: str) -> dict[str, Any]:
        """按摘要读取负载并做字节级自验；缺失或被篡改即抛 BlobError。"""
        cached = self._cache.get(d)
        if cached is not None:
            self._cache.move_to_end(d)
            self.mem_hits += 1
            return cached
        loc = self._index.get(d)
        if loc is None:
            raise BlobError(f"未知 blob：{d[:16]}…")
        pack_no, offset = loc
        fh = self._read_handles.get(pack_no)
        if fh is None:
            fh = open(self._pack_path(pack_no), "rb")
            self._read_handles[pack_no] = fh
        fh.seek(offset)
        raw = fh.readline()
        self.pack_reads += 1
        rec = json.loads(raw.decode("utf-8"))
        if digest(rec["b"]) != d or rec["d"] != d:
            raise BlobError(f"blob {d[:16]}… 内容与摘要不符（疑似篡改）")
        payload = rec["b"]
        self._cache_put(d, payload)
        return payload

    def contains(self, d: str) -> bool:
        return d in self._index

    def all_blob_digests(self) -> set[str]:
        """返回已知全部 blob 摘要（基于索引；验证器自行扫描 pack 不使用本方法）。"""
        return set(self._index)

    # ------------------------------------------------------------------
    # 计量
    # ------------------------------------------------------------------
    def counters(self) -> dict[str, int]:
        return {
            "mem_hits": self.mem_hits,
            "pack_reads": self.pack_reads,
            "writes": self.writes,
            "dedup_puts": self.dedup_puts,
            "cached_blobs": len(self._cache),
            "known_blobs": len(self._index),
        }

    def reset_counters(self) -> None:
        self.mem_hits = 0
        self.pack_reads = 0
        self.writes = 0
        self.dedup_puts = 0

    def node_visits(self) -> int:
        """访问节点总数 = 内存命中 + pack 实读。"""
        return self.mem_hits + self.pack_reads
