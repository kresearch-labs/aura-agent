"""teamserver/perms.py

Same judge for a browser session and an Aura device, same answer.

allowed(person, permission, project):
  disabled person → no
  no project membership (that project) → no
  the member's role row (built-in or custom) carries the tick → yes
  reader carries only "view_*" + add_work_package_comments + chat
  write-own ticks — never a write tick it doesn't hold.
"""
from __future__ import annotations

import json

# The permission → its module
MODULE_OF = {
    # Board / tasks
    "view_work_packages": "board",
    "add_work_packages": "board",
    "edit_work_packages": "board",
    "move_work_packages": "board",
    "add_work_package_comments": "board",
    "manage_work_package_relations": "board",
    "add_work_package_attachments": "board",
    "export_work_packages": "board",
    "delete_work_packages": "board",
    # Now (who's on what)
    "view_activity": "history",
                                        #   rides the same view tick
    "browse_repository": "tree",
    "manage_gantt": "flow",
    "export_activity": "history",
    "manage_activity": "history",
    # Chat
    "view_messages": "chat",
    "add_messages": "chat",
    "edit_own_messages": "chat",
    "edit_messages": "chat",
    "delete_messages": "chat",
    # Aura work
    "aura_work": "aura_work",
    # Members
    "view_members": "members",
    "manage_members": "members",
    # Settings
    "edit_project": "settings",
    "archive_project": "settings",
}
# Modules every project has, switchable per project
ALL_MODULES = ("board", "now", "tree", "flow", "history", "chat",
               "aura_work", "members", "settings")


class Forbidden(Exception):
    pass


def is_owner_admin(person: dict) -> bool:
    """Team authority, including authority carried by a legacy role row."""
    return (person.get("team_role") in ("owner", "admin")
            or person.get("role") in ("owner", "admin"))


_is_owner_admin = is_owner_admin


# the areas a person's own switches can turn off (or on) in one project
AREAS = ("board", "now", "tree", "flow", "history", "chat", "aura_work")


def _member_row(conn, person_id: int, project: int):
    if conn is None or project is None:
        return None
    return conn.execute(
        "SELECT role_id, role, overrides FROM project_members WHERE project = ?"
        " AND person = ?", (int(project), person_id)).fetchone()


def overrides_of(row) -> dict:
    """A member row's own switches: {area: bool}, only the known areas."""
    try:
        raw = json.loads((row["overrides"] if row is not None else None) or "{}")
    except (ValueError, TypeError, IndexError, KeyError):
        return {}
    return {a: bool(v) for a, v in raw.items() if a in AREAS and isinstance(v, bool)}


def _role_perms_transpose(p: dict) -> dict:
    """The stored `{module: [ticks]}` shape → {tick: True} for lookup."""
    out = {}
    for tks in (p or {}).values():
        for t in tks:
            out[t] = True
    return out


