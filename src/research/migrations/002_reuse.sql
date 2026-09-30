CREATE TABLE run_identity (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), run_instance_id TEXT NOT NULL UNIQUE,
  runs_root TEXT NOT NULL, owner TEXT NOT NULL, parent_run_id TEXT, parent_instance_id TEXT
);
CREATE TABLE reuse_imports (
  import_key TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('planned','copying','committed')),
  error_code TEXT, copied_blobs INTEGER NOT NULL DEFAULT 0, receipt_json TEXT
);
CREATE TABLE reuse_mappings (
  import_key TEXT NOT NULL REFERENCES reuse_imports(import_key),
  source_instance_id TEXT NOT NULL, source_version_id TEXT NOT NULL,
  target_version_id TEXT NOT NULL REFERENCES entity_versions(version_id),
  origin_json TEXT NOT NULL, imported_from_json TEXT NOT NULL,
  PRIMARY KEY(import_key,source_version_id), UNIQUE(target_version_id)
);
CREATE INDEX reuse_source_idx ON reuse_mappings(source_instance_id,source_version_id);
CREATE TABLE reuse_candidates (
  evidence_version_id TEXT NOT NULL REFERENCES entity_versions(version_id),
  claim_version_id TEXT NOT NULL REFERENCES entity_versions(version_id),
  import_key TEXT NOT NULL REFERENCES reuse_imports(import_key), history_tier TEXT NOT NULL,
  source_requirement_ids TEXT NOT NULL, PRIMARY KEY(evidence_version_id,claim_version_id)
);
CREATE TABLE reuse_bindings (
  binding_id TEXT PRIMARY KEY, evidence_version_id TEXT NOT NULL REFERENCES entity_versions(version_id),
  claim_version_id TEXT NOT NULL REFERENCES entity_versions(version_id), requirement_id TEXT NOT NULL,
  mapping_json TEXT NOT NULL, disposition TEXT NOT NULL, assessment_version_id TEXT,
  local_claim_version_id TEXT, local_audit_version_id TEXT, epoch INTEGER NOT NULL DEFAULT 0,
  cause_notice_ids TEXT NOT NULL DEFAULT '[]', UNIQUE(evidence_version_id,claim_version_id,requirement_id)
);
CREATE TABLE reuse_ancestors (
  run_instance_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, locator TEXT NOT NULL,
  owner TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, cursor_hash TEXT NOT NULL DEFAULT '',
  high_water INTEGER, status TEXT NOT NULL DEFAULT 'unknown', checked_at TEXT
);
CREATE TABLE reuse_lineage (
  ancestor_instance_id TEXT NOT NULL REFERENCES reuse_ancestors(run_instance_id),
  ancestor_version_id TEXT NOT NULL, ancestor_payload_hash TEXT NOT NULL,
  local_version_id TEXT NOT NULL REFERENCES entity_versions(version_id),
  relation TEXT NOT NULL DEFAULT 'copied_from',
  PRIMARY KEY(ancestor_instance_id,ancestor_version_id,local_version_id)
);
CREATE INDEX reuse_lineage_local_idx ON reuse_lineage(local_version_id);
CREATE TABLE invalidation_outbox (
  sequence INTEGER PRIMARY KEY, notice_id TEXT NOT NULL UNIQUE,
  previous_notice_hash TEXT NOT NULL, notice_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE invalidation_receipts (
  issuer_instance_id TEXT NOT NULL, notice_id TEXT NOT NULL, local_version_id TEXT NOT NULL,
  PRIMARY KEY(issuer_instance_id,notice_id,local_version_id)
);
CREATE TABLE research_checkpoints (
  state_version INTEGER PRIMARY KEY, digest_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE delivery_archives (
  stop_version_id TEXT PRIMARY KEY REFERENCES entity_versions(version_id),
  state_version INTEGER NOT NULL, artifacts_json TEXT NOT NULL
);
CREATE TABLE context_packets (
  packet_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
  input_manifest_hash TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TRIGGER no_context_update BEFORE UPDATE ON context_packets BEGIN SELECT RAISE(ABORT,'immutable context'); END;
CREATE TRIGGER no_context_delete BEFORE DELETE ON context_packets BEGIN SELECT RAISE(ABORT,'immutable context'); END;
CREATE INDEX evidence_links_audit_idx ON evidence_links(audit_version_id);
CREATE INDEX dependency_edges_to_idx ON dependency_edges(to_version_id);
CREATE VIRTUAL TABLE reuse_fts USING fts5(evidence_version_id UNINDEXED, claim_version_id UNINDEXED, body, tokenize='trigram');
