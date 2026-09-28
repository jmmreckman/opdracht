"""Controleert dat het verzoek dat we naar de API sturen door de SDK heen komt
en de juiste velden bevat (zonder echte API-sleutel: nep-server)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic

from app import agent, claude


class H(BaseHTTPRequestHandler):
    laatste = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers["content-length"])
        H.laatste = {"body": json.loads(self.rfile.read(n)), "beta": self.headers.get("anthropic-beta")}
        antwoord = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
                    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                    "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 2}}
        data = json.dumps(antwoord).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_verzoek_vorm(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(claude, "_client", anthropic.Anthropic(
        api_key="x", base_url=f"http://127.0.0.1:{srv.server_port}"))
    resp = claude.vraag("sys", [{"role": "user", "content": "hoi"}], claude.WEB_TOOLS + agent.EIGEN_TOOLS)
    srv.shutdown()
    assert claude.tekst_van(resp.content) == "ok"
    body = H.laatste["body"]
    assert body["model"] == "claude-opus-5" and body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"} and body["cache_control"] == {"type": "ephemeral"}
    assert "server-side-fallback-2026-07-01" in H.laatste["beta"]
    assert [t.get("type", "custom") for t in body["tools"]][:2] == ["web_search_20260209", "web_fetch_20260209"]
    assert claude.kosten(resp)["usd"] > 0
