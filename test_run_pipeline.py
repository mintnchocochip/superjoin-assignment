"""Checks for the two bits of run_pipeline that only misbehave on someone else's
machine, which is exactly where they cannot be debugged.

Both were real failures on an Ubuntu box: a .env copied from Windows sent the
runner at a port nothing was listening on, and `systemctl enable --now ollama`
failed silently on a machine whose Ollama was installed without a systemd unit.
Neither is reachable from the developer's own laptop, so they get faked here.

    python test_run_pipeline.py
"""

import http.server
import os
import pathlib
import socket
import subprocess
import tempfile
import threading

import label
import run_pipeline as r


def stub_ollama():
    """A server that answers /api/tags the way Ollama does. Returns its URL."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"models":[{"name":"qwen3:4b"}]}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def dead_url():
    """A port nobody is on. Not a fixed one: this laptop runs Ollama on 12345."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def test_stale_ollama_host_is_repaired():
    """A .env carried over from Windows must not strand the run on a dead port."""
    server, live = stub_ollama()
    try:
        root, dead = pathlib.Path(tempfile.mkdtemp()), dead_url()
        (root / ".env").write_text(
            f"MONGO_DB=factlayer\nOLLAMA_HOST={dead.removeprefix('http://')}\n")
        r.ROOT, r.DEFAULT_OLLAMA, label.HOST = root, live, dead
        os.environ.pop("OLLAMA_HOST", None)

        assert not label.available(), "the stale host must really be silent"
        r.retune_ollama_host()

        assert label.HOST == live
        text = (root / ".env").read_text()
        assert "OLLAMA_HOST" not in text, text
        assert "MONGO_DB=factlayer" in text, "the rest of .env must survive"

        before = text
        r.retune_ollama_host()
        assert (root / ".env").read_text() == before, "healthy host: leave it alone"
    finally:
        server.shutdown()


def test_falls_back_to_serving_ollama_itself():
    """No systemd unit is not the same as no Ollama. It used to be treated as one."""
    server, live = stub_ollama()
    calls, spawned = [], []
    try:
        r.LINUX, r.OLLAMA_LOG = True, pathlib.Path(tempfile.mkdtemp()) / "ollama.log"
        r.have = lambda name: True
        r.quiet = lambda cmd: calls.append(cmd) or False   # systemctl: unit not found
        r.subprocess.Popen = lambda *a, **kw: (spawned.append(a[0]),
                                               label.__setattr__("HOST", live))[0]
        label.HOST = dead_url()
        r.ensure_ollama_running()

        assert any("systemctl" in c for c in calls[0]), calls
        assert spawned == [["ollama", "serve"]], spawned
        assert label.available(), "should be reachable once we start it ourselves"
    finally:
        r.subprocess.Popen = subprocess.Popen
        server.shutdown()


if __name__ == "__main__":
    test_stale_ollama_host_is_repaired()
    test_falls_back_to_serving_ollama_itself()
    print("run_pipeline checks pass")
