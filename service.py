"""考古现场上下文档案的服务入口。

装配领域契约、事件溯源存储与 HTTP API：
- 事件日志默认保存在 --data 指定的 JSONL 文件（缺省 data/events.jsonl）。
- 令牌可由环境变量 ARCH_TOKENS（JSON: {"token": ["角色组","身份名"]}）覆盖。
"""

import argparse
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path

from api import DEFAULT_TOKENS, make_handler
from store import ContextStore

SERVICE_ID = "archaeology-context"
SERVICE_NAME = "考古现场上下文档案"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")
DEFAULT_DATA = Path(__file__).with_name("data").joinpath("events.jsonl")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    for key in ("actors", "states", "invariants", "spatial_hierarchy"):
        if not contract.get(key):
            raise ValueError(f"契约缺少 {key}")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def load_tokens():
    raw = os.environ.get("ARCH_TOKENS")
    if not raw:
        return DEFAULT_TOKENS
    tokens = json.loads(raw)
    return {k: tuple(v) for k, v in tokens.items()}


def build_handler(data_path=None):
    contract = load_contract()
    store = ContextStore(data_path)
    return make_handler(store, tokens=load_tokens(),
                        contract=contract, health=health_payload())


# 无状态默认处理器（内存存储），便于契约测试直接引用
Handler = build_handler()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA,
                        help="事件日志 JSONL 路径")
    parser.add_argument("--check", action="store_true", help="配置自检后退出")
    args = parser.parse_args()

    contract = load_contract()
    if args.check:
        assert contract["states"] and contract["invariants"]
        store = ContextStore(args.data if args.data.exists() else None)
        print(f"基础检查通过：契约 v{contract['contract_version']}，"
              f"事件 {len(store.events)} 条，复核单 {len(store.tickets)} 个，"
              f"已发布报告 {len(store.reports)} 份")
        return

    handler = build_handler(args.data)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
