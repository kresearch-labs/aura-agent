"""governor.py — context governor: compact-&-continue + token ledger.

  - 75% of ctx window = compact threshold (provider profile context_length)
  - Compact-&-Continue: summarize oldest turns PRESERVING VERBATIM the
    identifiers work depends on (paths, URLs, error lines, test ids,
    emails, host:port — VERBATIM_PATTERNS below)
  - compacted blocks are tagged untrusted-suspect (CaMeL-lite marking)
  - structured notes (journal) injected into every prompt — note-taking

The governor NEVER invents content: summarization is delegated to the LLM by
the caller (loop); this module computes budgets, selects what to keep verbatim,
and validates the compacted block against those rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from ..eventlog import log_swallowed
from ..notes import is_users_words, users_words

# What must survive compaction word-for-word. The old list kept security data
# (CVEs, MAC addresses, FINDING ids, scope refs) and a "cred-like k:v" pattern
# that carried credential-looking pairs INTO the summary — while file paths,
# tracebacks, test ids and error lines were summarized away. Now: general
# identifiers + what coding work needs.
_CODE_EXT = (r"py|pyi|js|jsx|ts|tsx|go|rs|java|kt|c|cc|cpp|h|hpp|rb|php|cs|"
             r"swift|scala|sh|toml|ya?ml|json|cfg|ini|md|rst|txt|sql|html|css")
VERBATIM_PATTERNS = [
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),                 # Emails
    re.compile(r"\bhttps?://[^\s\"'<>]+"),                      # URLs
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"),        # IPv4[:port]
    re.compile(r'File "[^"]+", line \d+'),                      # Traceback frames
    re.compile(r"\b[\w./-]+\.py::[\w:\[\].-]+"),                # Pytest node ids
    re.compile(r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.(?:" + _CODE_EXT + r")\b"),  # Paths
    re.compile(r"(?<![\w/.-])[\w-]+\.(?:" + _CODE_EXT + r")\b"),  # Bare filenames
    re.compile(r"\b[A-Z]\w*(?:Error|Exception):[^\n]{1,120}"),  # Error lines
]
VERBATIM_CAP = 120    # A file-heavy conversation must not balloon the summary
COMPACT_TAG = "[COMPACTED — untrusted-suspect; verify before reuse]"
PRUNE_TAG = "[PRUNED — tool output archived; artifact ref below if present]"
# (prune tier now lives per-instance: Governor(prune_pct=...))


def extract_verbatim(text: str) -> list[str]:
    """All verbatim-must-survive tokens found in a message."""
    out = []
    for pat in VERBATIM_PATTERNS:
        out.extend(m.rstrip(",.;:") for m in pat.findall(text))
    return [t for t in out if t]


# The compaction cliff (hallucination research: 53% of rules survive one
# compaction, 10% survive five; type-aware triage that keeps CONSTRAINTS word
# for word reaches 96%). Sentences in the USER's messages that read as standing
# rules — handed to helpers word for word (delegate.py).
_RULE_MARKERS = re.compile(
    r"\b(always|never|don'?t|do not|must(?:n'?t)?|should(?:n'?t)?|only|"
    r"avoid|make sure|from now on|remember|no more|instead|prefer|"
    r"keep using|stop using)\b|,\s*not\s+\w", re.IGNORECASE)
RULES_CAP = 12


def standing_rules(messages: list[dict]) -> list[str]:
    """Rule-like sentences from user messages, oldest first, deduped."""
    out: list[str] = []
    for m in messages:
        if not is_users_words(m):          # Aura's notes are not rules
            continue
        for s in re.split(r"(?<=[.!?])\s+|\n+", users_words(m)):
            s = s.strip()
            if 8 <= len(s) <= 300 and _RULE_MARKERS.search(s):
                out.append(s)
    return list(dict.fromkeys(out))[-RULES_CAP:]


# Compaction never summarizes the user's own messages — they are carried word
# for word; only the agent's working steps and tool outputs shrink. Aura's
# notes to the model ride as user messages but are not the user's words
# (notes.py tells them apart by who wrote them, not by a "[TAG]" the user could
# type too); [STEER] is.
WORDS_TAG = "[YOUR MESSAGES — word for word, never summarized]"
WORDS_CAP_CHARS = 80_000


def _is_summary(m: dict) -> bool:
    """An earlier compaction's block (a system message Aura wrote)."""
    c = str(m.get("content") or "")
    return m.get("role") == "system" and (c.startswith(WORDS_TAG) or COMPACT_TAG in c)


