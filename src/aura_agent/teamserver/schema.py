"""teamserver/schema.py — every CREATE statement, one string per version.

Version 1 (plan) is the whole store: 19 tables, 2 triggers, 3 indexes. Chat
tables arrive with version 2; later phases add versions 3+. An applied version
is NEVER edited — a new change is a new version (migrate is forward only).
"""
from __future__ import annotations

V1 = """
CREATE TABLE team (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL,
  created REAL NOT NULL, cert_fp TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}');
CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display TEXT NOT NULL DEFAULT '', role TEXT NOT NULL
  CHECK (role IN ('owner','admin','member','guest')),
  browser_only INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, disabled_at REAL);
CREATE TABLE devices (id TEXT PRIMARY KEY, person INTEGER NOT NULL REFERENCES people(id),
  name TEXT NOT NULL, pubkey BLOB NOT NULL UNIQUE, created REAL NOT NULL,
  last_seen REAL, revoked_at REAL);
CREATE TABLE sessions (token_hash BLOB PRIMARY KEY, person INTEGER NOT NULL,
  approved_by TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
  revoked_at REAL, agent TEXT NOT NULL DEFAULT '');
CREATE TABLE nonces (nonce TEXT PRIMARY KEY, device TEXT NOT NULL, seen REAL NOT NULL);
CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL, created REAL NOT NULL, created_by INTEGER NOT NULL,
  settings TEXT NOT NULL DEFAULT '{}', archived_at REAL, deleted_at REAL);
CREATE TABLE project_members (project INTEGER NOT NULL REFERENCES projects(id),
  person INTEGER NOT NULL REFERENCES people(id),
  role TEXT NOT NULL CHECK (role IN ('manager','member','viewer')),
  PRIMARY KEY (project, person));
CREATE TABLE statuses (project INTEGER NOT NULL, key TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('backlog','todo','in_progress','in_review','done','canceled')),
  name TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY (project, key));
CREATE TABLE tasks (id INTEGER PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id),
  number INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, start REAL, due REAL,
  created_by INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
  version INTEGER NOT NULL DEFAULT 1, merged_at REAL, merged_by INTEGER,
  deleted_at REAL, UNIQUE (project, number));
CREATE TABLE task_assignees (task INTEGER NOT NULL, person INTEGER NOT NULL,
  PRIMARY KEY (task, person));
CREATE TABLE task_labels (task INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY (task, label));
CREATE TABLE task_links (task INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('blocked_by','relates_to','parent')),
  other INTEGER NOT NULL, PRIMARY KEY (task, kind, other));
CREATE TABLE comments (id INTEGER PRIMARY KEY, task INTEGER NOT NULL, person INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('text','start','done','decision','system')),
  body TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL,
  edited REAL, deleted_at REAL);
CREATE TABLE log (seq INTEGER PRIMARY KEY, project INTEGER, source TEXT NOT NULL,
  ev_id TEXT NOT NULL, type TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
  actor INTEGER, time REAL NOT NULL, data TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL, UNIQUE (source, ev_id));
CREATE TRIGGER log_no_update BEFORE UPDATE ON log BEGIN SELECT RAISE(ABORT, 'the log is append-only'); END;
CREATE TRIGGER log_no_delete BEFORE DELETE ON log BEGIN SELECT RAISE(ABORT, 'the log is append-only'); END;
CREATE TABLE log_heads (chain TEXT PRIMARY KEY, seq INTEGER NOT NULL, hash TEXT NOT NULL);
CREATE TABLE map_files (project INTEGER NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
  lang TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', rank REAL NOT NULL DEFAULT 0,
  commit_id TEXT NOT NULL DEFAULT '', by_person INTEGER, updated REAL NOT NULL,
  PRIMARY KEY (project, path));
CREATE TABLE map_symbols (project INTEGER NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
  name TEXT NOT NULL, signature TEXT NOT NULL DEFAULT '', line INTEGER NOT NULL DEFAULT 0,
  rank REAL NOT NULL DEFAULT 0);
CREATE INDEX map_symbols_file ON map_symbols (project, path);
CREATE INDEX log_project ON log (project, seq);
CREATE INDEX tasks_project ON tasks (project, status);
"""

