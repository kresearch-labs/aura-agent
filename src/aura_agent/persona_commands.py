"""persona_commands.py — persona in the chat.

Persona is the product; one trained unit is a pblock; the pblocks working
together in a chat are its panel. persona opens the collection (it can hold
hundreds, so it lives here, not in the sidebar): switch Persona on or off, turn
a pblock on or off for this chat, add one, discard one from this chat, delete
one's files, or start training new ones.

  /persona the collection (a picker)
  /persona on | off Persona itself
  /persona use <name> turn a pblock on for this chat
  /persona stop <name> turn it off for this chat
  /persona discard <name> take it out of this chat (files stay)
  /persona delete <name> delete its files (asks first)
  /persona add <path> bring a pblock in (store file, folder or .zip)
  /persona train Aura plans new pblocks with you
"""
from __future__ import annotations

from . import pblocks as P
from . import ui
from .chat_commands import Result, command

def _train_task(topic: str = "") -> str:
    from pathlib import Path
    docs = Path(__file__).parent / "pblock_engine" / "docs"
    first = (f"I want to work on: {topic}. " if topic.strip() else
             "First ask me what I want to work on, with the clarify tool. ")
    return (
        "I want to train new pblocks for Persona. " + first +
        "Then decide how many pblocks that work needs: suggest at least 1 and at most 7 "
        "(I may add more myself). Go through them ONE AT A TIME with the pblock_propose "
        "tool (name, what it judges, the general kind of training data); only propose real "
        "pblocks for my work — never call it to test it. If I answer "
        "Change it, Tell me more or Other, do that and propose the same pblock again. "
        "When every pblock has an answer, train each approved one: read the guides in "
        f"{docs} (HOW-PBLOCKS-WORK.md, TRAINING-PBLOCKS.md, CORPUS_TRAINING_METHODOLOGY.md, "
        "and example-pblock.yml for the template format) and follow them — find real "
        "data with real outcomes, declare the decision space, never pad the corpus to a "
        "number. Ask me before any download. Write the template (.yml) and the corpus "
        "plan (.yml: craft_truths, dimensions, negations, vocabulary, and real_records, "
        "each with situation, outcome and its source) for each pblock, then call "
        "pblock_train. "
        "At the end, tell me which pblocks were added and which did not pass their gates, "
        "and why.")


def _chat(ctx) -> str:
    return ctx.session.path.stem


def _set_persona(on: bool) -> None:
    from . import options
    options.set_("pblocks.on", on, who="user")
    ui.success("Persona is " + ("on" if on else "off — plain Aura, nothing used or learned"))


def _confirm(ctx, title: str) -> bool:
    return ctx.choose(title, [("No", False, "", True), ("Yes", True, "", False)]) is True


def _one(ctx, name: str) -> None:
    """The actions on one pblock."""
    chat = _chat(ctx)
    on = P.chosen(chat).get(name)
    opts = []
    if on:
        opts.append(("switch off for this chat", "stop", "it stays in this chat, unused", False))
    else:
        opts.append(("use in this chat", "use", "turns it on for this chat", False))
    if on is not None:
        opts.append(("discard from this chat", "discard", "takes it out of this chat; files stay", False))
    opts.append(("what it learned", "learned", "the newest things it learned while working", False))
    opts.append(("review all lessons", "review", "a page in your browser: tick, say what you want", False))
    if name != P.PERSONAL:
        opts.append(("delete", "delete", "removes its files from the pblocks folder", False))
    pick = ctx.choose(f"{name} — {P.purpose(name)}", opts)
    if pick == "learned":
        _learned(name)
    elif pick == "review":
        _review(name)
    if pick == "use":
        P.switch(chat, name, True)
        ui.success(f"{name} is on for this chat")
    elif pick == "stop":
        P.switch(chat, name, False)
        ui.info(f"{name} is off for this chat")
    elif pick == "discard":
        P.discard(chat, name)
        ui.info(f"{name} is out of this chat (its files stay)")
    elif pick == "delete" and _confirm(ctx, f"delete {name}'s files? this can't be undone"):
        gone = P.delete(name)
        P.discard(chat, name)
        ui.success(f"deleted {name}: {', '.join(gone) or 'nothing found'}")


