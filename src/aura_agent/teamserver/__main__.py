"""`python -m aura_agent.teamserver run` — the server process (plan).

Also `--tls-cert/--tls-key` (the team's real certificate, online version) and
`--behind-proxy` (127.0.0.1 only, plain HTTP, the proxy does TLS —). Default
port 7443; if taken, the next free one in 7443–7463.
"""
from __future__ import annotations

import argparse
import socket
import sys

from . import PROTOCOL

DEFAULT_PORT = 7443
PORT_RANGE = range(7443, 7464)


def free_port(preferred: int = DEFAULT_PORT, host: str = "0.0.0.0") -> int:
    """7443, or the next free one in 7443–7463."""
    for port in [preferred] + [p for p in PORT_RANGE if p != preferred]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise OSError("no free port in 7443–7463")




# Nothing else is served as a static. The old vanilla page is gone; the team
# page lives at team/ (http.py's _team_page lane reads out of the built
# webui/team dist). "/" forwards there — the same place the browser would look
# for the shell.
STATIC_FILES = {}


def _instrumented(method: str, fn):
    """1. RESPONSE METRICS — every routed API request's status code is counted
    (http_note) from the response path via a send_response sniff; a handler
    that raises counts 500 (http.py then turns it into the 500 response).
    Static files, 404s with no route and rate-limited opens never reach a
    handler and are not counted — this meter means "the API". 2. POST-COMMIT
    DISPATCH — after every successful non-GET handler, the api_log dispatcher
    pushes new log/chat rows to notices, webhooks and linked rooms. This is why
    api_chat and api_views never needed to learn about the outside: a chat post
    relays to linked rooms from here, and an overlap row collected by
    reports_in's own dispatch is exactly the same code path.
    """
    def go(handler, match, body):
        status = [500]
        orig = handler.send_response

        def sniff(code, *a, **k):
            try:
                status[0] = int(code)
            except Exception:                        # noqa: BLE001
                pass
            return orig(code, *a, **k)
        handler.send_response = sniff
        try:
            return fn(handler, match, body)
        finally:
            handler.send_response = orig
            try:
                from .api_monitor import http_note
                http_note(status[0])
            except Exception:                        # noqa: BLE001
                pass
            if method != "GET":
                # Outside sends ride the background lane only — the request's
                # answer never waits on a webhook (a slow outside hub must not
                # drag this hub's response)
                import threading
                threading.Thread(
                    target=_dispatch_outside, daemon=True,
                    name="aura-dispatch-outside").start()
    return go


def _dispatch_outside() -> None:
    """The post-commit outside-send lane: notices, webhooks and linked-room relays
    pushed off the request thread.
    """
    import threading                              # Honor self own loop
    try:
        from .api_log import dispatch_pending
        from .teamserver_db import open_team
        conn = open_team()
        if conn is not None:
            dispatch_pending(conn)
    except (OSError, Exception):                    # noqa: BLE001
        pass


