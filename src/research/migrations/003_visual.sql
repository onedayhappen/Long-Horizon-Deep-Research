-- E8 extends existing entities, blobs, attempts and dependencies, not a second store.
CREATE TABLE visual_cache (
  cache_key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);
CREATE TABLE visual_selections (
  action_id TEXT NOT NULL,
  figure_key TEXT NOT NULL,
  PRIMARY KEY(action_id,figure_key)
);
