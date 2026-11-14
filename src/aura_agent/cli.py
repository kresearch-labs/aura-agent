"""cli.py — aura command line.

Commands: aura run <task> one-shot: create session, run loop to completion aura
resume [file] tail-replay latest (or given) session aura doctor
profile/endpoint self-check (mock-aware) aura profiles list provider
profiles.kill in engagement root halts the loop at the next mediator check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import PROFILES, resolve
from .engine.governor import Governor, register_notes
from .engine.loop import AgentLoop, LoopConfig
from .llm import LLMClient
from .mediator import Budgets, Mediator, Scope
from .paths import aura_home as _aura_home_dir
from .sessions import Session, latest_session, new_session
from .tools.findings import register as register_findings
from .tools.fs import register as register_fs
from .tools.graph_tools import register as register_graph
from .tools.shell import register as register_shell
from .tools.web import register as register_web


def engagement_dir(path: str | None, base: Path | None = None) -> Path:
    """The notebook (where Aura keeps its notes) for this launch.

    The folder you are in (or --dir) is the WORKSPACE — your project; the
    file tools, commands and tests work there and Aura writes none of its
    own files into it. Its notebook lives in the profile:
    <aura home>/profiles/<profile>/projects/<folder>-<id>, and remembers
    its workspace (workspace.json; build_agent reads it back).

    Exceptions, so nothing that worked before breaks:
      - a folder that already holds Aura's notes (scope.json,
        findings.jsonl, sessions/) is used as-is: notebook = workspace;
      - --dir naming a folder that does not exist yet creates it as a
        same-folder notebook (as before);
      - home folder, drive root, system folders, Aura's own folder: not a
        project — the no-project notebook (no System32 trap)."""
    from .paths import (has_aura_state, no_project_dir, project_dir,
                        remember_workspace, unsafe_workspace)
    cwd = (base or Path.cwd()).resolve()
    default_engagement = no_project_dir()
    # Idiot-proof law: unwritable targets fall back and SPEAK.
    def _mkdir_or_fallback(p: Path) -> Path:
        try:
            p.mkdir(parents=True, exist_ok=True)
            (p / "sessions").mkdir(exist_ok=True)
            return p
        except PermissionError:
            from . import ui
            fallback = default_engagement
            fallback.mkdir(parents=True, exist_ok=True)
            (fallback / "sessions").mkdir(exist_ok=True)
            try:
                ui.warn(f"'{p}' is not writable (admin dir?) — notebook "
                        f"placed at {fallback}")
            except Exception:
                print(f"  [!] '{p}' is not writable — notebook at {fallback}")
            return fallback
    if path is None:
        target = cwd
    else:
        target = Path(path)
        if not target.is_absolute():
            target = cwd / target
        target = target.resolve()
        if not target.exists():
            return _mkdir_or_fallback(target)
    # Visual-audit fix: a folder that IS an engagement is used as-is, never
    # nested — the nested trap made every prior finding invisible
    if target.is_dir() and has_aura_state(target):
        return _mkdir_or_fallback(target)
    why = unsafe_workspace(target) if target.is_dir() else "not a folder"
    if why:
        if path is not None:
            from . import ui
            ui.warn(f"'{target}' is {why} — not used as a project; "
                    f"Aura works in {default_engagement}")
        return _mkdir_or_fallback(default_engagement)
    fresh = not project_dir(target).exists()
    nb = _mkdir_or_fallback(project_dir(target))
    if nb != default_engagement:
        remember_workspace(nb, target)
        if fresh:
            FRESH_NOTEBOOKS.add(nb.resolve())
    return nb


# Notebooks this launch made: one that is still empty when Aura closes is taken
# away again (discard_if_unused)
FRESH_NOTEBOOKS: set = set()
_STARTER_FILES = {"notes.md", "memories/MEMORY.md", "workspace.json"}


def discard_if_unused(nb: Path) -> bool:
    """Remove a notebook this launch made when nothing happened in it: no
    chat was written, and only the starter files (empty journal and memory,
    the workspace link) are there. True when removed."""
    import shutil
    nb = Path(nb).resolve()
    if nb not in FRESH_NOTEBOOKS or not nb.is_dir():
        return False
    try:
        files = [p.relative_to(nb).as_posix() for p in nb.rglob("*") if p.is_file()]
    except OSError:
        return False
    if any(f not in _STARTER_FILES for f in files):
        return False                       # Something was done: keep it all
    for name, start in (("notes.md", "# Agent Journal"),
                        ("memories/MEMORY.md", ("# Notebook Memory", "# Engagement Memory"))):
        p = nb / name
        if p.exists():
            body = p.read_text(encoding="utf-8", errors="replace")
            if not body.startswith(start) or "\n- " in body or len(body) > 400:
                return False               # A note was written: keep it
    try:
        shutil.rmtree(nb)
    except OSError:
        return False
    FRESH_NOTEBOOKS.discard(nb)
    return True


def _resolve_engagement_dir(path: str | None, cwd: Path | None = None) -> Path:
    """Fix #1 (root mismatch): when launched with no --dir and a TASK.md sits at
    the caller cwd, root the engagement AT the cwd instead of./engagement - the
    arena prompt says "read TASK.md in the current directory", and jailing file
    tools one level down made TASK.md invisible to read_file (histA-a1 /
    -errand_gate-a3 operator-query bails) while write_file delivered report.md
    into engagement/ where graders never looked (-debug2-a1). Explicit --dir
    always wins; no TASK.md -> historic./engagement behavior. Engagement
    internals (audit tape, sessions, shadow-git) still live under
    <root>/engagement/.

    orchestrators keep sessions OUT of the task repo via AURA_ENGAGEMENT_DIR /
    AURA_SESSIONS_DIR — honored before the TASK.md arena-rooting (the env var
    is the harness's explicit instruction and outranks cwd heuristics).
    """
    base = (cwd or Path.cwd()).resolve()
    if path:
        return engagement_dir(path)
    env_dir = os.environ.get("AURA_ENGAGEMENT_DIR") \
        or os.environ.get("AURA_SESSIONS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        (p / "sessions").mkdir(exist_ok=True)
        return p
    if (base / "TASK.md").is_file():
        # (same-folder, as before — not a profile project notebook)
        base.mkdir(parents=True, exist_ok=True)
        (base / "sessions").mkdir(exist_ok=True)
        return base
    return engagement_dir(None, base)


def _load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _browser_enabled() -> bool:
    """Playwright installed and the setup capability not set to 'off'."""
    try:
        from .tools.browser import playwright_available
        if not playwright_available():
            return False
        from .setup_pickers import load_capabilities
        return load_capabilities().get("browser", "playwright") != "off"
    except Exception:
        return False


def apply_model(loop, profile, eng: Path) -> str:
    """The one place where everything model-specific is picked — at a model
    switch (/model) as at startup: the backend's cache mode and that model's
    prompt (its approved copy, or the default). Nothing from the previous
    model is left behind. Returns where the prompt came from."""
    from .agents_md import system_prompt_with_agents_md
    from .engine.loop import DEFAULT_SYSTEM
    from .kv_cache import cache_mode_for
    from .prompt_profiles import prompt_for
    core = getattr(loop, "core_prompt", None) or DEFAULT_SYSTEM
    loop.cache_mode = cache_mode_for(profile)
    model_prompt, src = prompt_for(getattr(profile, "model", None), core)
    # the project's AGENTS.md lives in the workspace, not the notebook
    final_sys, agents_loaded = system_prompt_with_agents_md(
        model_prompt, getattr(loop, "workspace", None) or eng)
    # the labels Aura puts on messages, explained once (legend.py)
    from .legend import with_legend
    loop.cfg.system_prompt = with_legend(final_sys)
    loop.agents_md = agents_loaded
    loop.prompt_source = src
    return src


# --add-dir: folders outside the project this launch may use (outside.py)
_ADD_DIRS: list[str] = []


def build_agent(eng: Path, profile_name: str | None):
    from .config import resolve_settings
    settings = resolve_settings()   # Default layer; per-task flag = UNDISCUSSED
    profile = resolve(profile_name)
    scope = Scope.from_dict(_load_json(eng / "scope.json", {}))
    m = Mediator(scope, Budgets(), eng / "audit.jsonl", eng)
    # where a new chat starts (setup step 2); a resumed chat restores its own
    try:
        from .options import get as _opt
        m.assist = _opt("assist.level")
        m.mode = _opt("assist.start")
    except Exception:                                   # noqa: BLE001
        pass
    m.budgets.token_ceiling = (settings.token_ceiling
                               if (settings.token_ceiling or 0) > 0 else None)
    # eng = the notebook (Aura's notes); workspace = the user's project,
    # where files, commands and tests work. Same folder in old-style
    # engagements and when not in a project.
    from .paths import workspace_for
    workspace = workspace_for(eng)
    m.work_dir = workspace
    m.same_folder = workspace == Path(eng).resolve()
    register_fs(m, workspace, shadow=eng / ".shadow")
    from .outside import addable as _addable
    for _d in _ADD_DIRS:
        _why = _addable(Path(_d).expanduser())
        if _why:
            from . import ui
            ui.warn(f"--add-dir {_d}: {_why}")
        else:
            m.outside_ok.add(Path(_d).expanduser().resolve())
    llm = LLMClient(profile)
    register_notes(m, m._fs, eng)
    register_graph(m, m._fs, eng)
    register_findings(m, m._fs, eng)
    register_shell(m, m._fs)
    # Clarify — ask the user mid-task (records assumptions)
    from .tools.clarify_tool import register as register_clarify
    register_clarify(m, m._fs)
    # Aura Teams by asking: connect, the hidden token box, wire any hub
    # Persona: plan and train pblocks with the user
    from .tools.persona_tool import register as register_persona
    register_persona(m, m._fs)
    from .editions import teams_present
    if teams_present():
        from .tools.team_tool import register as register_team
        register_team(m, m._fs)
    # find_way.py: `get` — find a way to what's missing (the /findaway toggle)
    from .tools.get_tool import register as register_get
    register_get(m, m._fs)
    # updating Aura by asking (aura_update; a card before any change)
    from .tools.update_tool import register as register_update
    register_update(m, m._fs)
    # Aura's options — settings (look) / settings_set (asks every time)
    from .options import register as register_options
    register_options(m, m._fs)
    # Commands that keep running (process_start / process)
    from .tools.process import register as register_process
    register_process(m, m._fs, eng, workspace)
    # search_files (text in files, files by name)
    from .tools.search import register as register_search
    register_search(m, workspace, m._fs)
    # Past chats (history_search)
    from .history import register as register_history
    register_history(m, eng, m._fs)
    register_web(m, m._fs)
    from .todo import register_todo
    register_todo(m, m._fs, eng)
    # Persistent memory (MEMORY.md + USER.md, mediator-gated writes)
    from .memory_store import register_memory
    register_memory(m, m._fs, eng)
    # Skills (procedural memory, agentskills.io SKILL.md format)
    from .skills import register_skills
    register_skills(m, m._fs, eng)
    # Sub-agent delegation (isolated context, read-only child toolset)
    from .delegate import register_delegate
    register_delegate(m, m._fs, eng, profile, scope, workspace=workspace)
    # Operator constraints — file-backed, pointer-injected; the agent
    # reads/adds/removes rules via these surgical tools (audited writes)
    from .tools.constraints_tool import register as register_constraints
    register_constraints(m, m._fs, eng)
    # 2.5: the project's own test / lint / typecheck commands
    from .tools.checks_tool import register as register_checks
    register_checks(m, m._fs, eng)
    # 2.9: the operator's hooks (workspace hooks/ and ~/.aura/profiles/<profile>/hooks)
    from .cronops import register_hook_points
    register_hook_points(m, eng)
    # Project symbols — enumerated real entities; the model reads the sheet (or
    # filtered views) before calling project APIs
    from .tools.symbols_tool import register as register_symbols
    _sym_table = register_symbols(m, m._fs, workspace)
    # Definition / references / outline — the installed language server when
    # there is one, built-in search otherwise
    from .tools.code_nav import register as register_code_nav
    register_code_nav(m, workspace, m._fs)
    from .tools.cron_tool import register as register_cron   # Model-made jobs (asks first)
    register_cron(m, m._fs, eng)
    # A script whose tool calls pass the same mediator
    from .tools.run_code import register as register_run_code
    register_run_code(m, workspace, m._fs)
    # Look at an image file (main model if it sees, else a checked helper)
    from .tools.view_image import register as register_view_image
    register_view_image(m, workspace, eng, m._fs)
    # .1: probe the endpoint for the TRUE context length. An explicit user
    # override (AI_CONTEXT_LENGTH / profile field) still wins.
    from .ctx_probe import probe_context_length
    ctx, ctx_src = probe_context_length(profile.base_url, profile.model,
                                        configured=profile.context_length)
    profile.context_length = ctx
    governor = Governor(ctx, threshold=settings.compact_pct,
                        prune_pct=settings.prune_pct)
    # Persistent operator instructions (AGENTS.md / CLAUDE.md convention)
    from .agents_md import system_prompt_with_agents_md
    from .engine.loop import DEFAULT_SYSTEM
    base_cfg = LoopConfig(max_tool_output=settings.tool_output,
                          _run_settings=settings)
    base_prompt = base_cfg.system_prompt or DEFAULT_SYSTEM
    # the active model's prompt: its approved copy, or the default unchanged
    from .prompt_profiles import prompt_for
    model_prompt, prompt_src = prompt_for(profile.model, base_prompt)
    final_sys, agents_loaded = system_prompt_with_agents_md(model_prompt,
                                                            workspace)
    from .legend import with_legend
    base_cfg.system_prompt = with_legend(final_sys)
    # One schema registry shared with helper agents
    from .tool_schemas import main_agent_schemas
    schemas = main_agent_schemas()
    # The browser tool existed but was never registered. Offer it only when
    # Playwright is installed (otherwise every call would just answer "not
    # installed") and the operator hasn't turned it off in setup — the first
    # time that setup choice is actually honoured.: configured MCP servers
    # become tools, graded by their own hints
    try:
        from .mcp_tools import register as register_mcp
        schemas += register_mcp(m, m._fs, eng)
    except Exception as _e:
        from .eventlog import log_swallowed
        log_swallowed("cli.mcp_register", _e)
    if _browser_enabled():
        from .tools.browser import register as register_browser
        from .tool_schemas import schema_for
        register_browser(m, m._fs)
        schemas.append(schema_for("browser_run"))
    # The governor counts what is really sent.
    from .toolsets import ToolList, register_tools_find
    tool_list = ToolList(schemas)
    _holder: dict = {}
    register_tools_find(m, m._fs, lambda: _holder.get("tl"))
    _holder["tl"] = tool_list
    schemas = tool_list.schemas()
    governor.schema_chars = len(json.dumps(schemas))
    governor.reply_reserve = min(int(os.environ.get("AI_MAX_TOKENS", "16384")),
                                 max(1, ctx // 4))
    loop = AgentLoop(llm, m, schemas, m._fs, base_cfg,
                     governor=governor, root=eng)
    loop.tool_list = tool_list
    loop.workspace = workspace
    # Roadmap 1.4: the reviewer existed but was never constructed, so the
    # fresh-context diagnosis after repeated failures never ran. Uses the
    # same graph instance the task_* tools write to.
    from .engine.reviewer import Reviewer
    if getattr(m, "_graph", None) is not None:
        loop.reviewer = Reviewer(llm, m, m._graph, eng / "audit.jsonl")
    loop.ctx_source = ctx_src      # .1: show where ctx number came from
    loop.agents_md = agents_loaded  # AGENTS.md/CLAUDE.md loaded (or None)
    # Model-adaptive KV-cache alignment (per-profile strategy)
    from .kv_cache import cache_mode_for
    loop.cache_mode = cache_mode_for(profile)
    loop.core_prompt = base_prompt         # The default rules every copy keeps
    loop.prompt_source = prompt_src
    # Intent engine — scope + agents.md for contract building
    loop.intent_scope = _load_json(eng / "scope.json", {})
    # keys in a plain file (no OS keychain): say so, once per start
    try:
        from .keystore import warn_if_plain
        _w = warn_if_plain()
        if _w:
            from . import ui
            ui.warn(_w)
    except Exception as _e:                        # noqa: BLE001
        from .eventlog import log_swallowed
        log_swallowed("cli.keystore_warn", _e)
    return profile, m, loop


def _kill_watch(eng: Path, state: dict):
    flag = eng / ".kill"
    while not state.get("stop"):
        if flag.exists():
            state["killed"] = True
            return
        time.sleep(0.5)


def _budget_nudges(eng: Path, steer_queue: list[str], state: dict) -> None:
    """At 50% and 75% of the wall budget (AI_BUDGET_S env, default off) a converge
    nudge is queued; loop.run consumes the queue as [STEER] user turns BEFORE
    the next proposal.
    """
    try:
        budget = float(os.environ.get("AI_BUDGET_S", "0") or 0)
    except ValueError:
        budget = 0.0
    if budget <= 0:
        return
    t0 = time.monotonic()
    for frac in (0.50, 0.75, 0.90):
        target = t0 + budget * frac
        while not state.get("killed"):
            now = time.monotonic()
            if now >= target:
                break
            if now < target - 0.5:
                time.sleep(min(target - now, 1.0))
            else:
                time.sleep(max(target - now, 0.01))
        if state.get("killed"):
            return
        pct = int(frac * 100)
        left = int(budget * (1.0 - frac))
        if frac < 0.90:
            steer_queue.append(
                f"[BUDGET {pct}%] {left}s of wall budget remain. Converge now: "
                "stop investigating — apply your fixes, write every TASK.md-named "
                "deliverable (report, action log), and finish before the timeout.")
        else:
            steer_queue.append(
                f"[BUDGET {pct}%] {left}s remain. LAND NOW: this is the final "
                "window — if any TASK.md-named deliverable (report, action log, "
                "output files) is not yet on disk, write it immediately in "
                "minimal-complete form. Do not start new investigation. Finish.")


def cmd_run(args) -> int:
    from . import ui
    ui.enable()
    eng = _resolve_engagement_dir(args.dir, Path.cwd())
    # First-run gate — no working profile -> guided onboarding
    if not getattr(args, "profile", None):
        from .first_run import ensure_working_profile
        prof = ensure_working_profile(eng, interactive=sys.stdout.isatty())
        if prof is not None:
            args.profile = prof.name
    profile, m, loop = build_agent(eng, args.profile)
    if getattr(args, "research", None):
        loop.research_mode = args.research
    session = new_session(eng / "sessions", args.task)
    # Seq'd always-on ledger — the L1 truth source for every render
    from .ledger import Ledger
    from .contracts.bridge import LedgerBridge
    _wo_ledger = Ledger(eng)
    WO_LEDGER_BRIDGE = LedgerBridge(_wo_ledger)
    state: dict = {}
    t = threading.Thread(target=_kill_watch, args=(eng, state), daemon=True)
    t.start()
    ui.banner(profile=profile.name, model=profile.model,
              endpoint=profile.base_url, version=__version__,
              tools=len(m.tools))
    ui.info(f"session {session.path.name}   kill: touch {eng}/.kill")

    from . import ui as _ui
    from .hud import Hud, phase
    try:
        budget = float(os.environ.get("AI_BUDGET_S", "0") or 0)
    except ValueError:
        budget = 0.0
    hud_bar = Hud(max_steps=None, budget_s=budget)

    def _on_event(kind: str, data: dict) -> None:
        from .eventlog import EventLog
        EventLog(eng)(kind, data)   # AURA_EVENTS=1 → events.jsonl
        WO_LEDGER_BRIDGE(kind, data)  # Every event -> seq'd ledger (L1)
        hud_bar.observe(kind, data)
        if kind == "llm_done":
            # step/budget HUD line after every LLM turn (replaces silence)
            print(hud_bar.line())
        elif kind == "tool_done":
            _ui.tool_card(data.get("tool", "?"), data.get("blast", "read"),
                          data.get("status") == "ok",
                          data.get("duration_ms", 0),
                          data.get("detail", ""))
        elif kind == "phase":
            # Surface the loop's silent phases (gate, finish-verify,
            # compaction, prune) as visible one-liners
            phase(data.get("name", ""), data.get("detail", ""))
        elif kind == "steer":
            _ui.info(f"steer: step {data.get('step', '?')}")
        elif kind == "notice":            # A calm notice is information, not a warning
            (_ui.info if data.get("calm") else _ui.warn)(str(data.get("text", "")))
        elif kind == "team":                    # Teams: the line, plain
            _ui.info("team: " + str(data.get("text", ""))
                     + "".join("\n  · " + str(x) for x in data.get("lines") or []))
        else:
            from .run_narrator import watch_note
            note = watch_note(kind, data)
            if note:
                phase(*note)
    loop.on_event = _on_event
    # On a terminal, gated calls are asked here (the run is on this thread).
    # Piped runs have nobody to ask: the run stops and waits for `aura
    # approve|reject <id>` from another terminal — nothing moves on
    if sys.stdin.isatty():
        m.approver = _terminal_approver
        m.asker = _terminal_asker      # Questions (Teams, ask_user) too
    else:
        from .approvals import waiting_approver
        m.approver = waiting_approver(eng)

    steer_queue: list[str] = []
    nt = threading.Thread(target=_budget_nudges,
                          args=(eng, steer_queue, state), daemon=True)
    nt.start()
    # Files the task names are looked for in the project
    _ws = getattr(loop, "workspace", None) or eng
    rec = loop.run(session, args.task, kill_check=lambda: state.get("killed", False),
                   steer_queue=steer_queue, work_dir=_ws)
    # (FP wiring): timeout/kill must not lose partial work — flush a
    # best-effort diff to a KNOWN path before exiting, honestly flagged.
    try:
        from .contracts.session_location import flush_partial_diff
        ok, flush_err = flush_partial_diff(
            _ws, eng / "salvage" / "patch.diff")
        if ok:
            ui.info(f"salvage: diff flushed to {eng / 'salvage' / 'patch.diff'}")
        elif not rec.gate_missing and "not a git repository" not in (flush_err or ""):
            ui.warn(f"salvage: no diff recoverable ({flush_err or 'none'})")
    except Exception:
        pass  # Salvage must never mask the real exit path
    if rec.gate_missing:
        ui.warn(f"not written — the task asked for: "
                f"{', '.join(rec.gate_missing)}")
    # Five-line end-of-run receipt summary (status/proof/tokens/
    # artifacts/next-step) — rendered from the receipt, zero model calls
    if getattr(rec, "receipt_summary", ""):
        print()
        print(rec.receipt_summary)
    if not sys.stdin.isatty():
        # nobody watching: the end goes to the phone too (`aura setup away`;
        # scheduled jobs send their own) — notify.py says which events push
        try:
            from . import gateway_approvals as GA
            from .gateway import deliver_message
            from .notify import pick_tier
            if pick_tier("run_ended") == "push":
                status = ((getattr(rec, "receipt", None) or {}).get("status") or
                          ("stopped" if rec.error else "done"))
                text = (f"Aura finished: {str(args.task)[:120]}\n{status}"
                        + (f" — {rec.error}" if rec.error else "")
                        + (f"\n{(rec.content or '')[:600]}" if rec.content else ""))
                for ch in GA.channels():
                    deliver_message(text, ch, eng=eng)
        except Exception:                                # noqa: BLE001
            pass
    # The arena sandboxes keep actions.log at the working dir root (TASK.md
    # names it there); default to the caller's cwd when it differs from eng/
    # the actions log stays in the notebook — never written into the user's
    # project
    _actions_log_shim(eng)
    # Independent referee — completion scorecard from OUTSIDE the loop. The
    # agent's "done" is checked against disk here; the scorecard rides the
    # final output (: self-graded completion is not evidence).
    try:
        from .referee import check_task
        sc = check_task(_ws, args.task, task_md=_ws / "TASK.md")
        if sc.verdicts:
            ui.section("scorecard")
            print(sc.render())
            (eng / "scorecard.json").write_text(
                json.dumps({"task": sc.task, "all_good": sc.all_good,
                            "summary": sc.summary,
                            "verdicts": [v.__dict__ for v in sc.verdicts]},
                           indent=1), encoding="utf-8")
            if not sc.all_good:
                ui.warn("referee: not all deliverables verified — see scorecard")
    except Exception as e:
        sc = None
        ui.info(f"referee skipped: {e}")
    if rec.error:
        ui.warn(f"stopped: {rec.error}")
    else:
        ui.section("final")
    ui.info(f"stats {json.dumps(session.stats())}")
    problems = []
    if rec.error:
        problems.append(rec.error)
    if rec.gate_missing:
        problems.append("not written: " + ", ".join(rec.gate_missing))
    _bad = [v.name for v in (getattr(sc, "verdicts", None) or [])
            if v.status in ("missing", "present-broken")
            and v.name not in (rec.gate_missing or [])]
    if _bad:
        problems.append("broken or missing: " + ", ".join(_bad))
    if problems:
        print(f"\nNot finished — {'; '.join(problems)}")
        return 1
    print("\nDone.")
    return 0


def _actions_log_shim(eng: Path, actions_log: Path | None = None) -> None:
    """When actions.log is underweight (<5 lines) but the mediator audit tape has
    the real calls, synthesize the missing entries from audit.jsonl
    (append-only; never rewrites existing lines). `actions_log` overrides the
    target file (arena sandboxes keep actions.log at the working-dir root while
    the audit tape lives under engagement/).
    """
    alog = actions_log or (eng / "actions.log")
    audit = eng / "audit.jsonl"
    try:
        have = 0
        if alog.exists():
            have = sum(1 for l in alog.read_text(encoding="utf-8").splitlines()
                       if l.strip())
        if have >= 5 or not audit.exists():
            return
        from datetime import datetime
        lines = []
        if alog.exists():
            lines = [l for l in alog.read_text(encoding="utf-8").splitlines() if l.strip()]
        covered = set()
        for l in lines:
            covered.add(l[9:] if len(l) > 9 and l[8:9] == " " else l)
        with audit.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                ts = rec.get("ts")
                call = rec.get("call") or "tool"
                status = rec.get("status") or "?"
                target = rec.get("target") or ""
                detail = f"{call} {target} -> {status}".strip()
                stamp = (datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                         if isinstance(ts, (int, float)) else "00:00:00")
                line = f"{stamp} ACTION {detail}"
                if line[9:].strip() in covered:
                    continue
                lines.append(line)
                covered.add(line[9:].strip())
        if len(lines) > have:
            alog.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass  # The shim is best-effort; never break a finished run


def cmd_resume(args) -> int:
    """`aura resume [#id|title|latest]` — opens that chat where it was left."""
    eng = engagement_dir(args.dir)
    if not getattr(args, "run_last", False):
        args.resume = args.file or "latest"
        args.session, args.task, args.name, args.fork = None, None, None, None
        args.continue_ = False
        return cmd_chat(args)
    return _cmd_resume_run(args, eng)


def _cmd_resume_run(args, eng) -> int:
    """The old way (`aura resume --run`): run the chat's last task again."""
    profile, m, loop = build_agent(eng, args.profile)
    s = (Session(Path(args.file)) if args.file
         else latest_session(eng / "sessions"))
    if s is None:
        from . import ui
        ui.warn("no sessions found"); return 1
    from . import ui
    ui.banner(profile=profile.name, model=profile.model,
              endpoint=profile.base_url, version=__version__,
              tools=len(m.tools))
    ui.info(f"resumed {s.path.name} — {s.stats()['messages']} messages replayed")
    from .last_state import resume_honest_limits
    print(ui.muted("  " + resume_honest_limits()))
    task = s.last_user_task() or "continue the task"
    rec = loop.run(s, task)
    ui.info(f"stats {json.dumps(s.stats())}")
    return 0


def _terminal_approver(req: dict) -> str:
    """Ask on the terminal; anything but a clear answer asks again."""
    from . import ui
    from .approvals import card_lines, parse_answer
    print()
    for ln in card_lines(req):
        print("  " + (ui.accent(ln) if ln.startswith("?") else ui.muted(ln)))
    while True:
        try:
            ans = parse_answer(input(ui.muted("  approve> ")), req)
        except (KeyboardInterrupt, EOFError):
            return "deny"
        if ans is not None:
            return ans


def _terminal_asker(question: str, options: list[str], multi: bool = False,
                    free: bool = False):
    """A question on the terminal: numbered options (several when multi),
    the person's own words when free. Enter alone = None (left to Aura)."""
    from . import ui
    print()
    print("  " + ui.accent("? " + question))
    for i, o in enumerate(options, 1):
        print("  " + ui.muted(f"  {i}. {o}"))
    while True:
        try:
            t = input(ui.muted("  answer> ")).strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not t:
            return None
        nums = [x.strip() for x in t.split(",")]
        if options and all(x.isdigit() and 1 <= int(x) <= len(options) for x in nums) \
                and (len(nums) == 1 or multi):
            picked = [options[int(x) - 1] for x in nums]
            return picked if multi else picked[0]
        if free or not options:
            return t
        print(ui.muted(f"  answer with a number 1-{len(options)}"))


def cmd_setup(args) -> int:
    """Setup: quick = the model + how much Aura asks; full = every step; minimal =
    no scanning, typed by hand; a step name = that step alone; status = what
    the setup receipt shows. --answers / AURA_SETUP_* / --non-interactive /
    --dry-run for scripts.
    """
    import os as _os
    from . import setup_steps as SS
    words = list(getattr(args, "mode", None) or [])
    if not words and _os.environ.get("AURA_SETUP_LANE"):
        words = [_os.environ["AURA_SETUP_LANE"].strip().lower()]
    if not words and _sys_tty():
        from . import setup_ui as SU
        SU.banner("Set up Aura", "↑↓ and Enter to choose")
        try:
            pick = SU.pick_one("Which setup?", [
                SU.Opt("quick", "Quick", "your username, the model, how often Aura asks · 1 minute"),
                SU.Opt("full", "Full", "every part, one at a time · about 5 minutes")], "quick",
                rec="quick")
        except KeyboardInterrupt:
            print()
            print("  Setup stopped — run 'aura setup' any time.")
            return 130
        words = ["full"] if pick == "full" else ["quick"]
    mode = words[0] if words else "quick"
    if mode == "status":
        print("setup — what each step showed when it was checked:")
        for line in SS.status_lines():
            print(line)
        return 0
    if mode == "minimal":
        from .setup import run_setup
        rc = run_setup("minimal")
        _offer_eval(rc)
        return rc
    words = [SS.ALIASES.get(w, w) for w in words]       # Old step names still work
    only = list(dict.fromkeys(w for w in words if w in SS.BY_ID))
    bad = [w for w in words if w not in SS.BY_ID and w not in ("quick", "full")]
    if bad:
        print(f"unknown setup step: {', '.join(bad)} — steps: "
              + ", ".join(SS.BY_ID) + "  (or quick / full / minimal / status)")
        return 2
    try:
        answers = SS.answers_from(getattr(args, "answers", None))
    except (OSError, ValueError) as e:
        print(f"✗ couldn't read the answers file: {e}")
        return 2
    interactive = not getattr(args, "non_interactive", False) and _sys_tty()
    rc = SS.run(mode="full" if mode == "full" else "quick", only=only or None,
                answers=answers, interactive=interactive,
                dry_run=bool(getattr(args, "dry_run", False)))
    if interactive and rc != 130 and (mode in ("quick", "full") or "model" in only):
        try:
            _offer_eval(0 if rc in (0, 2) else rc, ask_prompt=(mode == "full"))
        except KeyboardInterrupt:
            print()
    if mode == "quick" and rc in (0, 2):
        from . import setup_ui as SU
        SU.note("more choices (safety, tools, web, apps, messages): aura setup full")
    return rc


def _sys_tty() -> bool:
    import sys as _s
    return _s.stdin.isatty()


def _offer_eval(rc: int, ask_prompt: bool = False) -> None:
    """After a model is picked: the model's own prompt and ONE offer to learn the
    model.
    """
    if rc != 0 or not sys.stdin.isatty():
        return
    prof = resolve(None)
    model = getattr(prof, "model", None)
    # 1) the model maker's own settings
    try:
        from . import model_facts as MF
        from . import sampling as SP
        key = MF.model_key(prof.base_url, model)
        if not SP.choice(key):
            vals, src = SP.find(prof.base_url, model, prof.api_key)
            if vals:
                from . import setup_ui as SU
                SU.banner("The model's own settings")
                SU.note(f"{model}'s maker recommends {SP.describe(vals)}")
                SU.line(f"from {src}", "detail")
                use = SU.yes_no("Use them for this model?", False, "Yes, use the maker's",
                                "No, keep Aura's", no_hint="temperature 0.4")
                SP.save(key, "maker" if use else "aura", vals, src)
                SU.line(("the maker's settings from the next message" if use
                         else "Aura's settings stay")
                        + SU.dim("  (change: aura model settings maker|aura)"), "ok")
    except (Exception, KeyboardInterrupt):           # noqa: BLE001
        pass                    # An offer must never break setup
    # 2) the quick check: one call (tools) and what the server reports. The
    # deeper fine-tuning stays optional: aura eval model --deep
    try:
        from . import model_facts as MF
        MF.offer(LLMClient(prof), MF.model_key(prof.base_url, prof.model))
    except (Exception, KeyboardInterrupt):           # noqa: BLE001
        pass                    # An offer must never break setup


def _model_settings(word: str | None) -> int:
    """`aura model settings [maker|aura]`: show the model maker's own
    settings and which ones are in use, or choose (sampling.py)."""
    from . import model_facts as MF
    from . import sampling as SP
    prof = resolve(None)
    key = MF.model_key(prof.base_url, prof.model)
    c = SP.choice(key)
    if word in ("maker", "aura"):
        vals, src = (c.get("values") or {}), (c.get("source") or "")
        if word == "maker" and not vals:
            vals, src = SP.find(prof.base_url, prof.model, prof.api_key)
            if not vals:
                print(f"{prof.model}: no settings from its maker found ({src}) — Aura's stay")
                return 1
        SP.save(key, word, vals, src)
        print(f"{prof.model}: " + (f"the maker's settings — {SP.describe(vals)}"
                                   if word == "maker" else "Aura's settings (temperature 0.4)")
              + " from the next message")
        return 0
    if word:
        print("usage: aura model settings [maker|aura]")
        return 1
    vals, src = SP.find(prof.base_url, prof.model, prof.api_key)
    print(f"{prof.model}: the maker recommends {SP.describe(vals)}" + (f" ({src})" if vals else f" — {src}"))
    print("  in use: " + ("the maker's — " + SP.describe(c.get("values") or {})
                          if c.get("choice") == "maker" else "Aura's (temperature 0.4)"))
    return 0


def cmd_prompt(args) -> int:
    """Which prompt a model gets (Aura's own, always): show / reset."""
    from . import prompt_profiles as PP
    from .engine.loop import DEFAULT_SYSTEM
    prof = resolve(getattr(args, "profile", None))
    model = getattr(args, "model", None) or prof.model
    if args.prompt_cmd == "show":
        text, src = PP.prompt_for(model, DEFAULT_SYSTEM)
        print(f"model {model} · prompt: {src}")
        if getattr(args, "full", False):
            print(text)
        return 0
    if args.prompt_cmd == "reset":
        ok = PP.remove_copy(model)
        print(f"deleted the old tuned copy for {model}"
              if ok else f"{model} has no old copy — it uses Aura's prompt")
        return 0
    return 1


def cmd_model(args) -> int:
    """1.3: pick a provider + model (the shared setup menu), or list them."""
    from .model_picker import cmd_model_list
    action = getattr(args, "action", "pick")
    if action == "list":
        return cmd_model_list()
    if action == "settings":
        return _model_settings(getattr(args, "name", None))
    if action == "price":
        # What the model in use costs ($ per million tokens), so receipts can
        # show money; local servers are $0; never guessed
        from . import model_facts as MF
        prof = resolve(None)
        key = MF.model_key(prof.base_url, prof.model)
        nums = [args.name] + list(getattr(args, "prices", []) or []) if args.name else []
        if not nums:
            p = MF.price(key)
            print(f"{prof.model}: " + ("local server — $0" if p and p.get("local") else
                  f"${p['in']}/M in, ${p['out']}/M out, ${p['cached']}/M cached" if p else
                  "no price set — aura model price <input> <output> [cached] ($ per million tokens)"))
            return 0
        try:
            vals = [float(x) for x in nums[:3]]
            if len(vals) < 2 or any(v < 0 for v in vals):
                raise ValueError
        except ValueError:
            print("usage: aura model price <input> <output> [cached]  — $ per million tokens")
            return 1
        p = MF.set_price(key, *vals)
        print(f"{prof.model}: ${p['in']}/M in, ${p['out']}/M out, ${p['cached']}/M cached — "
              "receipts now show the cost")
        return 0
    if action in ("use", "show"):
        from .profile_store import active_name, get_profile, set_active
        from .paths import profile_name
        if action == "use":
            if not args.name or not set_active(args.name):
                print(f"no model setup '{args.name or ''}' — "
                      "aura model list")
                return 1
            print(f"profile '{profile_name()}' now uses model setup "
                  f"'{args.name}'")
            return 0
        cur = active_name()
        cfg = get_profile(cur) or {}
        print(f"model setup: {cur}  ({cfg.get('base_url', '?')} "
              f"model={cfg.get('model', '?')})  · profile '{profile_name()}'")
        return 0
    from .setup_quick import choose_endpoint
    rc = 0 if choose_endpoint() is not None else 1
    _offer_eval(rc)
    return rc


def cmd_update(args) -> int:
    """The newest release (checked, asks first), --check, --rollback, or a
    folder (--from); lifecycle.cmd_update / updater.py do the work."""
    from .lifecycle import cmd_update as _u
    return _u(getattr(args, "from_dir", None), check_only=getattr(args, "check", False),
              rollback=getattr(args, "rollback", False), assume_yes=getattr(args, "yes", False))


def cmd_uninstall(args) -> int:
    """1.7: remove launcher/venv/repo; --purge also deletes data."""
    from .lifecycle import cmd_uninstall as _u
    return _u(purge=bool(getattr(args, "purge", False)),
              assume_yes=bool(getattr(args, "yes", False)))


def cmd_auth(_args) -> int:
    """2.5: credential visibility — env keys + per-profile status, masked."""
    from .auth import collect, render
    print(render(collect()))
    return 0


def cmd_stats(args) -> int:
    """2.7: token usage across recent sessions."""
    from .usage_stats import collect, render
    eng = engagement_dir(getattr(args, "dir", None))
    print(render(collect(eng, limit=getattr(args, "limit", 10))))
    return 0


def cmd_send(args) -> int:
    """2.10: one-shot delivery, no LLM."""
    from .send import deliver, read_message
    eng = engagement_dir(getattr(args, "send_dir", None))
    msg = read_message(list(args.message or []))
    if not msg:
        print("send: empty message — pass text or pipe via stdin")
        return 1
    dest = deliver(eng, msg, to=getattr(args, "to", None))
    print(f"sent → {dest}")
    return 0


def cmd_insights(args) -> int:
    """3.13: session analytics from the audit tape."""
    from .insights import collect, render
    eng = engagement_dir(getattr(args, "ins_dir", None))
    print(render(collect(eng)))
    return 0


def cmd_proxy(args) -> int:
    """3.14: local OpenAI-compatible proxy over the active profile."""
    from .proxy import serve
    return serve(host=args.host, port=args.port,
                 profile_name=getattr(args, "profile", None))


def cmd_serve(args) -> int:
    """3.17: read-only engagement dashboard."""
    from .serve_dash import serve
    return serve(engagement_dir(getattr(args, "serve_dir", None)),
                 host=args.host, port=args.port)


def cmd_dump(_args) -> int:
    """4.6: support-sharing summary — what a bug report should include."""
    import os
    import platform
    from . import __version__
    from .profile_store import active_name, get_profile
    print("aura support dump")
    print(f"  aura      : v{__version__}")
    print(f"  python    : {platform.python_version()} "
          f"({platform.system()} {platform.release()})")
    name = active_name()
    prof = get_profile(name) or {}
    print(f"  profile   : {name} -> {prof.get('base_url', '?')} "
          f"model={prof.get('model', '?')}")
    print(f"  env       : AI_PROVIDER={os.environ.get('AI_PROVIDER', '-')} "
          f"AI_MODEL={os.environ.get('AI_MODEL', '-')} "
          f"AI_CONTEXT_LENGTH={os.environ.get('AI_CONTEXT_LENGTH', '-')}")
    # doctor-lite: live endpoint only, no keys
    try:
        from .setup import _fetch_models
        ok, models, err, code = _fetch_models(prof.get("base_url", ""),
                                              prof.get("api_key"))
        if ok:
            print(f"  endpoint  : live ({len(models)} models)")
        elif code in (401, 403):
            print(f"  endpoint  : needs key (HTTP {code})")
        else:
            print(f"  endpoint  : unreachable ({err[:60]})")
    except Exception as e:
        print(f"  endpoint  : probe failed ({e})")
    from .doctor_checks import run_checks
    checks, _ = run_checks(fix=False)
    bad = sum(1 for c in checks if not c["ok"])
    print(f"  doctor    : {'OK' if bad == 0 else f'{bad} issue(s)'}")
    print("  (no API keys or notebook data included)")
    return 0


def cmd_completion(args) -> str:
    """4.2: emit shell completion (argparse-generated, stdlib)."""
    # argparse ships a completion emitter via argcomplete only; we emit a
    # static, always-correct script from the command table instead
    cmds = ["run", "resume", "setup", "model", "update", "uninstall", "auth",
            "stats", "send", "insights", "proxy", "serve", "fleet", "skills",
            "gateway", "doctor", "profiles", "init", "status", "selftest",
            "export", "report", "receipts-verify", "cron", "import-agent",
            "chat", "eval", "scope", "settings", "approve", "reject", "prompt", "completion"]
    if args.shell == "bash":
        script = (
            "# aura bash completion\n"
            "_aura_completions() {\n"
            "  local cur=\"${COMP_WORDS[COMP_CWORD]}\"\n"
            f"  local cmds=\"{' '.join(cmds)}\"\n"
            "  COMPREPLY=($(compgen -W \"$cmds\" -- \"$cur\"))\n"
            "}\n"
            "complete -F _aura_completions aura\n"
            "complete -F _aura_completions aura\n")
    elif args.shell == "zsh":
        script = (
            "#compdef aura\n"
            f"local cmds=({' '.join(cmds)})\n"
            "_aura() { _describe 'command' cmds }\n"
            "compdef _aura aura\n")
    else:  # Fish
        cmds_fish = "\n".join(
            f"complete -c aura -n '__fish_use_subcommand' -a {c}"
            for c in cmds)
        script = f"# aura fish completion\n{cmds_fish}\n"
    print(script, end="")
    return 0


def cmd_gateway(args) -> int:
    """3.11: messaging delivery channels."""
    from . import gateway as G
    eng = engagement_dir(args.dir)
    cmd = args.gw_cmd
    if cmd == "status":
        print(G.status(eng=eng))
        trouble = G.health_lines()
        if trouble:
            print("targets with trouble:\n" + "\n".join(trouble))
        return 0
    if cmd == "send":
        out = G.deliver_message(" ".join(args.text), args.channel, eng=eng)
        mark = "✓" if out["ok"] else "✗"
        print(f"{mark} {out['where']}: {out['detail']}")
        return 0 if out["ok"] else 1
    if cmd == "test":
        results = G.probe(eng=eng)
        bad = 0
        for ch, r in results.items():
            mark = "✓" if r["ok"] else "✗"
            if not r["ok"]:
                bad += 1
            print(f"  {mark} {ch:9s} {r['detail'][:70]}")
        return 1 if bad else 0
    return 1


def _skills_signing(args) -> int:
    """keygen / sign / trust-key / keys for skill-pack publishers."""
    from .tools import packsign as PS
    cmd = args.skills_cmd
    try:
        if cmd == "keygen":
            out = Path(args.out).expanduser() if args.out else \
                _aura_home_dir() / "keys" / "signing.key"
            if out.exists():
                print(f"refusing to overwrite {out} — choose --out")
                return 1
            priv, pub = PS.generate_keypair()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(priv.hex(), encoding="utf-8")
            try:
                os.chmod(out, 0o600)
            except OSError:
                pass
            print(f"private key → {out}  (keep it secret; never share it)")
            print(f"public key  → {pub}")
            print("share the public key with people who install your packs;")
            print(f"to trust your own packs: aura skills trust-key {pub} "
                  "--name me")
            return 0
        if cmd == "sign":
            key = Path(args.key).expanduser()
            priv = bytes.fromhex(key.read_text(encoding="utf-8").strip())
            mf = PS.sign_pack(Path(args.pack).expanduser(), priv)
            doc = json.loads(mf.read_text(encoding="utf-8"))
            print(f"signed {args.pack}: {len(doc['manifest']['files'])} "
                  f"file(s), publisher key {doc['public_key']}")
            return 0
        if cmd == "trust-key":
            r = PS.trust_publisher(args.key, args.name or "")
            if not r["ok"]:
                print(f"trust-key refused: {r['error']}")
                return 1
            print(f"trusted publisher {r['name']} ({r['key'][:16]}…)")
            return 0
        if cmd == "keys":
            keys = PS.trusted_publishers()
            if not keys:
                print("no trusted publishers — aura skills trust-key <key>")
            for k, v in keys.items():
                print(f"  {v.get('name', '?'):16s} {k}")
            return 0
    except ImportError:
        print("pynacl is not installed — pip install pynacl")
        return 1
    except (OSError, ValueError) as e:
        print(f"{cmd} failed: {e}")
        return 1
    return 1


def cmd_teams(args) -> int:
    """Aura Teams — the shared board. The hub is offered, never forced: licence
    and source are shown first, the download happens on a yes (architecture).
    The token goes to the key store, never to the screen or the config.
    """
    from . import teams as T
    from . import teams_board as B
    cmd = getattr(args, "tm_cmd", None)
    if cmd == "hub":
        hcmd = getattr(args, "hub_cmd", "status")
        if hcmd == "setup":
            rel = T.latest_release()
            print(T.offer_text(rel))
            ans = input("download and start it? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("okay — nothing downloaded.")
                return 0
            meta = T.download_release(rel)
            print(f"downloaded and checksum-verified: {meta['asset']} "
                  f"(sha256 {meta['sha256'][:16]}…)")
            r = T.start_hub(getattr(args, "port", None))
            print(f"hub is up — teammates open {r['address']} ({r.get('version')})"
                  if r.get("ok") else f"hub failed to start: {r.get('detail')}")
            if r.get("ok"):
                print(T.ADMIN_NOTE)
            return 0 if r.get("ok") else 1
        if hcmd == "start":
            if T.load_config().get("mode") == "online":
                print("this Aura connects to an online hub — the hub runs on "
                      "the other machine; nothing to start here")
                return 1
            r = T.start_hub(getattr(args, "port", None))
            print(f"hub is up — teammates open {r['address']}"
                  + (f" · {r['chat']}" if r.get("chat") else "")
                  if r.get("ok") else f"hub failed to start: {r.get('detail')}")
            if r.get("ok"):
                print(T.ADMIN_NOTE)
            return 0 if r.get("ok") else 1
        if hcmd == "stop":
            r = T.stop_hub()
            print(r["detail"])
            return 0 if r["ok"] else 1
        if hcmd == "update":
            text, rel = T.update_offer()
            print(text)
            if rel is None:
                return 0
            ans = input("update it? (a dump is made first) [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("okay — nothing changed.")
                return 0
            r = T.apply_update(rel)
            print(r["detail"] + (f" · backup: {r['backup']}" if r.get("backup") else ""))
            return 0 if r["ok"] else 1
        if hcmd == "dump":
            r = T.hub_dump()
            print(r["detail"])
            return 0 if r["ok"] else 1
        if hcmd == "restore":
            r = T.hub_restore(args.dump_file)
            print(r["detail"])
            return 0 if r["ok"] else 1
        # hub status (default)
        st = T.hub_status()
        print("hub binary:", "installed" if st["binary"] else "not installed",
              f"({st['release']})" if st["release"] else "")
        return 0
    if cmd == "connect":
        import os as _os
        token = _os.environ.get("AURA_HUB_TOKEN", "")
        if not token and getattr(args, "token_stdin", False):
            token = sys.stdin.readline().strip()
        elif not token and sys.stdin.isatty():
            import getpass
            token = getpass.getpass("hub API token (input hidden): ").strip()
        if not token:
            print("no token: set AURA_HUB_TOKEN or use --token-stdin "
                  "(make one in Vikunja: Settings → API tokens)")
            return 2
        r = T.connect(args.url, args.user, token)
        if r["ok"]:
            print(f"connected to {r['hub']} as {r['username']} "
                  f"(hub {r.get('version')}); token in the key store")
            from . import teams_queue as Q
            c = Q.link_caution(r["hub"])
            if c:
                print(c)
            return 0
        print(r["detail"])
        return 1
    if cmd == "board":
        try:
            print(B.team_summary() if getattr(args, "all", False) else B.board_summary())
            return 0
        except B.BoardError as e:
            print(e)
            return 1
    if cmd == "link":
        try:
            r = B.match_request(" ".join(args.request))
        except B.BoardError as e:
            print(e)
            return 1
        if r["by_id"]:
            t = r["match"]
            if t:
                print(f"task #{t['id']} — {t.get('title', '')}")
                return 0
            print(f"task #{r['by_id']} is not on the board (or not yours)")
            return 1
        if r["candidates"]:
            if r["match"]:
                t = r["match"]
                print(f"This looks like #{t['id']} {t.get('title', '')} — "
                      f"is it that one? (yes/no)")
            else:
                print("Which task is it? Candidates:")
                for t in r["candidates"]:
                    print(f"  #{t['id']} {t.get('title', '')}")
            return 0
        print("No task on the board matches. Add it as a task so the team "
              "can see it? (without the board, the collision check can't "
              "cover this work)")
        return 1
    if cmd == "start":
        from . import teams_queue as Q
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        try:
            r = Q.task_start(args.task_id, args.summary,
                             T.load_config().get("username", ""), files)
        except B.BoardError as e:
            print(e)
            return 1
        if r.get("queued"):
            print("queued:", r["detail"])
        else:
            print(f"#{args.task_id} marked in progress")
        return 0 if r["ok"] else 1
    if cmd == "done":
        from . import teams_queue as Q
        changed = [f.strip() for f in args.changed.split(",") if f.strip()]
        try:
            r = Q.task_done(args.task_id, changed, args.shared, args.decision,
                            args.commit, args.pushed, args.proof)
            if r.get("held"):
                print(r["detail"])
                return 1
            if r.get("queued"):
                print("queued:", r["detail"])
                return 0
            if r["ok"]:
                print("done — proof and comment are on the board"
                      + (f" ({r['detail']})" if r.get("detail") else ""))
                return 0
            print(r.get("detail") or "could not mark done")
            return 1
        except B.BoardError as e:
            print(e)
            return 1
    if cmd == "decision":
        from . import teams_queue as Q
        try:
            r = Q.task_decision(args.task_id, " ".join(args.text))
            if r.get("queued"):
                print("queued:", r["detail"])
            else:
                print("decision recorded" if r["ok"] else "failed")
            return 0 if r["ok"] else 1
        except B.BoardError as e:
            print(e)
            return 1
    if cmd == "read":
        from . import teams_board as B
        try:
            r = B.read_task(args.task_id)
            for l in r["tells"]:
                print("·", l)
            print("labels:", ", ".join(r["labels"]) or "none")
            print("comments:", len(r["comments"]))
            return 0
        except B.BoardError as e:
            print(e)
            return 1
    if cmd == "check":
        from . import teams_collide as C
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        try:
            r = C.collision_check(args.task_id, files, args.repo,
                                  T.load_config().get("username", ""))
        except C.GitError as e:
            print("git trouble:", e)
            return 1
        for line in r["lines"]:
            print("·", line)
        if not r["can_check"]:
            return 1
        return 2 if r["hits"] else 0          # 2 = hits found, user decides
    if cmd == "git":
        from . import teams_collide as C
        try:
            if args.git_cmd == "branch":
                name = T.load_config().get("username") or ""
                if not name:
                    print("no username on record — `aura teams connect` first")
                    return 1
                print("on branch:", C.ensure_branch(args.repo, name))
                return 0
            if args.git_cmd == "commit":
                changed = [f.strip() for f in args.changed.split(",") if f.strip()]
                cid = C.commit_local(args.repo, changed, args.message)
                print("committed:", cid or "nothing to commit")
                return 0
            if args.git_cmd == "push":
                name = T.load_config().get("username") or ""
                r = C.push_branch(args.repo, name)
                print("pushed" if r["pushed"] else r["detail"])
                return 0
        except Exception as e:                       # noqa: BLE001
            print("git:", e)
            return 1
    if cmd == "queue":
        from . import teams_queue as Q
        if getattr(args, "drop", None) is not None:
            print(f"dropped {Q.drop(args.drop)} waiting update(s) for #{args.drop}")
            return 0
        print(Q.queue_summary())
        return 0
    if cmd == "sync":
        from . import teams_queue as Q
        try:
            r = Q.sync()
            for l in r.get("lines", []):
                print("·", l)
            print(r["detail"])
            return 0
        except Exception as e:                       # noqa: BLE001
            print("sync:", e)
            return 1
    if cmd == "chat":
        from . import teams_chat as CH
        ccmd = getattr(args, "chat_cmd", "status")
        if ccmd in ("on", "off"):
            cfg = T.load_config()            # Remembered: starts with the hub or not
            cfg["chat"] = ccmd
            T.save_config(cfg)
        if ccmd == "on":
            r = CH.start(f"http://127.0.0.1:{T.hub_port()}")
            print(f"chat page: http://{T.lan_address()}:{r['port']}/"
                  if r["ok"] else r["detail"])
            return 0 if r["ok"] else 1
        if ccmd == "off":
            print(CH.stop()["detail"] + " — it stays off when the hub starts "
                  "(`aura teams chat on` to bring it back)")
            return 0
        if CH.running():
            print(f"chat page: http://{T.lan_address()}:{CH.chat_port()}/ (running)")
        else:
            print("chat page: not running (starts with `aura teams hub start`)")
        return 0
    if cmd == "security":
        from . import teams_queue as Q
        ok = True
        for what, passed in Q.security_review():
            print(("✓" if passed else "✗"), what)
            ok = ok and passed
        return 0 if ok else 1
    # teams status (default)
    print(T.status_line())
    return 0
    return 0


def cmd_skills(args) -> int:
    """3.15: skill-pack marketplace with trust gates."""
    from . import skills_registry as SR
    global_ = not getattr(args, "local", False)
    cmd = args.skills_cmd
    # Signing commands (they were documented but never existed)
    if cmd in ("keygen", "sign", "trust-key", "keys"):
        return _skills_signing(args)
    if cmd in ("curator", "archive", "restore", "pin", "unpin"):
        # The person's own hand on their skills
        from . import skill_curator as SC
        eng = engagement_dir(None, Path.cwd())
        if cmd == "curator":
            print(SC.status(eng))
            return 0
        if cmd == "archive":
            print(SC.archive(eng, args.name))
        elif cmd == "restore":
            print(SC.restore(eng, args.name))
        else:
            print(SC.pin(eng, args.name, on=(cmd == "pin")))
        return 0
    if cmd in ("search", "add"):
        # public SKILL.md skills from GitHub repos (skills.sources), added as
        # untrusted packs — adopt after review
        from . import skill_hub as H
        if cmd == "search":
            try:
                hits = H.search(" ".join(args.words))
            except Exception as e:                       # noqa: BLE001
                print(f"couldn't search: {e}")
                return 1
            found = [h for h in hits if "name" in h]
            for h in hits:
                if "error" in h:
                    print(f"  ({h['repo']}: {h['error']})")
            for h in found:
                print(f"  {h['repo']}/{h['folder']}  [licence: {h['license']}]\n      {h['description']}")
            print("add one: aura skills add <repo>/<folder>" if found else "nothing found")
            return 0
        repo, _, folder = args.source.partition("/")
        repo2, _, folder = folder.partition("/")
        reg = PK_reg = None
        from . import packs as PK
        PK_reg = PK._registry(global_, engagement_dir(None) if not global_ else None)
        out = H.add(f"{repo}/{repo2}", folder, PK_reg)
        if not out["ok"]:
            print(f"not added: {out['error']}")
            return 1
        print(f"added {out['name']} (UNTRUSTED — it won't load yet) — {out['path']}")
        print(f"  licence: {out['license']}")
        if out["scripts"]:
            print(f"  it includes scripts: {', '.join(out['scripts'][:6])} — read them before trusting")
        print(f"  after reading it: aura skills adopt {out['name']}")
        return 0
    if cmd in ("import", "adopt"):
        from . import packs as PK
        eng = engagement_dir(None) if not global_ else None
        if cmd == "import":
            out = PK.import_plugin(Path(args.source).expanduser(), global_=global_, eng=eng)
            if not out["ok"]:
                print(f"import failed: {out['error']}")
                return 1
            print(f"imported {out['name']} (UNTRUSTED, not signed) — {out['path']}")
            print(f"  brings: {', '.join(out['parts']) or 'nothing usable'}"
                  + (f"; MCP servers: {', '.join(out['mcp_servers'])}" if out["mcp_servers"] else ""))
            if out["kept_not_run"]:
                print(f"  kept but not run: {', '.join(out['kept_not_run'])}")
            print("  review its files (and capabilities in manifest.json), then: "
                  f"aura skills adopt {out['name']}")
            return 0
        out = PK.adopt(args.name, global_=global_, eng=eng)
        print(f"adopted {args.name}: signed with your key and trusted" if out.get("ok")
              else f"not adopted: {out.get('error')}")
        return 0 if out.get("ok") else 1
    if cmd == "list":
        rows = SR.list_packs() if global_ else SR.list_packs(
            SR._registry(global_=False))
        if not rows:
            print("skills: none installed — aura skills install <pack-dir>")
            return 0
        for r in rows:
            mark = "trusted" if r["trusted"] else (
                "verified" if r["verified"] else "UNTRUSTED")
            print(f"  {r['name']:16s} {mark:10s} {r['fingerprint']}")
        return 0
    if cmd == "install":
        out = SR.install(Path(args.source).expanduser(), global_=global_)
        if out["ok"]:
            state = "verified" if out["verified"] else \
                "NOT verified: " + str((out.get("detail") or {}).get(
                    "reason", "unknown reason"))
            print(f"installed {out['name']} ({state}) — trust with: "
                  f"aura skills trust {out['name']}")
            return 0
        print(f"install failed: {out['error']}")
        return 1
    if cmd == "verify":
        out = SR.verify(args.name, global_=global_)
        print(f"{args.name}: {'OK' if out.get('ok') else 'FAIL'} "
              f"({out.get('signed_by', out.get('reason', ''))})")
        return 0 if out.get("ok") else 1
    if cmd == "trust":
        out = SR.trust(args.name, global_=global_)
        if out["ok"]:
            print(f"trusted {args.name} (fingerprint {out['fingerprint']})")
            # What the operator just allowed, in one line
            from .pack_limits import from_pack_dir, summary
            reg = SR._registry(global_=global_)
            lim = from_pack_dir(reg / args.name)
            if lim is not None:
                print(f"  once loaded, it may only — {summary(lim)}")
            return 0
        print(f"trust refused: {out['error']}")
        return 1
    if cmd == "audit":
        rows = SR.audit(global_=global_)
        if not rows:
            print("audit: nothing trusted yet")
            return 0
        bad = 0
        for r in rows:
            mark = "OK" if r["status"] == "OK" else r["status"]
            if r["status"] != "OK":
                bad += 1
            print(f"  {r['name']:16s} {mark}"
                  + (f" — {r.get('detail', '')}" if r.get("detail") else ""))
        return 1 if bad else 0
    return 1


def cmd_fleet(args) -> int:
    """3.8: background agent fleet."""
    from . import fleet as F
    from . import ui
    from .background import list_handles
    eng = engagement_dir(args.dir)
    cmd = args.fleet_cmd
    if cmd == "status":
        if list_handles():
            print(F.status_table())
            return 0
        mirrored = F.load_mirrored(eng)
        rows = mirrored.get("dispatches") or []
        if not rows:
            print("fleet: no dispatches yet — aura fleet dispatch \"task\" …")
            return 0
        print(f"fleet: last dispatch, {len(rows)} helper(s) "
              f"(saved {time.strftime('%Y-%m-%d %H:%M', time.localtime(mirrored.get('updated', 0)))})")
        for d in rows:
            mark = {"done": "✓", "failed": "✗"}.get(d.get("status"), "?")
            print(f"  {mark} {d.get('handle')} {d.get('name')}: "
                  f"{d.get('task', '')[:60]}")
            body = d.get("summary") or d.get("error") or ""
            if body:
                print(ui.dim("      " + body[:400].replace("\n", " ")))
        return 0
    if cmd == "dispatch":
        from .cli import build_agent
        _, m, loop = build_agent(eng, getattr(args, "profile", None))
        profile = loop.llm.p
        handles = F.dispatch(eng, list(args.tasks), profile, m.scope,
                             eng / "audit.jsonl", names=args.name)
        for h in handles:
            ui.info(f"dispatched {h['handle']}  {h['name']}: {h['task'][:60]}")
        print(ui.dim("  working — results print as each helper finishes "
                     "(Ctrl+C asks them to wrap up)"))

        def _landed(d):
            mark = "✓" if d.get("status") == "done" else "✗"
            ui.info(f"{mark} {d['handle']} {d['name']}: "
                    f"{(d.get('summary') or d.get('error') or '')[:300]}")
        done = F.wait_all(eng, _landed)
        failed = sum(1 for d in done if d.get("status") != "done")
        ui.info(f"fleet: {len(done) - failed} done, {failed} failed — "
                "full summaries: aura fleet status")
        return 1 if failed else 0
    if cmd == "collect":
        done = F.collect(eng)
        if not done:
            ui.info("nothing finished yet — aura fleet status")
            return 0
        for d in done:
            mark = "✓" if not d.get("error") else "✗"
            ui.info(f"{mark} {d['handle']} {d['name']}: "
                    f"{(d.get('summary') or d.get('error') or '')[:120]}")
        return 0
    if cmd == "steer":
        ok = F.steer(args.handle_id, " ".join(args.message))
        ui.info("steered" if ok else f"unknown handle {args.handle_id}")
        return 0 if ok else 1
    if cmd == "stop":
        ok = F.stop(args.handle_id)
        ui.info("stop queued" if ok else f"unknown handle {args.handle_id}")
        return 0 if ok else 1
    ui.warn("unknown fleet subcommand")
    return 1


def cmd_doctor(args) -> int:
    # 1.6: extended checks (profiles, endpoints, cache) + --fix, then the
    # original live endpoint ping
    from .accessibility import strip_glyphs
    from .doctor_checks import run_checks, render
    fix = bool(getattr(args, "fix", False))
    checks, fixed = run_checks(fix=fix)
    print("health checks:")
    print(strip_glyphs(render(checks, fixed)))
    print()
    profile = resolve(args.profile)
    llm = LLMClient(profile)
    print(f"profile : {profile.name}")
    print(f"endpoint: {profile.base_url}")
    print(f"model   : {profile.model}")
    # Provenance — WHERE each config value came from (source-not-value; kills
    # the env-var mystery class)
    import os as _os
    _prov = []
    for var in ("AI_BASE_URL", "AI_MODEL", "AI_API_KEY", "AI_PROVIDER"):
        val = _os.environ.get(var)
        if val:
            shown = " (value hidden)" if "KEY" in var else f" = {val}"
            _prov.append(f"{var} from env{shown}")
    if not _prov:
        _prov.append("no AI_* env overrides — profile file is the source")
    print("sources : " + " · ".join(_prov))
    ping = llm.ping()
    print("models  : " + (", ".join(ping["models"][:8]) if ping["ok"]
                         else f"UNREACHABLE ({ping['error']})"))
    ok = ping["ok"]
    # .1: probe the true context window + show its source
    from .ctx_probe import describe_source, probe_context_length
    ctx, src = probe_context_length(profile.base_url, profile.model,
                                    configured=None)  # Probe only, ignore profile override here
    cfg_ctx = profile.context_length
    flag = ""
    if cfg_ctx and cfg_ctx != ctx:
        # a size you set always wins over what the server reports
        flag = f"  ⚠ you set {cfg_ctx} — Aura uses that, not this"
    print(f"ctx     : {ctx} ({describe_source(src)}){flag}")
    print(f"verdict : {'OK' if ok else 'FAIL'} — endpoint "
          f"{'answers' if ok else 'not reachable'}")
    return 0 if ok else 1


def _pick_profiles(args) -> None:
    """-p/--profile names the WORK profile; --model the model setup. Before
    work profiles, --profile named the model setup — a name that is not a
    work profile but is a model setup still works that way, with a hint."""
    from . import ui
    name = getattr(args, "profile", None)
    model = getattr(args, "model_profile", None)
    args.profile = model                 # Everything below reads the model
    if not name:
        return
    from .paths import valid_profile_name
    from .workprofiles import exists, names
    if valid_profile_name(name) and exists(name):
        os.environ["AURA_PROFILE"] = name
        return
    from .profile_store import get_profile
    if get_profile(name) is not None:
        if not model:
            args.profile = name
        ui.warn(f"'{name}' is a model setup — use --model {name} "
                "(-p/--profile now picks a work profile)")
        return
    raise SystemExit(f"no profile '{name}' — work profiles: "
                     f"{', '.join(names())} (aura profile create {name}); "
                     "model setups: aura model list")


def cmd_keys(args) -> int:
    from .keystore import command
    return command(args)


def cmd_blueprint(args) -> int:
    """Ready-made scheduled jobs (blueprints.py): list, or fill one in, see
    exactly what will run, and add it after your yes — never silently."""
    from . import blueprints as BP
    if not args.name:
        print("ready-made scheduled jobs (aura blueprint <name> field=value ...):")
        for n, bp in BP.BLUEPRINTS.items():
            fields = " ".join(f"{k}={v or '...'}" for k, v in bp["fields"].items())
            print(f"  {n:16}{bp['about']}\n  {'':16}{fields}")
        return 0
    if args.name not in BP.BLUEPRINTS:
        print(f"no blueprint {args.name!r} — aura blueprint lists them")
        return 1
    values = BP.parse_values(args.values)
    task, sched_text, missing = BP.fill(args.name, values)
    if missing:
        try:
            for k in list(missing):
                key = k.split(" ")[0]
                values[key] = input(f"{key} ({k}): ").strip()
        except EOFError:
            print("not added — give " + ", ".join(m.split(" ")[0] + "=..." for m in missing))
            return 1
        task, sched_text, missing = BP.fill(args.name, values)
        if missing:
            print("not added — still missing: " + ", ".join(missing))
            return 1
    from .tools.cron_tool import MAX_JOBS, schedule_of
    from .cronops import add_job, load_jobs
    sched, _ = schedule_of(sched_text)
    if sched is None:
        print(f"the schedule {sched_text!r} isn't understood — use 'every 2 hours', 'every day', "
              "'every week' or 'at 09:30'")
        return 1
    eng = engagement_dir(getattr(args, "dir", None))
    if sum(1 for j in load_jobs(eng) if j.get("status", "active") == "active") >= MAX_JOBS:
        print(f"{MAX_JOBS} jobs are active already — remove or pause one first (aura cron list)")
        return 1
    print(f"{args.name}: {sched_text}\n  it will run: {task}\n"
          "  each run starts fresh and asks before anything risky, like every scheduled job")
    site_field = BP.BLUEPRINTS[args.name].get("needs_site")
    if site_field:
        import urllib.parse
        host = urllib.parse.urlparse(values.get(site_field, "")).hostname or ""
        print(f"  note: Aura reads only this project's allowed sites — `aura scope add {host}` "
              "if it isn't allowed yet")
    if not args.yes:
        try:
            if input("add it? [y/N] ").strip().lower() not in ("y", "yes"):
                print("not added")
                return 0
        except EOFError:
            print("not added (nobody to confirm — add --yes)")
            return 1
    j = add_job(eng, task, sched)
    print(f"added {j['id']} — it runs while `aura cron watch` is running (aura cron list shows it)")
    return 0


def cmd_vault(args) -> int:
    """Saved site logins the model never sees (site_vault.py): the browser's
    login action fills them; the model sees site + user name only."""
    from . import site_vault as V
    if args.vault_cmd == "list":
        labels = V.labels()
        print("saved logins (the model sees these names, never the passwords):" if labels
              else "no saved logins — aura vault add <site>")
        for lb in labels:
            print(f"  {lb['site']}  ({lb['user']})")
        return 0
    if args.vault_cmd == "remove":
        n = V.remove(args.site, args.user)
        print(f"removed {n} login(s) for {V.normal_site(args.site)}" if n else "nothing to remove")
        return 0 if n else 1
    import getpass
    site = V.normal_site(args.site)
    if not site:
        print("give a site, e.g. aura vault add github.com")
        return 1
    try:
        user = args.user or input(f"user name for {site}: ").strip()
        pw = getpass.getpass(f"password for {user} on {site} (not shown): ")
        totp = getpass.getpass("one-time-code secret (base32, Enter for none): ").strip() if args.totp else None
    except (EOFError, KeyboardInterrupt):
        print("\nnot saved")
        return 1
    try:
        ok = V.add(site, user, pw, totp or None)
    except Exception as e:                           # noqa: BLE001
        print(f"not saved: {e}")
        return 1
    print(f"saved the login for {site} ({user}) in your key store — the browser fills it on "
          f"{site} and its subdomains only, after asking you" if ok else "couldn't save it")
    return 0 if ok else 1


def cmd_profile(args) -> int:
    from .workprofiles import command
    return command(args)


def cmd_profiles(_args) -> int:
    print("(model setups — `aura model list` also shows yours and checks "
          "them; work profiles are `aura profile list`)")
    for name, cfg in PROFILES.items():
        print(f"{name:10s} {cfg['base_url']}  (the model is picked from the server's list: aura setup)")
    return 0


def cmd_init(args) -> int:
    """1.4: guided onboarding — skeleton + scope walk + verified ready."""
    from .init_onboard import run_onboard
    return run_onboard(args.name,
                       dir_hint=getattr(args, "init_dir", None),
                       assume_yes=bool(getattr(args, "yes", False)))


def _eval_helper(args) -> int:
    """Check the helper model keeps names and numbers exact (1 call)."""
    from . import helper as H
    p = H.profile(resolve(getattr(args, "profile", None)))
    if p is None:
        print("no helper model set — aura settings set helper.model <setup or model>")
        return 1
    out = H.check(p)
    print(H.state())
    return 0 if out.get("value") == "yes" else 1


def _eval_model(args) -> int:
    """Measure per-model choices once on the user's own model."""
    from . import model_facts as MF
    prof = resolve(getattr(args, "profile", None))
    key = MF.model_key(prof.base_url, prof.model)
    if getattr(args, "show", False):
        print("\n".join(MF.show(key)))
        return 0
    names = {"caps": "caps", "edit": "edit_form", "ask": "early_check"}
    only = [names.get(w.strip(), w.strip()) for w in
            (getattr(args, "only", "") or "").split(",") if w.strip()]
    bad = [w for w in only if w not in MF.ALL]
    if bad:
        print(f"--only takes caps, edit, ask (not {', '.join(bad)})")
        return 1
    which = tuple(only) or (MF.QUICK + (MF.DEEP if getattr(args, "deep", False) else ()))
    if not getattr(args, "again", False):
        # measure only what this model hasn't been measured for yet
        have = MF.load().get(key) or {}
        which = tuple(w for w in which if w not in have)
        if not which:
            print("\n".join(MF.show(key)))
            print("already measured — aura eval model --again to measure again")
            return 0
    full = bool(getattr(args, "full", False))
    print(f"measuring {key} ({', '.join(which)}): {MF.cost_note(which, full)}")
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print("not measured: nobody to confirm the cost (add --yes)")
            return 1
        if input("  go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not measured")
            return 1
    MF.measure(LLMClient(prof), key, which=which, full=full)
    print("\n".join(MF.show(key)))
    return 0


def cmd_eval(args) -> int:
    """2.1: the scored task suite — check (no model), run, compare, list."""
    from .evals import runner as R
    from .evals.suite import SUITE
    cmd = args.eval_cmd
    if cmd == "model":
        return _eval_model(args)
    if cmd == "helper":
        return _eval_helper(args)
    ids = [s for s in (getattr(args, "tasks", "") or "").split(",") if s]
    if cmd == "list":
        for t in R.select(ids):
            print(f"  {t['id']:26s} {t['category']:10s} {t['prompt'][:70]}")
        return 0
    if cmd == "check":
        rows = R.check_suite(R.select(ids))
        for r in rows:
            print(f"  {'ok ' if r['ok'] else 'BAD'} {r['id']}"
                  + (f" — {r['why']}" if r["why"] else ""))
        bad = sum(1 for r in rows if not r["ok"])
        print(f"{SUITE}: {len(rows) - bad}/{len(rows)} tasks valid "
              "(checker fails unsolved, passes the reference solution)")
        return 1 if bad else 0
    if cmd == "compare":
        a = json.loads(Path(args.a).read_text(encoding="utf-8"))
        b = json.loads(Path(args.b).read_text(encoding="utf-8"))
        print("\n".join(R.compare(a, b)))
        return 0
    if cmd == "run":
        tasks = R.select(ids)
        if not tasks:
            print("no tasks match — aura eval list")
            return 1
        prof = resolve(args.profile)
        if getattr(args, "prompt", None):
            # which prompt to measure: the model's copy or the default
            os.environ["AURA_PROMPT"] = args.prompt
        print(f"running {len(tasks)} task(s) of {SUITE} against "
              f"{getattr(prof, 'model', '?')} ({getattr(prof, 'name', '?')}) "
              "— each in a fresh temporary workspace, headless")

        def show(r):
            print(f"  {'PASS' if r['passed'] else 'fail'} {r['id']:26s} "
                  f"{r['steps']:>3} steps {r['tokens']:>8,} tok "
                  f"{r['seconds']:>6.1f}s"
                  + ("" if r["passed"] else f"  {r['why'][:80]}"))
        doc = R.run_suite(tasks, args.profile, on_result=show,
                          max_steps=args.max_steps,
                          time_limit_s=args.time_limit, keep=args.keep)
        if doc.get("aborted"):
            print(f"stopped: {doc['aborted']}")
            return 1
        out = Path(args.out or f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json")
        out.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        from .evals.auto import save as _save_measured
        _save_measured(doc)
        print(f"score {doc['passed']}/{len(doc['tasks'])} "
              f"({doc['score']:.0%}) · {doc['tokens']:,} tokens · saved {out}")
        _hr = doc.get("cache_hit_ratio")
        print(f"measured with prompt: {doc.get('prompt')} · cache mode "
              f"{doc.get('cache_mode')}"
              + (f" · {_hr:.0%} of prompt tokens from cache" if _hr is not None
                 else " · provider reported no cache data"))
        print("compare two runs: aura eval compare <old.json> <new.json>")
        return 0
    return 1


def _chat_ref(eng, ref: str | None) -> tuple[str | None, int]:
    """A chat named on the command line (#id, a start of it, latest, its
    title, or a file) → its file, or (None, exit code) after saying why."""
    from . import ui
    from .session_store import resolve
    if not ref:
        return None, 0
    p = Path(ref)
    if p.suffix == ".jsonl" and p.exists():
        return str(p), 0
    path, many = resolve(eng, ref)
    if path is not None:
        return str(path), 0
    if many:
        ui.warn(f"{len(many)} chats match {ref!r}: "
                + ", ".join(f"#{s['id']}" for s in many[:6]) + " — give more of the id")
    else:
        ui.warn(f"no chat {ref!r} in this project — `aura chat` then /resume lists them")
    return None, 1


def cmd_chat(args) -> int:
    from .repl import chat
    eng = engagement_dir(args.dir)
    import atexit
    atexit.register(discard_if_unused, eng)   # Opened and left: nothing stays
    ref = getattr(args, "resume", None) or ("latest" if getattr(args, "continue_", False) else None)
    if ref and not getattr(args, "session", None):
        args.session, code = _chat_ref(eng, ref)
        if code:
            return code
    # First-run gate (chat path) — only when no explicit profile/session
    if not getattr(args, "profile", None) and not getattr(args, "session", None):
        import sys as _sys
        from .first_run import ensure_working_profile
        prof = ensure_working_profile(eng, interactive=_sys.stdout.isatty())
        if prof is not None:
            args.profile = prof.name
    return chat(eng, args.profile, args.session, args.task,
                name=getattr(args, "name", None),
                fork=getattr(args, "fork", None))


def cmd_selftest(args) -> int:
    from .engine.selftest import run_selftest
    print(run_selftest())
    return 0


def cmd_report(args) -> int:
    """Write report.md from the PROVEN results (unproven ones are listed as left
    out, never reported).
    """
    from . import ui
    from .engine.report import generate_report
    from .tools.findings import FindingStore
    ui.enable()
    eng = engagement_dir(args.dir)
    store = FindingStore(eng / "findings.jsonl")
    dest = Path(args.out) if args.out else eng / "report.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(generate_report(eng, store), encoding="utf-8")
    proven = len(store.reportable())
    ids = {r["id"] for r in store.records}
    ui.info(f"report → {dest}  ({proven} proven, "
            f"{len(ids) - proven} left out)")
    return 0


def cmd_export(args) -> int:
    """`aura export trajectories`."""
    from . import ui
    ui.enable()
    eng = engagement_dir(args.dir)
    if args.what == "trajectories":
        from .trajectories import dataset_stats, export_trajectories
        dest = export_trajectories(eng, Path(args.out) if args.out else None)
        stats = dataset_stats(dest)
        ui.info(f"{stats['records']} trajectory record(s) → {dest} "
                f"({stats['by_kind']}, {stats['message_turns']} turns, "
                f"{stats['tool_calls']} tool calls)")
        return 0
    ui.warn(f"unknown export target: {args.what}")
    return 1


def cmd_receipts_verify(args) -> int:
    """Check the run-receipt chain. Exit 1 when it is broken."""
    from .receipts import render_verify, verify_chain
    result = verify_chain(engagement_dir(args.dir))
    print(render_verify(result))
    return 0 if result["ok"] else 1


def cmd_cron(args) -> int:
    """Local cron — list|add|run|remove."""
    from . import ui
    ui.enable()
    from .cronops import (
        add_job,
        load_jobs,
        parse_schedule,
        remove_job,
        run_due,
        run_job,
    )
    eng = engagement_dir(args.dir)

    if args.cron_cmd == "list":
        jobs = load_jobs(eng)
        if not jobs:
            ui.info("no cron jobs — `aura cron add`")
            return 0
        for j in jobs:
            last = time.strftime("%H:%M:%S", time.localtime(
                j["last_run_ts"])) if j.get("last_run_ts") else "never"
            ui.info(f"{j['id']}  [{j['schedule'].get('kind')}] "
                    f"runs={j.get('runs', 0)} last={last} "
                    f"status={j.get('status', 'active')}")
            print(ui.dim(f"    task: {j['task'][:90]}"))
        return 0
    if args.cron_cmd == "add":
        schedule, remaining = parse_schedule(args.task)
        job = add_job(eng, remaining or args.task, schedule)
        ui.success(f"added {job['id']} [{schedule.get('kind')}]")
        print(ui.dim(f"    task: {(remaining or args.task)[:90]}"))
        # nothing runs jobs by itself — say what does
        print(ui.dim("    it runs while `aura cron watch` is running (from "
                     "any folder — it serves every project of this "
                     "profile), or have your OS scheduler run `aura cron "
                     "run` in this folder (Task Scheduler / crontab)"))
        return 0
    if args.cron_cmd == "run":
        if getattr(args, "job_id", None):
            out = run_job(eng, args.job_id, args.profile)
            ui.info(("ok" if out["ok"] else "failed") +
                    f" — steps {out.get('steps')}")
            print(ui.dim(f"    {out.get('summary', '')[:160]}"))
            return 0 if out["ok"] else 1
        results = run_due(eng, args.profile)
        ui.info(f"{len(results)} due job(s) run")
        for r in results:
            ui.info(("ok" if r["ok"] else "failed") +
                    f" {r['job_id']} — {r.get('summary', '')[:80]}")
        return 0
    if args.cron_cmd == "watch":
        from .cronops import cron_notebooks, watch
        nbs = cron_notebooks()
        ui.info(f"watching {len(nbs)} project(s) with scheduled jobs — "
                "checks every minute; Ctrl+C stops")

        def _ran(nb, r):
            ui.info(("ok" if r["ok"] else "failed") + f" {r['job_id']} "
                    f"({nb.name}) — {r.get('summary', '')[:80]}")
        try:
            n = watch(_ran, once=bool(getattr(args, "once", False)))
            ui.info(f"{n} job(s) run")
        except KeyboardInterrupt:
            ui.info("stopped watching")
        return 0
    if args.cron_cmd == "remove":
        ok = remove_job(eng, args.job_id)
        ui.info("removed" if ok else "no such job")
        return 0 if ok else 1
    if args.cron_cmd == "runner":
        from . import setup_steps as SS
        ctx: dict = {}
        if args.state in ("on", "off"):
            for line in SS._bg_save(ctx, {"background.runner": args.state}):
                print("  " + line)
        for f in SS._bg_look(ctx):
            print(f"  {'✓' if f.state == 'ok' else '!' if f.state == 'warn' else '·'} {f.text}")
        proof = SS._bg_check(ctx)
        print(f"  {'✓' if proof.ok else '!'} {proof.evidence}")
        if proof.fix:
            print(f"    fix: {proof.fix}")
        if SS.runner_kind() == "none" and args.state == "on":
            print("  this system has no per-user service manager — keep `aura cron watch` "
                  "running (a terminal / tmux), or add `aura cron run` to crontab")
        return 0 if proof.ok else 1
    ui.warn("unknown cron subcommand")
    return 1


def cmd_import_agent(args) -> int:
    """Import another agent's instruction files → engagement AGENTS.md."""
    from . import ui
    ui.enable()
    from .onboard import import_agent
    eng = engagement_dir(args.dir)
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        ui.error(f"not a directory: {source}")
        return 1
    out = import_agent(eng, source)
    if not out["imported"]:
        ui.warn(f"no instruction files found in {source} "
                "(looked for AGENTS.md, CLAUDE.md, .cursorrules)")
        return 1
    ui.success(f"imported: {', '.join(out['imported'])}")
    ui.info(f"notebook AGENTS.md → {out['agents_md']} "
            f"({out['merged_chars']} chars)")
    if out["profile_hint"]:
        ui.info(f"profile hint: {out['profile_hint'].get('model')} "
                f"(from {out['profile_hint'].get('source')}) — "
                "verify then `aura profiles use`")
    return 0


def cmd_settings(args) -> int:
    """The same settings text commands, from the terminal."""
    from .options_cmd import settings_command
    lines = settings_command(list(getattr(args, "words", None) or []))
    for ln in lines:
        print(ln)
    return 1 if lines and lines[0].startswith("no option") else 0


def cmd_history(args) -> int:
    """Search past chats from the terminal (same engine as search and the model's
    history_search).
    """
    from .history import parse_inline, rebuild, search, update
    if getattr(args, "rebuild", False):
        rebuild()
        print(f"index rebuilt: {update(engagement_dir(None))} chats read")
        return 0
    a = parse_inline(" ".join(getattr(args, "words", None) or []))
    if getattr(args, "session", None):
        a["session"] = args.session
        if getattr(args, "around", None):
            a["around"] = args.around
    print(search(a, engagement_dir(None)))
    return 0


# what Aura uses (pyproject.toml): required, then optional extras
_PARTS = (("pynacl", "required"), ("wcwidth", "required"), ("keyring", "required"),
          ("playwright", "optional: web"), ("pypdf", "optional: pdf"),
          ("cryptography", "optional: vault"), ("pytest", "optional: test"),
          ("hypothesis", "optional: test"))


def cmd_mcp(args) -> int:
    """MCP servers: search the official registry, add (asks), list, remove."""
    from . import mcp_registry as R
    eng = engagement_dir(getattr(args, "dir", None))
    path = R.config_file(eng, getattr(args, "project", False))
    if args.mcp_cmd == "search":
        try:
            hits = R.search(" ".join(args.words), limit=args.limit)
        except Exception as e:                           # noqa: BLE001
            print(f"couldn't reach the MCP registry: {e}")
            return 1
        if not hits:
            print("nothing found — try other words")
        for h in hits:
            how = (h["spec"] or {}).get("command") or ("remote" if (h["spec"] or {}).get("url") else "?")
            need = f" · needs {', '.join(h['needs'])}" if h["needs"] else ""
            print(f"  {h['name']}  [{how}]{need}\n      {h['description']}")
        if hits:
            print("add one: aura mcp add <name>   (shows what it runs and asks first)")
        return 0
    if args.mcp_cmd == "list":
        servers = R.listing(path)
        print(f"MCP servers in {path}:" if servers else f"no MCP servers in {path}")
        for n, sp in servers.items():
            print(f"  {n}: {sp.get('url') or ' '.join([sp.get('command', '')] + sp.get('args', []))}")
        return 0
    if args.mcp_cmd in ("login", "logout"):
        # a browser sign-in to a remote server (OAuth 2.1 + PKCE, mcp_oauth.py)
        servers = {**R.listing(R.config_file(eng, True)), **R.listing(R.config_file(eng, False))}
        sp = servers.get(args.name)
        if not sp or not sp.get("url"):
            print(f"no remote server {args.name!r} — aura mcp list shows them (only remote ones sign in)")
            return 1
        from . import mcp_oauth as O
        if args.mcp_cmd == "logout":
            O.logout(sp["url"])
            print(f"signed out of {args.name}")
            return 0
        try:
            # --paste: a machine the browser can't call back to (SSH, a container)
            O.login(sp["url"], wait=5 if args.paste else 300,
                    paste=input if (args.paste or sys.stdin.isatty()) else None)
        except O.OAuthError as e:
            print(f"sign-in failed: {e}")
            return 1
        print(f"signed in to {args.name} — the token is kept in your key store and renewed as needed")
        return 0
    if args.mcp_cmd == "remove":
        ok = R.remove(args.name, path)
        print(f"removed {args.name}" if ok else f"no server {args.name!r} in {path}")
        return 0 if ok else 1
    if args.mcp_cmd == "popular":
        print("well-known servers, each from its own publisher (checked in the official registry):")
        for short, p in R.POPULAR.items():
            print(f"  {short:16}{p['what']}\n  {'':16}by {p['by']} · {p['name']} · needs: {p['account']}")
        print("add one: aura mcp add <short name>   (shows what it runs and asks first)")
        return 0
    # Add: the exact registry entry (a short name from `aura mcp popular`, or a
    # full registry name) — its NEWEST version, what it will run, then your
    # yes.
    try:
        h = R.exact(args.name)
        near = [] if h else R.search(args.name, limit=5)
    except Exception as e:                               # noqa: BLE001
        print(f"couldn't reach the MCP registry: {e}")
        return 1
    if h is None:
        print(f"no server named exactly {args.name!r}.")
        if near:
            print("did you mean one of these? (check the publisher — lookalikes exist)")
            for n in near:
                print(f"  {n['name']}  — {n['description'][:80]}")
        print("well-known ones: aura mcp popular")
        return 1
    if not h["spec"]:
        print(f"{h['name']} can't run here (no package or address Aura knows how to start)")
        return 1
    local = args.as_name or (args.name.lower() if args.name.lower() in R.POPULAR
                             else R.short_name(h["name"]))
    sp = h["spec"]
    runs = sp.get("url") or " ".join([sp["command"]] + sp.get("args", []))
    print(f"{h['name']} {h['version']}\n  {h['description']}\n  it runs: {runs}")
    if h["needs"]:
        print(f"  it needs: {', '.join(h['needs'])} — store them with `aura keys set NAME` "
              "(Aura passes them as ${NAME}, never as text)")
    missing = R.runner_missing(sp)
    if missing:
        print(f"  note: {missing} isn't on this machine — the server can't start until it is")
    print("  this downloads and runs someone else's code each time Aura starts; its tools "
          "that change things ask you first")
    if not args.yes:
        try:
            if input(f"add it as '{local}'? [y/N] ").strip().lower() not in ("y", "yes"):
                print("not added")
                return 0
        except EOFError:
            print("not added (nobody to confirm — add --yes)")
            return 1
    R.add(local, sp, path)
    print(f"added '{local}' to {path}")
    return 0


def cmd_licenses(args) -> int:
    """The parts Aura uses: version and licence (a release must list them).
    Aura bundles none of them and installs none by itself."""
    from importlib.metadata import PackageNotFoundError, metadata
    print(f"{'part':14}{'role':18}{'version':12}licence")
    for name, role in _PARTS:
        try:
            m = metadata(name)
            lic = (m.get("License-Expression") or "").strip()
            if not lic:
                cls = [c.split("::")[-1].strip() for c in (m.get_all("Classifier") or [])
                       if c.startswith("License ::")]
                lic = ", ".join(cls) or (m.get("License") or "?").splitlines()[0][:40]
            print(f"{name:14}{role:18}{m['Version']:12}{lic}")
        except PackageNotFoundError:
            print(f"{name:14}{role:18}{'-':12}(not installed)")
    print("Aura itself: Apache-2.0. Language servers and browsers you install separately "
          "(pyright: MIT; Playwright's Chromium: open source) keep their own licences.")
    return 0


def cmd_usage(args) -> int:
    """What runs used and cost: per day or per model, this project or all."""
    from .usage_report import project_dirs, table
    here = engagement_dir(getattr(args, "dir", None))
    if getattr(args, "cache", False):
        from .usage_report import cache_lines
        for ln in cache_lines(project_dirs(args.all, here), since=args.since):
            print(ln)
        return 0
    for ln in table(project_dirs(args.all, here), since=args.since, by=args.by):
        print(ln)
    return 0


def cmd_regress(args) -> int:
    """'Did Aura get worse?' — record a task once (live), replay it free."""
    from . import regress as RG
    if args.regress_cmd == "list":
        for n in RG.names():
            print(f"  {n}")
        if not RG.names():
            print("no recordings yet — aura regress record <name> \"<task>\"")
        return 0
    if args.regress_cmd == "record":
        proj = Path(getattr(args, "project", None) or Path.cwd())
        print(f"recording '{args.name}' on a copy of {proj} — a live run, it uses model calls")
        RG.record(args.name, args.task, proj)
        return 0
    todo = args.names or RG.names()
    if not todo:
        print("no recordings yet — aura regress record <name> \"<task>\"")
        return 1
    bad = 0
    for n in todo:
        r = RG.replay(n)
        if r["same"]:
            print(f"  {n}: same as recorded ({r['recorded']} replies replayed, no model calls)")
            if r.get("where"):
                print(f"    (same outcome, but {r['where']})")
        else:
            bad += 1
            print(f"  {n}: CHANGED")
            for d in r["diffs"]:
                print(f"    - {d}")
            if r.get("where"):
                print(f"    - {r['where']}")
    return 1 if bad else 0


def cmd_scope(args) -> int:
    """Where this project's agent may reach on the network."""
    from .engagement import scope_add, scope_lines, scope_remove
    eng = engagement_dir(args.dir)
    if args.action == "list":
        print("\n".join(scope_lines(eng)))
        return 0
    if not args.item:
        print(f"usage: aura scope {args.action} <example.com | IP | range>")
        return 1
    try:
        print((scope_add if args.action == "add" else scope_remove)(
            eng, args.item))
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    return 0


def cmd_approve(args) -> int:
    """Answer a run that is waiting for approval (piped `aura run`, cron).
    No id: list the open approvals of this project."""
    from .approvals import answer, load_pending
    eng = engagement_dir(args.dir)
    if not args.id:
        open_ = load_pending(eng)
        if not open_:
            print("no run is waiting for approval in this project")
            return 0
        for r in open_:
            print(f"{r.get('id')}  {r.get('tool')} → {r.get('target')} "
                  f"({r.get('grade')})")
        return 0
    msg = answer(args.id, args.approve, " ".join(args.reason or []),
                 first=eng)
    print(msg)
    return 1 if msg.startswith("no open approval") else 0


def cmd_status(args) -> int:
    # 2.2: agent-level HUD first (version/profile/live/ctx/doctor), then the
    # engagement section when one exists
    from .accessibility import strip_glyphs
    from .agent_status import render_agent_status
    eng = engagement_dir(args.dir)
    print(strip_glyphs(render_agent_status(eng)))
    return 0


def _drop_teams_parser(sub) -> None:
    """A copy of Aura shipped without Teams has no `aura teams`."""
    from .editions import teams_present
    if teams_present():
        return
    sub._name_parser_map.pop("teams", None)
    sub._choices_actions[:] = [a for a in sub._choices_actions if a.dest != "teams"]


def build_parser() -> argparse.ArgumentParser:
    """Every `aura …` command (also used by the chat's /team, so the chat
    runs the same commands, not copies of them)."""
    ap = argparse.ArgumentParser(prog="aura",
                                 description=f"Aura — local-first autonomous agent v{__version__}")
    ap.add_argument("--version", "-V", action="version",
                    version=f"Aura v{__version__}")
    ap.add_argument("-p", "--profile",
                    help="work profile for this launch (default: the one "
                         "`aura profile use` picked, else 'default')")
    ap.add_argument("--model", dest="model_profile",
                    help="model setup to use (endpoint + model + key); "
                         "see `aura model list`")
    ap.add_argument("--dir", help="project folder (default: where you are)")
    ap.add_argument("--add-dir", dest="add_dirs", action="append", default=[],
                    metavar="FOLDER",
                    help="a folder outside the project Aura may also use (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # UX: the global flags are ALSO accepted after the subcommand —
    # `aura run --profile x task` must not be a raw usage dump (idiot-proof
    # law: the error path suggests the fix, never just the grammar).
    for _name in ("run", "resume", "chat", "serve", "status"):
        _sp = sub.choices.get(_name)
        if _sp is None:
            continue
        _sp.add_argument("--profile", help=argparse.SUPPRESS)
        _sp.add_argument("--dir", help=argparse.SUPPRESS)
    r = sub.add_parser("run"); r.add_argument("task")
    r.add_argument("--research", choices=["basic", "normal", "deep"],
                   help="basic = only the listed sources; normal (default) = anything on "
                        "the web, kept small; deep = plan first, two sources per question")
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("resume", help="open a chat where it was left: #id, a start "
                                        "of it, its title, or latest (the default)")
    rs.add_argument("file", nargs="?", metavar="CHAT")
    rs.add_argument("--run", dest="run_last", action="store_true",
                    help=argparse.SUPPRESS)       # The old way: run its last task again
    rs.set_defaults(fn=cmd_resume)
    st = sub.add_parser("setup",
                        help="guided setup wizard (detects local endpoints)")
    st.add_argument("mode", nargs="*",
                    help="quick (default): the model, how much Aura asks, your team "
                         "username · full: every step · minimal: no scanning · "
                         "a step (import, model, asking, safety, tools, web, connect, away, "
                         "you, team) · status")
    st.add_argument("--answers", metavar="FILE",
                    help="answers for unattended setup (JSON; AURA_SETUP_<KEY> too)")
    st.add_argument("--non-interactive", action="store_true",
                    help="ask nothing: answers file, environment, else the suggestions")
    st.add_argument("--dry-run", action="store_true",
                    help="show what would be saved, save nothing")
    st.set_defaults(fn=cmd_setup)
    md = sub.add_parser("model",
                        help="model setups (endpoint + model + key): pick / "
                             "list / use / show")
    md.add_argument("action", nargs="?", default="pick",
                    choices=["pick", "list", "use", "show", "price", "settings"],
                    help="pick: interactive chooser (default) · "
                         "list: setups + live reachability · "
                         "use <name>: the default setup for this profile · "
                         "show: the one in use · "
                         "settings [maker|aura]: the model maker's own settings "
                         "(temperature …) or Aura's")
    md.add_argument("name", nargs="?")
    md.add_argument("prices", nargs="*", help="price: input output [cached] — $ per million tokens")
    md.set_defaults(fn=cmd_model)
    stats_p = sub.add_parser(
        "stats", help="token usage across recent sessions (measured/est)")
    stats_p.add_argument("--limit", type=int, default=10,
                         help="how many recent sessions to include")
    stats_p.set_defaults(fn=cmd_stats)
    snd = sub.add_parser("send",
                         help="one-shot message delivery (no LLM): notebook "
                              "outbox or --to file:PATH")
    snd.add_argument("message", nargs="*", help="message text (or stdin)")
    snd.add_argument("--to", default=None,
                     help="file:PATH (notebook-relative ok) instead of "
                          "the outbox")
    snd.add_argument("--dir", dest="send_dir", default=None,
                     help="workspace folder (default ./engagement)")
    snd.set_defaults(fn=cmd_send)
    sv = sub.add_parser("serve",
                        help="read-only notebook dashboard over HTTP "
                             "(127.0.0.1 by default)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8092)
    sv.add_argument("--dir", dest="serve_dir", default=None,
                    help="workspace folder (default ./engagement)")
    sv.set_defaults(fn=cmd_serve)
    px = sub.add_parser("proxy",
                        help="local OpenAI-compatible endpoint forwarding to "
                             "the active profile (127.0.0.1 only)")
    px.add_argument("--port", type=int, default=8091)
    px.add_argument("--host", default="127.0.0.1")
    px.add_argument("--profile", default=None,
                    help="profile to forward to (default: active)")
    px.set_defaults(fn=cmd_proxy)
    ins = sub.add_parser("insights",
                         help="session analytics from the audit tape "
                              "(tools, targets, failures, activity)")
    ins.add_argument("--dir", dest="ins_dir", default=None,
                     help="workspace folder (default ./engagement)")
    ins.set_defaults(fn=cmd_insights)
    fl = sub.add_parser("fleet",
                        help="background agent fleet: dispatch N parallel "
                             "tasks, monitor, steer, collect")
    fl_sub = fl.add_subparsers(dest="fleet_cmd", required=True)
    fdis = fl_sub.add_parser("dispatch",
                             help="spawn one background agent per task")
    fdis.add_argument("tasks", nargs="+", help="task text(s)")
    fdis.add_argument("--name", action="append", default=None,
                      help="name per agent (repeatable)")
    fst = fl_sub.add_parser("status", help="dispatch table")
    fcol = fl_sub.add_parser("collect",
                             help="drain finished dispatches + summaries")
    fsteer = fl_sub.add_parser("steer",
                               help="course-correct a running dispatch")
    fsteer.add_argument("handle_id")
    fsteer.add_argument("message", nargs="+")
    fstop = fl_sub.add_parser("stop", help="cooperative stop a dispatch")
    fstop.add_argument("handle_id")
    fl.set_defaults(fn=cmd_fleet)
    sk = sub.add_parser("skills",
                        help="skill-pack marketplace: install/verify/trust/"
                             "audit with Ed25519 trust gates")
    sk_sub = sk.add_subparsers(dest="skills_cmd", required=True)
    skl = sk_sub.add_parser("list", help="installed packs + trust state")
    ski = sk_sub.add_parser("install", help="stage a pack (untrusted)")
    ski.add_argument("source", help="pack dir with signed manifest.json")
    ski.add_argument("--local", action="store_true",
                     help="install into the notebook's skills/ instead of ~/.aura")
    skv = sk_sub.add_parser("verify", help="signature + digest check")
    skv.add_argument("name")
    skv.add_argument("--local", action="store_true")
    skt = sk_sub.add_parser("trust",
                            help="mark a VERIFIED pack trusted (fingerprint "
                                 "bound)")
    skt.add_argument("name")
    skt.add_argument("--local", action="store_true")
    ska = sk_sub.add_parser("audit",
                            help="re-verify packs; report DRIFT/BROKEN")
    ska.add_argument("--local", action="store_true")
    skg = sk_sub.add_parser("keygen", help="create a publisher signing key")
    skg.add_argument("--out", help="where to write the private key")
    sks = sk_sub.add_parser("sign", help="sign a pack directory")
    sks.add_argument("pack", help="pack directory")
    sks.add_argument("--key", required=True, help="private key file")
    skk = sk_sub.add_parser("trust-key",
                            help="trust a publisher's public key")
    skk.add_argument("key", help="64-hex-character public key")
    skk.add_argument("--name", help="a label for this publisher")
    sk_sub.add_parser("keys", help="list trusted publisher keys")
    sks = sk_sub.add_parser("search", help="find public SKILL.md skills (skills.sources, "
                                           "default anthropics/skills)")
    sks.add_argument("words", nargs="*")
    ska = sk_sub.add_parser("add", help="add one as an UNTRUSTED pack: <owner>/<repo>/<folder>")
    ska.add_argument("source")
    ska.add_argument("--local", action="store_true", help="this project only")
    skm = sk_sub.add_parser("import", help="turn a Claude-Code-format plugin folder "
                                           "into a pack (untrusted)")
    skm.add_argument("source", help="plugin folder (has .claude-plugin/plugin.json)")
    skm.add_argument("--local", action="store_true")
    skd = sk_sub.add_parser("adopt", help="after review: sign a pack with your own "
                                          "key and trust it")
    skd.add_argument("name")
    skd.add_argument("--local", action="store_true")
    sk_sub.add_parser("curator", help="your skills: pinned, how each did when picked, "
                                      "what is put aside")
    for _c, _h in (("archive", "put one of your skills aside (restorable)"),
                   ("restore", "bring a put-aside skill back"),
                   ("pin", "offer this skill to the model on every turn"),
                   ("unpin", "stop offering it on every turn")):
        sk_sub.add_parser(_c, help=_h).add_argument("name")
    sk.set_defaults(fn=cmd_skills)
    gw = sub.add_parser("gateway",
                        help="messaging delivery: telegram/discord/slack/"
                             "webhook/outbox")
    gw_sub = gw.add_subparsers(dest="gw_cmd", required=True)
    gst = gw_sub.add_parser("status", help="configured channels (masked)")
    gsnd = gw_sub.add_parser("send", help="deliver a message now")
    gsnd.add_argument("channel", choices=list(
        ("telegram", "discord", "slack", "webhook", "outbox")))
    gsnd.add_argument("text", nargs="+")
    gte = gw_sub.add_parser("test", help="probe every configured channel")
    gw.set_defaults(fn=cmd_gateway)
    tm = sub.add_parser("teams",
                        help="Aura Teams: the shared board (hub) and your "
                             "connection to it")
    tm_sub = tm.add_subparsers(dest="tm_cmd")
    tmh = tm_sub.add_parser("hub", help="the hub program (works with Vikunja)")
    tmh_sub = tmh.add_subparsers(dest="hub_cmd")
    tmhs = tmh_sub.add_parser("setup",
                              help="offer the hub download: licence, source and "
                                   "size first; downloads only on a yes")
    tmhs.add_argument("--port", type=int, help="the hub's port (default 3456; remembered)")
    tmhst = tmh_sub.add_parser("start", help="start the hub (data in Aura's folder)")
    tmhst.add_argument("--port", type=int, help="the hub's port (default: the last one, else 3456)")
    tmh_sub.add_parser("stop", help="stop the hub")
    tmh_sub.add_parser("status", help="is the hub installed / running")
    tmh_sub.add_parser("update",
                       help="offer a new hub version (only on a yes)")
    tmh_sub.add_parser("dump", help="Vikunja's own dump, of this hub's data")
    tmhr2 = tmh_sub.add_parser("restore", help="restore a dump on this machine")
    tmhr2.add_argument("dump_file")
    tmc = tm_sub.add_parser("connect",
                            help="connect to a hub: address + your name; "
                                 "the token is read from AURA_HUB_TOKEN "
                                 "or asked for quietly")
    tmc.add_argument("url", help="the hub's address, e.g. http://192.168.1.10:3456")
    tmc.add_argument("--user", default=None,
                     help="your name on the team (your branch name) — "
                          "asked at setup; this sets or changes it")
    tmc.add_argument("--token-stdin", action="store_true",
                     help="read the API token from stdin instead of the "
                          "AURA_HUB_TOKEN environment variable")
    tm_sub.add_parser("status", help="hub + connection at a glance")
    tmb = tm_sub.add_parser("board", help="your open tasks, blocked and due marks")
    tmb.add_argument("--all", action="store_true",
                     help="everyone's open work you can see (for the lead)")
    tml = tm_sub.add_parser("link",
                            help="match a request to your tasks and ask "
                                 "(#12 auto-matches; otherwise word match)")
    tml.add_argument("request", nargs="+")
    tms = tm_sub.add_parser("start",
                            help="mark a task started: label + comment "
                                 "(planned files are an estimate)")
    tms.add_argument("task_id", type=int)
    tms.add_argument("--summary", required=True)
    tms.add_argument("--files", default="", help="planned files, comma-separated")
    tmd = tm_sub.add_parser("done",
                            help="mark a task done — only with proof")
    tmd.add_argument("task_id", type=int)
    tmd.add_argument("--changed", required=True, help="files changed, comma-separated")
    tmd.add_argument("--shared", default="none")
    tmd.add_argument("--decision", default="none")
    tmd.add_argument("--commit", default="none")
    tmd.add_argument("--pushed", action="store_true")
    tmd.add_argument("--proof", required=True, help="checks run + their result")
    tmc2 = tm_sub.add_parser("decision", help="record a decision on a task")
    tmc2.add_argument("task_id", type=int)
    tmc2.add_argument("text", nargs="+")
    tmr = tm_sub.add_parser("read", help="a task: blocked-by, due, labels, comments")
    tmr.add_argument("task_id", type=int)
    tmq = tm_sub.add_parser("queue", help="updates waiting for the hub, in order")
    tmq.add_argument("drop_word", nargs="?", choices=["drop"],
                     help="drop what waits for one task (you decided)")
    tmq.add_argument("drop", nargs="?", type=int, help="the task number")
    tm_sub.add_parser("sync", help="send the queued updates (re-reads first: "
                                   "nothing of yours is overwritten)")
    tmq2 = tm_sub.add_parser("security", help="the security pass, as checks")
    tmc3 = tm_sub.add_parser("chat", help="the LAN chat page (starts/stops with the hub)")
    tmc3_sub = tmc3.add_subparsers(dest="chat_cmd", required=True)
    tmc3_sub.add_parser("status", help="is the chat page up, and where")
    tmc3_sub.add_parser("off", help="stop the chat page and keep it off — the "
                                    "team uses another chat app (the hub stays up)")
    tmc3_sub.add_parser("on", help="start the chat page now, and with the hub from now on")
    tm.set_defaults(fn=cmd_teams)
    tmk = tm_sub.add_parser("check",
                            help="the collision check before starting work: "
                                 "same task / same files / shared pieces / "
                                 "a newer decision")
    tmk.add_argument("task_id", type=int)
    tmk.add_argument("--files", default="", help="planned files, comma-separated")
    tmk.add_argument("--repo", default=".", help="the repository, if there is one")
    tmg = tm_sub.add_parser("git", help="branch / commit / push (a branch per "
                                        "person, push on a yes)")
    tmg_sub = tmg.add_subparsers(dest="git_cmd", required=True)
    tmgb = tmg_sub.add_parser("branch", help="switch to (or create) your branch")
    tmgb.add_argument("--repo", default=".")
    tmgc = tmg_sub.add_parser("commit", help="commit locally after a task")
    tmgc.add_argument("--repo", default=".")
    tmgc.add_argument("--changed", required=True,
                      help="files to commit, comma-separated")
    tmgc.add_argument("--message", required=True)
    tmgp = tmg_sub.add_parser("push", help="push your branch (only on a yes)")
    tmgp.add_argument("--repo", default=".")
    tm.set_defaults(fn=cmd_teams)
    comp = sub.add_parser("completion",
                          help="shell completion script (bash|zsh|fish)")
    comp.add_argument("shell", choices=["bash", "zsh", "fish"])
    comp.set_defaults(fn=cmd_completion)
    dmp = sub.add_parser(
        "dump", help="support-sharing summary (versions, profiles, "
                     "doctor-lite; no keys, no notebook data)")
    dmp.set_defaults(fn=cmd_dump)
    upd = sub.add_parser(
        "update", help="get the newest Aura release (checked before it is installed; asks first); "
                       "your data and settings stay")
    upd.add_argument("--check", action="store_true", help="only say whether a newer release exists")
    upd.add_argument("--rollback", action="store_true", help="go back to the version kept from before")
    upd.add_argument("--yes", "-y", action="store_true", help="don't ask (scripts)")
    upd.add_argument("--from", dest="from_dir", metavar="FOLDER",
                     help="install an unzipped Aura folder instead")
    upd.set_defaults(fn=cmd_update)
    au = sub.add_parser("auth",
                        help="show credentials: env keys + per-profile key "
                             "status (masked)")
    au.add_argument("action", nargs="?", default="list",
                    choices=["list"], help="list (default)")
    au.set_defaults(fn=cmd_auth)
    unp = sub.add_parser("uninstall",
                         help="remove Aura (add --purge to delete data)")
    unp.add_argument("--purge", action="store_true",
                     help="also delete your Aura data (chats, memory, "
                          "settings, keys) — asks first")
    unp.add_argument("--yes", action="store_true",
                     help="don't ask (for scripts)")
    unp.set_defaults(fn=cmd_uninstall)
    dr = sub.add_parser("doctor",
                        help="health check (endpoint, profiles, cache)")
    dr.add_argument("--fix", action="store_true",
                    help="repair what is safely fixable (URL /v1, cache)")
    dr.set_defaults(fn=cmd_doctor)
    sub.add_parser("profiles").set_defaults(fn=cmd_profiles)
    ini = sub.add_parser("init",
                         help="set up this project: what the agent may "
                              "reach on the network, its purpose")
    ini.add_argument("name", nargs="?", default=None,
                     help="a name for it (default: the folder's name)")
    ini.add_argument("--dir", dest="init_dir", default=None,
                     help="project folder (default: where you are)")
    ini.add_argument("-y", "--yes", action="store_true",
                     help="non-interactive: localhost-only lab scope")
    ini.set_defaults(fn=cmd_init)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    stg = sub.add_parser("settings", help="Aura's options: list, find, set, "
                                          "reset, undo (aura settings set "
                                          "edit.line_tags off)")
    stg.add_argument("words", nargs="*")
    stg.set_defaults(fn=cmd_settings)
    mcpp = sub.add_parser("mcp", help="MCP servers: search the official registry, add, list, remove")
    mcp_sub = mcpp.add_subparsers(dest="mcp_cmd", required=True)
    mcs = mcp_sub.add_parser("search", help="find servers in the official MCP registry")
    mcs.add_argument("words", nargs="+")
    mcs.add_argument("--limit", type=int, default=10)
    for _n, _h in (("login", "sign in to a remote server in the browser"),
                   ("logout", "forget the sign-in for a remote server")):
        _p = mcp_sub.add_parser(_n, help=_h)
        _p.add_argument("name")
        if _n == "login":
            _p.add_argument("--paste", action="store_true",
                            help="this machine can't get the browser's answer (SSH, a container): paste it")
    mca = mcp_sub.add_parser("add", help="add a registry server (shows what it runs, asks first)")
    mca.add_argument("name")
    mca.add_argument("--as", dest="as_name", help="the name to use in Aura")
    mca.add_argument("--project", action="store_true", help="this project only")
    mca.add_argument("--yes", action="store_true", help="don't ask (scripts)")
    mcp_sub.add_parser("popular", help="well-known servers, each from its own publisher")
    mcl = mcp_sub.add_parser("list", help="servers added")
    mcl.add_argument("--project", action="store_true")
    mcr = mcp_sub.add_parser("remove", help="remove a server")
    mcr.add_argument("name")
    mcr.add_argument("--project", action="store_true")
    mcpp.set_defaults(fn=cmd_mcp)
    sub.add_parser("licenses", help="the parts Aura uses, their versions and licences"
                   ).set_defaults(fn=cmd_licenses)
    usp = sub.add_parser("usage", help="tokens and cost of past runs, per day or model "
                                       "(this project; --all for every project)")
    usp.add_argument("--since", help="7d, 2w, today, yesterday or YYYY-MM-DD")
    usp.add_argument("--by", choices=["day", "model"], default="day")
    usp.add_argument("--all", action="store_true", help="every project of this profile")
    usp.add_argument("--cache", action="store_true",
                     help="how much of each call the server could reuse, and what broke it")
    usp.set_defaults(fn=cmd_usage)
    rgp = sub.add_parser("regress", help="did Aura get worse? record a task once (live), "
                                         "replay it any time (free)")
    rg_sub = rgp.add_subparsers(dest="regress_cmd", required=True)
    rgr = rg_sub.add_parser("record", help="run a task live once and save it")
    rgr.add_argument("name")
    rgr.add_argument("task")
    rgr.add_argument("--project", help="the project folder to copy (default: here)")
    rgx = rg_sub.add_parser("run", help="replay recordings against this Aura (no model calls)")
    rgx.add_argument("names", nargs="*")
    rg_sub.add_parser("list", help="saved recordings")
    rgp.set_defaults(fn=cmd_regress)
    his = sub.add_parser("history", help="search past chats: aura history port "
                                         "project:all after:7d outcome:proven")
    his.add_argument("words", nargs="*")
    his.add_argument("--session", help="read one chat")
    his.add_argument("--around", help="message number in that chat")
    his.add_argument("--rebuild", action="store_true", help="build the index again")
    his.set_defaults(fn=cmd_history)
    scp = sub.add_parser("scope", help="websites / hosts this project may "
                                       "reach: list, add, remove")
    scp.add_argument("action", nargs="?", default="list",
                     choices=["list", "add", "remove"])
    scp.add_argument("item", nargs="?",
                     help="example.com · 10.0.0.5 · 10.0.0.0/24")
    scp.set_defaults(fn=cmd_scope)
    for _verb, _yes in (("approve", True), ("reject", False)):
        apv = sub.add_parser(_verb, help=f"{_verb} a call a waiting run "
                                         "asked about (no id: list them)")
        apv.add_argument("id", nargs="?")
        apv.add_argument("reason", nargs="*")
        apv.set_defaults(fn=cmd_approve, approve=_yes)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    rp = sub.add_parser("report", help="write report.md from the proven "
                                       "results")
    rp.add_argument("--out", default=None,
                    help="destination path (default: report.md in the "
                         "notebook)")
    rp.set_defaults(fn=cmd_report)
    ex = sub.add_parser("export", help="export SFT-ready records")
    exsub = ex.add_subparsers(dest="what", required=True)
    for target in ("trajectories",):
        t = exsub.add_parser(target)
        t.add_argument("--out", default=None,
                       help="destination path (default inside the notebook)")
    ex.set_defaults(fn=cmd_export)
    sub.add_parser("receipts-verify",
                   help="check the run-receipt chain for edits or gaps"
                   ).set_defaults(fn=cmd_receipts_verify)
    cr = sub.add_parser("cron", help="scheduled jobs (list|add|run|watch|remove)")
    crsub = cr.add_subparsers(dest="cron_cmd", required=True)
    crsub.add_parser("list")
    ca = crsub.add_parser("add", help='e.g. "every 30 min: sweep output/"')
    ca.add_argument("task")
    cr_run = crsub.add_parser("run", help="run one job or all due jobs")
    cr_run.add_argument("job_id", nargs="?", default=None)
    cr_w = crsub.add_parser("watch", help="stay running and run due jobs of "
                                          "every project (the scheduler)")
    cr_w.add_argument("--once", action="store_true",
                      help="one pass, then exit (for OS schedulers)")
    cr_rm = crsub.add_parser("remove")
    cr_rm.add_argument("job_id")
    cr_rn = crsub.add_parser("runner", help="run scheduled jobs in the background, even "
                                            "with no chat open (for your user, no admin)")
    cr_rn.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    cr.set_defaults(fn=cmd_cron)
    ia = sub.add_parser("import-agent",
                        help="import CLAUDE.md/AGENTS.md/cursorrules "
                             "→ notebook AGENTS.md")
    ia.add_argument("source", help="project dir with the other agent's files")
    ia.set_defaults(fn=cmd_import_agent)
    ch = sub.add_parser("chat")
    ch.add_argument("--session", help="existing session jsonl to continue")
    ch.add_argument("--resume", "-r", metavar="CHAT",
                    help="open a chat: #id (or a start of it), its title, or latest")
    ch.add_argument("--continue", "-c", dest="continue_", action="store_true",
                    help="open the latest chat")
    ch.add_argument("--task", help="intro task for a fresh chat session")
    ch.add_argument("--name", help="friendly name for a new session "
                                   "(used in the filename)")
    ch.add_argument("--fork", metavar="NAME", default=None,
                    help="fork the latest/current session into NAME and "
                         "continue there")
    ch.set_defaults(fn=cmd_chat)
    # 2.1: the scored eval suite (provider-agnostic)
    ev = sub.add_parser("eval", help="scored task suite: check / run / "
                                     "compare / list")
    ev_sub = ev.add_subparsers(dest="eval_cmd", required=True)
    for _n, _h in (("list", "show the tasks"),
                   ("check", "validate the suite itself — no model needed")):
        _p = ev_sub.add_parser(_n, help=_h)
        _p.add_argument("--tasks", help="ids or categories, comma-separated")
    evr = ev_sub.add_parser("run", help="run the agent on the tasks with the "
                                        "configured profile")
    evr.add_argument("--tasks", help="ids or categories, comma-separated")
    evr.add_argument("--profile", default=argparse.SUPPRESS)
    evr.add_argument("--out", help="results file (default eval-<time>.json)")
    evr.add_argument("--max-steps", type=int, default=30)
    evr.add_argument("--time-limit", type=float, default=600.0,
                     help="seconds per task")
    evr.add_argument("--prompt", choices=["default", "generic"],
                     help="measure Aura's prompt (default) or the research "
                          "candidate")
    evr.add_argument("--keep", action="store_true",
                     help="keep each task's workspace for inspection")
    evh = ev_sub.add_parser("helper", help="check the helper model (one short call)")
    evh.add_argument("--profile", default=argparse.SUPPRESS)
    evm = ev_sub.add_parser("model", help="measure per-model choices on the model "
                                          "in use (edit form, ask-first check)")
    evm.add_argument("--show", action="store_true", help="what was measured, no model calls")
    evm.add_argument("--again", action="store_true", help="measure again")
    evm.add_argument("--yes", action="store_true", help="don't ask before spending tokens")
    evm.add_argument("--only", default="", help="caps, edit, ask (comma-separated)")
    evm.add_argument("--deep", action="store_true",
                     help="also fine-tune for this model: edit form and ask-first habit (about 30 calls)")
    evm.add_argument("--profile", default=argparse.SUPPRESS)
    evc = ev_sub.add_parser("compare", help="what changed between two runs")
    evc.add_argument("a")
    evc.add_argument("b")
    ev.set_defaults(fn=cmd_eval)
    # Which prompt a model gets: Aura's own, always
    pr = sub.add_parser("prompt", help="which prompt a model gets (Aura's own): "
                                       "show / reset")
    pr_sub = pr.add_subparsers(dest="prompt_cmd", required=True)
    prs = pr_sub.add_parser("show", help="which prompt a model uses")
    prs.add_argument("--model")
    prs.add_argument("--full", action="store_true", help="print it")
    prr = pr_sub.add_parser("reset", help="delete an old tuned copy")
    prr.add_argument("--model")
    for _p in (prs, prr):
        _p.add_argument("--profile", default=argparse.SUPPRESS)
    pr.set_defaults(fn=cmd_prompt)
    # Idiot-proof: the global flags are ALSO declared on the common subcommands
    # so `aura run --profile glm "task"` parses. Values land on the same attrs
    # the globals set.
    for _name in ("run", "resume", "chat", "status"):
        _sp = sub.choices.get(_name)
        if _sp is None:
            continue
        _sp.add_argument("-p", "--profile", help=argparse.SUPPRESS,
                         default=argparse.SUPPRESS)
        _sp.add_argument("--model", dest="model_profile",
                         help=argparse.SUPPRESS, default=argparse.SUPPRESS)
        _sp.add_argument("--dir", help=argparse.SUPPRESS,
                         default=argparse.SUPPRESS)
    # Work profiles; "default" always exists
    pf = sub.add_parser("profile", help="work profiles: list / create / "
                                        "use / delete")
    pf_sub = pf.add_subparsers(dest="profile_cmd")
    pf_sub.add_parser("show", help="the profile in use")
    pf_sub.add_parser("list", help="all profiles")
    pfc = pf_sub.add_parser("create", help="a new, empty profile")
    pfc.add_argument("name")
    pfc.add_argument("--copy-models", action="store_true",
                     help="bring the model setups (and keys) of the "
                          "current profile")
    pfu = pf_sub.add_parser("use", help="open this profile from now on")
    pfu.add_argument("name")
    pfd = pf_sub.add_parser("delete",
                            help="move a profile to Aura's trash folder")
    pfd.add_argument("name")
    pfd.add_argument("--yes", action="store_true")
    pf.set_defaults(fn=cmd_profile)
    ky = sub.add_parser("keys", help="the profile's keys: list / set / "
                                     "remove / lock / unlock")
    ky_sub = ky.add_subparsers(dest="keys_cmd")
    ky_sub.add_parser("list", help="names only, never values")
    kys = ky_sub.add_parser("set", help="store a credential commands may "
                                        "use (value asked, not shown)")
    kys.add_argument("name")
    kyr = ky_sub.add_parser("remove", help="remove a key")
    kyr.add_argument("name")
    ky_sub.add_parser("lock", help="move the keys into an AES-256 "
                                   "password vault")
    ky_sub.add_parser("unlock", help="move them back to the OS keychain")
    ky_sub.add_parser("move-to-keychain",
                      help="move keys from a plain file into the OS "
                           "keychain")
    ky.set_defaults(fn=cmd_keys)
    bpp = sub.add_parser("blueprint", help="ready-made scheduled jobs: list, or add one (asks first)")
    bpp.add_argument("name", nargs="?")
    bpp.add_argument("values", nargs="*", help="field=value")
    bpp.add_argument("--yes", action="store_true", help="don't ask (scripts)")
    bpp.set_defaults(fn=cmd_blueprint)
    vt = sub.add_parser("vault", help="saved site logins for the browser — the model never sees them")
    vt_sub = vt.add_subparsers(dest="vault_cmd", required=True)
    vta = vt_sub.add_parser("add", help="save a login (password asked, not shown)")
    vta.add_argument("site")
    vta.add_argument("--user")
    vta.add_argument("--totp", action="store_true", help="also save a one-time-code secret")
    vt_sub.add_parser("list", help="sites and user names")
    vtr = vt_sub.add_parser("remove", help="remove a site's login(s)")
    vtr.add_argument("site")
    vtr.add_argument("--user")
    vt.set_defaults(fn=cmd_vault)
    _drop_teams_parser(sub)
    return ap


def main(argv=None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    _pick_profiles(args)
    _ADD_DIRS[:] = list(getattr(args, "add_dirs", None) or [])
    # misplaced-flag normalization: subcommand-level values win only when
    # the global-level ones were not given (argparse default None)
    if getattr(args, "profile", None) and not isinstance(args.profile, str) \
            or args.profile is None:
        pass  # Global already on args via dest sharing
    # a TERM/HUP exits normally, so what Aura started is stopped with it
    from .tools.process import stop_on_signals
    stop_on_signals()
    # 1.5: no tracebacks — every dispatch goes through the guided-error guard
    from .guided_errors import run_guarded
    sys.exit(run_guarded(args.fn, args))
