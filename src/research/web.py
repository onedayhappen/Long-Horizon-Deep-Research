"""Loopback-only workbench over the existing research protocol and CLI.

No second research engine: child processes use the same contracts, configuration,
leases, mailbox and SQLite ledger as command-line runs. Credentials are ephemeral.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .config import ResearchConfig
from .jsonio import canonical, load
from .models import ResearchContract, SearchBatch, RoleResponse, FetchResult
from .storage import Store

STATIC = Path(__file__).with_name("web_static")
PROJECT = Path(__file__).resolve().parents[2]
ARTIFACTS = {"report.md", "report.json", "evidence.json", "coverage.json", "outline.json", "stop.json", "execution.json", "contract.json", "conflicts.json"}
PROVIDERS = {"deepseek": "https://api.deepseek.com", "siliconflow": "https://api.siliconflow.cn/v1"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelSettings(Input):
    provider: Literal["deepseek", "siliconflow"] = "deepseek"
    model: str = Field(default="deepseek-v4-pro", min_length=1, max_length=120)
    api_key: SecretStr = SecretStr("")
    clear_key: bool = False


class NewResearch(Input):
    question: str = Field(min_length=5, max_length=3000)
    requirements: list[str] = Field(default_factory=list, max_length=20)
    scope: str = Field(default="", max_length=4000)
    mode: Literal["assisted", "demo"] = "assisted"
    max_calls: int = Field(default=120, ge=20, le=1000)
    max_minutes: int = Field(default=60, ge=5, le=240)
    contract: dict | None = None


def make_contract(data: NewResearch, run_id: str) -> ResearchContract:
    if data.contract is not None:
        return ResearchContract.model_validate(data.contract, strict=True)
    questions = [q.strip() for q in data.requirements if q.strip()] or [data.question]
    requirements = []
    for i, question in enumerate(questions, 1):
        rid = f"R{i}"
        requirements.append({"id": rid, "question": question, "priority": "must", "weight": 1,
            "allow_unknown": False, "unknown_check_ids": [], "acceptance_checks": [
                {"check_id": f"{rid}.support", "code": "has_current_support", "stage": "research", "params": {"claim_kinds": ["attributed", "factual", "comparative", "derived", "inference"], "min_claims": 1}, "reason": "回答必须有可定位的证据支持"},
                {"check_id": f"{rid}.answer", "code": "answer_in_report", "stage": "delivery", "params": {"require_limitation_disclosure": True}, "reason": "在报告中回答并披露限制"}]})
    return ResearchContract.model_validate({"schema_version": 1, "contract_id": run_id, "version": 1,
        "question": data.question, "scope": {"subjects": [data.question], "include": [data.scope] if data.scope else []},
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "requirements": requirements,
        "output_contract": {"language": "zh-CN", "format": "markdown", "required_sections": ["结论", "研究发现", "依据与限制"], "max_characters": 16000}}, strict=True)


class Workbench:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = ModelSettings()
        self.lock = threading.RLock()
        self.jobs: dict[str, subprocess.Popen] = {}
        self.errors: dict[str, str] = {}

    def path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise HTTPException(400, "无效的研究 ID")
        path = self.root / run_id
        if path.is_symlink() or path.resolve().parent != self.root:
            raise HTTPException(400, "研究路径不在工作区内")
        if not path.is_dir():
            raise HTTPException(404, "研究不存在")
        return path

    def file(self, run_id: str, name: str) -> Path:
        base = self.path(run_id)
        path = base / name
        if not path.resolve().is_relative_to(base) or path.is_symlink():
            raise HTTPException(400, "无效的文件路径")
        return path

    def read(self, run_id: str, name: str, fallback=None):
        path = self.file(run_id, name)
        return load(path, max_bytes=8_000_000) if path.is_file() else fallback

    def pending(self, run_id: str) -> list[dict]:
        bridge = self.file(run_id, "bridge")
        return [load(self.file(run_id, f"bridge/{p.name}"), max_bytes=4_000_000)
                for p in sorted(bridge.glob("*.request.json"))
                if not p.with_name(p.name.replace(".request.json", ".response.json")).exists()]

    def summary(self, run_id: str, detail=False) -> dict:
        directory = self.path(run_id)
        contract = self.read(run_id, "contract.json", {})
        result = {"run_id": run_id, "question": contract.get("question", run_id), "phase": "scoping",
                  "lifecycle_status": "starting", "created_at": datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc).isoformat()}
        if self.file(run_id, "state.sqlite").is_file():
            store = Store(directory, readonly=True)
            try:
                result.update(store.status())
                result["counts"] = {r["kind"]: r["n"] for r in store.db.execute("SELECT kind, COUNT(*) AS n FROM entity_heads GROUP BY kind")}
                stamp = store.db.execute("SELECT MIN(created_at) FROM events").fetchone()[0]
                if stamp:
                    result["created_at"] = stamp
                lease = store.db.execute("SELECT expires_at FROM leases WHERE singleton=1").fetchone()
                result["lease_active"] = bool(lease and lease[0] > datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
                if detail:
                    entities: dict[str, list] = {}
                    for row in store.db.execute("SELECT v.kind,v.id,v.payload_json FROM entity_versions v JOIN entity_heads h ON v.kind=h.kind AND v.id=h.id AND v.version=h.version ORDER BY v.id"):
                        entities.setdefault(row["kind"], []).append({"id": row["id"], **json.loads(row["payload_json"])})
                    result["entities"] = entities
                    result["events"] = [dict(row) for row in store.db.execute("SELECT event_seq,kind,created_at FROM events ORDER BY event_seq DESC LIMIT 60")]
            finally:
                store.close()
        process = self.jobs.get(run_id)
        result["running"] = bool((process and process.poll() is None) or result.get("lease_active"))
        result["exit_code"] = process.poll() if process else None
        result["error"] = self.errors.get(run_id)
        result["pending"] = self.pending(run_id)
        result["execution"] = self.read(run_id, "execution.json", {})
        result["artifacts"] = [name for name in sorted(ARTIFACTS) if self.file(run_id, name).is_file()]
        if detail:
            result["contract"] = contract
            result["coverage"] = self.read(run_id, "coverage.json", result.get("entities", {}).get("coverage", []))
            result["evidence"] = self.read(run_id, "evidence.json", result.get("entities", {}).get("evidence", []))
            report = self.file(run_id, "report.md")
            result["report"] = report.read_text("utf-8") if report.is_file() and report.stat().st_size <= 8_000_000 else ""
            result["stop"] = self.read(run_id, "stop.json", {})
        else:
            result["pending"] = [{"kind": p["kind"]} for p in result["pending"]]
        return result

    def key(self):
        env_name = "DEEPSEEK_API_KEY" if self.settings.provider == "deepseek" else "SILICONFLOW_API_KEY"
        return self.settings.api_key.get_secret_value().strip() or os.environ.get(env_name, "").strip()

    def launch(self, run_id: str, arguments: list[str], key_env="RESEARCH_WEB_API_KEY"):
        with self.lock:
            if run_id in self.jobs and self.jobs[run_id].poll() is None:
                raise HTTPException(409, "研究已在运行")
            if sum(p.poll() is None for p in self.jobs.values()) >= 3:
                raise HTTPException(409, "最多同时运行 3 个研究，请等待现有任务完成")
            environment = os.environ.copy()
            secret = self.key()
            if secret:
                environment[key_env] = secret
            process = subprocess.Popen([sys.executable, "-m", "src", "research", *arguments], cwd=PROJECT,
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.jobs[run_id] = process
            self.errors.pop(run_id, None)
            def collect():
                # Continuously drain output; retain only a bounded failure tail.
                tail = b""
                while chunk := process.stdout.read(1024):
                    tail = (tail + chunk)[-6000:]
                code = process.wait()
                process.stdout.close()
                if code:
                    message = tail.decode("utf-8", errors="replace")
                    self.errors[run_id] = message.replace(secret, "[redacted]") if secret else message
            threading.Thread(target=collect, daemon=True).start()


def create_app(runs_root: Path) -> FastAPI:
    wb = Workbench(runs_root)

    @asynccontextmanager
    async def lifespan(app):
        yield
        for process in list(wb.jobs.values()):
            if process.poll() is None:
                process.terminate()
        for process in list(wb.jobs.values()):
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)

    app = FastAPI(title="Deep Research", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.workbench = wb

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = urlsplit(f"http://{request.headers.get('host', '')}").hostname
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "仅接受本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
        if request.method in {"POST", "PUT", "DELETE"}:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "请发送 JSON"}, status_code=415)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 12_000_000:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # Validation errors must never echo the credentials in a request body.
        return JSONResponse({"detail": "输入格式不正确，请检查字段及长度"}, status_code=422)

    @app.get("/api/settings")
    def settings():
        return {"provider": wb.settings.provider, "model": wb.settings.model, "configured": bool(wb.key()), "runs_root": str(wb.root), "mode": "assisted"}

    @app.post("/api/settings")
    def save_settings(value: ModelSettings):
        if len(value.api_key.get_secret_value()) > 4096:
            raise HTTPException(422, "密钥长度超出限制")
        with wb.lock:
            if not value.api_key.get_secret_value() and not value.clear_key and value.provider == wb.settings.provider:
                value.api_key = wb.settings.api_key
            wb.settings = value
        return settings()

    @app.post("/api/settings/check")
    async def check():
        key = wb.key()
        if not key:
            raise HTTPException(400, "请先配置模型密钥")
        config = wb.settings
        try:
            async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
                response = await client.post(PROVIDERS[config.provider] + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"}, json={"model": config.model,
                    "messages": [{"role": "user", "content": 'Return JSON: {"ok":true}'}],
                    "response_format": {"type": "json_object"}, "max_tokens": 128, "stream": False,
                    **({"thinking": {"type": "disabled"}} if config.provider == "deepseek" else {})})
                response.raise_for_status()
                parsed = response.json()
                if not parsed.get("choices") or not parsed["choices"][0].get("message", {}).get("content"):
                    raise ValueError("empty model response")
                return {"ok": True, "model": config.model, "usage": parsed.get("usage")}
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"模型服务返回 {exc.response.status_code}，请检查密钥、模型名称和账户余额") from None
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(502, "未取得有效模型响应，请检查网络和模型配置") from None

    @app.get("/api/runs")
    def runs():
        items, warnings = [], []
        for path in wb.root.iterdir():
            if path.is_dir() and not path.is_symlink() and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", path.name) and (path / "contract.json").is_file():
                try:
                    items.append(wb.summary(path.name))
                except Exception:
                    warnings.append(f"{path.name} 暂时无法读取")
        return {"runs": sorted(items, key=lambda r: r["created_at"], reverse=True), "warnings": warnings}

    @app.post("/api/runs", status_code=201)
    def new_run(data: NewResearch):
        run_id = "study-" + datetime.now().strftime("%Y%m%d-") + uuid.uuid4().hex[:8]
        if data.mode == "assisted" and not wb.key():
            raise HTTPException(400, "请先在模型设置中配置 API 密钥")
        staging = wb.root / ".web-inputs" / run_id
        if not staging.resolve().is_relative_to(wb.root):
            raise HTTPException(400, "无效的输入目录")
        staging.mkdir(parents=True)
        if data.mode == "demo":
            fixture = PROJECT / "tests/fixtures/research/success"
            if not fixture.is_dir():
                raise HTTPException(400, "离线样例仅在源码仓库中提供")
            contract = ResearchContract.model_validate(load(fixture / "contract.json"), strict=True)
            config_text = (fixture / "research.toml").read_text("utf-8").replace('fixture_dir = "."', "fixture_dir = " + json.dumps(fixture.as_posix()))
        else:
            try:
                contract = make_contract(data, run_id)
            except ValueError:
                raise HTTPException(422, "研究合约不符合协议，请检查需求与验收项") from None
            config_text = f'''[research.execution]
mode = "assisted"
[research.model]
backend = "chat_json"
api_key_env = "RESEARCH_WEB_API_KEY"
base_url = {json.dumps(PROVIDERS[wb.settings.provider])}
model = {json.dumps(wb.settings.model)}
max_output_tokens = 8192
{('thinking = "disabled"' if wb.settings.provider == 'deepseek' else '')}
[research.search]
provider = "external"
[research.fetch]
mode = "external"
[research.budget]
max_external_calls = {data.max_calls}
max_duration_seconds = {data.max_minutes * 60}
'''
        (staging / "contract.json").write_bytes(canonical(contract.model_dump(mode="json")))
        (staging / "research.toml").write_text(config_text, encoding="utf-8")
        wb.launch(run_id, ["run", "--contract", str(staging / "contract.json"), "--config", str(staging / "research.toml"), "--runs-root", str(wb.root), "--run-id", run_id])
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def detail(run_id: str):
        if not (wb.root / run_id).exists() and run_id in wb.jobs:
            return {"run_id": run_id, "lifecycle_status": "starting" if wb.jobs[run_id].poll() is None else "failed", "error": wb.errors.get(run_id), "question": "正在初始化研究…", "pending": [], "artifacts": []}
        try:
            return wb.summary(run_id, detail=True)
        except HTTPException:
            raise
        except Exception:
            # The child creates its directory/schema before initializing the
            # run row. Polling must tolerate that short, observable interval.
            process = wb.jobs.get(run_id)
            if process is not None and process.poll() is None:
                return {"run_id": run_id, "lifecycle_status": "starting", "question": "正在初始化研究…", "pending": [], "artifacts": []}
            raise HTTPException(503, "研究状态暂时无法读取，请稍后刷新") from None

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        with wb.lock:
            summary = wb.summary(run_id)
            if summary["running"] or summary["lifecycle_status"] == "complete":
                raise HTTPException(409, "该研究正在运行或已完成")
            config = ResearchConfig.model_validate_json(wb.file(run_id, "resolved_config.json").read_bytes(), strict=True)
            if config.research.model.backend == "chat_json":
                if config.research.model.base_url != PROVIDERS[wb.settings.provider] or config.research.model.model != wb.settings.model:
                    raise HTTPException(409, "请先将模型设置切换为该研究使用的服务商和模型")
                if not wb.key() and not os.environ.get(config.research.model.api_key_env):
                    raise HTTPException(400, "请先配置该研究使用的模型密钥")
            wb.launch(run_id, ["resume", "--run-dir", str(wb.path(run_id))], config.research.model.api_key_env)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/artifacts/{name}")
    def artifact(run_id: str, name: str):
        if name not in ARTIFACTS:
            raise HTTPException(404, "文件不存在")
        path = wb.file(run_id, name)
        if not path.is_file():
            raise HTTPException(404, "文件尚未生成")
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    @app.get("/api/runs/{run_id}/snapshots/{sha}")
    def snapshot(run_id: str, sha: str):
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise HTTPException(400, "无效的快照标识")
        path = wb.file(run_id, f"blobs/{sha}")
        if not path.is_file():
            raise HTTPException(404, "快照不存在")
        # Raw pages/PDFs are downloaded, never executed in the workbench origin.
        return FileResponse(path, media_type="application/octet-stream", filename=f"snapshot-{sha}.bin")

    @app.post("/api/runs/{run_id}/responses/{request_id}")
    def respond(run_id: str, request_id: str, body: dict):
        if not re.fullmatch(r"[a-f0-9]{64}", request_id):
            raise HTTPException(400, "无效的请求 ID")
        with wb.lock:
            pending = next((p for p in wb.pending(run_id) if p["request_id"] == request_id), None)
            if pending is None:
                raise HTTPException(409, "请求已处理或不存在")
            if body.get("input_hash") != pending["input_hash"]:
                raise HTTPException(409, "响应与请求不匹配")
            if "request_id" in body and body["request_id"] != request_id:
                raise HTTPException(409, "响应与请求不匹配")
            producer = body.get("producer")
            result = body.get("result")
            if not isinstance(producer, str) or not producer.strip() or not isinstance(result, dict):
                raise HTTPException(422, "请提供 producer 和 result 对象")
            try:
                if pending["kind"] == "search":
                    value = SearchBatch.model_validate(result, strict=True)
                    if value.query_id != pending["payload"]["query_id"]:
                        raise ValueError()
                elif pending["kind"] == "role":
                    RoleResponse.model_validate(result, strict=True)
                elif pending["kind"] == "fetch":
                    value = FetchResult.model_validate(result, strict=True)
                    if value.source_id != pending["payload"]["source_id"] or value.requested_url != pending["payload"]["url"]:
                        raise ValueError()
                    snapshot = body.get("snapshot_text")
                    if snapshot is not None:
                        raw = snapshot.encode("utf-8")
                        if len(raw) > 8_000_000 or hashlib.sha256(raw).hexdigest() != value.blob_hash:
                            raise ValueError()
                        blob = wb.file(run_id, "bridge/blobs/" + value.blob_hash)
                        blob.parent.mkdir(parents=True, exist_ok=True)
                        blob.write_bytes(raw)
                    elif not value.blob_hash or not re.fullmatch(r"[a-f0-9]{64}", value.blob_hash) or not wb.file(run_id, "bridge/blobs/" + value.blob_hash).is_file():
                        raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(422, "响应数据或快照哈希不符合请求协议") from None
            target = wb.file(run_id, f"bridge/{request_id}.response.json")
            temp = target.with_suffix(".web.tmp")
            temp.write_bytes(canonical({"request_id": request_id, "input_hash": pending["input_hash"], "producer": producer, "result": result}))
            os.replace(temp, target)
        return {"ok": True}

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def serve(runs_root: Path, port: int = 8765) -> int:
    import uvicorn
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    print(f"Deep Research workbench: http://127.0.0.1:{port}", flush=True)
    uvicorn.run(create_app(runs_root), host="127.0.0.1", port=port)
    return 0
