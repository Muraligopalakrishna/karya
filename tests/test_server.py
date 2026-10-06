import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from karya.agent import Agent
from karya.server import create_app

from .conftest import FakeLLM, reply, tool_call
from .test_agent import RAN  # noqa: F401 - registers _test_critical

ORIGIN = {"origin": "http://testserver"}


def make_client(script):
    agent = Agent(llm=FakeLLM(script), persist=False)
    app = create_app(agent=agent, token="secret-token", port=8765, extra_hosts={"testserver"})
    return TestClient(app), agent


def receive_until(ws, kind):
    events = []
    while True:
        ev = ws.receive_json()
        events.append(ev)
        if ev["type"] == kind:
            return events


def test_http_guards():
    client, _ = make_client([])
    assert client.get("/").status_code == 200
    assert "Karya" in client.get("/").text
    assert client.get("/", headers={"host": "evil.example.com"}).status_code == 403
    assert client.get("/api/status").status_code == 401
    ok = client.get("/api/status?token=secret-token")
    assert ok.status_code == 200 and ok.json()["tools"] > 50
    assert client.get("/static/app.js").status_code == 200
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_websocket_rejects_bad_token_and_origin():
    client, _ = make_client([])
    for url, headers in [("/ws?token=wrong", ORIGIN), ("/ws", ORIGIN),
                         ("/ws?token=secret-token", {"origin": "https://evil.example.com"})]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers) as ws:
                ws.receive_json()


def test_chat_with_approval_over_websocket():
    client, agent = make_client([tool_call("_test_critical", {"x": "post it"}), reply("Posted!")])
    with client.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
        first = ws.receive_json()
        assert first["type"] == "history" and first["messages"] == []
        ws.send_json({"type": "chat", "text": "post on linkedin"})
        events = receive_until(ws, "confirm")
        confirm = events[-1]
        assert confirm["risk"] == "critical" and confirm["tool"] == "_test_critical"
        ws.send_json({"type": "confirm", "id": confirm["id"], "approved": True})
        events = receive_until(ws, "assistant")
        assert events[-1]["text"] == "Posted!"
        results = [e for e in events if e["type"] == "tool_result"]
        assert results and results[0]["ok"] is True
        assert receive_until(ws, "busy")[-1]["value"] is False
        ws.send_json({"type": "set_mode", "auto": True})
        assert receive_until(ws, "mode")[-1]["auto_mode"] is True
        ws.send_json({"type": "reset"})
        assert receive_until(ws, "history")[-1]["messages"] == []
    assert agent.history == []


def test_stop_denies_pending_approval():
    client, agent = make_client([tool_call("_test_critical", {"x": "1"}), reply("unused")])
    with client.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_json({"type": "chat", "text": "go"})
        receive_until(ws, "confirm")
        ws.send_json({"type": "stop"})
        events = receive_until(ws, "busy")
        while events[-1].get("value") is not False:
            events += receive_until(ws, "busy")
        texts = [e.get("text") for e in events if e["type"] == "assistant"]
        assert "Stopped." in texts or any("DENIED" in (e.get("preview") or "") for e in events)
