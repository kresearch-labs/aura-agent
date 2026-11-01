"""stream_guard.py — stop a reply that is thinking in circles, while it streams.

What the log measured: a model kept guessing inside ONE reply — 21,582 guesses
in 2.7 MB, 9.2M prompt tokens in 30 min; another repeated a template ("I think
it might be *X*? No. OK — writing.") with X changing every pass. Checks between
replies never saw it (the reply never ended), and a silence timer never fires
(the stream is busy). Guards for one loop shape at a time kept missing the next
shape, so this watches the one thing every shape has: nothing new is being
said. Each sentence's word set is compared with what this reply already said;
when most recent sentences are near-copies, the reply is stopped. Distinct
reasoning never trips it, however long.

Watches the THINKING stream only: an answer may rightly repeat a shape (table
rows, similar clauses, code lines). Thinking that a server sends inside the
answer as <think>…</think> is moved to the thinking stream first (ThinkSplit).
Any script: words are letters of any language; Chinese, Japanese and Korean (no
spaces) count as character pairs. Generic: no model-specific code.
"""
from __future__ import annotations

import re
from collections import deque

WINDOW = 60          # Recent sentences looked at
SHARE = 0.8          # This share of them near-copies → going in circles
SIMILAR = 0.7        # Word-set overlap (Jaccard) that counts as a copy:
                     # a 7-word template with one slot changed is 0.75
MAX_KEPT = 200       # Earlier sentences compared against (loops repeat far sooner)
_SPLIT = re.compile(r"(?<=[.!?…。！？])\s*|\n+")
_WORD = re.compile(r"\w+", re.UNICODE)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")


def tokens(s: str) -> frozenset:
    """Words of any script; runs of CJK characters as character pairs."""
    out = set()
    for w in _WORD.findall(s.lower()):
        if _CJK.search(w):
            chars = [c for c in w if not c.isspace()]
            out.update(a + b for a, b in zip(chars, chars[1:]))
            if len(chars) == 1:
                out.add(chars[0])
        else:
            out.add(w)
    return frozenset(out)


class ProgressWatch:
    def __init__(self, share: float = SHARE) -> None:
        self.share = share
        self.buf = ""
        self.seen: deque[frozenset] = deque(maxlen=MAX_KEPT)
        self.exact: dict[frozenset, int] = {}
        self.recent: deque[bool] = deque(maxlen=WINDOW)
        self.sentences = 0
        self.fired = False

    def feed(self, text: str) -> bool:
        """Add streamed text; True once the reply is going in circles."""
        if self.fired or not text:
            return self.fired
        # Backstop for loops with no sentences at all, in any script
        self.tail = (getattr(self, "tail", "") + text)[-1500:]
        self.since = getattr(self, "since", 0) + len(text)
        if self.since >= 200:
            self.since = 0
            if _tail_repeat(self.tail):
                self.fired = True
                return True
        self.buf += text
        parts = _SPLIT.split(self.buf)
        self.buf = parts.pop()                # The unfinished sentence
        if len(self.buf) > 400:               # No punctuation at all ("hmm hmm …")
            parts.append(self.buf[:400])
            self.buf = self.buf[400:]
        for p in parts:
            if p:
                self._sentence(p)
        return self.fired

    def _sentence(self, s: str) -> None:
        words = tokens(s)
        if len(words) < 3:                    # "No." "Wait." "OK — writing." say nothing either way
            if not s.strip() or len(s) < 300:
                return
            words = frozenset([s.strip()[:80]])   # A run with no words (or "hmm hmm …")
        self.sentences += 1
        copy = words in self.exact or any(_jaccard(words, w) >= SIMILAR for w in self.seen)
        self.recent.append(copy)
        self.seen.append(words)
        self.exact[words] = self.exact.get(words, 0) + 1
        if len(self.recent) == WINDOW and sum(self.recent) >= self.share * WINDOW:
            self.fired = True


def _tail_repeat(tail: str) -> bool:
    """A block of 8-120 characters repeated 4+ times back to back at the
    end. Blocks with fewer than 4 different characters don't count — a
    divider line ("--------", "|---|---|") is not a loop."""
    t = tail.rstrip()
    for n in range(8, 121):
        if 4 * n > len(t):
            break
        block = t[-n:]
        if t[-4 * n:] == block * 4 and len(set(block.strip())) >= 4:
            return True
    return False


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_OPEN, _CLOSE = "<think>", "</think>"


class ThinkSplit:
    """Servers without a reasoning parser send thinking inside the answer:
    "<think>…</think>answer". feed() returns [(kind, text)] with kind
    "reasoning" or "content", as the text streams (a tag split across chunks
    is held back until it is whole). finish() flushes the rest. A reply whose
    opening tag was in the prompt (only "</think>" arrives) is fixed by
    split_closed() at the end."""

    def __init__(self, assume_open: bool = False) -> None:
        # assume_open: this model was seen sending a bare "</think>" (GLM-4.7
        # and Qwen often leave the opening tag out — vLLM forum, 2026), so
        # its replies start as thinking and are watched while they stream
        self.mode = "think" if assume_open else "start"
        self.assumed = assume_open
        self.closed = False
        self.buf = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.buf += text or ""
        while self.buf:
            if self.mode == "start":
                s = self.buf.lstrip()
                if s.startswith(_OPEN):
                    self.mode, self.buf = "think", s[len(_OPEN):]
                    continue
                if len(s) < len(_OPEN) and _OPEN.startswith(s):
                    break                         # Could still become "<think>"
                self.mode = "answer"
                continue
            if self.mode == "think":
                i = self.buf.find(_CLOSE)
                if i >= 0:
                    if i:
                        out.append(("reasoning", self.buf[:i]))
                    self.mode, self.buf = "answer", self.buf[i + len(_CLOSE):].lstrip("\n")
                    self.closed = True
                    continue
                keep = len(_CLOSE) - 1            # A "</thi" at the end may continue
                if len(self.buf) > keep:
                    out.append(("reasoning", self.buf[:-keep]))
                    self.buf = self.buf[-keep:]
                break
            out.append(("content", self.buf))
            self.buf = ""
        return out

    def finish(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        if not rest:
            return []
        return [("reasoning" if self.mode == "think" else "content", rest)]


def split_closed(content: str) -> tuple[str, str]:
    """(thinking, answer) for an answer that has "</think>" with no opening
    tag (the template put "<think>" in the prompt). Unchanged otherwise."""
    if _CLOSE in content and _OPEN not in content:
        head, _, tail = content.partition(_CLOSE)
        return head, tail.lstrip("\n")
    return "", content