def build_routes() -> list:
    """The route table — anchored regexes; later phases append."""
    import re
    from .api_avatars import (avatar_me, avatar_photo_delete,
                              avatar_photo_get, avatar_photo_put,
                              avatars_list)
    from .api_people import (
        aura_device, device_delete, devices_list, health, hub_lan, info, me,
        me_password, me_permissions,
        roles_route_create, roles_route_delete, roles_route_list, roles_route_patch,
        people_create, people_list, people_me, people_patch, session_delete,
        sessions_list, signin, signout)
    from .api_hub import hub_address, hub_bundle
    from .api_log import log_head, log_verify, reports_in
    from .api_views import (activity, who as who_view,
                            tree as tree_view, gantt,
                            diagram, digest)
    from .api_map import map_get, map_put
    from .api_mirror import mirror_get, mirror_put
    from .api_hub import backups_list, backups_now
    from .api_monitor import (metrics, metrics_key, webhook_delete,
                              webhook_list, webhook_subscribe, export_log)
    from .api_chat import (delete as chat_delete,
                           edit as chat_edit, history,
                           make_dm as chat_dm, make_room as chat_room,
                           post as chat_post, read as chat_read,
                           react as chat_react, rooms,
                           search as chat_search, typing as chat_typing,
                           upload as chat_upload,
                           file_dl as chat_file_dl,
                           presence as chat_presence)
    from .api_projects import (
        list_projects, member_access_put, member_delete, member_put, project_archive,
        project_create, project_patch, status_patch, statuses_list)
    from .api_tasks import (
        comment_add, comment_delete, comment_patch, comments_list, link_delete,
        link_put, list_tasks, task_create, task_delete, task_get_alias,
        task_patch)
    from .chatlinks import (link_create, link_delete as chatlink_delete,
                            link_ingest, links_list, pull_link)
    from .. import team_notices as _team_notices
    D = "signed"
    A = "any"          # Signed or session
    S = "session"
    O = "open"
    routes = [
        # Each route says its gate ("open" | "person"), the permission, the
        # module it gates under, and the project source: None | ("path", group)
        # | ("query","project") | ("body", key) | ("task", group) | "any" (any
        # membership). The dispatcher checks BEFORE the handler; a session and
        # a device of the same person get the same answer.
        ("GET", re.compile(r"^/api/v1/health$"), health, "open", None, None, None),
        ("GET", re.compile(r"^/api/v1/info$"), info, "open", None, None, None),
        ("GET", re.compile(r"^/api/v1/hub/lan$"), hub_lan, "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/signin$"), signin, "open", None, None, None),
        ("POST", re.compile(r"^/api/v1/aura/device$"), aura_device, "open", None, None, None),
        ("GET", re.compile(r"^/api/v1/me$"), me, "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/me/password$"), me_password,
         "person", None, None, None),
        ("GET", re.compile(r"^/api/v1/me/permissions$"), me_permissions,
         "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/signout$"), signout, "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/people$"), people_create,
         "person", "people.manage", None, None),
        ("PATCH", re.compile(r"^/api/v1/team/settings$"),
         team_settings_patch, "person", "team.manage", None, None),
        ("GET", re.compile(r"^/api/v1/people$"), people_list,
         "person", "view_members", "members", "any"),
        ("PATCH", re.compile(r"^/api/v1/people/me$"), people_me,
         "person", None, None, None),
        ("GET", re.compile(r"^/api/v1/avatars$"), avatars_list,
         "person", None, None, None),
        ("PATCH", re.compile(r"^/api/v1/people/me/avatar$"), avatar_me,
         "person", None, None, None),
        ("PUT", re.compile(r"^/api/v1/people/me/avatar/photo$"), avatar_photo_put,
         "person", None, None, None),
        ("DELETE", re.compile(r"^/api/v1/people/me/avatar/photo$"), avatar_photo_delete,
         "person", None, None, None),
        ("GET", re.compile(r"^/api/v1/people/(\d+)/avatar$"), avatar_photo_get,
         "person", None, None, None),
        ("PATCH", re.compile(r"^/api/v1/people/(\d+)$"), people_patch,
         "person", "people.manage", None, None),
        ("GET", re.compile(r"^/api/v1/roles$"), roles_route_list,
         "person", "people.manage", None, None),
        ("POST", re.compile(r"^/api/v1/roles$"), roles_route_create,
         "person", "team.manage", None, None),
        ("DELETE", re.compile(r"^/api/v1/roles/(\d+)$"), roles_route_delete,
         "person", "team.manage", None, None),
        ("PATCH", re.compile(r"^/api/v1/roles/(\d+)$"), roles_route_patch,
         "person", "team.manage", None, None),
        ("GET", re.compile(r"^/api/v1/devices$"), devices_list,
         "person", None, None, None),
        ("DELETE", re.compile(r"^/api/v1/devices/([a-z0-9-]{1,64})$"),
         device_delete, "person", None, None, None),
        ("GET", re.compile(r"^/api/v1/sessions$"), sessions_list,
         "person", None, None, None),
        ("DELETE", re.compile(r"^/api/v1/sessions/([a-f0-9]{12})$"),
         session_delete, "person", None, None, None),
        ("GET", re.compile(r"^/api/v1/hub/bundle$"), hub_bundle,
         "person", "hub.handover", None, None),
        ("GET", re.compile(r"^/api/v1/hub/address$"), hub_address,
         "person", None, None, None),
        ("GET", re.compile(r"^/api/v1/log/verify$"), log_verify,
         "person", "manage_activity", "history", "any"),
        ("GET", re.compile(r"^/api/v1/log/head$"), log_head,
         "person", "view_activity", "history", "any"),
        ("POST", re.compile(r"^/api/v1/reports$"), reports_in,
         "person", "aura_work", "aura_work", "any"),
        ("GET", re.compile(r"^/api/v1/chat/rooms$"), rooms,
         "person", "view_messages", "chat", "any"),
        ("GET", re.compile(r"^/api/v1/chat/history$"), history,
         "person", "view_messages", "chat", "any"),
        ("GET", re.compile(r"^/api/v1/chat/search$"), chat_search,
         "person", "view_messages", "chat", "any"),
        ("GET", re.compile(r"^/api/v1/presence$"), chat_presence,
         "person", "view_members", "members", "any"),
        ("GET", re.compile(r"^/api/v1/stream$"), stream, "person", None, None, None),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/activity$"),
         activity, "person", "view_activity", "history", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/who$"),
         who_view, "person", "view_activity", "now", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/tree$"),
         tree_view, "person", "browse_repository", "tree", ("path", 1)),
        ("PUT", re.compile(r"^/api/v1/projects/(\d+)/map$"), map_put,
         "person", "aura_work", "aura_work", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/map$"), map_get,
         "person", "browse_repository", "tree", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/gantt$"), gantt,
         "person", "view_work_packages", "flow", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/diagram$"), diagram,
         "person", "view_work_packages", "flow", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/digest$"), digest,
         "person", "view_activity", "history", ("path", 1)),
        ("PUT", re.compile(r"^/api/v1/projects/(\d+)/mirror$"), mirror_put,
         "person", "aura_work", "aura_work", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/mirror$"), mirror_get,
         "person", "view_work_packages", "board", ("path", 1)),
        ("GET", re.compile(r"^/metrics$"), metrics, "open", None, None, None),
        ("POST", re.compile(r"^/api/v1/metrics/key$"), metrics_key,
         "person", "team.manage", None, None),
        ("POST", re.compile(r"^/api/v1/webhooks$"), webhook_subscribe,
         "person", "team.manage", None, None),
        ("DELETE", re.compile(r"^/api/v1/webhooks/(\d+)$"), webhook_delete,
         "person", "team.manage", None, None),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/export$"), export_log,
         "person", "export_activity", "history", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/backups$"), backups_list,
         "person", "team.manage", None, None),
        ("POST", re.compile(r"^/api/v1/backups$"), backups_now,
         "person", "team.manage", None, None),
        ("GET", re.compile(r"^/api/v1/chat/file/([0-9a-f]{6,})$"),
         chat_file_dl, "person", "view_messages", "chat", "any"),
        ("POST", re.compile(r"^/api/v1/chat/post$"), chat_post,
         "person", "add_messages", "chat", ("body", "room")),
        ("POST", re.compile(r"^/api/v1/chat/edit$"), chat_edit,
         "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/chat/delete$"), chat_delete,
         "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/chat/react$"), chat_react,
         "person", "add_messages", "chat", ("body", "room")),
        ("POST", re.compile(r"^/api/v1/chat/read$"), chat_read,
         "person", "view_messages", "chat", "any"),
        ("POST", re.compile(r"^/api/v1/chat/typing$"), chat_typing,
         "person", "add_messages", "chat", "any"),
        ("POST", re.compile(r"^/api/v1/chat/room$"), chat_room,
         "person", "edit_project", "settings", ("body", "project")),
        ("POST", re.compile(r"^/api/v1/chat/dm$"), chat_dm,
         "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/chat/upload$"), chat_upload,
         "person", "add_work_package_attachments", "board", "any"),
        ("GET", re.compile(r"^/api/v1/projects$"), list_projects,
         "person", None, None, None),
        ("POST", re.compile(r"^/api/v1/projects$"), project_create,
         "person", "project.create", None, None),
        ("PATCH", re.compile(r"^/api/v1/projects/(\d+)$"), project_patch,
         "person", "edit_project", "settings", ("path", 1)),
        ("PUT", re.compile(r"^/api/v1/projects/(\d+)/members/(\d+)$"),
         member_put, "person", "manage_members", "members", ("path", 1)),
        ("PUT", re.compile(r"^/api/v1/projects/(\d+)/members/(\d+)/access$"),
         member_access_put, "person", "manage_members", "members", ("path", 1)),
        ("DELETE", re.compile(r"^/api/v1/projects/(\d+)/members/(\d+)$"),
         member_delete, "person", "manage_members", "members", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/statuses$"),
         statuses_list, "person", "view_work_packages", "board", ("path", 1)),
        ("PATCH", re.compile(r"^/api/v1/projects/(\d+)/statuses/([a-z0-9_-]+)$"),
         status_patch, "person", "edit_project", "settings", ("path", 1)),
        ("POST", re.compile(r"^/api/v1/projects/(\d+)/archive$"),
         project_archive, "person", "archive_project", "settings", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/tasks$"), list_tasks,
         "person", "view_work_packages", "board", ("query", "project")),
        ("POST", re.compile(r"^/api/v1/tasks$"), task_create,
         "person", "add_work_packages", "board", ("body", "project")),
        ("GET", re.compile(r"^/api/v1/tasks/(\d+)$"), task_get_alias,
         "person", "view_work_packages", "board", ("task", 1)),
        ("PATCH", re.compile(r"^/api/v1/tasks/(\d+)$"), task_patch,
         "person", "edit_work_packages", "board", ("task", 1)),
        ("DELETE", re.compile(r"^/api/v1/tasks/(\d+)$"), task_delete,
         "person", "delete_work_packages", "board", ("task", 1)),
        ("PUT", re.compile(r"^/api/v1/tasks/(\d+)/links/([a-z_]+)/(\d+)$"),
         link_put, "person", "manage_work_package_relations", "board",
         ("task", 1)),
        ("DELETE", re.compile(r"^/api/v1/tasks/(\d+)/links/([a-z_]+)/(\d+)$"),
         link_delete, "person", "manage_work_package_relations", "board",
         ("task", 1)),
        ("GET", re.compile(r"^/api/v1/tasks/(\d+)/comments$"), comments_list,
         "person", "view_work_packages", "board", ("task", 1)),
        ("POST", re.compile(r"^/api/v1/tasks/(\d+)/comments$"), comment_add,
         "person", "add_work_package_comments", "board", ("task", 1)),
        ("PATCH", re.compile(r"^/api/v1/comments/(\d+)$"), comment_patch,
         "person", "edit_own_messages", "chat", "any"),
        ("DELETE", re.compile(r"^/api/v1/comments/(\d+)$"), comment_delete,
         # The handler below enforces authorship. Ordinary members have
         # edit-own here; delete_messages is a separate moderator-style
         # capability and must not be granted merely to let someone delete
         # their own comment.
         "person", "edit_own_messages", "chat", "any"),
        # Linked rooms + the / read-backs; ingest is the open inbound (shared
        # secret + Standard-Webhooks signature)
        ("POST", re.compile(r"^/api/v1/projects/(\d+)/links$"), link_create,
         "person", "edit_project", "settings", ("path", 1)),
        ("GET", re.compile(r"^/api/v1/projects/(\d+)/links$"), links_list,
         "person", "view_messages", "chat", ("path", 1)),
        ("DELETE", re.compile(r"^/api/v1/projects/(\d+)/links/(\d+)$"),
         chatlink_delete, "person", "edit_project", "settings", ("path", 1)),
        ("POST", re.compile(r"^/api/v1/projects/(\d+)/links/(\d+)/pull$"),
         pull_link, "person", "edit_project", "settings", ("path", 1)),
        ("POST", re.compile(r"^/api/v1/links/(\d+)/ingest$"), link_ingest,
         "open", None, None, None),
        ("GET", re.compile(r"^/api/v1/webhooks$"), webhook_list,
         "person", "team.manage", None, None),
    ]
    # The tuple widened to (method, rx, handler, gate, perm, module, pspec);
    # the instrumentation wrapper reads merely the method — the gate metadata
    # passes untouched to the dispatcher.
    return [(m, rx, _instrumented(m, h), gate, perm, module, pspec)
            for m, rx, h, gate, perm, module, pspec in routes]


