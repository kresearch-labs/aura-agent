"""teamserver/api_projects.py — projects, members, statuses (plan).

perms.require before every write; every change logged; archived projects refuse
writes.
"""
from __future__ import annotations

import json
import re

from . import log as logmod
from . import store
from .api_people import _actor, _db
from .http import parse_json as _parse_json
from .perms import Forbidden, require

SLUG_OK = re.compile(r"[a-z0-9][a-z0-9-]{0,38}")


def _project(conn, pid: int):
    return conn.execute("SELECT * FROM projects WHERE id = ?",
                        (pid,)).fetchone()


def _member_role(conn, person_id: int, project_id: int) -> str | None:
    row = conn.execute(
        "SELECT role FROM project_members WHERE person = ? AND project = ?",
        (person_id, project_id)).fetchone()
    return row["role"] if row else None


def _require(handler, conn, actor, action, pid):
    try:
        require(conn, actor, action, pid)
        return True
    except Forbidden as e:
        handler._error(403, str(e))
        return False


def _visible(conn, actor, pid) -> bool:
    """."""
    from .perms import is_owner_admin
    if is_owner_admin(actor):
        return conn.execute(
            "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL",
            (pid,)).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM project_members WHERE project = ? AND person = ?",
        (pid, actor["person_id"])).fetchone() is not None


def list_projects(handler, match, body) -> None:
    """GET api/v1/projects — what this person can see. Notices secrets ride as
    "[stored]" + fingerprints, even entries stored before the split.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from .perms import is_owner_admin
    if is_owner_admin(actor):
        rows = conn.execute(
            "SELECT id, slug, name, settings, created, archived_at,"
            " main_branch, modules FROM projects WHERE deleted_at IS NULL ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT p.id, p.slug, p.name, p.settings, p.created,"
            " p.archived_at, p.main_branch, p.modules"
            " FROM projects p JOIN project_members m"
            " ON m.project = p.id WHERE m.person = ? AND p.deleted_at IS NULL"
            " ORDER BY p.id", (actor["person_id"],)).fetchall()
    from .. import team_notices as _TN
    members = {}
    for r in conn.execute(
            "SELECT m.project, m.person, p.name,"
            " COALESCE(r.name, m.role) AS role, m.overrides"
            " FROM project_members m JOIN people p ON p.id = m.person"
            " LEFT JOIN roles r ON r.id = m.role_id"
            " ORDER BY m.project, p.name"):
        from .perms import overrides_of
        members.setdefault(r["project"], []).append(
            {"person": r["person"], "name": r["name"], "role": r["role"],
             "overrides": overrides_of(r)})
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["settings"] = json.dumps(
                _TN.redact_settings(json.loads(d["settings"] or "{}")),
                ensure_ascii=False)
        except Exception:                            # noqa: BLE001
            d["settings"] = "{}"
        try:
            d["modules"] = json.loads(d["modules"] or "{}")
        except (TypeError, ValueError):
            d["modules"] = {}
        d["main_branch"] = d["main_branch"] or "main"
        d["members"] = [dict(m) for m in members.get(d["id"], [])]
        out.append(d)
    handler._json(200, {"projects": out})


def project_create(handler, match, body) -> None:
    """POST api/v1/projects — project.create: default statuses, creator = manager,
    a chat room; optional repos/folders bindings.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    try:
        require(conn, actor, "project.create")
    except Forbidden as e:
        handler._error(403, str(e))
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    name = str(req.get("name") or "").strip()[:80]
    slug = str(req.get("slug") or "").strip().lower()
    if not name:
        handler._error(400, "give the project a name")
        return
    slug = slug or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:38]
    if not SLUG_OK.fullmatch(slug):
        handler._error(400, "the slug is lowercase letters, digits, dashes")
        return
    if conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        handler._error(409, "a project with that slug exists")
        return
    repos = [str(r)[:200] for r in (req.get("repos") or [])][:8]
    folders = [str(f)[:80] for f in (req.get("folders") or [])][:8]
    from .schema import DEFAULT_STATUSES, status_colour
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO projects (slug, name, created, created_by, settings)"
            " VALUES (?,?,?,?,?)",
            (slug, name, store.now(), actor["person_id"],
             json.dumps({"repos": repos, "folders": folders})))
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO project_members (project, person, role)"
            " VALUES (?, ?, 'manager')", (pid, actor["person_id"]))
        for key, type, sname, pos in DEFAULT_STATUSES:
            conn.execute(
                "INSERT INTO statuses (project, key, type, name, position, colour)"
                " VALUES (?,?,?,?,?,?)",
                (pid, key, type, sname, pos, status_colour(type)))
        # Every project has its chat room, made with the project
        conn.execute(
            "INSERT INTO rooms (kind, name, project, made_by, ts)"
            " VALUES ('project', ?, ?, ?, ?)",
            (f"#{slug}", pid, actor["person_id"], store.now()))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "project.created", slug,
                      actor["person_id"],
                      {"name": name, "repos": repos, "folders": folders})
    handler._json(200, {
        "project": {"id": pid, "slug": slug, "name": name},
        "statuses": [dict(r) for r in conn.execute(
            "SELECT key, type, name, position, colour FROM statuses WHERE project = ?"
            " ORDER BY position", (pid,)).fetchall()]})


