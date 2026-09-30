"""The Icinga plugins in contrib/icinga, run as real subprocesses against local stand-ins."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

CONTRIB = Path(__file__).parents[1] / "contrib" / "icinga"


class Status:
    total = 0
    failed = 0


def serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"system": {"total_requests": Status.total, "total_failures": Status.failed}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def run(script, *args):
    p = subprocess.run([sys.executable, str(CONTRIB / script), *args], capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip()


def test_balancer_errors_baseline_ok_warning_critical_and_reset(tmp_path):
    srv, url = serve(StatusHandler)
    try:
        args = ("--url", url, "--name", "t", "-w", "1", "-c", "5", "--state-dir", str(tmp_path))
        Status.total, Status.failed = 100, 3
        assert run("check_llm_balancer_errors", *args)[0] == 0  # baseline
        Status.total, Status.failed = 120, 3
        code, out = run("check_llm_balancer_errors", *args)
        assert code == 0 and "0 failed of 20" in out
        Status.total, Status.failed = 130, 5
        assert run("check_llm_balancer_errors", *args)[0] == 1
        Status.total, Status.failed = 150, 12
        code, out = run("check_llm_balancer_errors", *args)
        assert code == 2 and "7 failed of 20" in out
        Status.total, Status.failed = 2, 0  # balancer restarted: counters reset
        code, out = run("check_llm_balancer_errors", *args)
        assert code == 0 and "baseline" in out
    finally:
        srv.shutdown()


def test_balancer_errors_unreachable_is_critical(tmp_path):
    code, out = run("check_llm_balancer_errors", "--url", "http://127.0.0.1:1", "--name", "x",
                    "--state-dir", str(tmp_path), "--timeout", "2")
    assert code == 2 and "cannot read" in out


class OllamaHandler(BaseHTTPRequestHandler):
    hang = set()

    def do_GET(self):
        body = json.dumps({"models": [{"name": "qwen:7b", "context_length": 8192}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        OllamaHandler.seen.append(req)
        if req["model"] in self.hang:
            threading.Event().wait(5)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"done": true}')

    def log_message(self, *a):
        pass


@pytest.fixture
def ollama():
    OllamaHandler.seen = []
    OllamaHandler.hang = set()
    srv, url = serve(OllamaHandler)
    yield url
    srv.shutdown()


def test_model_probe_uses_loaded_context_size(ollama):
    code, out = run("check_ollama_models", "--host", ollama, "--model", "small:1b", "--loaded")
    assert code == 0 and "small:1b" in out and "qwen:7b" in out
    by_model = {r["model"]: r for r in OllamaHandler.seen}
    # The loaded model is probed at the size it's loaded with, so the probe never forces a reload.
    assert by_model["qwen:7b"]["options"]["num_ctx"] == 8192
    assert "num_ctx" not in by_model["small:1b"]["options"]


def test_model_probe_hang_is_critical(ollama):
    OllamaHandler.hang = {"small:1b"}
    code, out = run("check_ollama_models", "--host", ollama, "--model", "small:1b", "--timeout", "1")
    assert code == 2 and "no answer" in out
