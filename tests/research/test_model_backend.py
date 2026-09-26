import asyncio
import json

import httpx
import pytest

from lh_harness.research.jsonio import digest
from lh_harness.research.model_backend import ChatJsonRoleBackend, ProtocolError
from lh_harness.research.models import RoleRequest
from lh_harness.research.prompts import template


def request():
    role = "auditor.evidence"
    return RoleRequest(role=role, logical_action_key="a", input_manifest_hash="0"*64, contract_version=1, policy_version=1, system_template_id=f"{role}.v1", system_template_hash=digest(template(role)), data_packet={"excerpt": "untrusted page"}, response_schema_id="EvidenceAuditVerdict", response_schema_version=1, max_output_tokens=64)


def test_chat_json_sends_no_tools_and_parses_one_object(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    async def handler(req):
        body = json.loads(req.content)
        assert set(body) == {"model", "messages", "temperature", "max_tokens", "response_format", "stream"}
        assert body["response_format"] == {"type": "json_object"}
        assert '"required"' in body["messages"][0]["content"]
        assert req.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"id": "r1", "choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"supported"}'}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    backend = ChatJsonRoleBackend(base_url="https://model.example", model="test", api_key_env="TEST_MODEL_KEY", temperature=0, profile_hash="profile")
    response = asyncio.run(backend.generate(request()))
    assert response.usage.input_tokens == 4
    assert json.loads(response.raw_text)["verdict"] == "supported"


def test_chat_json_rejects_duplicate_output_keys(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    async def handler(req):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"x":1,"x":2}'}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    backend = ChatJsonRoleBackend(base_url="https://model.example", model="test", api_key_env="TEST_MODEL_KEY", temperature=0, profile_hash="profile")
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(backend.generate(request()))
