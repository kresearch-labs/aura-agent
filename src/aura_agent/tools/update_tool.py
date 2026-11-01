"""update_tool.py — `aura_update`: update Aura by asking.

  check what is installed and what the newest release is — reads one
            small file, changes nothing, no card
  update a card (current → new, from where, checked how) — on a yes the
            package is downloaded, its SHA-256 checked, and installed
            (Windows: it finishes as soon as Aura closes)
  rollback a card — back to the package kept from before
"""
from __future__ import annotations

from ..mediator import BlastRadius, Mediator, ToolSpec


def register(mediator: Mediator, dispatch: dict) -> None:
    from .. import __version__
    from .. import updater as U

    def ask(title: str, why: str) -> bool:
        if mediator.approver is None:
            return False
        from ..approvals import ask_operator
        ok, _ = ask_operator(mediator.root, mediator.approver, "aura_update", {"what": title}, "G2",
                             title, why, allow_always=False, heading="update Aura")
        return ok

    def aura_update(args) -> str:
        action = str(args.get("action") or "check").strip().lower()
        lay = U.layout() or {}
        if lay.get("method") != "uv-tool":
            return ("this Aura wasn't installed by Aura's installer (pip, a container or a source "
                    "folder), so it can't update itself — say how that install is updated: pip "
                    "install -U, a newer container image, or git pull in the folder.")
        if action == "rollback":
            t = U.rollback_target()
            if not t:
                return f"no earlier version is kept here (this is {__version__}) — nothing to go back to."
            v, wheel = t
            if not ask(f"go back to Aura {v}", f"{__version__} → {v}, the package kept from before "
                       "(its checksum still matches). Your chats and settings stay."):
                return "the user said no — nothing changed."
            return U.install(wheel, v, lay, verb="went back")["message"]
        info = U.check(lay)
        if not info["ok"]:
            return "couldn't check for an update: " + info["error"]
        if not info["newer"]:
            return f"Aura {__version__} is the newest release — nothing to update."
        if action != "update":
            return (f"Aura {info['latest']} is out (this is {__version__}). Updating asks the user "
                    "first; their chats and settings stay.")
        if not ask(f"update Aura {__version__} → {info['latest']}",
                   f"from {info['base']} — the package's SHA-256 is checked against the release's "
                   "SHA256SUMS before anything is installed. Your chats, settings, keys and projects "
                   "stay; the old version is kept to go back to."):
            return "the user said no — nothing changed."
        try:
            wheel = U.download(info)
        except Exception as e:                          # noqa: BLE001
            return f"not updated: {e}"
        return U.install(wheel, info["latest"], lay)["message"]

    mediator.register(ToolSpec("aura_update", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["aura_update"] = aura_update