TEAM_SETTINGS_KEYS = ("chat_enabled",)


def team_settings_patch(handler, match, body) -> None:
    """PATCH api/v1/team/settings — the team's visibility switches; this is the
    chat off switch promises ("a team that uses another chat app can switch
    ours off") — nothing else could set `team.chat_enabled`, which
    api_chat._chat_on reads live (off → the chat routes 404).

    An Aura's signed request or the Admin tab's browser session — with device
    headers the signature is verified and a failed verify has ALREADY sent its
    401 (no session fall-through,; api_hub's _backups_actor rule); the session
    path carries the same-origin Origin check on state changes. Allowed keys:
    chat_enabled — stored as settings key 'team.chat_enabled'; unknown keys are
    refused, never silently dropped. Every flip is a settings.changed link on
    the team chain (: settings changes are logged).
    """
    import json as _json
    from . import log as logmod
    from . import store
    from .api_people import _actor, _db
    from .perms import can
    conn = _db()
    # 's one-resolve: the dispatcher's gate already verified this request
    # (device nonce or session) — re-verifying reads as a replay
    actor = _actor(conn, handler, body or b"", need="any")
    if actor is None:
        return                               # ONE error already sent
    if not can(conn, actor, "team.manage"):
        handler._error(403, "only the owner or an admin change team "
                            "settings")
        return
    from .http import parse_json as _parse_json
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    if not isinstance(req, dict) or not req:
        handler._error(400, "send the settings to change — the only one "
                            "here is 'chat_enabled' (true/false)")
        return
    unknown = sorted(k for k in req if k not in TEAM_SETTINGS_KEYS)
    if unknown:
        handler._error(400, "unknown team setting: "
                            + ", ".join(str(k)[:40] for k in unknown)
                            + " — the only one here is 'chat_enabled'")
        return
    changes = {}
    if "chat_enabled" in req:
        if not isinstance(req["chat_enabled"], bool):
            handler._error(400, "chat_enabled is true or false")
            return
        changes["team.chat_enabled"] = req["chat_enabled"]
    old = {}
    with store.write(conn):
        for key, value in changes.items():
            row = conn.execute("SELECT value FROM settings WHERE key = ?",
                               (key,)).fetchone()
            try:
                old[key] = _json.loads(row["value"]) if row else True
            except Exception:                    # noqa: BLE001
                old[key] = True          # Unreadable = the default (on)
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _json.dumps(value)))
        src, ev = logmod.server_event()
        logmod.append(conn, None, src, ev, "settings.changed", "team",
                      actor["person_id"], {"old": old, "new": changes})
    handler._json(200, {"ok": True, "settings": {
        k.split(".", 1)[1]: v for k, v in changes.items()}})