V2 = """
-- chat lives in the team server (forward-only; V1 never edited).
CREATE TABLE rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                -- project | room | dm
  name TEXT NOT NULL,
  project INTEGER REFERENCES projects(id),
  made_by INTEGER REFERENCES people(id),
  ts REAL NOT NULL);
CREATE TABLE chat_members (
  room INTEGER NOT NULL REFERENCES rooms(id),
  person INTEGER NOT NULL REFERENCES people(id),
  PRIMARY KEY (room, person));
CREATE TABLE msgs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room INTEGER NOT NULL REFERENCES rooms(id),
  person INTEGER NOT NULL REFERENCES people(id),
  body TEXT NOT NULL,
  reply_to INTEGER REFERENCES msgs(id),
  file TEXT,
  ts REAL NOT NULL,
  edited REAL,
  deleted REAL);
CREATE INDEX msgs_room ON msgs (room, id);
CREATE TABLE reactions (
  msg INTEGER NOT NULL REFERENCES msgs(id),
  person INTEGER NOT NULL REFERENCES people(id),
  emoji TEXT NOT NULL,
  PRIMARY KEY (msg, person, emoji));
CREATE TABLE reads (
  room INTEGER NOT NULL REFERENCES rooms(id),
  person INTEGER NOT NULL REFERENCES people(id),
  last INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (room, person));
CREATE TABLE chat_files (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  size INTEGER NOT NULL,
  person INTEGER NOT NULL REFERENCES people(id),
  ts REAL NOT NULL);
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL);
"""

V3 = """
CREATE TABLE mirror_tasks (
  project INTEGER NOT NULL REFERENCES projects(id),
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  updated REAL NOT NULL,
  PRIMARY KEY (project, key));
"""

V4 = """
CREATE TABLE notice_outbox (
  project INTEGER NOT NULL REFERENCES projects(id),
  ev_id TEXT NOT NULL,
  sent_at REAL NOT NULL,
  PRIMARY KEY (project, ev_id));
CREATE TABLE chatlinks_seen (
  project INTEGER NOT NULL REFERENCES projects(id),
  origin TEXT NOT NULL,
  ingested REAL NOT NULL,
  PRIMARY KEY (project, origin));
"""

