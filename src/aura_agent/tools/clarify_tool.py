"""clarify_tool.py — ask the user mid-task.

What Aura does with that:
  - choices with a short description each, a recommended one, multi-
    select, up to 6, and the user's own words always allowed;
  - no timeout: a question waits for the user (nothing silently assumed);
  - nobody to ask (scheduled / piped runs): the model goes on with the
    recommended choice or its best judgment — and the assumption is
    RECORDED, shown when the run ends and kept in the receipt;
  - the same question twice returns the earlier answer; a cap per run;
  - a question asking for a password, key or token is refused (text
    planted in a web page can't phish through Aura); the answer comes only
    from the user's keyboard and is marked as their words.
"""
from __future__ import annotations

import re

from ..mediator import BlastRadius, Mediator, ToolSpec
from ..tool_names import CLARIFY

MAX_PER_RUN = 4
MAX_OPTIONS = 6
_SECRET = re.compile(r"\b(passwords?|passphrase|pass code|api[ _-]?keys?|"
                     r"(?:access|auth|bearer|api|github|personal access)[ _-]?tokens?|"
                     r"secret[ _-]?keys?|private[ _-]?keys?|credentials?|2fa code|otp|"
                     r"one[- ]time code|credit card|card number|cvv|ssn|social security)\b", re.I)


_WAIT = re.compile(r"\b(wait for (my|an?) (answer|reply|ok|go)|ask me (first|before)|"
                   r"check with me|don'?t (assume|guess)|ask me which)\b", re.I)


def _user_said_wait(mediator) -> bool:
    """The user's own words (any message in this chat) ask to be asked."""
    try:
        from ..notes import is_users_words, users_words
        s = getattr(mediator, "_history_session", None)
        return any(_WAIT.search(users_words(m)) for m in (getattr(s, "messages", None) or [])
                   if is_users_words(m))
    except Exception:                                # noqa: BLE001
        return False


def _rule_in_question(mediator, text: str) -> str | None:
    """The user's standing rule this question is about, if any. Only the user can
    set a rule aside.
    """
    try:
        from ..constraints import _read_rules
        root = getattr(mediator, "constraints_root", None)
        rules = _read_rules(root) if root is not None else []
    except Exception:                                # noqa: BLE001
        return None
    low = text.lower()
    said = set(re.findall(r"[a-z0-9_.-]{4,}", low))
    for r in rules:
        words = set(re.findall(r"[a-z0-9_.-]{4,}", r.lower()))
        if r.lower() in low or (words and len(words & said) / len(words) >= 0.6):
            return r
    return None


def _norm_q(q: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", q.lower()))


def _options(raw) -> list[tuple[str, str]]:
    out = []
    for o in raw or []:
        if isinstance(o, dict):
            label = str(o.get("label") or "").strip()
            desc = str(o.get("description") or "").strip()
        else:
            label, desc = str(o).strip(), ""
        if label:
            out.append((label[:80], desc[:160]))
    return out[:MAX_OPTIONS]


def register(mediator: Mediator, dispatch: dict) -> None:
    state = {"asked": {}, "count": 0}
    mediator._clarify_state = state                  # Reset per run by the loop
    mediator._assumptions = []

    def clarify(args) -> str:
        q = str(args.get("question") or "").strip()
        if not q:
            return "error: give the question to ask"
        if _SECRET.search(q):
            return ("refused: Aura never asks for passwords, keys, tokens or card details "
                    "in a chat question. A team hub's token goes in the hidden box "
                    "(team_token); for anything else, say in your answer what is needed.")
        key = _norm_q(q)
        if key in state["asked"]:
            return f"You asked this already; the user's answer was: {state['asked'][key]}"
        if state["count"] >= MAX_PER_RUN:
            return (f"You have asked {MAX_PER_RUN} questions in this task already. "
                    "Continue with what you know; say in your answer what you assumed.")
        opts = _options(args.get("options"))
        multi = bool(args.get("multi"))
        rec = str(args.get("recommended") or "").strip()
        if rec and rec not in [o[0] for o in opts]:
            rec = ""
        state["count"] += 1
        asker = getattr(mediator, "asker", None)
        if asker is None:
            # A user who said to wait gets a stop, and only ONE assumption is
            # made per run.
            stop = ""
            rule = _rule_in_question(mediator, q + " " + " ".join(f"{a} {b}" for a, b in opts))
            if rule:
                state["asked"][key] = "(nobody to ask — the standing rule stays)"
                return (f"Nobody is here to answer, and this question is about the user's "
                        f"standing rule \"{rule}\". Only the user can set a rule aside: keep "
                        "to the rule — do not do the part that breaks it. End your turn, say "
                        "plainly what you did not do and why, and ask the user in your final "
                        "answer.")
            if _user_said_wait(mediator):
                stop = "The user asked you to wait for their answer"
            elif mediator._assumptions:
                stop = "One answer was already assumed in this task"
            if stop:
                state["asked"][key] = "(nobody to ask — stopped)"
                return (f"{stop}, and nobody is here to answer. Do not assume again and "
                        "do not continue the part that needs the answer: end your turn "
                        "now, ask the question in your final answer, and say what is "
                        "done so far.")
            assumed = rec or "the model's best judgment"
            mediator._assumptions.append({"question": q[:300], "assumed": assumed})
            state["asked"][key] = f"(nobody to ask — assumed: {assumed})"
            return ("Nobody is here to answer right now. Continue with "
                    + (f"the recommended choice '{rec}'" if rec else "your best judgment")
                    + ". Aura records this assumption and shows it to the user; "
                    "also say it plainly in your final answer.")
        labels = [f"{lb} — {d}" if d else lb for lb, d in opts]
        if rec:
            labels = [x + ("  (recommended)" if x.split(" — ")[0] == rec else "") for x in labels]
        try:
            try:
                answer = asker(q, labels, multi=multi, free=True)
            except TypeError:                        # An asker without the extras
                answer = asker(q, labels)
        except Exception as e:                       # noqa: BLE001
            mediator._assumptions.append({"question": q[:300], "assumed": rec or "best judgment"})
            return f"The question could not be shown ({e}). Continue with your best judgment and say what you assumed."
        if isinstance(answer, (list, tuple)):
            answer = "; ".join(str(a) for a in answer)
        answer = re.sub(r"\s+\(recommended\)$", "", str(answer or "").strip())
        answer = answer.split(" — ")[0] if any(answer.startswith(lb) for lb, _ in opts) else answer
        if not answer:
            state["asked"][key] = "(no answer)"
            return ("The user left it to you. Continue with "
                    + (f"'{rec}'" if rec else "your best judgment")
                    + " and say what you chose.")
        state["asked"][key] = answer
        return f"[USER ANSWER — typed by the user] {answer}"

    mediator.register(ToolSpec(CLARIFY, BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch[CLARIFY] = clarify
