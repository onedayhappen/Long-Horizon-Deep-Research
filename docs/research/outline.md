# Evidence-driven outline refinement

The outline starts as a concise set of top-level questions. After each search,
the planner reviews audited material and may add second- or third-level sections.
The report title is outside the section levels: Markdown starts with `##`.
`research.planning.max_depth` defaults to 4 and accepts 1–5. This is a guardrail,
not a requirement to produce a deep outline.

## When to expand

- Add a second-level section when sources reveal a distinct relevant mechanism,
  comparison dimension, condition, case, or limitation that needs explanation.
- Add a third-level section only when a second-level topic contains further
  substantive aspects. Different branches may stop at different depths.
- Preserve a flat outline for simple questions and sparse evidence. Source count
  is not a heading count; avoid one heading per source and fixed length quotas.
- A missing-evidence topic may be a research question or marked gap. Its heading
  does not establish a conclusion or satisfy evidence coverage.

For example, after a source establishes CSV export, `Evidence and limitations`
can gain a `CSV export` child. After a second source reveals a row limit, that
child can be split into `Export format` and `Row limit`. The original headings
remain overviews; the leaves explain their respective evidence and conditions.

## Planning and review

Both `planner.next` and `auditor.question_space` receive `outline_review_json`:
the current node paths, levels, children, siblings and material bindings, plus
audited claim/evidence pairs grouped by requirement. This includes new evidence
outside a node's existing bindings, so a narrow section cannot hide a new topic.
Raw candidate claims are not labelled as audited support in this catalog.

The planner checks structure after searches and before termination, including
when coverage already passes. The question-space auditor can name a missing
dimension, affected node and supporting references. Its existing failure gate
prevents freezing until the issue is resolved. This is a semantic model review,
not a deterministic guarantee that a particular topic requires a subheading.

Use `add` with `target_parent_id` for incremental expansion, or `split` with
fresh child IDs for multiple aspects. `bind_evidence` updates an existing node's
claim/evidence bindings without renaming it. Each patch retains its reason
references, operations and version history. Local validation rejects unknown
bindings, cycles, inactive parents, loss of required sections/requirements, and
any active path beyond the configured depth (including moved subtrees).

## Chapter questions and gaps

Each node retains a stable ID, a `research_question`, a `purpose`, sibling
`order`, requirement mappings and optional audited material bindings. Use
`update_node` to revise its intent and `reorder` with exactly one of `before_id`
and `after_id` to change sibling order. Writers and exports use the same order.
Question/purpose changes reopen previously closed gaps, requiring fresh review.

`mark_gap` creates a first-class `ResearchGap`, not a free-text note. It records
owners, original requirements, a concrete question, kind and priority. Lifecycle:

```text
open → investigating → resolved | accepted_unknown | deferred
                         ↘ reopen_gap → open
```

Only supplementary work can be deferred. Blocking gaps prohibit freezing.
`resolve_gap` must be a separate patch and pass `auditor.gap`, which checks the
chapter question against the supplied material. `resolved` requires current,
audited claim-version references. `accepted_unknown` requires a successful
targeted query, contract permission and current bounded-unknown coverage.
The receipt and checked references persist. Revoked supporting evidence blocks
completion again; a previous closure flag cannot override it.

Splitting/merging transfers gap ownership; retiring a node with gaps requires
explicit active replacements. Neither deleting a heading nor reordering the
outline can erase an unresolved question. Existing required-section and must
requirement checks still apply.

## Directed research and memory

`SearchAction` carries `target_node_ids`, `target_gap_ids` and `research_goal`.
The controller validates the chapter/requirement relationship and passes this
scope into both source selection and extraction. Legacy actions infer chapter
targets from their requirement and the goal from query text. Successful query
IDs are recorded as investigation references, including empty results; failed
queries cannot justify accepted unknowns.