def allowed(conn, person: dict, permission: str, project=None,
            module: str | None = None) -> bool:
    """The single rule every route checks."""
    if person is None:
        return False
    if person.get("disabled_at"):
        return False
    if permission == "hub.handover":
        return (person.get("team_role") == "owner"
                or person.get("role") == "owner")
    # Existing team-face callers intentionally omit the project for checks such
    # as chat-room creation and task existence.
    if _is_owner_admin(person):
        return True
    if project is None:
        team_perm = {"project.create": _is_owner_admin(person)
                     or (person.get("global_role") == "staff and projects manager"),
                     "team.manage": _is_owner_admin(person),
                     # Users-admin the global role may do too ("staff and
                     # projects manager" — never acting over admins; the
                     # handler's carve-outs hold that line)
                     "people.manage": _is_owner_admin(person)
                     or (person.get("global_role") == "staff and projects manager")}
        return bool(team_perm.get(permission))
    if permission in ("team.manage",) or permission.startswith("hub."):
        return _is_owner_admin(person)  # Team-side, never over admins (the
                                        # callers split the last mile)
    module = module or MODULE_OF.get(permission)
    if module is None:
        return False
    row = _member_row(conn, person.get("person_id") or person.get("id"),
                      int(project))
    if row is None:
        return False
    # The module switch in the project.
    mods = conn.execute("SELECT modules FROM projects WHERE id = ?",
                        (int(project),)).fetchone()
    if mods is not None:
        try:
            m = json.loads(mods["modules"] or "{}")
        except ValueError:
            m = {}
        if module in m and m[module] is False:
            return False
    # the person's own switch for this area in this project wins over the role
    own = overrides_of(row).get(module)
    if own is False:
        return False
    # the member's role by role_id (built-in or custom) — the legacy text
    # column mapping survives for old rows
    the_role = None
    if row["role_id"]:
        rr = conn.execute("SELECT permissions FROM roles WHERE id = ?",
                          (row["role_id"],)).fetchone()
        if rr:
            try:
                the_role = json.loads(rr["permissions"])
            except ValueError:
                the_role = None
    if the_role is None:
        # legacy text era (pre-V8 writes)
        the_role = {"manager": _BUILTIN_PERMS["Project admin"],
                    "member": _BUILTIN_PERMS["Member"],
                    "viewer": _BUILTIN_PERMS["Reader"]}.get(
                row["role"] or "", _BUILTIN_PERMS["Reader"])
    if own is True and not (the_role or {}).get(module):
        # switched on for this person although the role gives nothing in the
        # area: what a Member may do there
        return permission in _BUILTIN_PERMS["Member"].get(module, [])
    return bool(_role_perms_transpose(the_role).get(permission))


def require(conn, person: dict, permission: str, project=None) -> None:
    if not allowed(conn, person, permission, project):
        raise Forbidden(f"not allowed here: {permission}")


def can(conn, person: dict, permission: str, project=None) -> bool:
    """Compatibility for the older interior call sites."""
    return allowed(conn, person, permission, project)


# the three built-ins (V8 also seeds them in SQL; kept here for the legacy
# fallback read and the research note's promised names)
_BUILTIN_PERMS = {
    "Reader": {"board": ["view_work_packages", "add_work_package_comments"],
               "now": ["view_activity"], "tree": ["browse_repository"],
               "flow": ["view_work_packages"], "history": ["view_activity"],
               "chat": ["view_messages", "add_messages", "edit_own_messages"],
               "aura_work": [], "members": [], "settings": []},
    "Member": {"board": ["view_work_packages", "add_work_packages",
                         "edit_work_packages", "move_work_packages",
                         "add_work_package_comments",
                         "manage_work_package_relations",
                         "add_work_package_attachments",
                         "export_work_packages"],
               "now": ["view_activity"], "tree": ["browse_repository"],
               "flow": ["view_work_packages"],
               "history": ["view_activity", "export_activity"],
               "chat": ["view_messages", "add_messages", "edit_own_messages"],
               "aura_work": ["aura_work"], "members": ["view_members"],
               "settings": []},
    "Project admin": {"board": ["view_work_packages", "add_work_packages",
                                "edit_work_packages", "move_work_packages",
                                "add_work_package_comments",
                                "manage_work_package_relations",
                                "add_work_package_attachments",
                                "export_work_packages", "delete_work_packages"],
                      "now": ["view_activity"], "tree": ["browse_repository"],
                      "flow": ["view_work_packages", "manage_gantt"],
                      "history": ["view_activity", "export_activity",
                                  "manage_activity"],
                      "chat": ["view_messages", "add_messages",
                               "edit_own_messages", "edit_messages",
                               "delete_messages"],
                      "aura_work": ["aura_work"],
                      "members": ["view_members", "manage_members"],
                      "settings": ["edit_project", "archive_project"]},
}


def allowed_any(conn, person: dict, permission: str, module: str | None = None) -> bool:
    """'Any project' scope (the members-list law)."""
    if person is None or person.get("disabled_at"):
        return False
    if _is_owner_admin(person):
        return True
    # A global people manager must reach the team directory even before
    # anyone has put that person in a project. This does not change the
    # project-scoped view_members check below.
    if permission == "view_members" and allowed(conn, person, "people.manage"):
        return True
    rows = conn.execute(
        "SELECT m.project FROM project_members m WHERE m.person = ?",
        (person.get("person_id") or person.get("id"),)).fetchall()
    return any(allowed(conn, person, permission, r["project"], module=module)
               for r in rows)