def _plan_notices(pid: int, value, old_list: list) -> tuple:
    """Validate a notices list and PLAN the secret split — (entries,
    secrets_to_store, show_once). Nothing is written here; the caller puts
    secrets_to_store into the server_secrets table inside its store.write, next
    to the settings update, so both commit or neither.
    """
    from .. import team_notices as _TN
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("notices is a list of at most 8 targets")
    entries, to_store, shown = [], [], []
    # A position is not a durable notice identity. References are opaque
    # metadata already returned by redact_settings; actual secret VALUES are
    # never accepted as a lookup. Search only this project's previous rows.
    old_rows = [row for row in old_list if isinstance(row, dict)]
    used_refs = {row[key] for row in old_rows
                 for key in ("where_ref", "token_ref", "secret_ref")
                 if isinstance(row.get(key), str) and row[key]}
    seen_identities = set()
    # Read all explicit identities before matching by position. If an old row
    # is explicitly retained elsewhere in the submitted order, an untagged
    # row over its old slot is an ADD, not permission to inherit that old
    # row's token/signing secret (delete first + append exercises this).
    claimed_identities = {
        row.get("where_ref") for row in value
        if isinstance(row, dict) and isinstance(row.get("where_ref"), str)
        and row.get("where_ref")
    }
    def fresh_ref(prefix: str) -> str:
        # Never reuse a deleted row's storage slot. In particular, deletion
        # followed by append cannot collide with a surviving B target's keys.
        for n in range(len(used_refs) + 1):
            candidate = f"{prefix}:{pid}:{n}"
            if candidate not in used_refs:
                used_refs.add(candidate)
                return candidate
        raise AssertionError("unused secret reference must exist")

    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError("a notice target is an object")
        app = _TN.APP_KIND.get(str(raw.get("app") or "webhook").lower())
        if app is None:
            raise ValueError("app is one of " + ", ".join(_TN.APP_KIND))
        types = [str(t)[:40] for t in (raw.get("types") or [])][:20]
        bad = [t for t in types if t not in _TN.NOTICE_TYPES]
        if bad:
            raise ValueError("unknown notice type: " + ", ".join(bad[:3]))
        identity = raw.get("where_ref")
        if identity is not None and not isinstance(identity, str):
            raise ValueError("stored notice identity must be a reference")
        # Opaque identifiers are compared exactly, never trimmed or repaired.
        identity = identity or ""
        where = str(raw.get("where") or "").strip()[:300]
        if identity:
            matches = [row for row in old_rows
                       if row.get("where_ref") == identity]
            if len(matches) != 1 or identity in seen_identities:
                raise ValueError("unknown, duplicate or ambiguous stored notice identity")
            seen_identities.add(identity)
            old = matches[0]
        else:
            if i >= len(old_list):
                old = {}
            elif len(value) < len(old_list):
                raise ValueError("stored notice identity is required for a removed target")
            else:
                candidate = old_list[i]
                if isinstance(candidate, dict) and \
                        candidate.get("where_ref") in claimed_identities:
                    if where == _TN.STORED or not where:
                        # A claimed slot cannot tell us which old survivor an
                        # untagged placeholder means. Refuse rather than erase
                        # it and let a later destination inherit its secrets.
                        raise ValueError("ambiguous stored notice identity")
                    old = {}
                else:
                    old = candidate if isinstance(candidate, dict) else {}
        # An untagged NEW value is never permission to reuse a positional
        # candidate's credentials. Reuse requires the explicit where_ref.
        if not identity and where and where != _TN.STORED:
            old = {}
        elif not identity and isinstance(old, dict):
            # Legacy rows can be edited in place. Reordering them without an
            # opaque reference must not silently exchange their values: the
            # redacted client still carries fingerprints for every secret.
            for field in ("where", "token", "secret"):
                supplied_fp = raw.get(field + "_fp")
                if supplied_fp is not None and not isinstance(supplied_fp, str):
                    raise ValueError("legacy notice identity must be a fingerprint")
                reference_fp = old.get(field + "_fp")
                legacy_value = old.get(field)
                if not reference_fp and isinstance(legacy_value, str) \
                        and legacy_value and legacy_value != _TN.STORED:
                    reference_fp = store.secret_fp(legacy_value)
                # Absence is not a wildcard. If only one legacy row carries a
                # binding, a missing redacted fingerprint cannot prove that
                # this positional candidate is that row. Opaque references
                # without a fingerprint are likewise ambiguous, not a pass.
                has_reference = bool(old.get(field + "_ref") or reference_fp)
                has_supplied = bool(supplied_fp)
                if has_reference != has_supplied or (
                        has_reference and reference_fp != supplied_fp):
                    raise ValueError("ambiguous legacy notice identity")
        entry = {"app": app, "types": types}
        if where == _TN.STORED or not where:
            # keep the existing secret pointer (or nothing, when new + empty)
            if old.get("where_ref"):
                entry["where_ref"] = old["where_ref"]
                entry["where_fp"] = old.get("where_fp") or ""
            elif isinstance(old.get("where"), str) and old["where"] \
                    and old["where"] != _TN.STORED:
                # A real value exists only in the server's old settings row.
                # Migrate it, rather than dropping what the redacted client
                # could not echo back. Never return this value to the client.
                entry["where_ref"] = fresh_ref("notice")
                entry["where_fp"] = old.get("where_fp") or store.secret_fp(old["where"])
                to_store.append((entry["where_ref"], old["where"]))
        else:
            if not where.startswith(("http://", "https://")) and \
                    app != "telegram":
                raise ValueError("the target is an http(s) URL")
            entry["where_ref"] = fresh_ref("notice")
            entry["where_fp"] = store.secret_fp(where)
            to_store.append((entry["where_ref"], where))
        entry["where"] = _TN.STORED if entry.get("where_ref") else ""
        token = str(raw.get("token") or "").strip()[:300]
        if token == _TN.STORED or not token:
            if old.get("token_ref"):
                entry["token_ref"] = old["token_ref"]
                entry["token_fp"] = old.get("token_fp") or ""
            elif isinstance(old.get("token"), str) and old["token"] \
                    and old["token"] != _TN.STORED:
                entry["token_ref"] = fresh_ref("noticetoken")
                entry["token_fp"] = old.get("token_fp") or store.secret_fp(old["token"])
                to_store.append((entry["token_ref"], old["token"]))
            if entry.get("token_ref"):
                entry["token"] = _TN.STORED
        else:
            entry["token_ref"] = fresh_ref("noticetoken")
            entry["token_fp"] = store.secret_fp(token)
            to_store.append((entry["token_ref"], token))
        if app == "webhook":
            # Generic-webhook notices are ALWAYS signed; reuse the entry's
            # secret or plan a new one, shown once
            entry["secret_ref"] = str(old.get("secret_ref")
                                      or fresh_ref("noticesign"))
            if old.get("secret_ref") and old.get("secret_fp"):
                entry["secret_fp"] = old["secret_fp"]
            else:
                secret = old.get("secret")
                if not isinstance(secret, str) or not secret or secret == _TN.STORED:
                    import secrets as _sec
                    secret = _sec.token_urlsafe(24)
                    shown.append({"index": i, "signing_secret": secret,
                                  "note": "shown once — the receiver verifies"
                                          " webhook-signature with it"})
                entry["secret_fp"] = old.get("secret_fp") or store.secret_fp(secret)
                to_store.append((entry["secret_ref"], secret))
            entry["secret"] = _TN.STORED
        entries.append(entry)
    return entries, to_store, shown