def _carried(summaries: list[dict]) -> tuple[list[str], list[str]]:
    """Its word-for-word list of the user's messages and its turn lines carry into
    the new one instead.
    """
    words, lines = [], []
    for m in summaries:
        part = "words"
        for ln in str(m.get("content") or "").splitlines():
            if ln.startswith(WORDS_TAG) or not ln.strip() or ln.startswith("("):
                continue
            if ln.startswith(COMPACT_TAG) or ln.startswith("[TASK STATE"):
                part = "lines"
                continue
            if part == "words" and ln.startswith("- "):
                words.append(ln[2:])
            elif part == "lines":
                lines.append(ln)
    return words, lines[-40:]


def _words_block(messages: list[dict], carried: list[str] | None = None) -> str:
    words = list(carried or []) + [users_words(m) for m in messages if is_users_words(m)]
    if not words:
        return ""
    kept, size = [], 0
    for w in reversed(words):
        if size + len(w) > WORDS_CAP_CHARS and kept:
            break
        kept.append(w)
        size += len(w)
    kept.reverse()
    older = len(words) - len(kept)
    head = WORDS_TAG + (f"\n({older} older messages of yours are not repeated here — "
                        "history_search finds them word for word)" if older else "")
    return head + "\n" + "\n".join(f"- {w}" for w in kept) + "\n"


@dataclass
class CompactionResult:
    kept: list[dict]        # Recent messages kept verbatim
    summary_msg: dict       # The compacted system-side block
    dropped: int            # Count of summarized-away messages
    verbatim_lines: list[str] = field(default_factory=list)


