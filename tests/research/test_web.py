"""Exercise the web adapter against real research state, without paid API calls."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.research.web import NewResearch, create_app, make_contract


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "runs")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def test_local_boundary_and_static_assets(client):
    assert client.get("/").status_code == 200
    js = client.get("/assets/app.js")
    assert "javascript" in js.headers["content-type"]
    assert "frame-ancestors 'none'" in js.headers["content-security-policy"]
    assert client.get("/api/runs", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/runs", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/settings", content='{}').status_code == 415
    assert client.get("/api/runs/not-valid!").status_code == 400


def test_secret_is_ephemeral_and_not_echoed(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secret = "fake-secret-never-persist"
    response = client.post("/api/settings", json={"api_key": secret})
    assert response.json()["configured"] is True
    assert secret not in response.text
    assert secret not in client.get("/api/settings").text
    response = client.post("/api/settings", json={"api_key": secret, "provider": "invalid"})
    assert response.status_code == 422 and secret not in response.text
    assert not list(client.app.state.workbench.root.rglob("*"))
    client.post("/api/settings", json={"clear_key": True})
    assert client.get("/api/settings").json()["configured"] is False


def test_missing_key_does_not_create_research(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    response = client.post("/api/runs", json={"question": "一个需要实际研究的问题"})
    assert response.status_code == 400
    assert not list(client.app.state.workbench.root.iterdir())


def test_contract_generated_with_real_acceptance_rules():
    contract = make_contract(NewResearch(question="研究 Python 多线程的适用范围", requirements=["解释 GIL", "比较进程"], scope="只用官方文档"), "study-test")
    assert len(contract.requirements) == 2
    assert contract.requirements[1].acceptance_checks[1].code == "answer_in_report"
    assert contract.scope.include == ["只用官方文档"]


def test_assisted_creation_uses_cli_and_never_persists_key(client, monkeypatch):
    wb = client.app.state.workbench
    launches = []
    monkeypatch.setattr(wb, "launch", lambda *args: launches.append(args))
    client.post("/api/settings", json={"api_key": "test-web-secret"})
    response = client.post("/api/runs", json={"question": "Python 线程在什么情况下可以并行？"})
    assert response.status_code == 201
    args = launches[0][1]
    assert args[0] == "run"
    from src.research.config import load_config
    config = load_config(Path(args[args.index("--config") + 1]))
    assert config.research.execution.mode == "assisted"
    assert config.research.model.backend == "chat_json"
    for path in wb.root.rglob("*"):
        if path.is_file():
            assert b"test-web-secret" not in path.read_bytes()


def test_model_check_calls_provider_without_echoing_key(client, monkeypatch):
    seen = []
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            seen.append((url, kwargs))
            return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    monkeypatch.setattr("src.research.web.httpx.AsyncClient", FakeClient)
    client.post("/api/settings", json={"api_key": "connection-test-key"})
    response = client.post("/api/settings/check", json={})
    assert response.json()["ok"]
    assert seen[0][0] == "https://api.deepseek.com/chat/completions"
    assert seen[0][1]["headers"]["Authorization"] == "Bearer connection-test-key"
    assert "connection-test-key" not in response.text


def test_mailbox_rejects_wrong_hash_and_duplicate_response(client):
    wb = client.app.state.workbench
    directory = wb.root / "study-one" / "bridge"
    directory.mkdir(parents=True)
    request_id = "a" * 64
    request = {"request_id": request_id, "input_hash": "b" * 64, "kind": "search", "payload": {"query_id": "q1"}}
    (directory / f"{request_id}.request.json").write_text(json.dumps(request), encoding="utf-8")
    path = f"/api/runs/study-one/responses/{request_id}"
    body = {"input_hash": "wrong", "producer": "test", "result": {"query_id": "q1", "hits": [], "status": "empty"}}
    assert client.post(path, json=body).status_code == 409
    assert not list(directory.glob("*.response.json"))
    body["input_hash"] = request["input_hash"]
    assert client.post(path, json=body).status_code == 200
    assert client.post(path, json=body).status_code == 409


def test_run_paths_do_not_follow_symlinks(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = client.app.state.workbench.root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    assert client.get("/api/runs/escape").status_code == 400


def test_offline_run_through_web_produces_real_report(client):
    response = client.post("/api/runs", json={"question": "离线流程演示", "mode": "demo"})
    assert response.status_code == 201
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 35
    result = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        if response.status_code == 200:
            result = response.json()
            if result.get("lifecycle_status") == "complete":
                break
            if result.get("error"):
                pytest.fail(result["error"])
        time.sleep(.15)
    assert result["lifecycle_status"] == "complete", result
    assert result["evidence"] and result["report"]
    assert result["execution"]["mode"] == "replay"
    assert client.get(f"/api/runs/{run_id}/artifacts/report.md").status_code == 200
    assert client.get(f"/api/runs/{run_id}/artifacts/state.sqlite").status_code == 404
    raw_hash = result["evidence"][0]["snapshot"]["raw_hash"]
    snapshot = client.get(f"/api/runs/{run_id}/snapshots/{raw_hash}")
    assert snapshot.status_code == 200
    assert "attachment" in snapshot.headers["content-disposition"]
    assert client.get(f"/api/runs/{run_id}/snapshots/not-a-hash").status_code == 400
    assert client.post(f"/api/runs/{run_id}/resume", json={}).status_code == 409
    assert client.get("/api/runs").json()["runs"][0]["run_id"] == run_id
