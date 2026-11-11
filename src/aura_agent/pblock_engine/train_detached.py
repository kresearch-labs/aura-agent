#!/usr/bin/env python3
"""train_detached.py — run a pblock training survivably.

Launches train_pblock.py fully detached (setsid, nohup): session timeouts,
kernel restarts, or parent kills can NEVER take the training down again. Usage:
train_detached.py --template X.yml --out-dir DIR [--corpus PATH] Writes
train_<name>.log in the out-dir; prints the detached PID.
"""
import os, sys, subprocess, argparse

ap = argparse.ArgumentParser()
ap.add_argument("--template", required=True)
ap.add_argument("--out-dir", required=True)
ap.add_argument("--corpus", default=None)
a = ap.parse_args()

name = os.path.splitext(os.path.basename(a.template))[0]
log = os.path.join(a.out_dir, f"train_{name}.log")
os.makedirs(a.out_dir, exist_ok=True)

cmd = [sys.executable, "-m", "aura_agent.pblock_engine.train_pblock",      # The Python running this
       "--template", a.template, "--out-dir", a.out_dir]
if a.corpus:
    cmd += ["--corpus", a.corpus]

with open(log, "w") as lf:
    p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                         start_new_session=True)   # Setsid: orphan-safe
print(f"detached pid {p.pid} | log: {log}")