def run(args) -> int:
    try:
        from .. import running as _running
        _running.register("hub")
    except Exception:                                    # noqa: BLE001
        pass
    return _run(args)


def _run(args) -> int:
    from .http import Handler, make_server, serve_forever
    from . import tls as TL
    from .teamserver_db import team_dir, db_path
    from . import store

    # Existing hubs must be upgraded by the normal server startup, not only by
    # setup steps or test fixtures. Do this before accepting any requests,
    # and never close/reuse the per-request thread-local connection cache.
    path = db_path()
    if path.exists():
        conn = store.connect(path)
        try:
            store.migrate(conn)
        finally:
            conn.close()

    host = "127.0.0.1" if args.behind_proxy else args.host
    fp = ""
    if args.behind_proxy:
        ctx = None                               # The proxy does TLS
    elif args.tls_cert and args.tls_key:
        ctx = TL.server_context(args.tls_cert, args.tls_key)
        fp = TL.fingerprint(open(args.tls_cert, "rb").read())
    else:
        cert_p, key_p, fp = TL.ensure_cert(team_dir())
        ctx = TL.server_context(cert_p, key_p)
    port = free_port(args.port, host)
    Handler.routes = build_routes()
    Handler.static = dict(STATIC_FILES)      # The page's allow-list
    Handler.server_dir = str(team_dir())
    srv = make_server((host, port), Handler, ctx, fingerprint=fp)
    import secrets as _secrets
    run_id = _secrets.token_hex(8)
    Handler.run_id = run_id
    try:
        import json as _j
        (team_dir() / "hub.lock").write_text(_j.dumps(
            {"port": port, "pid": __import__("os").getpid(),
             "run_id": run_id}))
    except OSError:
        pass
    print(f"team server on {host}:{port} (protocol {PROTOCOL})", flush=True)
    # The pre-Teams-era chat database hops in ONCE, at first hub run
    try:
        from . import migrate_chat as _MC
        from .teamserver_db import db_path, team_dir
        old_chat = team_dir() / "chat.db"      # What the old store was called
        if old_chat.exists():
            from .teamserver_db import open_team
            _c = open_team()
            if _c is not None:
                try:
                    out = _MC.migrate_chat(_c, str(old_chat))
                    if out.get("rooms") or out.get("msgs"):
                        print(f"[team-chat] migrated the old chat in: "
                              f"{out}", flush=True)
                finally:
                    _c.close()
    except (OSError, Exception) as _e:           # noqa: BLE001
        print(f"[team-chat] the old-chat move didn't run: {_e}", flush=True)
    import threading
    stop = threading.Event()
    daemons = [
        threading.Thread(target=_dispatch_loop, args=(stop,),
                         name="aura-dispatch", daemon=True),
        threading.Thread(target=_webhook_retry_loop, args=(stop,),
                         name="aura-webhook-retries", daemon=True),
        threading.Thread(target=_links_poll_loop, args=(stop,),
                         name="aura-links-poll", daemon=True),
    ]
    for t in daemons:
        t.start()
    try:
        serve_forever(srv)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