Extraction returns candidate evidence plus a goal-oriented `summary`,
`new_dimensions` and `remaining_questions`. Summaries are leads, not proof.
Only the existing forward/reverse evidence audit qualifies factual support.
Snapshots, candidates, summaries and audits remain separately persisted.

The planning packet contains the full outline/gaps, compact audited claim
catalog and whole summary entries fitting a share of `context_characters`.
It prioritizes inspected and recent summaries; `omitted_summary_ids` records
the rest. `inspect_material` prioritizes selected entries for the next packet.
Individual entries are never sliced mid-condition. `summary_characters` is a
soft extraction target, not permission to remove important qualifications.

This is bounded retrieval, not lossless compression of unlimited research.
An individual oversized summary can remain omitted; full required state or
the final review packet exceeding the character budget stops explicitly with
`incomplete_context`. Increase the budget within the model's capacity or narrow
the research scope. The limit counts serialized packet/contract characters,
not prompt/schema tokens, and is not an automatic model-context detector.

## Progress, source reuse and completion

Research progress records novel material, newly resolved gaps and added nodes.
Metadata-only patches do not reset the no-progress counter. Question-space
review inspects structure and newly discovered dimensions each planning cycle;
coverage results are reused when their requirements and evidence are unchanged.

Exact URLs are deduplicated before fetching. Above `max_sources_per_action`,
`researcher.select_sources` must give a selection/rejection reason for every
candidate. Fetch bytes are cached within a run; `refresh_sources=true` bypasses
that cache. Extraction reuse additionally requires the same goal/scope, snapshot,
contract and extraction/audit prompts, with all saved evidence pairs still
qualified. A new goal reads cached bytes again. Visual-enabled runs keep their
existing figure-reading checks rather than using text-reading reuse.

The research pool reserves `closing_reserve_calls` from exploration; planner
and review calls may consume the reserve. Before freezing, the controller checks
remaining first-draft/revision calls in the writing pool, then calls
`auditor.outline`. This independent review covers every active node, relevant
discoveries, logical order, duplication and balance. Each leaf needs qualified
support or accepted unknown coverage; only a parent may be an overview without
its own claims. Missing dimensions send feedback to the planner. All original
coverage and final report checks remain required. Review is a model judgment,
not a deterministic guarantee of report depth or completeness.

## Writing and export

The writer receives `section_context_json`. Parents synthesize findings and
introduce children; leaves explain the supported findings, reasoning,
comparisons and limitations in substantive paragraphs as the evidence permits.
Context headings are organizational information, not factual sources. The
writer must keep claims within its supplied material and the contract's total
character limit. No minimum paragraph or word count is imposed.

Explicit claim and evidence bindings filter **audited pairs** together. Binding
only evidence also restricts the available claims; incompatible bindings yield
no factual material. Unbound nodes retain requirement-level material for broad
overviews and backward compatibility. Existing report review checks substantive
exposition and repetition, and may request a rewrite or reopen research.

Markdown follows the tree's preorder. `report.json.sections` uses that same
order; `section_hierarchy` records each section's ID, parent, title and level.
`outline.json` retains all versions, investigations, summaries, gap/outline
reviews and progress records. Adding headings does not count as a search
round, but planning and writing still consume the existing time/call budgets.

## Verification

`tests/research/test_outline_hierarchy.py` runs a fictional two-search scenario:
flat outline → second level → missing-dimension review → rejected premature
termination → third level → scoped writing → ordered export. It also checks
binding isolation and excessive depth through add/split/move operations.
Existing single-topic and bounded-unknown replay fixtures remain flat. These
tests verify the protocol, not the quality of a live model's editorial judgment.

`tests/research/test_planning.py` additionally exercises reviewed gap closure,
premature termination rejection, crash/resume without duplicated review,
ownership transfer, sibling order, scoped source reading, fetch/extraction
reuse, refresh, bounded memory, stale support, false final-review references
and closing budgets. The replay review responses are explicit test doubles.
