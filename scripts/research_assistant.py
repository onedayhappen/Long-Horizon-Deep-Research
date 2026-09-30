"""Inspect/respond to the assistant mailbox. No model API client is used."""
import argparse
import json
import os
from pathlib import Path

from src.research.jsonio import canonical, load


def pending(run_dir):
    return [p for p in sorted((run_dir / "bridge").glob("*.request.json"))
            if not p.with_name(p.name.replace(".request.json", ".response.json")).exists()]


def respond(path, result):
    request = load(path, max_bytes=4_000_000)
    if request["kind"] == "role":
        result = {"raw_text": canonical(result).decode(), "finish_reason": "stop",
                  "provider_request_id": None,
                  "usage": {"input_tokens": None, "output_tokens": None, "cost": None, "basis": "unknown"},
                  "model_profile_hash": "codex-current-session-unmetered"}
    envelope = {"request_id": request["request_id"], "input_hash": request["input_hash"],
                "producer": "Codex current conversation; assistant-authored",
                "result": result}
    target = path.with_name(path.name.replace(".request.json", ".response.json"))
    temp = target.with_suffix(".tmp")
    temp.write_bytes(canonical(envelope))
    os.replace(temp, target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    requests = pending(args.run_dir)
    if args.response:
        if len(requests) != 1:
            raise ValueError(f"expected one pending request, got {len(requests)}")
        respond(requests[0], load(args.response, max_bytes=4_000_000))
        print("response submitted")
        return
    for path in requests:
        request = load(path, max_bytes=4_000_000)
        print(path)
        print(request["kind"], request["logical_key"])
        if args.full:
            print(json.dumps(request, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