def project_patch(handler, match, body) -> None:
    """PATCH api/v1/projects/<id> — project.settings: name, settings (incl.
    notices, whose secrets are split per).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    project = _project(conn, pid)
    if project is None or project["deleted_at"]:
        handler._error(404, "no such project")
        return
    if not _require(handler, conn, actor, "edit_project", pid):
        return
    # Archived = read-only — no write lane skips this
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    import copy
    settings = json.loads(project["settings"] or "{}")
    new_settings = copy.deepcopy(settings)
    fields, args = [], []
    if "name" in req:
        fields.append("name = ?")
        args.append(str(req["name"])[:80])
    shown, secrets_to_store = [], []
    if isinstance(req.get("settings"), dict):
        for k in ("detail_visibility", "repos", "folders",
                      "description", "tasks_from"):
            if k in req["settings"]:
                new_settings[k] = req["settings"][k]
        if "description" in req["settings"]:
            new_settings["description"] = str(
                req["settings"]["description"] or "")[:4000]
        if "tasks_from" in req["settings"]:
            wire = str(req["settings"].get("tasks_from") or "").strip()
            if wire and not re.fullmatch(r"wired:[A-Za-z0-9_-]{1,120}", wire):
                handler._error(400, "outside mirror is wired:<outside-project-id>")
                return
            new_settings["tasks_from"] = wire
        if "notices" in req["settings"]:
            try:
                entries, secrets_to_store, shown = _plan_notices(
                    pid, req["settings"]["notices"],
                    settings.get("notices") or [])
            except ValueError as e:
                handler._error(400, str(e))
                return
            new_settings["notices"] = entries
        fields.append("settings = ?")
        args.append(json.dumps(new_settings, ensure_ascii=False))
    # The modules-on/off mask and the main branch, per project
    if "modules" in req:
        from .perms import ALL_MODULES
        if not isinstance(req["modules"], dict):
            handler._error(400, "modules is an object: {name: true/false}")
            return
        for mod in req["modules"]:
            if mod not in ALL_MODULES:
                handler._error(400, f"unknown module {mod!r} — the set is "
                                    + ", ".join(ALL_MODULES))
                return
        fields.append("modules = ?")
        args.append(json.dumps({m: bool(v) for m, v in req["modules"].items()}))
    if "main_branch" in req:
        fields.append("main_branch = ?")
        args.append(str(req["main_branch"]).strip()[:120])
    if not fields:
        handler._error(400, "nothing to change")
        return
    with store.write(conn):
        for key, value in secrets_to_store:
            store.secret_put(conn, key, value)
        args.append(pid)
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?",
                     args)
        src, ev = logmod.server_event()
        from .. import team_notices as _TN
        logmod.append(conn, pid, src, ev, "settings.changed", project["slug"],
                      actor["person_id"],
                      {"old": _TN.redact_settings(settings),
                       "new": _TN.redact_settings(new_settings)})
    out: dict = {"ok": True}
    if shown:                                        # Shown once
        out["secrets"] = shown
    handler._json(200, out)


def member_put(handler, match, body) -> None:
    """PUT /api/v1/projects/<id>/members/<person> — project.members."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid, person_id = int(match.group(1)), int(match.group(2))
    if not _require(handler, conn, actor, "manage_members", pid):
        return
    # Archived = read-only — no write lane skips this
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    role = str(req.get("role") or "Member")       # The role's NAME
    legacy_map = {"viewer": "Reader", "member": "Member",
                  "manager": "Project admin"}
    named = legacy_map.get(role.lower(), role)
    row = conn.execute("SELECT id, builtin FROM roles WHERE name = ?",
                       (named,)).fetchone()
    if row is None:
        handler._error(400, "role is a name from /api/v1/roles (Reader, "
                            "Member, Project admin, or a custom one)")
        return
    if not _require(handler, conn, actor, "manage_members", pid):
        return
    person = conn.execute("SELECT name FROM people WHERE id = ?",
                          (person_id,)).fetchone()
    if person is None:
        handler._error(404, "no such person")
        return
    text_role = {"Reader": "viewer", "Member": "member",
                 "Project admin": "manager"}.get(named, "member")   # Legacy
                                                                      # column
    with store.write(conn):
        conn.execute(
            "INSERT INTO project_members (project, person, role, role_id)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(project, person) DO UPDATE SET role = excluded.role,"
            " role_id = excluded.role_id",
            (pid, person_id, text_role, row["id"]))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "project.member_changed",
                      person["name"], actor["person_id"],
                      {"role": role, "by": actor["name"]})
    handler._json(200, {"ok": True})


