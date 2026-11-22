#!/usr/bin/env python3
"""Aura Teams chain verifier — stdlib only.

This file ships inside every log-export bundle as 10-verify.py; the
bundler reads ITS OWN SOURCE (see api_monitor._export_bundle), so what
you run is what the project runs.

Recipe (mirrors teamserver/log.py):
  canonical(row) = json.dumps({"p","s","i","t","sub","a","time","d"},
                              sort_keys=True, separators=(",",":"),
                              ensure_ascii=False) with time rounded (3)
  hash_i = sha256(prev_hash + chr(10) + canonical(row_i)).hexdigest()
  row 1's prev is the genesis ("0" * 64); every later prev is the row
  before it's hash.
Read 20-log.jsonl top-to-bottom; print OK or BROKEN (+ first bad seq).
Exit 0 when the chain holds, 1 when it doesn't.
"""
import hashlib
import json
import sys

GENESIS = "0" * 64


def canon(p, s, i, t, sub, a, time, d):
    return json.dumps({"p": p, "s": s, "i": i, "t": t, "sub": sub,
                       "a": a, "time": round(float(time or 0), 3), "d": d},
                      sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def main(path="20-log.jsonl"):
    prev = GENESIS
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            n += 1
            body = canon(r["project"], r["source"], r["ev_id"], r["type"],
                         r["subject"], r["actor"], r["time"],
                         json.loads(r["data"]) if isinstance(r["data"], str)
                         else r["data"])
            want = hashlib.sha256(
                (r["prev_hash"] + chr(10) + body).encode()).hexdigest()
            if r["prev_hash"] != prev or want != r["hash"]:
                print(f"BROKEN at seq {r['seq']}")
                return 1
            prev = r["hash"]
    if n == 0:
        print("EMPTY — no rows in the file")
        return 1
    print(f"OK — {n} events, head {prev[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
