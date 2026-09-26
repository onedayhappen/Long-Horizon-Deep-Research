import asyncio
import json
from pathlib import Path

import httpx
import pytest

from lh_harness.research.cli import main
from lh_harness.research.config import load_config
from lh_harness.research.jsonio import load
from lh_harness.research.model_backend import ChatJsonRoleBackend, configured_backend, response_schema
from lh_harness.research.replay import ReplayFetchProvider, ReplaySearchProvider
from scripts.outline_iteration_fixture import ScenarioAgent, prepare
from tests.research.test_model_backend import request


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "examples/research/deepseek/research.toml"
CONTRACT = ROOT / "examples/research/assisted/python_threads_contract.json"


def test_deepseek_request_and_environment_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    config = load_config(CONFIG)

    async def handler(req):
        body = json.loads(req.content)
        assert str(req.url) == "https://api.deepseek.com/chat/completions"
        assert req.headers["authorization"] == "Bearer test-only-secret"
        assert body["model"] == "deepseek-v4-pro"
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is False
        assert "tools" not in body
        assert "test-only-secret" not in req.content.decode()
        assert '"source_class_verified"' in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"verdict":"supported","reason":"exact excerpt","source_class_verified":true}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    backend = configured_backend(config.research.model)
    response = asyncio.run(backend.generate(request()))
    assert response.usage.output_tokens == 20
    assert backend.api_requests == 1
    assert "test-only-secret" not in config.model_dump_json()


def test_missing_key_fails_before_creating_run(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert main(["run", "--contract", str(CONTRACT), "--config", str(CONFIG),
                 "--runs-root", str(tmp_path), "--run-id", "missing-key"]) == 2
    assert not (tmp_path / "missing-key").exists()
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_chat_model_requires_endpoint():
    config = load_config(CONFIG)
    data = config.model_dump()
    data["research"]["model"]["base_url"] = None
    with pytest.raises(ValueError, match="chat_json requires"):
        type(config).model_validate(data)


def test_planner_union_schema_is_available():
    schema = response_schema("ResearchAction", 1)
    assert "SearchAction" in schema["$defs"]
    assert "OutlinePatchAction" in schema["$defs"]
    assert "TerminateProposal" in schema["$defs"]


@pytest.mark.parametrize("finish_reason,content", [("length", "{}"), ("stop", "[]")])
def test_bad_api_outputs_are_rejected(monkeypatch, finish_reason, content):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    async def handler(req):
        return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError):
        asyncio.run(configured_backend(load_config(CONFIG).research.model).generate(request()))


def test_cli_routes_all_roles_to_api_and_records_provenance(monkeypatch, tmp_path):
    from lh_harness.research import assisted
    fixture = tmp_path / "fixture"
    prepare(fixture)
    agent = ScenarioAgent()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    monkeypatch.setattr(assisted, "ExternalSearchProvider", lambda mailbox: ReplaySearchProvider(fixture))
    monkeypatch.setattr(assisted, "ExternalFetchProvider", lambda mailbox: ReplayFetchProvider(fixture))
    roles_seen = set()

    async def generate(self, role_request):
        assert self.model == "deepseek-v4-pro"
        assert role_request.max_output_tokens == 8192
        roles_seen.add(role_request.role)
        self.api_requests += 1
        return await agent.generate(role_request)

    monkeypatch.setattr(ChatJsonRoleBackend, "generate", generate)
    assert main(["run", "--contract", str(fixture / "contract.json"), "--config", str(CONFIG),
                 "--runs-root", str(tmp_path), "--run-id", "hybrid"]) == 0
    root = tmp_path / "hybrid"
    metadata = load(root / "execution.json")
    assert metadata["model_api_key_used"] is True
    assert metadata["model_api_requests"] > 5
    assert metadata["role_review_context"] == "separate_api_requests_same_model"
    assert {"planner.initialize", "planner.next", "auditor.evidence", "writer.section", "auditor.report"} <= roles_seen
    assert "deepseek-v4-pro API" in (root / "report.md").read_text("utf-8")
    assert "未配置模型或搜索 API key" not in (root / "report.md").read_text("utf-8")
    assert load(root / "outline.json")["revision_count"] == 1
    assert "test-only-secret" not in (root / "resolved_config.json").read_text("utf-8")
    assert main(["resume", "--run-dir", str(root)]) == 0
    assert load(root / "execution.json") == metadata