def person_actor(conn, person_id: int):
    """A people row in the shape allowed() reads — for filters that decide
    about someone other than the caller (chat rooms, the live stream)."""
    row = conn.execute(
        "SELECT id, name, role, team_role, global_role, disabled_at"
        " FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["person_id"] = d["id"]
    return d


def members_who_may(conn, project: int, checks) -> set:
    """The project's members who pass any (permission, area) in `checks`
    right now — role ticks, the project's module switch and their own
    switches all count, through allowed()."""
    out = set()
    for r in conn.execute("SELECT person FROM project_members WHERE project = ?",
                          (int(project),)).fetchall():
        a = person_actor(conn, r["person"])
        if a and any(allowed(conn, a, p, project, module=m) for p, m in checks):
            out.add(r["person"])
    return out


def projects_visible(conn, person: dict) -> list:
    """IDs of projects the person may see."""
    if _is_owner_admin(person):
        return [r["id"] for r in conn.execute(
            "SELECT id FROM projects WHERE archived_at IS NULL"
            " AND deleted_at IS NULL").fetchall()]
    return [int(r["project"]) for r in conn.execute(
        "SELECT project FROM project_members WHERE person = ?",
        (person.get("person_id") or person.get("id"),)).fetchall()]


# ------------------------------------------------------- role cards --
def roles_list(conn) -> list:
    # older member rows name their role in the text column only
    used = {r["rid"]: (r["people"], r["projects"]) for r in conn.execute(
        "SELECT COALESCE(m.role_id, r.id) AS rid, COUNT(DISTINCT m.person) AS people,"
        " COUNT(DISTINCT m.project) AS projects FROM project_members m"
        " LEFT JOIN roles r ON r.builtin = 1 AND r.name = CASE m.role"
        " WHEN 'manager' THEN 'Project admin' WHEN 'member' THEN 'Member'"
        " WHEN 'viewer' THEN 'Reader' END"
        " GROUP BY COALESCE(m.role_id, r.id)")}
    return [{"id": r["id"], "name": r["name"], "builtin": bool(r["builtin"]),
             "permissions": json.loads(r["permissions"] or "{}"),
             "people": used.get(r["id"], (0, 0))[0],
             "projects": used.get(r["id"], (0, 0))[1]}
            for r in conn.execute("SELECT * FROM roles ORDER BY builtin DESC,"
                                  " id").fetchall()]


def _checked_ticks(perms: dict) -> dict:
    out = {}
    for mod, tks in (perms or {}).items():
        if mod not in ALL_MODULES:
            raise ValueError(f"unknown module {mod!r}")
        if not isinstance(tks, list):
            raise ValueError(f"{mod}: a list of ticks")
        for t in tks:
            if t not in MODULE_OF:
                raise ValueError(f"unknown permission tick {t!r}")
        out[mod] = sorted(set(tks))
    return out


def role_update(conn, role_id: int, name: str | None, perms: dict | None,
                reset: bool = False) -> dict:
    """Change a role for everyone who has it: its ticks (any role), its name
    (a custom one), or a built-in back to its default ticks."""
    r = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if r is None:
        raise LookupError("no such role")
    new_name = r["name"]
    if name is not None and name.strip() != r["name"]:
        if r["builtin"]:
            raise ValueError("a built-in role keeps its name")
        new_name = name.strip()[:60]
        if not new_name:
            raise ValueError("the role needs a name")
        if conn.execute("SELECT 1 FROM roles WHERE name = ? AND id != ?",
                        (new_name, role_id)).fetchone():
            raise ValueError("another role has that name")
    if reset:
        if not r["builtin"]:
            raise ValueError("only a built-in role has a default to go back to")
        ticks = _BUILTIN_PERMS[r["name"]]
    elif perms is not None:
        ticks = _checked_ticks(perms)
    else:
        ticks = json.loads(r["permissions"] or "{}")
    conn.execute("UPDATE roles SET name = ?, permissions = ? WHERE id = ?",
                 (new_name, json.dumps(ticks), role_id))
    return role_get(conn, role_id)


def role_get(conn, role_id: int):
    r = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "name": r["name"], "builtin": bool(r["builtin"]),
            "permissions": json.loads(r["permissions"] or "{}")}


