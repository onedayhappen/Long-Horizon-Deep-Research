import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from lh_harness.research.config import load_config
from lh_harness.research.jsonio import load, loads
from lh_harness.research.models import ResearchContract, RoleRequest


ROOT = Path(__file__).parents[1] / "fixtures" / "research" / "success"
INVALID = ROOT.parent / "invalid"


def test_valid_contract_and_config():
    contract = ResearchContract.model_validate(load(ROOT / "contract.json"), strict=True)
    config = load_config(ROOT / "research.toml")
    assert contract.requirements[0].acceptance_checks[0].params.min_claims == 1
    assert config.research.execution.fixture_dir == ROOT.resolve()


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(extra=1),
    lambda x: x["requirements"][0].update(allow_unknown=1),
    lambda x: x["requirements"][0]["acceptance_checks"][0].update(stage="delivery"),
    lambda x: x["requirements"][0]["acceptance_checks"][0]["params"].update(min_claims=0),
    lambda x: x["requirements"][0]["acceptance_checks"][0]["params"].update(bogus=True),
])
def test_invalid_contract(mutate):
    data = deepcopy(load(ROOT / "contract.json"))
    mutate(data)
    with pytest.raises((ValidationError, ValueError)):
        ResearchContract.model_validate(data, strict=True)


def test_duplicate_keys_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        loads('{"schema_version":1,"schema_version":1}')


@pytest.mark.parametrize("name", ["duplicate_key.json", "extra_field.json", "bad_stage.json"])
def test_invalid_fixture_files(name):
    with pytest.raises((ValueError, ValidationError)):
        ResearchContract.model_validate(load(INVALID / name), strict=True)


def test_unsupported_config_field():
    with pytest.raises(ValidationError):
        load_config(INVALID / "extra_config.toml")


def test_role_request_rejects_unknown_field_and_role():
    base = {"role": "planner.next", "logical_action_key": "a", "input_manifest_hash": "0" * 64, "contract_version": 1, "policy_version": 1, "system_template_id": "planner.next.v1", "system_template_hash": "1" * 64, "data_packet": {"requirement_ids": ["R1"]}, "response_schema_id": "SearchAction", "response_schema_version": 1, "max_output_tokens": 32}
    RoleRequest.model_validate(base, strict=True)
    with pytest.raises(ValidationError):
        RoleRequest.model_validate(dict(base, role="untrusted"), strict=True)
    with pytest.raises(ValidationError):
        RoleRequest.model_validate(dict(base, tools=["shell"]), strict=True)