def member_access_put(handler, match, body) -> None:
    """PUT /api/v1/projects/<id>/members/<person>/access — this person's own
    switches in this project: {"overrides": {"tree": false, "chat": null}}.
    false = off for them, true = on although the role doesn't give it, null =
    back to what the role gives. Their role and everyone else stay as they are."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid, person_id = int(match.group(1)), int(match.group(2))
    if not _require(handler, conn, actor, "manage_members", pid):
        return
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None or not isinstance(req, dict) \
            or not isinstance(req.get("overrides"), dict):
        handler._error(400, _je or 'send {"overrides": {area: true|false|null}}')
        return
    from .perms import AREAS, overrides_of
    row = conn.execute(
        "SELECT m.overrides, p.name FROM project_members m JOIN people p"
        " ON p.id = m.person WHERE m.project = ? AND m.person = ?",
        (pid, person_id)).fetchone()
    if row is None:
        handler._error(404, "that person isn't in this project")
        return
    cur = overrides_of(row)
    for area, v in req["overrides"].items():
        if area not in AREAS:
            handler._error(400, f"unknown area {area!r} — one of {', '.join(AREAS)}")
            return
        if v is None:
            cur.pop(area, None)
        elif isinstance(v, bool):
            cur[area] = v
        else:
            handler._error(400, f"{area}: true, false or null")
            return
    with store.write(conn):
        conn.execute("UPDATE project_members SET overrides = ? WHERE project = ?"
                     " AND person = ?", (json.dumps(cur), pid, person_id))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "project.member_access",
                      row["name"], actor["person_id"],
                      {"overrides": cur, "by": actor["name"]})
    handler._json(200, {"ok": True, "overrides": cur})


def member_delete(handler, match, body) -> None:
    """DELETE /api/v1/projects/<id>/members/<person> — project.members."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid, person_id = int(match.group(1)), int(match.group(2))
    if not _require(handler, conn, actor, "manage_members", pid):
        return
    # Archived = read-only — no write lane skips this
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    person = conn.execute("SELECT name FROM people WHERE id = ?",
                          (person_id,)).fetchone()
    if person is None:
        handler._error(404, "no such person")
        return
    with store.write(conn):
        conn.execute("DELETE FROM project_members WHERE project = ?"
                     " AND person = ?", (pid, person_id))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "project.member_changed",
                      person["name"], actor["person_id"],
                      {"removed": True, "by": actor["name"]})
    handler._json(200, {"ok": True})