# ------------------------------------------- background jobs --
DISPATCH_TICK = 3.0          # Catch-up: anything a request wrapper missed
RETRY_TICK = 15.0            # Back-off runner cadence
LINKS_POLL_TICK = 10.0       # read_since polling for wired apps


def _bg_conn():
    from .teamserver_db import open_team
    return open_team()


def _dispatch_loop(stop) -> None:
    """Catch-up dispatcher: the request path dispatches synchronously after
    every write; this sweeps what a crash or a non-HTTP write left (and
    retries notices whose sends failed — sweep_unsent + the outbox)."""
    while not stop.is_set():
        try:
            conn = _bg_conn()
            if conn is not None:
                from .api_log import dispatch_pending
                from .. import team_notices
                dispatch_pending(conn)
                team_notices.sweep_unsent(conn)
        except Exception:                            # noqa: BLE001
            pass
        stop.wait(DISPATCH_TICK)


def _webhook_retry_loop(stop) -> None:
    """Retries, journaled in webhook_log, honoring RETRY_DELAYS."""
    while not stop.is_set():
        try:
            conn = _bg_conn()
            if conn is not None:
                from . import api_monitor
                api_monitor.retry_due(conn)
        except Exception:                            # noqa: BLE001
            pass
        stop.wait(RETRY_TICK)