V5 = """
CREATE TABLE webhook_subs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  types TEXT NOT NULL DEFAULT '[]',
  secret TEXT NOT NULL,
  created REAL NOT NULL,
  failures INTEGER NOT NULL DEFAULT 0);
CREATE TABLE webhook_log (
  url TEXT NOT NULL,
  type TEXT NOT NULL,
  ok INTEGER NOT NULL,
  at REAL NOT NULL,
  retries TEXT NOT NULL DEFAULT '[]');
CREATE TABLE http_codes (
  code INTEGER PRIMARY KEY,
  n INTEGER NOT NULL DEFAULT 0);
"""
V6 = """
-- linked rooms, the secret split, journaled
-- webhook delivery (forward-only; earlier versions never edited).
-- a webhook URL / token / signing secret never sits in
-- projects.settings or a list response — this table keeps the real value;
-- settings and API answers keep only the pointer (a key) and a fingerprint.
CREATE TABLE server_secrets (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created REAL NOT NULL);
-- one row per room↔channel link. endpoint/token/ingest-secret are
-- server_secrets KEYS, resolved only at send/verify time — never shown.
CREATE TABLE chat_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project INTEGER NOT NULL REFERENCES projects(id),
  room INTEGER NOT NULL REFERENCES rooms(id),
  name TEXT NOT NULL,
  app TEXT NOT NULL DEFAULT 'webhook',
  endpoint_ref TEXT NOT NULL DEFAULT '',
  token_ref TEXT NOT NULL DEFAULT '',
  secret_ref TEXT NOT NULL DEFAULT '',
  out_cursor TEXT NOT NULL DEFAULT '',
  in_cursor TEXT NOT NULL DEFAULT '',
  created_by INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL,
  last_error TEXT NOT NULL DEFAULT '',
  last_error_at REAL);
-- the post-commit dispatcher's high-water marks (api_log.dispatch_pending)
CREATE TABLE dispatcher_state (
  key TEXT PRIMARY KEY,
  val INTEGER NOT NULL DEFAULT 0);
-- failure observability per notice target: the last send result per (project, target fingerprint)
CREATE TABLE notice_health (
  project INTEGER NOT NULL,
  target TEXT NOT NULL,
  app TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL,
  PRIMARY KEY (project, target));
-- subs pick projects; deliveries are journaled with a persistent id
-- (retries reuse it — the receiver's idempotency key) and the retry state.
ALTER TABLE webhook_subs ADD COLUMN projects TEXT NOT NULL DEFAULT '[]';
ALTER TABLE webhook_log ADD COLUMN wid TEXT NOT NULL DEFAULT '';
ALTER TABLE webhook_log ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE webhook_log ADD COLUMN next_retry REAL;
ALTER TABLE webhook_log ADD COLUMN last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE webhook_log ADD COLUMN payload TEXT NOT NULL DEFAULT '';
-- no ghost accounts: messages arriving through a link are
-- authored by ONE clearly non-human sentinel person, never by person id 1
-- (the owner) and never by a fake per-app account — the body carries the
-- origin label ("Ravi (Slack): …") instead. Existing teams get the row
-- here (appended after their own people — person ids never renumber);
-- fresh teams get it lazily on first ingest (chatlinks.link_person_id),
-- because inserting at bootstrap would shift every later person's id.
INSERT OR IGNORE INTO people (name, display, role, browser_only, created)
  SELECT 'linked-app',
         'linked app (not a person) — authors every message a room link brings in',
         'guest', 1, 0.0
  WHERE EXISTS (SELECT 1 FROM team WHERE id = 1);
"""
V7 = """
-- the accounts shape. people: team_role replaces the legacy `role` card
-- (owner/admin/user), plus the global role and the password account state.
-- sessions: last_used ticks on use (the 7-day rule), `ended` for sign-out.
-- Older people move: owner -> owner, admin -> admin, member -> user,
-- guest -> user (their project roles move with the roles table).
ALTER TABLE people ADD COLUMN team_role TEXT NOT NULL DEFAULT '';
ALTER TABLE people ADD COLUMN global_role TEXT NOT NULL DEFAULT '';
ALTER TABLE people ADD COLUMN pw_hash TEXT;
ALTER TABLE people ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0;
ALTER TABLE people ADD COLUMN failed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE people ADD COLUMN locked_until REAL;
UPDATE people SET team_role = 'owner' WHERE role = 'owner';
UPDATE people SET team_role = 'admin' WHERE role = 'admin';
UPDATE people SET team_role = 'user'  WHERE role IN ('member', 'guest');
ALTER TABLE sessions ADD COLUMN last_used REAL;
ALTER TABLE sessions ADD COLUMN ended REAL;
"""


V8 = """
-- the roles table; project_members gains
-- role_id (its text column keeps old rows' labels; the role id wins).
-- projects gain main_branch + a module mask.
CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0, permissions TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL);
INSERT INTO roles (name, builtin, permissions, created)
  SELECT 'Reader', 1, '{"board": ["view_work_packages", "add_work_package_comments"], "now": ["view_activity"], "tree": ["browse_repository"], "flow": ["view_work_packages"], "history": ["view_activity"], "chat": ["view_messages", "add_messages", "edit_own_messages"], "aura_work": [], "members": [], "settings": []}', CAST(strftime('%s','now') AS REAL)
  WHERE NOT EXISTS (SELECT 1 FROM roles WHERE name = 'Reader');
INSERT INTO roles (name, builtin, permissions, created)
  SELECT 'Member', 1, '{"board": ["view_work_packages", "add_work_packages", "edit_work_packages", "move_work_packages", "add_work_package_comments", "manage_work_package_relations", "add_work_package_attachments", "export_work_packages"], "now": ["view_activity"], "tree": ["browse_repository"], "flow": ["view_work_packages"], "history": ["view_activity", "export_activity"], "chat": ["view_messages", "add_messages", "edit_own_messages"], "aura_work": ["aura_work"], "members": ["view_members"], "settings": []}', CAST(strftime('%s','now') AS REAL)
  WHERE NOT EXISTS (SELECT 1 FROM roles WHERE name = 'Member');
INSERT INTO roles (name, builtin, permissions, created)
  SELECT 'Project admin', 1, '{"board": ["view_work_packages", "add_work_packages", "edit_work_packages", "move_work_packages", "add_work_package_comments", "manage_work_package_relations", "add_work_package_attachments", "export_work_packages", "delete_work_packages"], "now": ["view_activity"], "tree": ["browse_repository"], "flow": ["view_work_packages", "manage_gantt"], "history": ["view_activity", "export_activity", "manage_activity"], "chat": ["view_messages", "add_messages", "edit_own_messages", "edit_messages", "delete_messages"], "aura_work": ["aura_work"], "members": ["view_members", "manage_members"], "settings": ["edit_project"]}', CAST(strftime('%s','now') AS REAL)
  WHERE NOT EXISTS (SELECT 1 FROM roles WHERE name = 'Project admin');
ALTER TABLE project_members ADD COLUMN role_id INTEGER REFERENCES roles(id);
UPDATE project_members SET role_id =
  (SELECT id FROM roles WHERE name = 'Project admin') WHERE role = 'manager';
UPDATE project_members SET role_id =
  (SELECT id FROM roles WHERE name = 'Member') WHERE role = 'member';
UPDATE project_members SET role_id =
  (SELECT id FROM roles WHERE name = 'Reader') WHERE role = 'viewer';
ALTER TABLE projects ADD COLUMN main_branch TEXT NOT NULL DEFAULT 'main';
ALTER TABLE projects ADD COLUMN modules TEXT NOT NULL DEFAULT '{}';
"""

