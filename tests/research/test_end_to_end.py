import json
import shutil
from pathlib import Path

from lh_harness.research.cli import main
from lh_harness.research.storage import Store


FIXTURE = Path(__file__).parents[1] / "fixtures/research/success"
UNKNOWN = Path(__file__).parents[1] / "fixtures/research/unknown_empty"


def test_success_and_completed_resume(tmp_path, capsys):
    root = tmp_path / "runs"
    args = ["run", "--contract", str(FIXTURE / "contract.json"), "--config", str(FIXTURE / "research.toml"), "--runs-root", str(root), "--run-id", "case"]
    assert main(args) == 0
    report = (root / "case/report.md").read_text("utf-8")
    assert "官方资料声明" in report
    assert "不能据此认定" in report
    assert "local snapshot q1.1" in report
    report_manifest = json.loads((root / "case/report.json").read_text("utf-8"))
    assert report_manifest["citation_evidence_ids"] == ["q1.1.e1"]
    evidence = json.loads((root / "case/evidence.json").read_text("utf-8"))
    assert evidence[0]["snapshot"]["raw_hash"]
    store = Store(root / "case")
    before = store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert store.status()["research_outcome"] == "complete"
    store.close()
    assert main(["resume", "--run-dir", str(root / "case")]) == 0
    store = Store(root / "case")
    assert store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == before
    store.close()


def test_replay_has_identical_semantic_entities_across_run_ids(tmp_path):
    for name in ("one", "two"):
        assert main(["run", "--contract", str(FIXTURE / "contract.json"), "--config", str(FIXTURE / "research.toml"), "--runs-root", str(tmp_path), "--run-id", name]) == 0
    def entities(name):
        store = Store(tmp_path / name)
        rows = [tuple(row) for row in store.db.execute("SELECT kind,id,version,version_id,payload_hash FROM entity_versions ORDER BY kind,id,version")]
        store.close()
        return rows
    assert entities("one") == entities("two")


def test_resume_after_missing_audit_fixture(tmp_path):
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    manifest_path = fixture / "fixture_manifest.json"
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    target = next(key for key in manifest["roles"] if key.startswith("auditor.evidence|"))
    del manifest["roles"][target]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    root = tmp_path / "runs"
    args = ["run", "--contract", str(fixture / "contract.json"), "--config", str(fixture / "research.toml"), "--runs-root", str(root), "--run-id", "crash"]
    assert main(args) == 5
    store = Store(root / "crash")
    before = store.db.execute("SELECT COUNT(*) FROM attempts WHERE state='result_saved'").fetchone()[0]
    store.close()
    manifest_path.write_bytes(original)
    assert main(["resume", "--run-dir", str(root / "crash")]) == 0
    store = Store(root / "crash")
    assert store.run()["research_outcome"] == "complete"
    assert store.db.execute("SELECT COUNT(*) FROM attempts WHERE state='result_saved'").fetchone()[0] > before
    assert store.db.execute("SELECT COUNT(*) FROM attempts WHERE action_id='role:initialize'").fetchone()[0] == 1
    store.close()


def test_bounded_unknown_discloses_empty_search(tmp_path):
    assert main(["run", "--contract", str(UNKNOWN / "contract.json"), "--config", str(UNKNOWN / "research.toml"), "--runs-root", str(tmp_path), "--run-id", "unknown"]) == 0
    store = Store(tmp_path / "unknown")
    assert store.status()["research_outcome"] == "complete_with_limitations"
    store.close()
    report = (tmp_path / "unknown/report.md").read_text("utf-8")
    assert "调查记录:q1" in report
    assert "不能证明测量不存在" in report


def test_budget_exhaustion_records_non_success_stop(tmp_path):
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    path = fixture / "research.toml"
    path.write_text(path.read_text("utf-8").replace("max_external_calls = 120", "max_external_calls = 10"), encoding="utf-8")
    root = tmp_path / "runs"
    assert main(["run", "--contract", str(fixture / "contract.json"), "--config", str(path), "--runs-root", str(root), "--run-id", "budget"]) == 3
    store = Store(root / "budget")
    assert store.status()["research_outcome"] == "incomplete_budget"
    assert store.status()["gate_results"][0]["status"] == "fail"
    store.close()
