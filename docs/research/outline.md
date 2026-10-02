# Evidence-driven outline refinement

The outline starts as a concise set of top-level questions. After each search,
the planner reviews audited material and may add second- or third-level sections.
The report title is outside these three section levels: Markdown uses `##`,
`###`, and `####` respectively.

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
any active path deeper than three levels (including moved subtrees).

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
`outline.json` retains all versions. Adding headings does not count as a search
round, but planning and writing still consume the existing time/call budgets.

## Verification

`tests/research/test_outline_hierarchy.py` runs a fictional two-search scenario:
flat outline → second level → missing-dimension review → rejected premature
termination → third level → scoped writing → ordered export. It also checks
binding isolation and excessive depth through add/split/move operations.
Existing single-topic and bounded-unknown replay fixtures remain flat. These
tests verify the protocol, not the quality of a live model's editorial judgment.
