"""research.py — when to stop researching.

           PubMed, Stack Overflow, GitHub); stops like normal.
  normal — research as the best agents do; Aura watches *novelty* (the
           share of a search's results not seen before, the strongest
           signal in RAAC, 2026) and, when the last searches bring almost
           nothing new — saturation, as systematic reviews call it — or
           after a set number of searches, tells the model once: write now.
  deep — `/research on`: a plan first (the questions go into the todo
           list, the user confirms), every question needs two independent
           checked sources, stale searches get a "re-think" note naming
           what is still uncovered, and it stops only when every question
           is covered AND searches have saturated, or at the budget (60
           searches by default) — never endless.

The receipt records the counts, the coverage and why research stopped.
"""
from __future__ import annotations

import urllib.parse

SATURATION = 0.2          # A search with < 20 % new addresses adds little
WINDOW = 3                # ... three such searches in a row = saturated
# Normal mode: "write now" after this many searches.
NORMAL_NUDGE_AT = 3
MAX_RETHINKS = 2          # Deep mode: re-think notes before giving up on gaps
# The knee (e-discovery): searches in a row that led to no new page worth
# opening.
KNEE = 4

DEEP_INSTRUCTION = (
    "[DEEP RESEARCH MODE — the user switched it on] Work like a careful "
    "researcher:\n"
    "1. First write the questions this task needs answered as todo items "
    "(todo_write), then show them to the user with clarify and ask if "
    "anything should be added or dropped.\n"
    "2. For each question find at least two independent sources (different "
    "sites) with web_search and read them with web_crawl.\n"
    "3. Mark a question done only when its sources are read. Keep going "
    "while questions are open — change your searches when they stop "
    "finding new things.\n"
    "4. Then write the answer, citing only pages you read; say plainly what "
    "stayed uncertain.")


def _key(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    return (p.hostname or "").lower().removeprefix("www.") + p.path.rstrip("/")


def _domain(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")


class ResearchMonitor:
    def __init__(self, mode: str = "normal", budget: int = 60):
        self.level = mode if mode in ("basic", "normal", "deep") else "normal"
        self.mode = "deep" if mode == "deep" else "normal"   # How it stops
        self.budget = max(5, int(budget or 60))
        self.seen: set[str] = set()
        self.novelty: list[float] = []      # Per search
        self.read_domains: set[str] = set()
        self.opened: set[str] = set()
        self.reads = 0
        self.nudged = False
        self.rethinks = 0
        self.stopped = ""                   # Why research was told to stop
        self.since_new_read = 0             # Searches since a new page was opened

    # -- what the web tools report
    def saw_search(self, urls: list[str]) -> None:
        keys = [_key(u) for u in urls if u]
        if not keys:
            self.novelty.append(0.0)
            return
        new = [k for k in keys if k not in self.seen]
        self.novelty.append(len(new) / len(keys))
        self.seen.update(keys)
        self.since_new_read += 1

    def saw_read(self, url: str) -> None:
        self.reads += 1
        if _key(url) not in self.opened:
            self.opened.add(_key(url))
            self.since_new_read = 0
        d = _domain(url)
        if d:
            self.read_domains.add(d)
        self.seen.add(_key(url))

    # -- signals
    @property
    def searches(self) -> int:
        return len(self.novelty)

    def saturated(self) -> bool:
        """Nothing new is coming: the results repeat (novelty), or the
        last searches led to no new page worth opening (the knee)."""
        last = self.novelty[-WINDOW:]
        repeats = len(last) == WINDOW and all(n < SATURATION for n in last)
        return repeats or self.since_new_read >= KNEE

    def next_note(self, todos: list[dict] | None = None) -> str | None:
        """A note for the model before its next step, or None."""
        if not self.searches:
            return None
        if self.mode == "normal":
            if self.nudged:
                return None
            if self.saturated() or self.searches >= NORMAL_NUDGE_AT:
                self.nudged = True
                self.stopped = ("saturated: the last searches brought nothing new to read"
                                if self.saturated() else f"{self.searches} searches")
                return ("[RESEARCH] " + ("Your last searches brought almost nothing new"
                        if self.saturated() else f"You have run {self.searches} searches")
                        + " — you likely have what the web will give. Write the answer "
                        "now with the sources you read; say what stayed uncertain.")
            return None
        # deep
        open_q = [t.get("text", "") for t in (todos or [])
                  if t.get("status") in ("pending", "in_progress")]
        if self.searches >= self.budget and not self.stopped:
            self.stopped = f"budget of {self.budget} searches reached"
            return (f"[RESEARCH] The deep-research budget ({self.budget} searches) is "
                    "used up. Write the answer now from the sources you read; list "
                    "the questions that stayed open: " + ("; ".join(open_q[:6]) or "none"))
        if not self.saturated() or self.stopped:
            return None
        if open_q and self.rethinks < MAX_RETHINKS:
            self.rethinks += 1
            self.novelty.append(1.0)          # A fresh start for the window
            self.since_new_read = 0
            return ("[RESEARCH] Your searches stopped finding new things, but these "
                    "questions are still open: " + "; ".join(open_q[:6]) + ". Re-think: "
                    "different words, a different kind of source (papers, code, "
                    "official docs), or narrow to one question at a time.")
        self.stopped = ("every question covered and searches saturated" if not open_q
                        else "searches saturated after re-thinking; questions left open")
        return ("[RESEARCH] " + ("Every question is covered and new searches bring "
                "nothing new" if not open_q else "Searches keep returning what you have")
                + " — the scope is exhausted. Write the answer now, citing only pages "
                "you read" + (f"; say these stayed open: {'; '.join(open_q[:6])}"
                              if open_q else "") + ".")

    def summary(self, todos: list[dict] | None = None) -> dict:
        return {"mode": self.mode, "searches": self.searches, "pages_read": self.reads,
                "searches_since_last_new_page": self.since_new_read,
                "sites_read": len(self.read_domains),
                "new_per_search": [round(n, 2) for n in self.novelty[-20:]],
                "stopped": self.stopped or "the model finished",
                "questions": [{"q": t.get("text", "")[:120], "status": t.get("status")}
                              for t in (todos or [])][:30] if self.mode == "deep" else []}
