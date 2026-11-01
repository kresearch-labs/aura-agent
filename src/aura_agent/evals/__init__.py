"""evals — 2.1: a small scored task suite, so "better" can be measured.

Provider-agnostic: `aura eval run` uses whatever profile is configured
(any OpenAI-compatible endpoint). `aura eval check` validates the suite
itself with no model at all: every checker must FAIL on the untouched
files and PASS on the task's reference solution.
"""
