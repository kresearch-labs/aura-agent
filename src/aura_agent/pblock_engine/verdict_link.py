"""verdict_link.py — spec v3 item 3 (+ items 4-5 write side).

Reads per-instance outcomes (harness/receipt side) and joins them to
diary entries by the stamp (instance_id + attempt). Produces
(framing -> outcome) pairs and applies the learning:
  - item 4: confidence updates (bounded steps) — GUARD D: only cards
    with >=1 attributed outcome change; GUARD C: mark decay
  - item 5: failure marks + supersession — GUARD B evidence bar

Scope: panel only. Reads receipts; never writes them.
"""
from __future__ import annotations

import numpy as np

CONF_STEP = 0.1          # Bounded step per attributed outcome
CONF_MAX, CONF_MIN = 1.0, 0.3
DECAY_PER = 5            # GUARD C: -1 mark per N attributed successes
SUPERSEDE_SIM = 0.85     # GUARD B territory bar (= CANDIDATE_SIM)
SUPERSEDE_N = 2          # Distinct-instance verified failures to supersede


def find_case(store, fid: str):
    """fid = case id or id prefix (the diary stores [id] prefixes)."""
    for c in store.cases:
        if c["id"] == fid or c["id"].startswith(fid):
            return c
    return None


def join(diary, outcomes: dict) -> list:
    """outcomes: instance_id -> ok(bool). Last-writer-wins per instance.
    Returns [{framing_id, instance_id, ok}]."""
    by_instance = {}
    for e in diary.entries():
        by_instance[e["instance_id"]] = e
    joined = []
    for iid, e in by_instance.items():
        ok = outcomes.get(iid)
        if ok is None:
            continue
        for fid in e["framing_ids"]:
            joined.append({"framing_id": fid, "instance_id": iid,
                           "ok": bool(ok)})
    return joined


def tally(joined: list) -> dict:
    """Aggregate attributed outcomes per framing id."""
    t: dict = {}
    for j in joined:
        d = t.setdefault(j["framing_id"], {"ok": 0, "fail": 0,
                                           "fail_instances": set(),
                                           "ok_instances": set()})
        if j["ok"]:
            d["ok"] += 1
            d["ok_instances"].add(j["instance_id"])
        else:
            d["fail"] += 1
            d["fail_instances"].add(j["instance_id"])
    return t


def apply_weights(store, tallies: dict) -> list:
    """Item 4. GUARD D: only cards with >=1 attributed outcome change;
    CONF_MIN floor (0.3) keeps a weighted card at/above an unverified
    card's starting confidence, so weighting never evicts young truth.
    GUARD C: every DECAY_PER attributed ok removes one failure mark.
    Returns modified case ids."""
    changed = []
    for fid, t in tallies.items():
        c = find_case(store, fid)
        if c is None:
            continue
        healed = t["ok"] // DECAY_PER
        if healed and c.get("failure_marks", 0) > 0:
            c["failure_marks"] = max(0, c.get("failure_marks", 0) - healed)
        if t["ok"]:
            c["confidence"] = min(CONF_MAX,
                                  c["confidence"] + CONF_STEP * t["ok"])
        if t["fail"]:
            c["confidence"] = max(CONF_MIN,
                                  c["confidence"] - CONF_STEP * t["fail"])
        c["attributed_ok"] = c.get("attributed_ok", 0) + t["ok"]
        c["attributed_fail"] = c.get("attributed_fail", 0) + t["fail"]
        changed.append(c["id"])
    if changed:
        store.save()
    return changed


def apply_scars(store, tallies: dict, failed_situation_emb: dict) -> list:
    """Item 5 with GUARD B.
    failed_situation_emb: instance_id -> embedding of the failed task.
    New-instance verified failure -> failure_marks += 1 (same-instance
    retries count once). Supersede when >= SUPERSEDE_N DISTINCT
    instances failed with failed-situation similarity >= SUPERSEDE_SIM
    to the card — only then does the failure-trap framing replace the
    old advice via the store's own supersede() (bi-temporal).
    Returns action dicts."""
    actions = []
    dirty = False
    for fid, t in tallies.items():
        c = find_case(store, fid)
        if c is None or not t["fail"]:
            continue
        marked = set(c.get("marked_instances", []))
        new_iids = sorted(t["fail_instances"] - marked)
        for iid in new_iids:
            emb = failed_situation_emb.get(iid)
            c.setdefault("marked_instances", []).append(iid)
            c["failure_marks"] = c.get("failure_marks", 0) + 1
            sim_ok = False
            if emb is not None and c.get("embedding"):
                sim = float(np.asarray(emb, dtype=np.float32) @
                            np.asarray(c["embedding"], dtype=np.float32))
                sim_ok = sim >= SUPERSEDE_SIM
            c.setdefault("strong_fail_instances", []).append(
                {"instance_id": iid, "sim_ok": sim_ok})
            dirty = True
        distinct_strong = {f["instance_id"]
                           for f in c.get("strong_fail_instances", [])
                           if f["sim_ok"]}
        if len(distinct_strong) >= SUPERSEDE_N and c["status"] == "active":
            from .schemas import new_case
            nc = new_case(c["persona"], c["situation"],
                          f"failure trap: the pattern in [{c['id'][:8]}] "
                          f"did not resolve on similar situations "
                          f"({len(distinct_strong)} instances)",
                          "verified-failure", confirmed=False,
                          origin="lived",
                          task_summary=c.get("task_summary", ""))
            nc["embedding"] = c.get("embedding")
            store.cases.append(nc)
            store.supersede(c["id"], nc["id"])
            dirty = True
            actions.append({"superseded": c["id"], "trap": nc["id"],
                            "instances": sorted(distinct_strong)})
        if new_iids:
            actions.append({"marked": c["id"], "instances": new_iids})
    if dirty:
        store.save()
    return actions