# Board status order + colour are persisted, not cosmetics held only in the
# admin page. This is a NEW migration; applied –V8 stay verbatim.
V9 = """
ALTER TABLE statuses ADD COLUMN colour TEXT NOT NULL DEFAULT '';
UPDATE statuses SET colour = CASE type
  WHEN 'backlog' THEN '#7d8896' WHEN 'todo' THEN '#5580a0'
  WHEN 'in_progress' THEN '#337ac3' WHEN 'in_review' THEN '#95682d'
  WHEN 'done' THEN '#358763' WHEN 'canceled' THEN '#b04f3e'
  ELSE '#7d8896' END;
"""

# G project administration: this permission was already named by the role
# matrix and required by the archive route, but absent from the V8 built-in
# role data. Add it only to the immutable Project admin role. Older V8 rows
# may carry the complete tick list; a custom role with the same human name is
# not a built-in and is never changed. Repeat applications append zero items.
# Older minimal hubs can carry a migration marker without the roles table.
# Re-declaring its exact V8 shape is harmless when the normal table exists;
# seeded rows remain V8's responsibility, and no custom role is rewritten.
V10 = """
CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0, permissions TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL);
UPDATE roles SET permissions =
  json_insert(permissions, '$.settings[#]', 'archive_project')
WHERE name = 'Project admin' AND builtin = 1
  AND NOT EXISTS (
    SELECT 1 FROM json_each(roles.permissions, '$.settings')
    WHERE json_each.value = 'archive_project');
"""

V11 = """
-- avatars: a person's drawn style (lorelei, the default, or avataaars), the
-- seed of their chosen drawing ('' = their username), and an optional
-- photo (its image type, '' for none) with a version for cache-busting.
ALTER TABLE people ADD COLUMN avatar_style TEXT NOT NULL DEFAULT '';
ALTER TABLE people ADD COLUMN avatar_seed TEXT NOT NULL DEFAULT '';
ALTER TABLE people ADD COLUMN avatar_photo TEXT NOT NULL DEFAULT '';
ALTER TABLE people ADD COLUMN avatar_ver REAL NOT NULL DEFAULT 0;
"""

V12 = """
-- a person's own switches in one project, on top of their role there:
-- {"tree": false} turns the Tree off for them only; {"aura_work": true} turns
-- an area on that their role doesn't give. Empty = exactly the role.
ALTER TABLE project_members ADD COLUMN overrides TEXT NOT NULL DEFAULT '{}';
"""

VERSIONS: dict[int, str] = {1: V1, 2: V2, 3: V3, 4: V4, 5: V5, 6: V6, 7: V7,
                            8: V8, 9: V9, 10: V10, 11: V11, 12: V12}

STATUS_COLOURS = {
    "backlog": "#7d8896", "todo": "#5580a0", "in_progress": "#337ac3",
    "in_review": "#95682d", "done": "#358763", "canceled": "#b04f3e",
}


def status_colour(kind: str) -> str:
    return STATUS_COLOURS.get(kind, "#7d8896")

# The six default statuses every new project gets (plan)
DEFAULT_STATUSES = [
    ("backlog", "backlog", "Backlog", 0),
    ("todo", "todo", "To do", 1),
    ("in_progress", "in_progress", "In progress", 2),
    ("in_review", "in_review", "In review", 3),
    ("done", "done", "Done", 4),
    ("canceled", "canceled", "Canceled", 5),
]
