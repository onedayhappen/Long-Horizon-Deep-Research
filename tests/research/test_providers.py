import json
import asyncio
import gzip

import httpx
import pytest

from src.research.extract import locate, parse_document, verify_locator
from src.research.models import FetchRequest, QuerySpec
from src.research.providers import EgressFetchProvider, ProviderError, SerperSearchProvider, validate_public_url


def query():
    return QuerySpec(query_id="q", question_id="R", text="example", strategy_family="primary", language="en", country_code=None, source_class_targets=["primary_official"], max_results=2)


@pytest.mark.parametrize("status,body,expected", [
    (200, {"organic": [{"link": "https://example.org/a", "title": "A", "snippet": "s"}]}, "hit"),
    (200, {"organic": []}, "empty"),
    (200, {"wrong": []}, "error"),
    (401, {}, "error"),
    (429, {}, "error"),
])
def test_serper_mock(monkeypatch, status, body, expected):
    monkeypatch.setenv("TEST_SERPER_KEY", "secret")
    async def handler(request):
        assert request.headers["x-api-key"] == "secret"
        assert request.url == "https://google.serper.dev/search"
        assert json.loads(request.content)["q"] == "example"
        return httpx.Response(status, json=body)
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await SerperSearchProvider("TEST_SERPER_KEY", client).search(query())
    batch = asyncio.run(invoke())
    assert batch.status == expected
    if expected == "error":
        assert batch.hits == [] and batch.error


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a", "http://[::1]/", "http://localhost/", "https://user:pass@example.org/", "http://10.0.0.1/"])
def test_private_url_rejected(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_locator_bound_to_original_text():
    parsed, text = parse_document(b"<h1>Title</h1><p>Original evidence.</p>", "text/html")
    locator = locate(text, "Original evidence.")
    assert verify_locator(text, locator, "Original evidence.")
    assert not verify_locator(text.replace("Original", "Altered"), locator, "Original evidence.")


def test_fetch_rejects_private_redirect():
    seen = []
    async def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = EgressFetchProvider("http://proxy.example:4750", client=client)
            await provider.fetch(FetchRequest(source_id="s", url="https://example.org/start"))
    with pytest.raises(ValueError, match="nonpublic"):
        asyncio.run(invoke())
    assert seen == ["https://example.org/start"]


def test_fetch_rejects_decompressed_size():
    async def handler(req):
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"}, content=gzip.compress(b"x" * 100))
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = EgressFetchProvider("http://proxy.example:4750", client=client, max_bytes=50)
            await provider.fetch(FetchRequest(source_id="s", url="https://example.org/start"))
    with pytest.raises(ProviderError, match="decompressed"):
        asyncio.run(invoke())
