CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, phase TEXT NOT NULL, lifecycle_status TEXT NOT NULL,
  research_outcome TEXT, state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0),
  contract_version INTEGER NOT NULL, config_hash TEXT NOT NULL, current_stop_version_id TEXT
);
CREATE TABLE entity_versions (
  kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
  version_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
  PRIMARY KEY(kind,id,version), UNIQUE(version_id,kind)
);
CREATE TABLE entity_heads (
  kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
  validity TEXT NOT NULL DEFAULT 'current', epistemic TEXT,
  PRIMARY KEY(kind,id), FOREIGN KEY(kind,id,version) REFERENCES entity_versions(kind,id,version)
);
CREATE TABLE evidence_links (
  link_id TEXT PRIMARY KEY NOT NULL,
  claim_version_id TEXT NOT NULL, claim_kind TEXT NOT NULL DEFAULT 'claim' CHECK(claim_kind='claim'),
  evidence_version_id TEXT NOT NULL, evidence_kind TEXT NOT NULL DEFAULT 'evidence' CHECK(evidence_kind='evidence'),
  audit_version_id TEXT NOT NULL, audit_kind TEXT NOT NULL DEFAULT 'audit' CHECK(audit_kind='audit'),
  relation TEXT NOT NULL CHECK(relation IN ('supports','refutes','contextualizes')),
  FOREIGN KEY(claim_version_id,claim_kind) REFERENCES entity_versions(version_id,kind),
  FOREIGN KEY(evidence_version_id,evidence_kind) REFERENCES entity_versions(version_id,kind),
  FOREIGN KEY(audit_version_id,audit_kind) REFERENCES entity_versions(version_id,kind)
);
CREATE INDEX evidence_links_claim_idx ON evidence_links(claim_version_id);
CREATE INDEX evidence_links_evidence_idx ON evidence_links(evidence_version_id);
CREATE TABLE dependency_edges (
  from_version_id TEXT NOT NULL REFERENCES entity_versions(version_id),
  to_version_id TEXT NOT NULL REFERENCES entity_versions(version_id),
  reason TEXT NOT NULL, UNIQUE(from_version_id,to_version_id,reason)
);
CREATE INDEX dependency_edges_from_idx ON dependency_edges(from_version_id);
CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY, action_id TEXT, proposal_hash TEXT NOT NULL,
  expected_state_version INTEGER NOT NULL, input_manifest_hash TEXT NOT NULL,
  receipt_json TEXT NOT NULL, UNIQUE(action_id,proposal_hash)
);
CREATE TABLE actions (
  action_id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
  input_manifest_hash TEXT NOT NULL, result_hash TEXT, result_json TEXT, error TEXT
);
CREATE TABLE attempts (
  attempt_id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(action_id),
  attempt_no INTEGER NOT NULL, state TEXT NOT NULL, usage_json TEXT, error TEXT,
  UNIQUE(action_id,attempt_no)
);
CREATE TABLE budget_reservations (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), pool TEXT NOT NULL,
  reserved_calls INTEGER NOT NULL, status TEXT NOT NULL, settled_usage_json TEXT
);
CREATE TABLE events (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT, state_version INTEGER NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE commands (
  command_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE blobs (
  sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, relative_path TEXT NOT NULL
);
CREATE TABLE review_tasks (
  task_id TEXT PRIMARY KEY, subject_input_hash TEXT NOT NULL, status TEXT NOT NULL,
  payload_json TEXT NOT NULL, decision_ref TEXT
);
CREATE TABLE leases (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner_id TEXT NOT NULL,
  generation INTEGER NOT NULL, expires_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
);
CREATE TABLE revision_jobs (
  job_id TEXT PRIMARY KEY, section_id TEXT NOT NULL, contract_version INTEGER NOT NULL,
  failed_audit_id TEXT NOT NULL, revision INTEGER NOT NULL,
  UNIQUE(section_id,contract_version,failed_audit_id)
);
CREATE TRIGGER no_entity_update BEFORE UPDATE ON entity_versions BEGIN SELECT RAISE(ABORT,'immutable entity'); END;
CREATE TRIGGER no_entity_delete BEFORE DELETE ON entity_versions BEGIN SELECT RAISE(ABORT,'immutable entity'); END;