class Governor:
    def __init__(self, context_length: int, threshold: float = 0.60,
                 keep_recent: int = 10, prune_pct: float = 0.40):
        self.ctx = context_length
        self.threshold = threshold
        self.prune_pct = prune_pct
        self.keep_recent = keep_recent
        self.token_ledger = 0  # Cumulative session tokens (HUD)
        self.measured = 0      # Real usage tokens seen (0 = estimates only)

    # ------------------------------------------------------------------ audit
    # ---------------------------------------------- calibrated counting
    # chars/4 undercounts code (closer to ~3), ignored the tool schemas sent
    # with every call, and left no room for the reply — so compaction could
    # fire after the window was already full. Now: start at a safer 3.5
    # chars/token, then learn the real ratio from the provider's own
    # prompt_tokens after every call (no tokenizer dependency).
    _DEFAULT_CPT = 3.5

    @staticmethod
    def chars_of(m: dict) -> int:
        n = len(str(m.get("content", "") or ""))
        for tc in m.get("tool_calls") or []:
            n += len(str(tc.get("function", {})))
        return n

    def _cpt(self) -> float:
        return getattr(self, "chars_per_token", self._DEFAULT_CPT)

    def count(self, messages: list[dict]) -> int:
        """Tokens the next call will send: messages + tool schemas."""
        chars = sum(self.chars_of(m) for m in messages)
        chars += getattr(self, "schema_chars", 0)
        return max(1, int(chars / self._cpt()))

    def est_tokens_msg(self, m: dict) -> int:
        """Token estimate for ONE message (content + tool_calls)."""
        return max(1, int(self.chars_of(m) / self._cpt()))

    def calibrate(self, messages_sent: list[dict], prompt_tokens: int) -> None:
        """Learn chars-per-token from a real call. The first measurement
        replaces the default; later ones are smoothed. Clamped so one odd
        response (cached prefix, provider quirk) can't wreck the estimate."""
        if not prompt_tokens or prompt_tokens <= 0:
            return
        chars = sum(self.chars_of(m) for m in messages_sent) \
            + getattr(self, "schema_chars", 0)
        if chars <= 0:
            return
        raw = chars / prompt_tokens
        # no tokenizer packs more than ~6 chars into a token: a reading
        # above that is not a full count (servers that report only the
        # new, uncached part of the prompt) — learning from it would shrink
        # every later estimate, so it is ignored instead of clamped
        if raw > 6.0:
            return
        obs = max(1.5, raw)
        if getattr(self, "_calibrated", False):
            self.chars_per_token = 0.5 * self._cpt() + 0.5 * obs
        else:
            self.chars_per_token = obs
            self._calibrated = True

    def _usable(self) -> int:
        """Context the prompt may use: the window minus room for the reply."""
        return max(1, self.ctx - getattr(self, "reply_reserve", 0))

    def record_usage(self, usage: dict) -> None:
        """Wire real SSE usage (prompt_tokens/completion_tokens) into ledger."""
        if not usage:
            return
        pt = usage.get("prompt_tokens") or 0
        ct = usage.get("completion_tokens") or 0
        if pt or ct:
            self.measured += pt + ct
        self.token_ledger += pt + ct

    def trim_trigger(self) -> int:
        """Tokens at which old outputs start being trimmed — the prune % of the
        window, but never above a fixed working budget, so a 1M window doesn't
        send ~500k tokens per call first.
        """
        from .workset import WORK_BUDGET
        return max(1, min(int(self._usable() * self.prune_pct), WORK_BUDGET))

    def needs_prune(self, messages: list[dict]) -> bool:
        return self.count(messages) > self.trim_trigger()

    def needs_compaction(self, messages: list[dict]) -> bool:
        return self.count(messages) > int(self._usable() * self.threshold)

    # ------------------------------------------------- compaction
    def compact(self, messages: list[dict], focus: str = "",
                state: str = "") -> CompactionResult:
        """Split messages: keep system + recent N verbatim; summarize middle.
        focus (from /compact [focus]) steers what the operator wants kept."""
        if not messages:
            return CompactionResult([], {"role": "system", "content": ""}, 0)
        old = [m for m in messages if _is_summary(m)]
        system = [m for m in messages if m["role"] == "system" and not _is_summary(m)]
        rest = [m for m in messages if m["role"] != "system"]
        cut = self._safe_cut(rest, max(0, len(rest) - self.keep_recent))
        to_summarize, keep = rest[:cut], rest[cut:]
        old_words, old_lines = _carried(old)

        verbatim: list[str] = []
        for m in to_summarize:
            verbatim.extend(extract_verbatim(str(m.get("content", ""))))
        verbatim = list(dict.fromkeys(verbatim))[:VERBATIM_CAP]  # Dedupe, cap

        lines = ([f"EARLIER: {ln.strip()[:160]}" for ln in old_lines]
                 + [f"- turn {i + 1}: {self._one_line(m)}"
                    for i, m in enumerate(to_summarize) if not is_users_words(m)])
        if focus:
            lines.insert(0, f"PRESERVE FOCUS: {focus}")
        if verbatim:
            lines.append("VERBATIM (preserve exactly):")
            lines.extend(f"  {v}" for v in verbatim)
        # Reorder by recall priority (scope → approvals → artifacts → findings
        # → current check); position = recall law
        lines = self._order_recall_first("\n".join(lines)).splitlines()
        summary = {
            "role": "system",
            "content": _words_block(to_summarize, old_words) + (state + "\n" if state else "")
                       + COMPACT_TAG + "\n" + "\n".join(lines),
        }
        return CompactionResult(kept=system + keep, summary_msg=summary,
                                dropped=len(to_summarize),
                                verbatim_lines=verbatim)

    # -------------------------------------------------------- LLM compact
    # Recall-first ordering: within the summary body, scope statement and
    # approvals FIRST, current check LAST; findings and artifacts between.
    # Position matters (Lost in the Middle, TACL 2024).
    _RECALL_ORDER = ("scope", "approval", "artifact", "finding", "current")

    @staticmethod
    def _order_recall_first(body: str) -> str:
        """Reorder summary lines by recall priority (stable for unknown
        lines, which keep position at the end, before current-check lines)."""
        def rank(ln: str) -> int:
            low = ln.lower()
            for i, key in enumerate(Governor._RECALL_ORDER):
                if key in low:
                    return i
            return len(Governor._RECALL_ORDER)  # Unknown → before current
        known = [ln for ln in body.splitlines() if ln.strip()]
        if len(known) <= 2:
            return body
        ranked = sorted(known, key=rank)  # Stable sort keeps same-rank order
        return "\n".join(ranked)

    def compact_with_summary(self, messages: list[dict], summary_text: str,
                             focus: str = "", state: str = "") -> CompactionResult:
        """LLM-driven compaction: the CALLER gets an LLM structured summary; this
        method validates it keeps the verbatim tokens and builds the
        CompactionResult. Falls back to heuristic lines when summary_text is
        empty. The N most recent messages always survive.
        """
        if not messages:
            return CompactionResult([], {"role": "system", "content": ""}, 0)
        old = [m for m in messages if _is_summary(m)]
        system = [m for m in messages if m["role"] == "system" and not _is_summary(m)]
        rest = [m for m in messages if m["role"] != "system"]
        cut = self._safe_cut(rest, max(0, len(rest) - self.keep_recent))
        to_summarize, keep = rest[:cut], rest[cut:]
        old_words, old_lines = _carried(old)
        verbatim = list(dict.fromkeys(
            v for m in to_summarize
            for v in extract_verbatim(str(m.get("content", "")))))[:VERBATIM_CAP]
        header = COMPACT_TAG
        if focus:
            header += f"\nPRESERVE FOCUS: {focus}"
        body = summary_text.strip()
        if old_lines:
            body = "\n".join(f"EARLIER: {ln.strip()[:160]}" for ln in old_lines) + "\n" + body
        if not body.strip():
            body = "\n".join(f"- turn {i + 1}: {self._one_line(m)}"
                             for i, m in enumerate(to_summarize)
                             if not is_users_words(m))
        missing = [v for v in verbatim if v not in body]
        if missing:
            body += "\nVERBATIM (preserve exactly): " + " | ".join(missing)
        # Reorder by recall priority
        body = self._order_recall_first(body)
        summary = {"role": "system",
                   "content": _words_block(to_summarize, old_words) + (state + "\n" if state else "")
                   + header + "\n" + body}
        return CompactionResult(kept=system + keep, summary_msg=summary,
                                dropped=len(to_summarize),
                                verbatim_lines=verbatim)

    # ------------------------------------------------------------------- prune
    def prune(self, messages: list[dict]) -> tuple[list[dict], int]:
        """Tier 1: replace OLD tool messages with short stubs. Tool outputs are
        the highest-volume, lowest-lasting-value content. Verbatim tokens
        inside pruned outputs survive in the stub. Recent keep_recent
        non-system messages are never pruned. Returns (new_messages,
        pruned_count).
        """
        if not messages:
            return messages, 0
        non_system_idx = [i for i, m in enumerate(messages)
                          if m["role"] != "system"]
        if len(non_system_idx) <= self.keep_recent:
            return messages, 0
        cut = len(non_system_idx) - self.keep_recent
        old_idx = set(non_system_idx[:cut])
        out, pruned = [], 0
        for i, m in enumerate(messages):
            if i in old_idx and m["role"] == "tool":
                body = str(m.get("content", ""))
                if not body or body.startswith(PRUNE_TAG):
                    out.append(m)          # Already a stub (or empty)
                    continue
                verbatim = list(dict.fromkeys(extract_verbatim(body)))
                ref = ""
                try:                        # Artifact ref when the store made one
                    meta = m.get("meta") or {}
                    if meta.get("artifact_ref"):
                        ref = f" artifact {meta['artifact_ref']}"
                except Exception as _swallowed:
                    log_swallowed("governor.artifact_ref", _swallowed)
                stub = (f"{PRUNE_TAG}\npruned({len(body)} chars):{ref}"
                        + (("\nkept verbatim: " + " | ".join(verbatim[:12]))
                           if verbatim else ""))
                out.append({**m, "content": stub})
                pruned += 1
            else:
                out.append(m)
        return out, pruned

    @staticmethod
    def _safe_cut(rest: list[dict], cut: int) -> int:
        """Never start the kept tail on a tool result: its parent assistant
        message (with tool_calls) would be summarized away, and OpenAI-style
        APIs reject orphan tool messages. Move the cut back to include it."""
        while 0 < cut < len(rest) and rest[cut].get("role") == "tool":
            cut -= 1
        return cut

    @staticmethod
    def clip_message(m: dict, max_chars: int) -> dict:
        """Overflow recovery: keep head + tail of one oversized message."""
        body = m.get("content")
        if not isinstance(body, str) or len(body) <= max_chars:
            return m
        half = max_chars // 2
        clipped = dict(m)
        marker = (f"[... {len(body) - max_chars:,} chars clipped to fit the "
                  "context window ...]")
        clipped["content"] = "\n".join((body[:half], marker, body[-half:]))
        return clipped

    @staticmethod
    def as_messages(c: CompactionResult) -> list[dict]:
        if c.dropped == 0:
            return c.kept
        # The system prompt stays first: then the summary, then the rest
        n = 0
        while n < len(c.kept) and c.kept[n].get("role") == "system":
            n += 1
        return c.kept[:n] + [c.summary_msg] + c.kept[n:]

    @staticmethod
    def _one_line(m: dict) -> str:
        role = m.get("role", "?")
        content = str(m.get("content", ""))[:100].replace("\n", " ")
        tc = m.get("tool_calls") or []
        if tc:
            names = ",".join(t["function"]["name"] for t in tc)
            return f"[{role}] tool_calls({names})"
        return f"[{role}] {content[:100]}"

    # ------------------------------------------------------------------ notes
    @staticmethod
    def journal_injection(notes: str, last_n: int = 20) -> str:
        """Last-N lines of the agent journal, injected into each prompt."""
        if not notes.strip():
            return ""
        lines = notes.strip().splitlines()
        return "\n".join(lines[-last_n:])