def role_make(conn, name: str, copy_from: str | None, perms: dict,
              by: dict | None = None) -> int:
    """A custom role, admin-made — its own name, the module ticks (copied from a
    role when `copy_from` names one). Builtin rows never.
    """
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("the role needs a name")
    from . import schema  # noqa: F401  (the table exists by migrate)
    base = {}
    if copy_from:
        src = conn.execute("SELECT permissions FROM roles WHERE name = ?",
                           (copy_from,)).fetchone()
        if src is None:
            raise ValueError(f"no role called {copy_from!r} to copy")
        base = json.loads(src["permissions"] or "{}")
    merged = {**base, **(perms or {})}
    for mod in merged:
        if mod not in ALL_MODULES:
            raise ValueError(f"unknown module {mod!r}")
    for tks in merged.values():
        for t in tks:
            if t not in MODULE_OF:
                raise ValueError(f"unknown permission tick {t!r}")
    import time as _t
    cur = conn.execute(
        "INSERT INTO roles (name, builtin, permissions, created)"
        " VALUES (?,?,?,?)", (name, 0, json.dumps(merged), _t.time()))
    return cur.lastrowid


def role_delete(conn, role_id: int) -> None:
    """A role in use can't be deleted (members hold its id)."""
    r = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if r is None:
        raise ValueError("no such role")
    if r["builtin"]:
        raise ValueError("a built-in role can't be deleted")
    used = conn.execute(
        "SELECT 1 FROM project_members WHERE role_id = ? LIMIT 1",
        (role_id,)).fetchone()
    if used:
        raise ValueError("that role is in use — reassign those members first")
    conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))


# --- the activity-detail level (the "activity.detail" lane in api_views
# rides on this map) ---------------------------------------------------------
DETAIL_DEFAULTS = {"manager": True, "member": True, "viewer": False,
                   "guest": False}


def detail_settings_of(settings, project: int) -> dict:
    """The project's `detail_visibility` switch as the detail map
    ({f"{role}:{project}": bool}). "members" (the default): Project admins and
    Members see the full log; "managers": admins only. Readers (viewers) and
    guests stay off under either value — 's column.
    """
    vis = "members"
    if isinstance(settings, dict):
        vis = str(settings.get("detail_visibility") or "members")
    out = {f"{role}:{project}": default
           for role, default in DETAIL_DEFAULTS.items()}
    if vis == "managers":
        out[f"member:{project}"] = False
    return out


def detail_ok(conn, person: dict, project: int) -> bool:
    """The History 'detail' level: the project's detail_visibility switch works
    WITH the table — "members": Reader excluded, everyone else
    member-of-the-project; "managers": Project admin only. Highest lane:
    manage_activity does it.
    """
    if allowed(conn, person, "manage_activity", project):
        return True
    row = _member_row(conn, person.get("person_id") or person.get("id"),
                      int(project))
    if row is None:
        return False
    prow = conn.execute("SELECT settings FROM projects WHERE id = ?",
                        (int(project),)).fetchone()
    vis = "members"
    if prow:
        try:
            vis = json.loads(prow["settings"] or "{}").get(
                "detail_visibility", "members")
        except ValueError:
            pass
    ro = conn.execute("SELECT r.name FROM roles r WHERE r.id = ?",
                      (row["role_id"],)).fetchone()
    rname = (ro["name"] if ro else None) or {"manager": "Project admin",
                                             "member": "Member",
                                             "viewer": "Reader"}.get(
            row["role"] or "", "Reader")
    if vis == "managers":
        return rname == "Project admin"
    return rname in ("Member", "Project admin")
