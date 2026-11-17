"""Shape A — deterministic workflow.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Fixed Python steps. Code owns every decision about money:

1. `get_claim_view` (allowlisted columns, never SELECT *). Unknown claim -> `failed`.
2. Missing order_id -> `needs_info`. Never parse an order id out of the ticket.
3. `lookup_order` scoped to the claim's principal. Not visible -> `escalated`.
4. `compute_compensation` from the order row + current policy. The ticket text
   (delay numbers, "ignore policy", ...) is user data and is never read here.
5. Quote 0 -> `denied`, no issue_refund call.
6. Quote > 0 -> `durability.issue_refund(conn, claim_id, FULL quote, crash_at=...)`.
   The host checks approval, per-order cap and idempotency. Host refusal ->
   `denied` (host writes `policy_denied`), amount conflict -> `failed`.
   `proposal_amount` is always the full quote.
7. Success -> `resolved`.

MockLLM is called once, only to draft reply text. Its output never feeds a
decision. InjectedCrash is not swallowed: the trace is flushed, then re-raised.

Trace safety: only allowlisted fields are traced (order view from
`lookup_order`, quote payload, host outcome reason). The ticket, the raw claim
row, principal ids and order rows from SELECT * are never written.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

SHAPE = "workflow"


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))

    def done(status: str, proposal: int, reason: str | None) -> RunResult:
        # Model only drafts text for the customer; the status is already decided.
        complete(budget, seed=seed, purpose="draft", prompt=f"status={status} proposal={proposal}")
        trace.add("decision", {"status": status, "proposal_amount": proposal, "reason": reason})
        return finish(
            conn,
            shape=SHAPE,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    # Step 1: claim through the allowlisted view.
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    trace.add("tool", {"name": "get_claim_view", "found": claim["found"]})
    if not claim["found"]:
        return done("failed", 0, "unknown_claim")

    # Step 2: order id must come from the structured column only.
    order_id = claim.get("order_id")
    trace.add("check", {"name": "has_order_id", "ok": bool(order_id)})
    if not order_id:
        return done("needs_info", 0, "missing_order_id")

    # Step 3: principal-scoped order lookup.
    budget.add_tool(tool_cost())
    order_view = lookup_order(conn, order_id, claim["principal_id"])
    trace.add("tool", _order_trace(order_view))
    if not order_view["visible"]:
        return done("escalated", 0, order_view["reason"])

    # Step 4: money from code, structured data and current policy only.
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order_view["order"])
    proposal = int(quote["amount_vnd"])
    trace.add(
        "quote",
        {"amount_vnd": proposal, "reason": quote["reason"], "clause_ids": list(quote["clause_ids"])},
    )

    # Step 5: nothing owed -> deny without touching the host write path.
    if proposal <= 0:
        return done("denied", 0, quote["reason"])

    # Step 6: full quote to the host gate. Crash propagates after flushing the trace.
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    trace.add(
        "issue_refund",
        {"ok": outcome["ok"], "reason": outcome["reason"], "conflict": outcome["conflict"]},
    )

    if not outcome["ok"]:
        return done("failed" if outcome["conflict"] else "denied", proposal, outcome["reason"])

    # Step 7.
    return done("resolved", proposal, None)


def _order_trace(view: dict[str, Any]) -> dict[str, Any]:
    """lookup_order already allowlists fields; keep only what the decision used."""
    event: dict[str, Any] = {"name": "lookup_order", "visible": view["visible"], "reason": view["reason"]}
    if view["visible"]:
        order = view["order"]
        event["order"] = {
            key: order[key]
            for key in ("order_id", "status", "delay_days", "shipping_fee_vnd", "paid_vnd", "damage_verified")
        }
    return event
