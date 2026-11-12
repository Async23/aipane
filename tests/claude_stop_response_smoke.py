"""Exercise a native Claude Stop hook with a loopback-only synthetic SSE response."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ANSWER = "STOP_RESPONSE_PATCH_OK"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        route = urlsplit(self.path).path
        if route.endswith("/count_tokens"):
            self.send_json({"input_tokens": 10})
            return
        if not route.endswith("/messages"):
            self.send_error(404)
            return
        blocks = [
            {"type": "thinking", "thinking": "synthetic prelude", "signature": "synthetic"},
            {"type": "text", "text": ANSWER},
            {"type": "thinking", "thinking": "synthetic trailing block", "signature": "synthetic"},
        ]
        message = {
            "id": "msg_stop_response_test", "type": "message", "role": "assistant",
            "model": request.get("model", "claude-sonnet-4-6"), "content": blocks,
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        if not request.get("stream"):
            self.send_json(message)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.event("message_start", {"message": {**message, "content": [], "stop_reason": None}})
        for index, block in enumerate(blocks):
            key = "text" if block["type"] == "text" else "thinking"
            self.event("content_block_start", {"index": index, "content_block": {**block, key: ""}})
            self.event("content_block_delta", {"index": index, "delta": {"type": key + "_delta", key: block[key]}})
            self.event("content_block_stop", {"index": index})
        self.event("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 10}})
        self.event("message_stop", {})
        self.close_connection = True

    def send_json(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def event(self, kind, fields):
        data = json.dumps({"type": kind, **fields})
        self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="claude-stop-smoke-") as directory:
        root = Path(directory)
        config = root / "config"
        config.mkdir()
        hook = root / "capture.py"
        hook.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text(sys.stdin.read())\n")
        payload_path = root / "stop.json"
        settings = root / "settings.json"
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": shlex.join([sys.executable, str(hook), str(payload_path)]),
            "timeout": 5,
        }]}]}}))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Allowlist the process environment: no inherited proxy/provider credentials,
        # active-session markers, user hooks, or MCP integrations enter the test.
        environment = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG") if key in os.environ}
        environment.update({
            "CLAUDE_CONFIG_DIR": str(config),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "ANTHROPIC_API_KEY": "synthetic-local-test",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        try:
            result = subprocess.run([
                str(args.binary.resolve()), "-p", "Return the test answer.",
                "--model", "claude-sonnet-4-6", "--tools", "",
                "--system-prompt", "This is a local hook integration test.",
                "--permission-mode", "dontAsk", "--setting-sources", "",
                "--settings", str(settings), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--disable-slash-commands", "--no-session-persistence", "--prompt-suggestions", "false",
            ], cwd=root, env=environment, text=True, capture_output=True, timeout=45)
        finally:
            server.shutdown()
            server.server_close()
        if result.returncode or not payload_path.exists():
            raise AssertionError(f"Claude/hook failed ({result.returncode}): {result.stdout[-1500:]} {result.stderr[-1500:]}")
        payload = json.loads(payload_path.read_text())
        actual = payload.get("last_assistant_message")
        assert actual == ANSWER, f"Stop.last_assistant_message: expected {ANSWER!r}, got {actual!r}"
        notifier = Path(__file__).resolve().parents[1] / "bin/aipane-claude-notify"
        preview = subprocess.run([str(notifier)], input=json.dumps(payload), text=True, capture_output=True,
                                 env={**environment, "CLAUDE_NOTIFY_DRY_RUN": "1"}, check=True, timeout=10)
        notification = json.loads(preview.stdout)
        assert notification["body"] == ANSWER, notification
        print(json.dumps({"binary": str(args.binary.resolve()), "stop_answer": actual,
                          "notification_body": notification["body"], "delivery": "dry-run"}))


if __name__ == "__main__":
    main()