# --------------------------------------------------------------- notes tool --
NOTES_FILE = "notes.md"
NOTES_HEADER = """# Agent Journal (structured notes)
<!-- sections: ## Findings | ## Leads | ## Questions | ## Decisions -->
<!-- injected into every prompt — write here what future-you must know -->
"""


def register_notes(mediator, fs_dispatch: dict, root) -> None:
    """Wire the `notes` tool (WRITE, engagement-scoped, plan-safe read)."""
    from pathlib import Path

    p = Path(root) / NOTES_FILE
    if not p.exists():
        p.write_text(NOTES_HEADER, encoding="utf-8")

    def notes_read(_args) -> str:
        return p.read_text(encoding="utf-8")

    def notes_write(args) -> str:
        section = (args.get("section") or "Questions").title()
        text = args["text"].rstrip()
        cur = p.read_text(encoding="utf-8")
        head = f"## {section}"
        if head in cur:
            # append under existing section header (before the next ## header
            # or EOF — never inside the HTML comment header block)
            idx = cur.index(head) + len(head)
            nxt = cur.find("\n## ", idx)
            insert_at = nxt if nxt != -1 else len(cur)
            cur = cur[:insert_at].rstrip() + f"\n- {text}\n" + cur[insert_at:]
        else:
            cur = cur.rstrip() + f"\n\n{head}\n- {text}\n"
        p.write_text(cur, encoding="utf-8")
        return f"noted [{section}]: {text[:80]}"

    mediator.register(
        __import__("aura_agent.mediator", fromlist=["ToolSpec"]).ToolSpec(
            "notes", __import__("aura_agent.mediator",
                                fromlist=["BlastRadius"]).BlastRadius.READ,
            lambda a: None), plan_safe=True)
    fs_dispatch["notes"] = notes_read   # Read whole journal
    # write path piggybacks on mediator with its own spec
    mediator.register(
        __import__("aura_agent.mediator", fromlist=["ToolSpec"]).ToolSpec(
            "notes_write", __import__("aura_agent.mediator",
                                      fromlist=["BlastRadius"]).BlastRadius.WRITE,
            lambda a: None))
    fs_dispatch["notes_write"] = notes_write
