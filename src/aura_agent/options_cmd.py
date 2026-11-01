"""options_cmd.py — /settings in chat and `aura settings` (plain text; the
picker comes at the UI stage)."""
from __future__ import annotations


def settings_command(args: list[str]) -> list[str]:
    from . import options as O
    try:
        if not args:
            return O.listing()
        a = args[0].lower()
        if a == "find" and len(args) > 1:
            hits = O.find(" ".join(args[1:]))
            return [O.explain(o.key) for o in hits] or ["no option matches"]
        if a == "set" and len(args) >= 3:
            old, new = O.set_(args[1], " ".join(args[2:]))
            return [f"{O.by_key(args[1]).key}: {O.fmt(old)} -> {O.fmt(new)} "
                    "(saved as the profile's default · /settings undo)"]
        if a == "reset" and len(args) >= 2:
            if args[1] == "all":
                return [f"{o.key}: back to {O.fmt(O.reset(o.key)[1])}"
                        for o in O.options() if O.changed(o)] or ["nothing was changed"]
            old, new = O.reset(args[1])
            return [f"{O.by_key(args[1]).key}: {O.fmt(old)} -> {O.fmt(new)} (default)"]
        if a == "undo":
            return [O.undo()]
        if a == "list" or len(args) == 1 and a in ("tools", "web", "editing", "background"):
            return O.listing("" if a == "list" else a)
        return O.explain(args[0]).splitlines()
    except (KeyError, ValueError) as e:
        return [str(e).strip("'\"")]
