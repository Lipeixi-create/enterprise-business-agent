"""Minimal OTLP/HTTP trace receiver used for Phase 9.3 acceptance.

This is a wire-level receiver, not a replacement for the OpenTelemetry
Collector. It parses real OTLP protobuf exports and records span names so the
application's exporter path can be verified when the collector image cannot be
pulled.

Usage:
    .venv/bin/python scripts/otlp_file_receiver.py --port 4318 \
        --output /tmp/eba-otlp/received.jsonl
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

_lock = threading.Lock()
_stats = {"requests": 0, "spans": 0, "last_at": None}
_output = Path("/tmp/eba-otlp/received.jsonl")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path in {"/health", "/"}:
            with _lock:
                self._json(dict(_stats))
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path not in {"/v1/traces", "/"}:
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
        except Exception as exc:  # noqa: BLE001 - report malformed payloads
            self._json({"error": f"protobuf parse failed: {exc}"}, 400)
            return

        records = []
        for resource_spans in request.resource_spans:
            service = ""
            for attribute in resource_spans.resource.attributes:
                if attribute.key == "service.name":
                    service = attribute.value.string_value
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    records.append(
                        {
                            "service": service,
                            "name": span.name,
                            "trace_id": span.trace_id.hex(),
                            "span_id": span.span_id.hex(),
                            "parent_span_id": span.parent_span_id.hex(),
                            "status": span.status.code,
                            "start_unix_nano": span.start_time_unix_nano,
                        }
                    )

        with _lock:
            _stats["requests"] += 1
            _stats["spans"] += len(records)
            _stats["last_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _output.parent.mkdir(parents=True, exist_ok=True)
            with _output.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        response = ExportTraceServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def main() -> int:
    global _output
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--output", default="/tmp/eba-otlp/received.jsonl")
    args = parser.parse_args()
    _output = Path(args.output)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on {args.host}:{args.port} -> {_output}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
