from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from .budget import BudgetDenied
from .config import ResearchConfig, load_config
from .controller import Controller, IncompleteResearch
from .loop import ResearchStopped
from .jsonio import canonical, digest, load
from .models import ResearchContract
from .model_backend import configured_backend
from .replay import FixtureMismatch
from .storage import RunBusy, Store


def _inputs(contract_path: Path, config_path: Path) -> tuple[ResearchContract, ResearchConfig]:
    contract = ResearchContract.model_validate(load(contract_path), strict=True)
    config = load_config(config_path)
    return contract, config


def _store(run_dir: Path, config: ResearchConfig) -> Store:
    if config.research.execution.mode == "assisted":
        return Store(run_dir)
    manifest = load(config.research.execution.fixture_dir / "fixture_manifest.json")
    return Store(run_dir, namespace_seed=manifest.get("namespace_seed"))


async def _execute(run_dir: Path, contract: ResearchContract, config: ResearchConfig) -> int:
    store = _store(run_dir, config)
    try:
        if store.run()["lifecycle_status"] == "complete":
            print(json.dumps(store.status(), ensure_ascii=False))
            return 0
        providers = None
        if config.research.execution.mode == "assisted":
            from .assisted import AssistantMailbox, ExternalRoleBackend, ExternalSearchProvider, ExternalFetchProvider
            elapsed = sum(json.loads(row[0])['seconds'] for row in store.db.execute("SELECT payload_json FROM events WHERE kind='active_time'"))
            mailbox = AssistantMailbox(run_dir / "bridge", max(0,config.research.budget.max_duration_seconds-elapsed))
            roles = configured_backend(config.research.model) if config.research.model.backend == "chat_json" else ExternalRoleBackend(mailbox)
            providers = (roles, ExternalSearchProvider(mailbox), ExternalFetchProvider(mailbox))
        controller = Controller(store, contract, config.research.execution.fixture_dir, config.research.budget.max_external_calls, config.research.budget.pool_percentages, external_providers=providers,
            max_rounds=config.research.runtime.max_rounds, max_duration_seconds=config.research.budget.max_duration_seconds,
            max_no_progress_actions=config.research.stopping.max_no_progress_actions,
            saturation_effective_rounds=config.research.stopping.saturation_effective_rounds,
            max_revision_cycles=config.research.stopping.max_revision_cycles,
            max_output_tokens=config.research.model.max_output_tokens,
            api_model_name=config.research.model.model if config.research.model.backend == "chat_json" else None)
        heartbeat = asyncio.create_task(controller.heartbeat_loop())
        try:
            try:
                outcome = await controller.run()
                print(json.dumps({"run_id": store.run()["run_id"], "outcome": outcome, "report": str(run_dir / "report.md")}, ensure_ascii=False))
                return 0
            except BudgetDenied as exc:
                controller.record_stop("incomplete_budget", str(exc))
                raise
            except ResearchStopped as exc:
                controller.record_stop(exc.outcome, str(exc))
                raise
            except IncompleteResearch as exc:
                controller.record_stop("incomplete_no_progress", str(exc))
                raise
            except Exception as exc:
                controller.record_stop("failed", f"{type(exc).__name__}: {exc}")
                raise
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            controller.close()
            if config.research.model.backend == "chat_json":
                metadata_path = run_dir / "execution.json"
                metadata = load(metadata_path)
                metadata["model_api_key_used"] = metadata.get("model_api_key_used", False) or roles.api_requests > 0
                metadata["model_api_requests"] = metadata.get("model_api_requests", 0) + roles.api_requests
                metadata_path.write_bytes(canonical(metadata))
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lh-harness research")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Validate a complete research contract and configuration")
    validate.add_argument("--contract", type=Path, required=True)
    validate.add_argument("--config", type=Path, required=True)
    model_check = sub.add_parser("model-check", help="Make one model initialization call and validate its JSON schema")
    model_check.add_argument("--contract", type=Path, required=True)
    model_check.add_argument("--config", type=Path, required=True)
    run = sub.add_parser("run", help="Start a replay or assistant-operated research run")
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--runs-root", type=Path, required=True)
    run.add_argument("--run-id", required=True)
    resume = sub.add_parser("resume", help="Resume a replay or assistant-operated run")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.add_argument("--budget-extension", type=Path)
    status = sub.add_parser("status", help="Read research state")
    status.add_argument("--run-dir", type=Path, required=True)
    status.add_argument("--json", action="store_true")
    export = sub.add_parser("export", help="Print report path after checking the run")
    export.add_argument("--run-dir", type=Path, required=True)
    review = sub.add_parser("review", help="Record a human decision")
    review.add_argument("--run-dir", type=Path, required=True)
    review.add_argument("--decision", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "model-check":
            from .models import InitializationProposal, RoleRequest
            from .prompts import template
            contract, config = _inputs(args.contract, args.config)
            backend = configured_backend(config.research.model)
            packet = {"contract_json": contract.model_dump_json()}
            role = "planner.initialize"
            request = RoleRequest(role=role, logical_action_key="model-check", input_manifest_hash=digest(packet),
                contract_version=contract.version, policy_version=1, system_template_id=f"{role}.v1",
                system_template_hash=digest(template(role)), data_packet=packet,
                response_schema_id="InitializationProposal", response_schema_version=1,
                max_output_tokens=config.research.model.max_output_tokens)
            response = asyncio.run(backend.generate(request))
            InitializationProposal.model_validate_json(response.raw_text, strict=True)
            print(json.dumps({"valid": True, "model": config.research.model.model,
                "usage": response.usage.model_dump(mode="json")}, ensure_ascii=False))
            return 0
        if args.command == "validate":
            contract, config = _inputs(args.contract, args.config)
            print(json.dumps({"contract_id": contract.contract_id, "config_mode": config.research.execution.mode, "valid": True}))
            return 0
        if args.command == "run":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id):
                raise ValueError("run-id must be 1-64 letters, digits, underscores or hyphens")
            contract, config = _inputs(args.contract, args.config)
            if config.research.execution.mode == "live":
                raise ValueError("live controller is not yet available; no live call was made")
            if config.research.execution.mode == "replay":
                load(config.research.execution.fixture_dir / "fixture_manifest.json")
            if config.research.model.backend == "chat_json":
                configured_backend(config.research.model)
            run_dir = (args.runs_root / args.run_id).resolve()
            if run_dir.exists():
                raise ValueError("run directory already exists")
            run_dir.mkdir(parents=True)
            shutil.copyfile(args.contract, run_dir / "contract.json")
            shutil.copyfile(args.config, run_dir / "research.toml")
            (run_dir / "resolved_config.json").write_bytes(canonical(config.model_dump(mode="json")))
            (run_dir / "execution.json").write_bytes(canonical({"mode": config.research.execution.mode,
                "model_api_key_used": False, "search_api_key_used": False,
                "model_backend": config.research.model.backend, "model": config.research.model.model,
                "role_review_context": "separate_api_requests_same_model" if config.research.model.backend == "chat_json" else "single_assistant_session" if config.research.execution.mode == "assisted" else "fixed_replay",
                "independent_model_review": False, "token_usage": None, "cost_usd": None}))
            store = _store(run_dir, config)
            store.init_run(args.run_id, contract.version, digest(config.model_dump(mode="json")))
            store.close()
            return asyncio.run(_execute(run_dir, contract, config))
        if args.command == "resume":
            if args.budget_extension:
                raise ValueError("budget extension is not implemented")
            run_dir = args.run_dir.resolve()
            contract = ResearchContract.model_validate(load(run_dir / "contract.json"), strict=True)
            config = ResearchConfig.model_validate_json((run_dir / "resolved_config.json").read_bytes(), strict=True)
            return asyncio.run(_execute(run_dir, contract, config))
        if args.command == "status":
            store = Store(args.run_dir, readonly=True)
            try:
                data = store.status()
            finally:
                store.close()
            print(json.dumps(data, ensure_ascii=False, indent=2 if args.json else None))
            return 0
        if args.command == "export":
            store = Store(args.run_dir, readonly=True)
            try:
                from .export import export_artifacts
                paths = export_artifacts(store)
                print(json.dumps({"report": str(store.run_dir / "report.md"), "artifacts": [str(p) for p in paths]}, ensure_ascii=False))
                return 0
            finally:
                store.close()
        if args.command == "review":
            raise ValueError("review command is not implemented yet")
    except (ValidationError, ValueError, FileNotFoundError) as exc:
        print(f"invalid_config: {exc}", file=sys.stderr)
        return 2
    except FixtureMismatch as exc:
        print(f"fixture_mismatch: {exc}", file=sys.stderr)
        return 5
    except RunBusy as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except BudgetDenied as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ResearchStopped as exc:
        print(f"{exc.outcome}: {exc}", file=sys.stderr)
        return 3
    except IncompleteResearch as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"research_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    return 2