def statuses_list(handler, match, body) -> None:
    """GET /api/v1/projects/<id>/statuses — task.read; types are fixed."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    if not _visible(conn, actor, pid):
        handler._error(404, "no such project")
        return
    rows = conn.execute(
        "SELECT key, type, name, position, colour FROM statuses WHERE project = ?"
        " ORDER BY position", (pid,)).fetchall()
    handler._json(200, {"statuses": [dict(r) for r in rows]})


def status_patch(handler, match, body) -> None:
    from .schema import status_colour
    """PATCH status name/position/colour; the status TYPE never changes.
    A move is one transaction that renumbers this project's rows only."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid, key = int(match.group(1)), match.group(2)
    if not _require(handler, conn, actor, "edit_project", pid):
        return
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    row = conn.execute("SELECT * FROM statuses WHERE project = ? AND key = ?",
                       (pid, key)).fetchone()
    if row is None:
        handler._error(404, "no such status")
        return
    old = {"name": row["name"], "position": row["position"],
           "colour": row["colour"] or ""}
    name = (str(req["name"]).strip()[:40] if "name" in req else row["name"])
    if not name:
        handler._error(400, "give the status a name")
        return
    colour = (req.get("colour", old["colour"]) or
              status_colour(row["type"]))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(colour)):
        handler._error(400, "colour is #RRGGBB")
        return
    if "position" in req and not isinstance(req["position"], int):
        handler._error(400, "position is an integer")
        return
    pos = req.get("position", row["position"])
    if isinstance(pos, bool) or pos < 0:
        handler._error(400, "position is zero or a positive integer")
        return
    rows = conn.execute("SELECT key, position FROM statuses WHERE project = ?"
                        " ORDER BY position, rowid", (pid,)).fetchall()
    if pos >= len(rows):
        handler._error(400, f"position is between 0 and {len(rows) - 1}")
        return
    new = {"name": name, "position": pos, "colour": str(colour).lower()}
    # Insert the changed row at its new position and compact EVERY row of this
    # one project. Legacy duplicate positions cannot poison later moves.
    ordered = [r["key"] for r in rows if r["key"] != key]
    ordered.insert(pos, key)
    with store.write(conn):
        conn.execute("UPDATE statuses SET name = ?, colour = ?"
                     " WHERE project = ? AND key = ?", (name, new["colour"], pid, key))
        for idx, status_key in enumerate(ordered):
            conn.execute("UPDATE statuses SET position = ? WHERE project = ?"
                         " AND key = ?", (idx, pid, status_key))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "settings.changed", f"status:{key}",
                      actor["person_id"], {"old": old, "new": new})
    handler._json(200, {"ok": True, "status": dict(conn.execute(
        "SELECT key, type, name, position, colour FROM statuses"
        " WHERE project = ? AND key = ?", (pid, key)).fetchone())})

def project_archive(handler, match, body) -> None:
    """POST api/v1/projects/<id>/archive — read-only afterwards; {unarchive: true}
    brings it back. Logged.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    if not _require(handler, conn, actor, "edit_project", pid):
        return
    _req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    unarchive = bool(_req.get("unarchive"))
    with store.write(conn):
        cur = conn.execute(
            "UPDATE projects SET archived_at = ? WHERE id = ?",
            (None if unarchive else store.now(), pid))
        if cur.rowcount != 1:
            handler._error(404, "no such project")
            return
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "settings.changed", "archived",
                      actor["person_id"], {"unarchive": unarchive})
    handler._json(200, {"ok": True, "archived": not unarchive})
