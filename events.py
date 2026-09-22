"""只追加的哈希链事件日志。

领域中的一切事实（登记、出土、交接、检测、释读、发布……）都以事件形式
追加到日志，任何事件都不可修改或删除。每条事件保存：

* ``seq``        全局序号（记录顺序，仅用于哈希链与存储）；
* ``occurred_at`` 业务发生时间（野外设备可能在离线补录时回填）；
* ``recorded_at`` 服务器实际接收时间；
* ``client_event_id`` 设备/客户端生成的幂等键，防止离线重试造成重复；
* ``prev_hash`` / ``hash`` 与前一条事件构成哈希链，任何篡改都会在校验时暴露。

事件体 ``data`` 对本模块不透明，领域投影在 :mod:`domain` 中解释。
"""

import hashlib
import json
import threading
import time
from pathlib import Path

GENESIS_HASH = "0" * 64


def now_ts():
    """服务器当前时间（ISO-8601，UTC，秒精度）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def event_hash(prev_hash, payload):
    """计算事件载荷哈希；payload 为已规范化的 JSON 字节串。"""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(b"\n")
    digest.update(payload)
    return digest.hexdigest()


def canonical(payload):
    """事件规范化序列化：键排序、无空白、不转义非 ASCII。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class EventStore:
    """线程安全的只追加事件存储，内存态可落盘为 JSONL。"""

    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._events = []
        self._idem = {}          # client_event_id -> seq
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    # ------------------------------------------------------------------ 持久化

    def _load(self):
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                self._verify_against_tail(event)
                self._events.append(event)
                cid = event.get("client_event_id")
                if cid:
                    self._idem[cid] = event["seq"]

    def _append_to_disk(self, event):
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()

    @staticmethod
    def _verify_against_tail(event):
        payload = canonical(event["payload"])
        if event["hash"] != event_hash(event["prev_hash"], payload):
            raise ValueError(f"事件 {event['seq']} 哈希不合法")

    def verify_chain(self):
        """重放整条哈希链，返回首尾序号；被篡改时抛出 :class:`ValueError`。"""
        with self._lock:
            prev = GENESIS_HASH
            for event in self._events:
                self._verify_against_tail(event)
                if event["prev_hash"] != prev:
                    raise ValueError(f"事件 {event['seq']} 断链")
                prev = event["hash"]
            return len(self._events)

    # ------------------------------------------------------------------ 写入

    def append(self, event_type, data, *, actor, occurred_at=None,
               client_event_id=None, recorded_at=None, device=None, note=None):
        """追加一条领域事件。

        ``occurred_at`` 为业务发生时间，离线补录时由设备带回；
        ``recorded_at`` 始终是服务器接收时间，二者之差即补录延迟。
        相同 ``client_event_id`` 重复提交时返回首次写入的事件（幂等）。
        """
        with self._lock:
            if client_event_id is not None:
                existing = self._idem.get(client_event_id)
                if existing is not None:
                    return self._events[existing - 1], True
            seq = len(self._events) + 1
            prev_hash = self._events[-1]["hash"] if self._events else GENESIS_HASH
            payload = {
                "type": event_type,
                "data": data,
                "actor": actor,
                "occurred_at": occurred_at or now_ts(),
                "recorded_at": recorded_at or now_ts(),
                "device": device,
                "note": note,
            }
            body = canonical(payload)
            event = {
                "seq": seq,
                "client_event_id": client_event_id,
                "prev_hash": prev_hash,
                "hash": event_hash(prev_hash, body),
                "payload": payload,
            }
            self._events.append(event)
            if client_event_id is not None:
                self._idem[client_event_id] = seq
            self._append_to_disk(event)
            return event, False

    # ------------------------------------------------------------------ 读取

    def events(self):
        """按记录顺序（哈希链顺序）返回全部事件。"""
        with self._lock:
            return list(self._events)

    def get(self, seq):
        with self._lock:
            return self._events[seq - 1]

    def __len__(self):
        with self._lock:
            return len(self._events)
