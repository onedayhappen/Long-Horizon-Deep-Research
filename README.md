# Deep Research

## Local web workbench

Run `python -m src research web --runs-root research-runs --port 8765`, then open
[http://127.0.0.1:8765](http://127.0.0.1:8765). The bundled interface supports creating
research, viewing progress, evidence and reports, responding to assistant mailbox
requests, downloading artifacts, and resuming runs. No frontend build is required.
Configure DeepSeek or SiliconFlow in **模型与设置**, or set `DEEPSEEK_API_KEY` /
`SILICONFLOW_API_KEY` before starting. Keys entered in the browser remain in server
memory only. Connection tests make one billable model request. The interface uses
the existing assisted engine: search and fetch still require external responses.
The server binds to loopback only. Shutting it down stops its child runs; saved
state remains resumable after their leases expire. The offline demo uses fictional
fixtures and requires a source checkout.

[简体中文](README.zh-CN.md)

Deep Research is an evidence based research and report workflow. Define a question, scope, and acceptance criteria; the system records source snapshots, checks claims and requirement coverage, and produces a report with traceable citations. Run state is stored locally so work can be inspected, exported, and resumed.

> This project is under development. Offline `replay` mode reproduces test scenarios. `assisted` mode needs an external assistant session to supply model, search, and fetch responses. The autonomous online `live` controller is not yet available.

## Features

- **Research contract:** A JSON contract defines the question, scope, required answers, evidence checks, and report structure. TOML controls execution mode and budgets.
- **Traceable evidence:** Source snapshots, text locations, claims, review decisions, and citation relationships are stored with the run.
- **Iterative outline:** Start with top-level questions, then develop evidence-backed second- and third-level sections as research uncovers distinct topics. Parents summarize; child sections explain scoped evidence and limits. See the [outline design](docs/research/outline.md).
- **Report review:** Section and whole report checks can request targeted revisions or send a gap back to the research stage.
- **Explicit reuse:** `--reuse-from` imports a frozen set of source materials into a new run. Candidates require purpose-specific time checks, document status validation and local audits. Imports recover after interruption; authorized ancestor invalidations are consumed before use.
- **Resumable runs:** SQLite stores state, budgets, and events. Reports and evidence can be exported after a run.

## Requirements and installation

- Python 3.11 or newer for the research command.
- Install from the project root:

```bash
python -m pip install -e ".[research,test]"
```

The current command entry point is `lh-harness research`. You can also use `python -m src research`, as in the examples below.

## Quick start: offline replay

The repository includes a **fictional** source fixture for checking the workflow. It does not produce a real research finding.

```bash
python -m src research validate \
  --contract tests/fixtures/research/success/contract.json \
  --config tests/fixtures/research/success/research.toml

python -m src research run \
  --contract tests/fixtures/research/success/contract.json \
  --config tests/fixtures/research/success/research.toml \
  --runs-root .lh-harness/demo-runs \
  --run-id example-001

python -m src research status \
  --run-dir .lh-harness/demo-runs/example-001 --json

python -m src research export \
  --run-dir .lh-harness/demo-runs/example-001
```

The line continuations above are for Bash; enter each command on one line in PowerShell. A run ID accepts letters, digits, underscores, and hyphens and must not reuse an existing run directory.

## Research your own question

To reuse an identified source within the same runs root, add `--reuse-from research-runs/research-a` to `research run`. Optionally pass `--reuse-selection selection.json`. Run `research reuse-sync --run-dir research-runs/research-b` to consume ancestor corrections without model or network calls. See [the reuse protocol and CLI examples](docs/research/reuse.md).

Research storage uses the pinned APSW wheel and its private SQLite engine. Install with `python -m pip install --only-binary=apsw -e ".[research,test]"`. Runs created before creation identity was recorded cannot serve as reuse sources (`reuse_source_unverified`).

1. Adapt [the example contract](examples/research/assisted/python_threads_contract.json) to define your question, scope, requirements, and acceptance checks.
2. Adapt [the assisted configuration](examples/research/assisted/research.toml) to set the mode and budget.
3. Validate the inputs, then start a run.
4. When the program emits `ASSISTANT_REQUEST`, read the request and submit the JSON response for that stage. Responses must match the request ID and input hash. Fetched source bytes must be stored under the required SHA-256 hash.

```powershell
python -m src research validate --contract examples/research/assisted/python_threads_contract.json --config examples/research/assisted/research.toml
python -m src research run --contract examples/research/assisted/python_threads_contract.json --config examples/research/assisted/research.toml --runs-root research-runs --run-id my-study

# In another terminal, inspect the pending request
python scripts/research_assistant.py research-runs/my-study --full

# After writing this stage's response to response.json, submit it
python scripts/research_assistant.py research-runs/my-study --response response.json
```

`assisted` mode requires an active external session. Running the command alone does not call a model or search provider automatically.

## Inspect and resume

```bash
python -m src research status --run-dir research-runs/my-study --json
python -m src research resume --run-dir research-runs/my-study
python -m src research export --run-dir research-runs/my-study
```

A run directory typically contains:

| Path | Purpose |
| --- | --- |
| `report.md`, `report.json` | Report and structured sections |
| `evidence.json`, `coverage.json` | Evidence, source locations, and requirement coverage |
| `outline.json` | Current outline and version history |
| `stop.json`, `execution.json` | Outcome, execution mode, and available usage data |
| `state.sqlite` | Recoverable state and event ledger |
| `blobs/`, `bridge/` | Source snapshots and assisted request/response exchange |

Run directories can contain source text, model output, and other sensitive material. They are excluded from Git by default.

## Long-form writing

The writer follows [WebWeaver's section-wise retrieval and writing approach](https://arxiv.org/html/2509.13312v2#S3.SS3). Each section receives the frozen outline, audited evidence, bounded local source context, and a bounded excerpt of preceding prose. Source context and previous prose do not authorize additional factual claims.

Optional TOML settings (defaults shown):

```toml
[research.writing]
max_output_tokens = 8192
target_section_characters = 1800
previous_context_characters = 2400
source_context_characters = 12000
max_expansion_rounds = 1
```

The writing token limit overrides `research.model.max_output_tokens` for writer calls; configure it for your provider. Body-character targets shrink to fit the contract and output allowance and are soft, so sparse evidence never requires padding. Sections with at least three supported claims but less than half their target receive at most one expansion by default, preserving first-draft calls for unfinished later sections. Increase the writing call pool (the second `research.budget.pool_percentages` entry) for larger outlines or more revisions.

Report review checks explanatory depth, repetition and omissions as well as factual support. It can request a section rewrite or reopen research for required missing evidence. Citation and final contract-length checks still apply. Fictional replay fixtures verify execution, not live model writing quality.

## Implementation status

Contract validation, offline replay, persistent state, iterative outlines, report revision, and export have implementations and tests. The `assisted` path has been exercised with real public documentation, but its role outputs were supplied by one assistant session. That is not an independent model review.

The autonomous online controller, full conflict handling, all stopping outcomes, and human review are still in progress. The `research review` command and budget extension are not implemented.

## Development

```bash
python -m pytest tests/research -q
```

## License

[MIT](LICENSE).