def _links_poll_loop(stop) -> None:
    """Inbound where an app's wiring knows read_since (the generic webhook's
    inbound is the links/<id>/ingest endpoint instead).
    """
    while not stop.is_set():
        try:
            conn = _bg_conn()
            if conn is not None:
                from . import chatlinks
                chatlinks.poll_links(conn)
        except Exception:                            # noqa: BLE001
            pass
        stop.wait(LINKS_POLL_TICK)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aura_agent.teamserver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run the team server (blocking)")
    run_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    run_p.add_argument("--host", default="0.0.0.0")
    run_p.add_argument("--tls-cert", default=None,
                       help="the team's real certificate (online version)")
    run_p.add_argument("--tls-key", default=None)
    run_p.add_argument("--behind-proxy", action="store_true",
                       help="listen on 127.0.0.1 only, plain HTTP; the "
                            "proxy does TLS")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    return 2





def stream(handler, match, body) -> None:
    """GET api/v1/stream — THE one live stream: subscribe on the broadcaster PER
    CONNECTION (a person's tabs never starve each other), replay the buffer
    (Last-Event-ID) filtered per subscriber exactly like live delivery — a
    reconnecting guest receives nothing it may not see — pump the queue; ping
    every 20 s; unsubscribe on disconnect. Presence is published on the
    online/offline transitions.
    """
    from . import teamserver_db
    from .sse import format_event, ping as sse_ping
    import queue as _q
    import time as _t
    conn = teamserver_db.open_team()
    if conn is None:
        handler._error(404, "no team yet")
        return
    from .api_people import _actor, _presence_publish
    actor = _actor(conn, handler, b"", need="any")
    if actor is None:
        return
    pid = actor.get("person_id")
    bc = teamserver_db.broadcaster()
    # Register live delivery and author the replay snapshot as ONE handoff.
    # A publish between those operations would otherwise enter both lanes.
    # Each operation still enforces its own connection/person filtering.
    with bc._lock:
        q, _last = bc.subscribe(pid)
        hdr = handler.headers.get("Last-Event-ID")
        replay = bc.replay(int(hdr), pid) if hdr and hdr.isdigit() else []
    if pid is not None and bc.connection_count(pid) == 1:
        _presence_publish(conn, pid, "online")   # The first stream opens
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Aura-Protocol", "1")
    handler.end_headers()
    if replay is None:
        handler.wfile.write(b"event: reset\ndata: {}\n\n")
    else:
        for eid, project, kind, data in replay:
            handler.wfile.write(format_event(eid, kind, data))
    try:
        handler.wfile.flush()
        last_ping = _t.time()
        while True:
            try:
                eid, project, kind, data = q.get(timeout=1)
                handler.wfile.write(format_event(eid, kind, data))
                handler.wfile.flush()
            except _q.Empty:
                if _t.time() - last_ping >= 20:
                    handler.wfile.write(sse_ping())
                    handler.wfile.flush()
                    last_ping = _t.time()
                    # Dropped sub (queue.Full somewhere upstream) must not ping
                    # on silently — close the stream; EventSource re-opens
                    # clean
                    if q.conn_key not in bc.subs.person_of:
                        break
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        if bc.unsubscribe(q) == 0:               # The last stream closed
            _presence_publish(conn, pid, "offline")


if __name__ == "__main__":
    sys.exit(main())
