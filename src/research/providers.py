from __future__ import annotations

import hashlib
import ipaddress
import os
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from .models import FetchRequest, FetchResult, QuerySpec, SearchBatch, SearchHit, Usage


class ProviderError(RuntimeError):
    pass


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL protocol, host, or credentials rejected")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("local host rejected")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("nonpublic IP address rejected")


class SerperSearchProvider:
    ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, api_key_env: str = "SERPER_API_KEY", client: httpx.AsyncClient | None = None):
        self.api_key_env = api_key_env
        self.client = client

    async def search(self, query: QuerySpec) -> SearchBatch:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderError("auth_error: missing Serper API key")
        body: dict[str, object] = {"q": query.text, "num": query.max_results}
        if query.language:
            body["hl"] = query.language
        if query.country_code:
            body["gl"] = query.country_code.lower()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(trust_env=False, timeout=30)
        try:
            response = await client.post(self.ENDPOINT, headers={"X-API-KEY": key}, json=body)
            if response.status_code in (401, 403):
                return SearchBatch(query_id=query.query_id, hits=[], status="error", error="auth_error")
            if response.status_code == 429:
                return SearchBatch(query_id=query.query_id, hits=[], status="error", error="rate_limited")
            if response.status_code >= 500:
                return SearchBatch(query_id=query.query_id, hits=[], status="error", error="provider_5xx")
            response.raise_for_status()
            data = response.json()
            organic = data.get("organic")
            if not isinstance(organic, list):
                return SearchBatch(query_id=query.query_id, hits=[], status="error", error="protocol_error")
            hits = []
            for index, item in enumerate(organic):
                if not isinstance(item, dict) or not isinstance(item.get("link"), str) or not isinstance(item.get("title"), str):
                    return SearchBatch(query_id=query.query_id, hits=[], status="error", error="protocol_error")
                hits.append(SearchHit(hit_id=f"{query.query_id}.{index+1}", url=item["link"], title=item["title"], snippet=item.get("snippet", ""), rank=index + 1, publisher=None, published_at=None))
            return SearchBatch(query_id=query.query_id, hits=hits, status="hit" if hits else "empty", usage=Usage(basis="unknown"))
        except httpx.TimeoutException:
            return SearchBatch(query_id=query.query_id, hits=[], status="error", error="network_timeout")
        except httpx.TransportError:
            return SearchBatch(query_id=query.query_id, hits=[], status="error", error="network_error")
        finally:
            if owned:
                await client.aclose()


class EgressFetchProvider:
    def __init__(self, proxy: str, *, max_bytes: int = 8_000_000, timeout: int = 30, max_redirects: int = 5, client: httpx.AsyncClient | None = None):
        if not proxy:
            raise ValueError("egress proxy required")
        self.proxy = proxy
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.client = client

    async def fetch(self, request: FetchRequest) -> tuple[FetchResult, bytes | None]:
        original = request.url
        url = original
        owned = self.client is None
        client = self.client or httpx.AsyncClient(proxy=self.proxy, trust_env=False, timeout=self.timeout, follow_redirects=False)
        total = 0
        deadline = time.monotonic() + self.timeout
        try:
            for _ in range(self.max_redirects + 1):
                validate_public_url(url)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError("fetch total timeout exceeded")
                async with client.stream("GET", url, follow_redirects=False, timeout=remaining, headers={"Accept": "text/html,application/pdf"}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ProviderError("redirect without Location")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise ProviderError(f"HTTP {response.status_code}")
                    mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if mime not in {"text/html", "application/pdf"}:
                        raise ProviderError("unsupported content type")
                    chunks = []
                    async for chunk in response.aiter_bytes():
                        if time.monotonic() >= deadline:
                            raise ProviderError("fetch total timeout exceeded")
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise ProviderError("decompressed response exceeds limit")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    result = FetchResult(source_id=request.source_id, requested_url=original, final_url=url, status="ok", mime=mime, headers={"content-type": response.headers.get("content-type", "")}, blob_hash=hashlib.sha256(raw).hexdigest(), fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), error=None)
                    return result, raw
            raise ProviderError("too many redirects")
        finally:
            if owned:
                await client.aclose()
