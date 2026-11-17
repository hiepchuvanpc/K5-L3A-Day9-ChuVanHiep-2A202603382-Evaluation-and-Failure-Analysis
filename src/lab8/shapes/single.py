"""Shape B — one ReAct agent. Student work.

Use `lab8.runtime.run_react` with max_steps=8. `decide` may pick a read tool.
It must not call `issue_refund`. After the loop, the host validates the quote
and is the only writer.

A decide() that trusts ticket text over the order row will fail hidden claims
where the customer overstates the delay or cites a superseded clause.

Per-order cap: an order may already have been refunded by an earlier claim.
The read tool `lookup_refund_history` shows how much. The host still enforces
the cap, so pass the full quote to `durability.issue_refund`, not a net amount.

Caps: stop at 8 steps. Count model calls in the Budget so cost stays comparable.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations

import json

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    budget = Budget()
    trace = Trace(make_run_id("single", claim_id, seed))
    # MockLLM returns seeded text, not structured tool actions. The bounded
    # controller below maps observations to read actions; it never grants writes.
    claim = {}

    def decide(observation):
        nonlocal claim
        last_tool = observation.get("last_tool")
        result = observation.get("last_result") or {}

        # Only explicit, safe facts enter the model prompt and trace. In
        # particular, get_claim_view's ticket must not be dumped wholesale.
        safe_observation = {"last_tool": last_tool}
        if last_tool == "get_claim_view":
            safe_observation.update(found=bool(result.get("found")),
                                    has_order=bool(result.get("order_id")))
        elif last_tool == "lookup_order":
            safe_observation["visible"] = bool(result.get("visible"))
        elif last_tool == "compute_compensation":
            safe_observation["amount_vnd"] = int(result["amount_vnd"])
        complete(budget, seed=seed, purpose="decide",
                 prompt=json.dumps(safe_observation, sort_keys=True))
        trace.add("observation", safe_observation)

        if last_tool is None:
            action = {"tool": "get_claim_view", "args": {"claim_id": claim_id}}
        elif last_tool == "get_claim_view":
            claim = result
            if not claim.get("found"):
                action = {"final": {"status": "failed", "error_reason": "unknown_claim"}}
            elif not claim.get("order_id"):
                action = {"final": {"status": "needs_info", "error_reason": "missing_order_id"}}
            else:
                action = {"tool": "lookup_order", "args": {
                    "order_id": claim["order_id"], "principal_id": claim["principal_id"]}}
        elif last_tool == "lookup_order":
            if not result.get("visible"):
                action = {"final": {"status": "escalated", "error_reason": result.get("reason")}}
            else:
                action = {"tool": "compute_compensation", "args": {"order": result["order"]}}
        elif last_tool == "compute_compensation":
            action = {"final": {"proposal_amount": int(result["amount_vnd"]),
                                "status": "denied", "error_reason": result.get("reason")}}
        else:
            action = {"final": {"status": "failed", "error_reason": "unexpected_observation"}}

        # Tool arguments are deliberately excluded (they can contain claim data).
        trace.add("action", {"tool": action["tool"]} if "tool" in action else action)
        return action

    try:
        loop = run_react(conn, budget, observation={}, decide=decide,
                         max_steps=8, worker=True)
        final = loop["final"]
        proposal = int(final.get("proposal_amount", 0))
        status = final["status"]
        reason = final.get("error_reason")
        if loop["stopped"] == "final" and proposal > 0:
            # Full entitlement, not the net amount: host owns approval and caps.
            outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
            status = "resolved" if outcome["ok"] else ("failed" if outcome["conflict"] else "denied")
            reason = None if outcome["ok"] else outcome["reason"]
        complete(budget, seed=seed, purpose="draft",
                 prompt=f"status={status};proposal={proposal}")
        trace.add("decision", {"status": status, "proposal_amount": proposal,
                               "reason": reason, "stopped": loop["stopped"]})
        return finish(conn, shape="single", claim_id=claim_id, seed=seed,
                      status=status, proposal_amount=proposal, budget=budget,
                      trace=trace, error_reason=reason)
    except InjectedCrash:
        # Flush diagnostics, then propagate so T12 observes the real crash.
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    finally:
        conn.close()
