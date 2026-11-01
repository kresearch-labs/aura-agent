"""get_tool.py — `get`: find a way to something that is missing here. Only with
the toggle on (/findaway).

  1. what is known costs nothing: already installed? the sandbox state? the
     model's measured abilities (a model that can't see images is a limit,
     not something to work around);
  2. the sandbox: the setup Aura already has (Microsoft's process container
     on Windows 11 24H2+, else Docker) — a card with what, from where, how
     big; on a yes Aura sets it up;
  3. a program or package: a card asks BEFORE any research; on a yes the
     model may look it up (at most RESEARCH_CAP web calls), and the install
     itself is a command the user approves like any other;
  4. anything else: the model asks the user (clarify) — no effort before a yes.
"""
from __future__ import annotations

import platform
import shutil

from ..mediator import BlastRadius, Mediator, ToolSpec


def _os_words() -> str:
    s = platform.system()
    return {"Windows": f"Windows {platform.release()}", "Darwin": "macOS"}.get(s, s or "this OS")


def register(mediator: Mediator, dispatch: dict) -> None:
    from .. import find_way as FW

    def ask(title: str, why: str) -> bool:
        if mediator.approver is None:
            return False
        from ..approvals import ask_operator
        ok, _ = ask_operator(mediator.root, mediator.approver, "get", {"what": title}, "G2",
                             title, why, allow_always=False, heading="find a way")
        if not ok:
            mediator._get_declined = True     # The user's no ends the search this run
        return ok

    def get(args) -> str:
        if not FW.on():
            return ("'find a way' is off (the user's toggle). Say plainly what is missing and "
                    "what the user can do; don't look for workarounds.")
        what = str(args.get("what") or "").strip()
        kind = str(args.get("kind") or "").strip().lower()
        if not what:
            return "error: what is missing"
        low = what.lower()
        # 1. known facts, free
        if kind == "model" or any(w in low for w in ("image", "vision", "see pictures", "audio")):
            try:
                from ..model_facts import fact, model_key
                p = getattr(getattr(mediator, "_main_llm", None), "p", None)
                v = fact("vision", model_key(p.base_url, p.model)) if p else None
            except Exception:                        # noqa: BLE001
                v = None
            if v == "no":
                return ("this is the model's own limit (checked: it can't see images) — don't "
                        "look for workarounds. Tell the user: switch to a model that can (they "
                        "choose, /model), or describe the image in words.")
            if v == "yes":
                return "this model can see images (checked) — attach the image and look at it."
        if kind in ("program", "package", "") and kind != "sandbox" and "sandbox" not in low:
            found = shutil.which(what)
            if found:
                return f"'{what}' is already installed here: {found}"
        # 2. the sandbox
        if kind == "sandbox" or "sandbox" in low:
            from .. import sandbox_os as SO
            from .. import sandbox_win as SW
            from ..sandbox import docker_available
            state = SO.describe()
            if not state.startswith(("a Windows job object", "none")):
                return f"a sandbox is already set up: {state}"
            if SW.offered() and SW.installed() is None:
                if not ask("set up a sandbox",
                           f"set up Microsoft's process container (MXC {SW.VERSION}): "
                           f"{SW.PACKAGE_SIZE / 1e6:.1f} MB from nuget.org (Microsoft's package "
                           "site), SHA-512 checked, nothing installed into Windows — then "
                           "commands may write only in the project and temp, network off "
                           "unless allowed"):
                    return ("the user said no to setting up a sandbox — don't run it without "
                            "one if they asked for a sandbox; say what's done and stop")
                try:
                    said = SW.get(say=lambda *_: None)
                except Exception as e:               # noqa: BLE001
                    return f"setting up the sandbox failed: {e} — tell the user plainly"
                return f"sandbox set up: {said}. Commands now run inside it: {SO.describe()}"
            if docker_available():
                return ("Docker is running here and Aura uses it as the sandbox for commands "
                        f"({state}) — run the command; it goes inside")
            return ("no sandbox can be set up automatically on this machine "
                    f"({_os_words()}): Microsoft's process container needs Windows 11 24H2+, "
                    "Docker isn't running. Tell the user these two options; ask before anything")
        # 3. a program or package: ask BEFORE any research
        if kind in ("program", "package", "tool", ""):
            if not ask(f"find {what}",
                       f"'{what}' isn't installed. Look up how to install it on {_os_words()}? "
                       f"(a quick web search — at most {FW.RESEARCH_CAP} — then you approve the "
                       "install command like any other)"):
                return (f"the user said no — don't look for '{what}' or a workaround; say what "
                        "you can do without it")
            mediator._research_left = FW.RESEARCH_CAP
            return (f"approved: look up how to install '{what}' on {_os_words()} — official "
                    f"source first, at most {FW.RESEARCH_CAP} web calls — then install it with "
                    "run_command (the user sees and approves that command) and go on")
        # 4. anything else
        return ("ask the user (clarify) whether to look for a way to get this, naming what you'd "
                "do — no searching before their yes")

    mediator.register(ToolSpec("get", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["get"] = get
