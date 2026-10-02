from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .models import Usage
from .storage import Store, stamp, now


class BudgetDenied(RuntimeError):
    pass


class BudgetManager:
    def __init__(self, store: Store, max_calls: int, pool_percentages: list[int]):
        self.store = store
        self.action_scope: tuple[str,int] | None = None
        self.exploration_floor = 0
        self.limits = {
            "research": max_calls * pool_percentages[0] // 100,
            "writing": max_calls * pool_percentages[1] // 100,
        }
        self.limits["report_audit"] = max_calls - self.limits["research"] - self.limits["writing"]

    def reserve(self, action_id: str, kind: str, input_manifest_hash: str, pool: str, owner: str, generation: int) -> str:
        if pool not in self.limits:
            raise ValueError("invalid budget pool")
        attempt_id = str(uuid.uuid4())
        with self.store.transaction() as db:
            self.store.check_lease(db, owner, generation)
            if self.action_scope:
                scope, limit = self.action_scope
                scoped_calls = sum(json.loads(row[0])['scope']==scope for row in db.execute("SELECT payload_json FROM events WHERE kind='action_budget_call'"))
                if scoped_calls >= limit:
                    raise BudgetDenied('action_call_limit')
            used = db.execute("SELECT COUNT(*) FROM budget_reservations WHERE pool=? AND status!='released'", (pool,)).fetchone()[0]
            if used >= self.limits[pool]:
                raise BudgetDenied("budget_denied")
            if pool == 'research' and self.action_scope and used >= self.limits[pool] - self.exploration_floor:
                raise BudgetDenied('research_closing_reserve')
            db.execute("INSERT OR IGNORE INTO actions(action_id,kind,state,input_manifest_hash) VALUES(?,?,?,?)", (action_id, kind, "planned", input_manifest_hash))
            attempt_no = db.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE action_id=?", (action_id,)).fetchone()[0]
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?)", (attempt_id, action_id, attempt_no, "running", None, None))
            db.execute("INSERT INTO budget_reservations VALUES(?,?,?,?,?)", (attempt_id, pool, 1, "reserved", None))
            db.execute("UPDATE actions SET state='running' WHERE action_id=?", (action_id,))
            db.execute("INSERT INTO events(state_version,kind,payload_json,created_at) VALUES((SELECT state_version FROM runs),'attempt_started',?,?)", (json.dumps({"attempt_id": attempt_id}), stamp(now())))
            if self.action_scope:
                db.execute("INSERT INTO events(state_version,kind,payload_json,created_at) VALUES((SELECT state_version FROM runs),'action_budget_call',?,?)", (json.dumps({'scope':self.action_scope[0],'attempt_id':attempt_id}), stamp(now())))
        return attempt_id

    def settle(self, attempt_id: str, usage: Usage, *, sent: bool = True, error: str | None = None) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT action_id FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("unknown attempt")
            status = "settled" if error is None else ("unknown" if sent else "released")
            db.execute("UPDATE attempts SET state=?,usage_json=?,error=? WHERE attempt_id=?", ("result_saved" if error is None else "failed", usage.model_dump_json(), error, attempt_id))
            db.execute("UPDATE budget_reservations SET status=?,settled_usage_json=? WHERE attempt_id=?", (status, usage.model_dump_json(), attempt_id))
            db.execute("UPDATE actions SET state=? WHERE action_id=?", ("result_saved" if error is None else "failed", row["action_id"]))

    def snapshot(self) -> dict[str, object]:
        counts = {row["pool"]: row["count"] for row in self.store.db.execute("SELECT pool,COUNT(*) AS count FROM budget_reservations WHERE status!='released' GROUP BY pool")}
        return {"limits": self.limits, "used": counts, "remaining": {pool: limit - counts.get(pool, 0) for pool, limit in self.limits.items()}}