def _picker(ctx) -> Result:
    while True:
        chat = _chat(ctx)
        state = P.chosen(chat)
        on = P.enabled()
        opts = [(("● " if on else "○ ") + "Persona", "toggle",
                 "on: your pblocks judge and learn · off: plain Aura", False),
                ("+ add a pblock", "add", "a store file, a folder or a .zip", False),
                ("+ train new pblocks", "train", "Aura plans them with you", False)]
        for name in P.all_pblocks():
            mark = "● " if state.get(name) else ("○ " if name in state else "  ")
            opts.append((mark + name, ("pb", name), P.purpose(name), False))
        pick = ctx.choose("persona — your pblocks (● on in this chat)", opts)
        if pick is None:
            return Result()
        if pick == "toggle":
            _set_persona(not on)
        elif pick == "add":
            path = ctx.ask("path to the pblock (store file, folder or .zip):").strip()
            if path:
                _add(path)
        elif pick == "train":
            topic = ctx.ask("what do you want to work on? (Enter: Aura asks you)").strip()
            return Result(task=_train_task(topic))
        elif isinstance(pick, tuple):
            _one(ctx, pick[1])


def _learned(name: str) -> None:
    """The pblock's own newest lessons, in its own words (no model)."""
    if name not in P.all_pblocks():
        ui.warn(f"no pblock named '{name}'")
        return
    rows = P.recent_learned(name)
    if not rows:
        ui.info(f"{name} hasn't learned anything new yet")
        return
    ui.info(f"{name} — the newest things it learned:")
    for r in rows:
        ui.info(f"  {'✔' if r['worked'] else '✘'} {r['text']}"
                + (f"  ({r['when']})" if r["when"] else ""))
    ui.info(f"to change any of them: /persona review {name}")


def _review(name: str) -> None:
    if name not in P.all_pblocks():
        ui.warn(f"no pblock named '{name}'")
        return
    page = P.review_page(name)
    try:
        import webbrowser
        webbrowser.open(page.as_uri())
    except Exception:                                    # noqa: BLE001
        pass
    ui.success(f"opened {page} — tick lessons, write what you want, make the request, "
               "then /persona apply <the file>")


def _add(path: str) -> None:
    try:
        names = P.add(path)
    except (OSError, ValueError) as e:
        ui.warn(f"couldn't add it: {e}")
        return
    if names:
        ui.success("added: " + ", ".join(names) + " — turn them on with /persona")
    else:
        ui.warn("no pblock found there (a pblock is a <name>_store.jsonl file)")


@command("/persona")
def _persona(ctx, parts, line):
    """Persona: the collection of pblocks and this chat's panel."""
    if len(parts) == 1:
        return _picker(ctx)
    sub, rest = parts[1].lower(), " ".join(parts[2:]).strip()
    chat = _chat(ctx)
    if sub in ("on", "off") and not rest:
        _set_persona(sub == "on")
    elif sub == "use" and rest:
        P.switch(chat, rest, True)
        ui.success(f"{rest} is on for this chat")
    elif sub == "stop" and rest:
        P.switch(chat, rest, False)
        ui.info(f"{rest} is off for this chat")
    elif sub == "discard" and rest:
        P.discard(chat, rest)
        ui.info(f"{rest} is out of this chat (its files stay)")
    elif sub == "delete" and rest:
        if rest == P.PERSONAL:
            ui.warn("the personal pblock can be switched off, not deleted")
        elif _confirm(ctx, f"delete {rest}'s files? this can't be undone"):
            gone = P.delete(rest)
            P.discard(chat, rest)
            ui.success(f"deleted {rest}: {', '.join(gone) or 'nothing found'}")
    elif sub == "add" and rest:
        _add(rest)
    elif sub == "train":
        return Result(task=_train_task(rest))
    elif sub == "learned":
        _learned(rest or P.PERSONAL)
    elif sub == "review":
        _review(rest or P.PERSONAL)
    elif sub == "apply" and rest:
        try:
            r = P.apply_request(rest.strip('"'))
        except (OSError, ValueError) as e:
            ui.warn(f"couldn't apply it: {e}")
        else:
            ui.success(f"{r['pblock']}: {r['retired']} lesson(s) retired"
                       + (", your note kept as a lesson" if r["taught"] else ""))
    else:
        ui.info("usage: /persona · on|off · use|stop|discard|delete <name> · add <path> · train [what you want to work on] · learned|review [name] · apply <file>")
    return Result()
