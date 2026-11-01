"""Opt-in native Codex RPC test; isolated profile and local stub model only.

Set REINS_TEST_NATIVE_CODEX_BIN to a real Codex executable. This never opens a
Desktop chat, reads account credentials, or calls a remote model provider.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import queue
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from harness_reins.engine import delivery, native
from harness_reins.harness.codex import app_server
import reins_records


@unittest.skipUnless(sys.platform in ("darwin", "linux") and os.environ.get("REINS_TEST_NATIVE_CODEX_BIN"),
                     "opt in with REINS_TEST_NATIVE_CODEX_BIN on macOS/Linux")
class NativeCoreTests(unittest.TestCase):
    def test_real_core_receives_tool_output_and_pushes_completion(self):
        incoming = queue.Queue()
        holding = threading.Event()
        release = threading.Event()
        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                incoming.put(body)
                if "ACTIVE_CALLBACK_START" in json.dumps(body) and not release.is_set():
                    holding.set()
                    release.wait(timeout=10)
                item = {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "CALLBACK_RECEIVED", "annotations": []}]}
                response = {"id": "resp_fixture", "object": "response", "status": "completed", "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                events = [
                    {"type": "response.created", "response": dict(response, status="in_progress", output=[])},
                    {"type": "response.output_item.added", "output_index": 0, "item": dict(item, content=[], status="in_progress")},
                    {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": "CALLBACK_RECEIVED"},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                data = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="reins-native-core-", dir="/tmp") as temporary:
                root = Path(temporary).resolve()
                profile = root / "profile"
                profile.mkdir()
                (profile / "config.toml").write_text(
                    'model = "fixture"\nmodel_provider = "fixture"\n'
                    '[model_providers.fixture]\nname = "Local fixture"\n'
                    f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                    'wire_api = "responses"\nrequires_openai_auth = false\n', encoding="utf-8")
                env = dict(os.environ, CODEX_HOME=str(profile), RUST_LOG="warn,codex_app_server_transport::transport::unix_socket=info")
                for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH", "OPENAI_API_KEY"):
                    env.pop(key, None)
                sock = root / "core.sock"
                process = subprocess.Popen([os.environ["REINS_TEST_NATIVE_CODEX_BIN"], "app-server", "--listen", f"unix://{sock}"],
                                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                owner = callback = None
                log_tail = []
                reader = None
                try:
                    # Listen for the server's readiness log event; no socket
                    # existence probing or repeated connect attempts.
                    sel = selectors.DefaultSelector()
                    sel.register(process.stderr, selectors.EVENT_READ)
                    deadline = time.monotonic() + 10
                    logs = b""
                    while b"app-server control socket listening" not in logs:
                        ready = sel.select(max(0, deadline - time.monotonic()))
                        self.assertTrue(ready, logs.decode(errors="replace"))
                        data = os.read(process.stderr.fileno(), 8192)
                        self.assertTrue(data, logs.decode(errors="replace"))
                        logs += data
                    sel.close()
                    def drain_logs():
                        for line in process.stderr:
                            log_tail.append(line.decode(errors="replace"))
                            del log_tail[:-30]
                    reader = threading.Thread(target=drain_logs, daemon=True)
                    reader.start()
                    owner = app_server.AppServerConnection(sock, 10)
                    native.initialize(owner)
                    thread = owner.request("thread/start", {"cwd": str(root), "ephemeral": False,
                                                           "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
                    # Persist a harmless context item before a second client
                    # subscribes; no model request is made by this operation.
                    owner.request("thread/inject_items", {"threadId": thread, "items": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Local protocol fixture."}]}]})
                    request = reins_records.callback_record("fixture-c1", str(root), agent="codex", callback_mode="app-server",
                        callback_socket=str(sock.resolve()), target={"kind": "session", "session": thread}, prompt="NATIVE_CALLBACK_MARKER")
                    # No durable queue in this isolated RPC test; durable lease
                    # and ACK behavior are covered by test_native_callbacks.
                    payload = {"request": request, "timeout": 10, "prompt": "NATIVE_CALLBACK_MARKER",
                               "cwd": str(root)}
                    callback = delivery.start_desktop_app_server_turn(payload)
                    self.assertIsInstance(callback, native.NativeAppServerDelivery)
                    self.assertTrue(callback.wait_for_completion(15))
                    sent = incoming.get(timeout=1)
                    self.assertIn("NATIVE_CALLBACK_MARKER", json.dumps(sent))
                    self.assertTrue(any(i.get("type") == "function_call_output" and "NATIVE_CALLBACK_MARKER" in str(i.get("output"))
                                        for i in sent.get("input", [])), sent.get("input"))
                    callback.close()
                    active = owner.request("turn/start", {"threadId": thread, "input": [
                        {"type": "text", "text": "ACTIVE_CALLBACK_START"}]})["turn"]["id"]
                    self.assertTrue(holding.wait(timeout=5))
                    payload["prompt"] = "ACTIVE_CALLBACK_RESULT"
                    callback = delivery.start_desktop_app_server_turn(payload)
                    self.assertEqual(callback.turn_id, active)
                    release.set()
                    self.assertTrue(callback.wait_for_completion(15))
                    first = incoming.get(timeout=1)
                    self.assertIn("ACTIVE_CALLBACK_START", json.dumps(first))
                    second = incoming.get(timeout=1)
                    self.assertIn("ACTIVE_CALLBACK_RESULT", json.dumps(second))
                except Exception:
                    print("".join(log_tail), file=sys.stderr)
                    raise
                finally:
                    release.set()
                    if callback: callback.close()
                    if owner: owner.close()
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                    if reader: reader.join(timeout=2)
                    process.stderr.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
