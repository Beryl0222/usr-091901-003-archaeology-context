"""考古现场上下文档案服务入口。

运行：

* ``python3 service.py --check``           校验契约与事件哈希链；
* ``python3 service.py --port 8000``       启动 HTTP 服务；
* ``python3 service.py --seed-demo``       写入上召窑秦陵演示事件后退出；
* ``python3 service.py --reset``           清空本地事件日志。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from events import EventStore
from api import Application, MAX_BODY
from domain import ApiError

SERVICE_ID = "archaeology-context"
SERVICE_NAME = "考古现场上下文档案"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")
DEFAULT_DATA = Path(__file__).with_name("data") / "events.jsonl"


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload(events=0):
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME,
            "events": events}


class Handler(BaseHTTPRequestHandler):
    """HTTP 编解码层，业务全部委托给 :class:`api.Application`。"""

    app = None  # 由 main 注入

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        # 基础探针不依赖事件存储，未注入应用时也可用
        if parsed.path == "/health":
            self._send_json(200, health_payload(
                len(self.app.store) if self.app else 0))
            return
        if parsed.path == "/contract":
            self._send_json(200, load_contract())
            return
        if self.app is None:
            self._send_json(404, {"error": "接口不存在"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send_json(413, {"error": "请求体过大"})
            return
        body = self.rfile.read(length) if length else b""
        try:
            status, payload = self.app.handle(
                method, parsed.path, parse_qs(parsed.query),
                {k.lower(): v for k, v in self.headers.items()}, body)
            self._send_json(status, payload)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - 服务边界兜底
            self._send_json(500, {"error": f"服务器内部错误: {exc}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    if args.reset and args.data.exists():
        args.data.unlink()
        print(f"已清空 {args.data}")

    store = EventStore(args.data)

    if not (args.check or args.seed_demo) and args.reset:
        return

    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        count = store.verify_chain()
        print(f"基础检查通过；契约 {contract['contract_version']}，事件 {count} 条")
        return

    if args.seed_demo:
        import demo
        added = demo.seed(store)
        store.verify_chain()
        print(f"演示事件写入完成：新增 {added} 条，日志共 {len(store)} 条，哈希链校验通过")
        return

    Handler.app = Application(store)
    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}（数据 {args.data}，事件 {len(store)} 条）")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
