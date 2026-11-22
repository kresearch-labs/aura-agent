<!-- ProjectAdmin.svelte — one project's own settings, following the real
     server contracts. Secret values never come back after being stored. -->
<script lang="ts">
  import { onDestroy } from "svelte";
  import { get as getStore } from "svelte/store";
  import { call } from "../../lib/api/client";
  import { projectRoleLabel, projectPermissionGranted } from "../../lib/admin_users";
  import type { Role } from "../../lib/role_permissions";
  import { refreshProjects, setCurrentProject, currentProjectId,
           type Project } from "../frame/shell_state";

  type Person = { id: number; name: string; display?: string; disabled_at?: number|null };
  type Props = { projects: Project[]; people: Person[]; roles: Role[];
                 changed: () => Promise<void> };
  let { projects, people, roles, changed }: Props = $props();

  type Status = { key: string; type: string; name: string; position: number;
                  colour: string };
  type Link = { id: number; name: string; app: string; room: number;
                endpoint?: boolean; has_token?: boolean;
                ingest_secret_set?: boolean; last_error?: string|null };
  const MODULES = ["board", "now", "tree", "flow", "history", "chat",
                   "aura_work", "members", "settings"];
  const APPS = ["slack", "discord", "telegram", "teams", "webhook"];

  let selected = $state<number|null>(null);
  let generation = 0;
  let loading = $state(false);
  let error = $state("");
  let note = $state("");
  let busy = $state(false);
  let createName = $state(""); let createSlug = $state("");
  let name = $state(""); let description = $state("");
  let mainBranch = $state("main");
  let modules = $state<Record<string, boolean>>({});
  let statuses = $state<Status[]>([]);
  let links = $state<Link[]>([]);
  let mirrorTasks = $state<{key: string; title: string; status?: string}[]>([]);
  let mirrorSource = $state("");
  let newPerson = $state(0); let newRole = $state("Member");
  let newLinkName = $state(""); let newLinkApp = $state("slack");
  let newLinkEndpoint = $state(""); let newLinkToken = $state("");
  let ingestSecret = $state("");
  let noticeApp = $state("slack"); let noticeURL = $state("");

  let projectPermissions = $state<Set<string> | undefined>(undefined);
  let permissionsPending = $state(false);
  onDestroy(() => { generation += 1; });
  let canEditProject = $derived(
    projectPermissionGranted(projectPermissions, "edit_project"));
  let canManageMembers = $derived(
    projectPermissionGranted(projectPermissions, "manage_members"));
  let canExportProject = $derived(
    projectPermissionGranted(projectPermissions, "export_activity"));
  let canReadBoard = $derived(
    projectPermissionGranted(projectPermissions, "view_work_packages"));
  let canReadMessages = $derived(
    projectPermissionGranted(projectPermissions, "view_messages"));
  let canArchiveProject = $derived(
    projectPermissionGranted(projectPermissions, "archive_project") && canEditProject);

  let project = $derived(projects.find((p) => p.id === selected) ?? null);
  let members = $derived(project?.members ?? []);
  let settings = $derived.by(() => {
    if (!project?.settings) return {} as Record<string, any>;
    if (typeof project.settings === "string") {
      try { return JSON.parse(project.settings); }
      catch { return {} as Record<string, any>; }
    }
    return project.settings;
  });
  let notices = $derived((settings.notices ?? []) as {
    app: string; types?: string[]; where?: string;
    where_ref?: string; where_fp?: string }[]);
  $effect(() => {
    if (!projects.length || selected !== null) return;
    const current = getStore(currentProjectId);
    const own = projects.find((p) => p.id === current)?.id ?? projects[0].id;
    void choose(own);
  });

  function periodSettings(p: Project|null): Record<string, any> {
    if (!p?.settings) return {};
    if (typeof p.settings === "string") {
      try { return JSON.parse(p.settings); } catch { return {}; }
    }
    return p.settings;
  }

  async function choose(pid: number) {
    const replacing = selected !== pid;
    selected = pid;
    const seq = ++generation;
    // Keep the editor mounted on same-project refreshes. Its write buttons
    // pause while both permissions and associated data are revalidated.
    loading = replacing; error = ""; note = "";
    permissionsPending = true;
    if (replacing) projectPermissions = undefined;
    const p = projects.find((x) => x.id === pid) ?? null;
    if (!p) {
      selected = null;
      projectPermissions = undefined;
      statuses = []; links = []; mirrorTasks = []; mirrorSource = "";
      loading = false; permissionsPending = false;
      return;
    }
    const s = periodSettings(p);
    name = p.name; description = String(s.description ?? "");
    mainBranch = p.main_branch ?? "main";
    modules = {...(p.modules ?? {})};
    if (replacing) ingestSecret = "";
    mirrorSource = String(s.tasks_from ?? "").replace(/^wired:/, "");
    setCurrentProject(pid);
    try {
      const pc = await call<{permissions: string[]}>(`/me/permissions?project=${pid}`);
      if (seq !== generation) return;
      const allowed = new Set(pc.permissions ?? []);
      projectPermissions = allowed;
      const canReadTasks = projectPermissionGranted(allowed, "view_work_packages");
      const canReadRooms = projectPermissionGranted(allowed, "view_messages");
      const [st, li, mi] = await Promise.all([
        canReadTasks ? call<{statuses: Status[]}>(`/projects/${pid}/statuses`)
                     : Promise.resolve({statuses: [] as Status[]}),
        canReadRooms ? call<{links: Link[]}>(`/projects/${pid}/links`)
                     : Promise.resolve({links: [] as Link[]}),
        canReadTasks ? call<{tasks: typeof mirrorTasks}>(`/projects/${pid}/mirror`)
                     : Promise.resolve({tasks: [] as typeof mirrorTasks}),
      ]);
      if (seq !== generation) return;   // another project is now selected
      statuses = st.statuses ?? []; links = li.links ?? [];
      mirrorTasks = mi.tasks ?? [];
    } catch (e) {
      if (seq === generation) {
        error = e instanceof Error ? e.message : "the project did not answer";
        projectPermissions = undefined;
        statuses = []; links = []; mirrorTasks = [];
      }
    } finally {
      if (seq === generation) {
        loading = false; permissionsPending = false;
      }
    }
  }

  async function act(fn: (expected: number|null) => Promise<void>, answer: string) {
    if (busy) { error = "Another change is still saving."; return; }
    const expected = selected;
    busy = true; error = ""; note = "";
    try {
      await fn(expected);
      if (selected === expected) note = answer;
    } catch (e) {
      if (selected === expected)
        error = e instanceof Error ? e.message : "the change was refused";
    } finally {
      // All writers share this busy gate, even across a selection change.
      // The completed operation must not strand the next selection as busy.
      busy = false;
    }
  }

  async function reload(expected: number|null) {
    if (selected !== expected) return;
    await refreshProjects();
    if (selected !== expected) return;
    await changed();
    if (selected !== expected) return;
    if (expected !== null) await choose(expected);
  }

  async function createProject(e: Event) {
    e.preventDefault();
    if (busy) { error = "Another change is still saving."; return; }
    const expected = selected;
    busy = true; error = ""; note = "";
    try {
      const r = await call<{project: Project}>("/projects", {
        name: createName.trim(), slug: createSlug.trim().toLowerCase(),
      }, "POST");
      createName = ""; createSlug = "";
      await Promise.all([refreshProjects(), changed()]);
      // A different deliberate selection wins. Selecting this new project is
      // the intended outcome only when the creator has not chosen another.
      if (selected !== expected && selected !== r.project.id) return;
      await choose(r.project.id);
      if (selected === r.project.id) note = "Project created.";
    } catch (e) {
      if (selected === expected)
        error = e instanceof Error ? e.message : "the change was refused";
    } finally {
      busy = false;
    }
  }
  async function saveBasics(e: Event) {
    e.preventDefault(); if (!project || !canEditProject) return;
    const pid = project.id;
    await act(async (expected) => {
      await call(`/projects/${pid}`, {
        name: name.trim(), main_branch: mainBranch.trim(),
        settings: {description},
      }, "PATCH");
      await reload(expected);
    }, "Project settings saved.");
  }
  async function toggleModule(mod: string, on: boolean) {
    if (!project || !canEditProject) return;
    modules[mod] = on;
    const payload = {...modules}; payload[mod] = on;
    await act(async (expected) => {
      await call(`/projects/${project!.id}`, {modules: payload}, "PATCH");
      await reload(expected);
    }, `${mod} is ${on ? "on" : "off"}.`);
  }
  async function saveMember(person: number, role: string) {
    if (!project || !canManageMembers) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}/members/${person}`, {role}, "PUT");
      await reload(expected);
    }, "Membership saved.");
  }
  async function addMember() {
    if (!newPerson || !canManageMembers) return;
    await saveMember(newPerson, newRole);
    newPerson = 0;
  }
  async function removeMember(person: number, display: string) {
    if (!project || !canManageMembers ||
        !confirm(`Remove ${display} from this project?`)) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}/members/${person}`, {}, "DELETE");
      await reload(expected);
    }, "Person removed.");
  }
  async function saveStatus(s: Status) {
    if (!project || !canEditProject) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}/statuses/${s.key}`, {
        name: s.name.trim(), position: Number(s.position), colour: s.colour,
      }, "PATCH");
      await reload(expected);
    }, "Status saved.");
  }
  async function moveStatus(s: Status, by: number) {
    if (!project || !canEditProject) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}/statuses/${s.key}`, {
        name: s.name, colour: s.colour, position: s.position + by,
      }, "PATCH");
      await reload(expected);
    }, "Status order saved.");
  }
  async function archive() {
    if (!project || !canArchiveProject) return;
    const restore = !!project.archived_at;
    if (!confirm(restore ? "Restore this project? Editing will be allowed again."
                        : "Archive this project? It becomes read-only.")) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}/archive`, {unarchive: restore}, "POST");
      await reload(expected);
    }, restore ? "Project restored." : "Project archived.");
  }
  async function addLink(e: Event) {
    e.preventDefault(); if (!project || !canEditProject) return;
    await act(async (expected) => {
      const r = await call<{ingest_secret: string}>(
        `/projects/${project!.id}/links`, {
          name: newLinkName.trim(), app: newLinkApp,
          endpoint: newLinkEndpoint, token: newLinkToken,
        }, "POST");
      if (selected !== expected) return;
      ingestSecret = r.ingest_secret;
      newLinkName = ""; newLinkEndpoint = ""; newLinkToken = "";
      await reload(expected);
    }, "Linked room created. Its ingest secret is shown once.");
  }
  async function deleteLink(l: Link) {
    if (!project || !canEditProject || !confirm(`Remove linked room ${l.name}?`)) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}/links/${l.id}`, {}, "DELETE");
      await reload(expected);
    }, "Linked room removed.");
  }
  async function pullLink(l: Link) {
    if (!project || !canEditProject) return;
    await act(async (expected) => {
      const r = await call<{landed?: number}>(`/projects/${project!.id}/links/${l.id}/pull`,
        {}, "POST");
      if (selected !== expected) return;
      note = `Pull finished: ${r.landed ?? 0} message(s).`;
      await reload(expected);
    }, "Pull finished.");
  }
  async function addNotice(e: Event) {
    e.preventDefault(); if (!project || !canEditProject) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}`, {settings: {notices: [
        ...notices.map((n) => ({...n, where: "[stored]", token: "[stored]"})),
        {app: noticeApp, where: noticeURL, types: []},
      ]}}, "PATCH");
      noticeURL = "";
      await reload(expected);
    }, "Notice target stored. It is shown only as a fingerprint.");
  }
  async function removeNotice(index: number) {
    if (!project || !canEditProject) return;
    await act(async (expected) => {
      const remaining = notices.filter((_, i) => i !== index)
        .map((n) => ({...n, where: "[stored]", token: "[stored]"}));
      await call(`/projects/${project!.id}`, {settings: {notices: remaining}}, "PATCH");
      await reload(expected);
    }, "Notice target removed.");
  }
  async function saveMirror(e: Event) {
    e.preventDefault(); if (!project || !canEditProject) return;
    await act(async (expected) => {
      await call(`/projects/${project!.id}`, {settings: {tasks_from:
        mirrorSource.trim() ? `wired:${mirrorSource.trim()}` : ""}}, "PATCH");
      await reload(expected);
    }, "Mirror source saved. It stays read-only here.");
  }
  import Avatar from "../../lib/parts/Avatar.svelte";
  import { hueFor } from "../../lib/chat_logic";
  import { ChevronDown, ArrowUp, ArrowDown, FolderPlus } from "@lucide/svelte";
  const MODULE_NAMES: Record<string, string> = {
    board: "Board", now: "Now", tree: "Tree (files)", flow: "Flow (timeline)",
    history: "History", chat: "Chat", aura_work: "Aura work", members: "Members",
    settings: "Project settings",
  };
</script>

<section class="pa" aria-label="project administration">
  <form class="new panel" onsubmit={createProject}>
    <h3>Create a project</h3>
    <label>Name <input bind:value={createName} required maxlength={80}
                       placeholder="Project name" /></label>
    <label>Slug <input bind:value={createSlug} placeholder="short-name-for-the-address"
                       pattern={"[a-z0-9\-]+"} /></label>
    <button class="btn pri" disabled={busy}><FolderPlus size={14} strokeWidth={1.75} />Create project</button>
  </form>

  {#if error}<p class="msg err" role="alert">{error}</p>{/if}
  {#if note}<p class="msg okay" role="status">{note}</p>{/if}
  {#if permissionsPending && !loading}<p class="muted" role="status">Checking project permissions…</p>{/if}

  <div class="pgrid">
    <nav class="panel picker" aria-label="projects to edit">
      <h3>Projects</h3>
      {#each projects as p (p.id)}
        <button class="project" class:active={selected === p.id}
                aria-label={`edit project ${p.name}`}
                onclick={() => void choose(p.id)}>
          <span class="pdot" style:background={hueFor(p.slug ?? p.name)}></span>
          <span class="pname"><b>{p.name}</b><span class="muted mono">/{p.slug}</span></span>
          {#if p.archived_at}<span class="chip warn">archived</span>{/if}
        </button>
      {:else}<p class="muted">No projects yet.</p>{/each}
    </nav>

    <div class="details">
      {#if !project}<p class="muted">Choose a project, or create the first one.</p>
      {:else if loading || (permissionsPending && projectPermissions === undefined)}<p class="muted" role="status">Loading project permissions and settings…</p>
      {:else}
        <section class="panel part">
          <h3>Project</h3>
          {#if canEditProject}
            <form onsubmit={saveBasics}>
              <label>Name <input bind:value={name} required maxlength={80}
                  aria-label="project name" disabled={project.archived_at != null} /></label>
              <label>Description <textarea bind:value={description} rows="2"
                  disabled={project.archived_at != null}></textarea></label>
              <label>Main branch <input bind:value={mainBranch}
                  aria-label="main branch" disabled={project.archived_at != null} /></label>
              <div><button class="btn pri"
                disabled={busy || permissionsPending || project.archived_at != null}>Save project</button></div>
            </form>
            <div class="modules" role="group" aria-label="modules">
              <h4>Modules</h4>
              {#each MODULES as m}
                <label class="switch"><input type="checkbox"
                  aria-label={`module ${m}`} checked={modules[m] !== false}
                  disabled={busy || permissionsPending || project.archived_at != null}
                  onchange={(e: Event) => void toggleModule(m, (e.currentTarget as HTMLInputElement).checked)} />
                  <span>{MODULE_NAMES[m] ?? m}</span></label>
              {/each}
            </div>
          {:else}
            <div class="readonly-meta">
              <h4>{project.name}</h4>
              <p>{description || "No description."}</p>
              <p>Main branch: <code>{mainBranch}</code></p>
              <p class="muted">Project settings are read-only.</p>
            </div>
          {/if}
          <div class="foot">
            {#if canExportProject}
              <a class="btn ghost" href={`/api/v1/projects/${project.id}/export`}>Export project history</a>
            {/if}
            {#if canArchiveProject}
              <button class={`btn ${project.archived_at ? "sec2" : "danger"}`} disabled={busy || permissionsPending} onclick={archive}>
                {project.archived_at ? "Restore project" : "Archive project"}</button>
            {/if}
          </div>
        </section>

        <section class="panel part">
          <h3>Members</h3>
          {#if canManageMembers}
            <div class="liner">
              <span class="sel"><select bind:value={newPerson} aria-label="person to add">
                <option value={0}>Choose a person</option>
                {#each people.filter((p) => !p.disabled_at && !members.some((m) => m.person === p.id)) as p}
                  <option value={p.id}>{p.name}</option>
                {/each}
              </select><ChevronDown size={14} strokeWidth={1.75} /></span>
              <span class="sel"><select bind:value={newRole} aria-label="role to give">
                {#each roles as r}<option value={r.name}>{r.name}</option>{/each}
              </select><ChevronDown size={14} strokeWidth={1.75} /></span>
              <button class="btn pri"
                disabled={busy || permissionsPending || project.archived_at != null || !newPerson}
                onclick={addMember}>Add person</button>
            </div>
          {/if}
          {#each members as m (m.person)}
            <div class="member">
              <Avatar name={m.name} />
              <b class="mname">{m.name}</b>
              {#if canManageMembers}
                <span class="sel"><select value={projectRoleLabel(m.role)} aria-label={`role for ${m.name}`}
                    disabled={busy || permissionsPending || project.archived_at != null}
                    onchange={(e: Event) => void saveMember(m.person, (e.currentTarget as HTMLSelectElement).value)}>
                    {#each roles as r}<option value={r.name} selected={projectRoleLabel(m.role) === r.name}>{r.name}</option>{/each}
                  </select><ChevronDown size={14} strokeWidth={1.75} /></span>
                <button class="btn ghost tiny"
                    disabled={busy || permissionsPending || project.archived_at != null}
                    onclick={() => void removeMember(m.person, m.name)}>Remove person</button>
              {:else}
                <span class="chip">{projectRoleLabel(m.role)}</span>
              {/if}
            </div>
          {:else}<p class="muted">No memberships to show.</p>{/each}
        </section>

        <section class="panel part">
          <h3>Board statuses</h3>
          {#if canReadBoard}
            {#each statuses as s (s.key)}
              {#if canEditProject}
                <div class="status" data-status={s.key}>
                  <input type="color" class="swatch" bind:value={s.colour}
                    aria-label={`status colour ${s.key}`}
                    disabled={project.archived_at != null} />
                  <input bind:value={s.name} aria-label={`status name ${s.key}`}
                    disabled={project.archived_at != null} />
                  <input type="number" min="0" class="pos" bind:value={s.position}
                    aria-label={`status position ${s.key}`} title="Position"
                    disabled={project.archived_at != null} />
                  <button class="ib" aria-label={`move ${s.key} up`}
                    disabled={busy || permissionsPending || project.archived_at != null || s.position <= 0}
                    onclick={() => void moveStatus(s, -1)}><ArrowUp size={14} strokeWidth={1.75} /></button>
                  <button class="ib" aria-label={`move ${s.key} down`}
                    disabled={busy || permissionsPending || project.archived_at != null || s.position >= statuses.length - 1}
                    onclick={() => void moveStatus(s, 1)}><ArrowDown size={14} strokeWidth={1.75} /></button>
                  <button class="btn tiny"
                    disabled={busy || permissionsPending || project.archived_at != null}
                    onclick={() => void saveStatus(s)}>Save status</button>
                </div>
              {:else}
                <div class="status readonly" data-status={s.key}>
                  <b class="readonly-name">{s.name}</b>
                  <span class="muted">position {s.position}</span>
                  <span class="chip">{s.colour}</span>
                </div>
              {/if}
            {:else}<p class="muted">No statuses yet.</p>{/each}
          {:else}
            <p class="muted">Board statuses are not available with this project's current permissions.</p>
          {/if}
        </section>

        {#if canReadMessages || canEditProject}
          <details class="panel part fold">
            <summary>Linked rooms</summary>
            {#if ingestSecret}
              <p class="secret"><b>Store this ingest secret now.</b> It is never shown again:
                <code>{ingestSecret}</code></p>{/if}
            {#if canReadMessages}
              {#each links as l (l.id)}
                <div class="liner">
                  <b>{l.name}</b><span class="chip">{l.app}</span>
                  {#if canEditProject}
                    <button class="btn tiny" disabled={busy || permissionsPending || project.archived_at != null}
                      onclick={() => void pullLink(l)}>Pull in</button>
                    <button class="btn tiny" disabled={busy || permissionsPending || project.archived_at != null}
                      onclick={() => void deleteLink(l)}>Remove link</button>
                  {/if}
                </div>
                {#if l.last_error}<span class="err">{l.last_error}</span>{/if}
              {:else}<p class="muted">No outside rooms linked.</p>{/each}
            {:else}
              <p class="muted">Linked-room details are not available with this project's current permissions.</p>
            {/if}
            {#if canEditProject}
              <form class="stack" onsubmit={addLink}>
                <input bind:value={newLinkName} required aria-label="linked room name"
                  disabled={project.archived_at != null} placeholder="Channel name" />
                <select bind:value={newLinkApp} aria-label="linked room app"
                  disabled={project.archived_at != null}>
                  {#each APPS as a}<option value={a}>{a}</option>{/each}
                </select>
                <input type="password" autocomplete="new-password" bind:value={newLinkEndpoint}
                  disabled={project.archived_at != null}
                  aria-label="outside endpoint (hidden)" placeholder="Endpoint URL · hidden" />
                <input type="password" autocomplete="new-password" bind:value={newLinkToken}
                  disabled={project.archived_at != null}
                  aria-label="linked room token (hidden)" placeholder="Token, if needed · hidden" />
                <button class="btn" disabled={busy || permissionsPending || project.archived_at != null}>Link a room</button>
              </form>
            {/if}
          </details>
        {/if}

        <details class="panel part fold">
          <summary>Notices</summary>
          {#each notices as n, i}
            <div class="liner"><b>{n.app}</b>
              <span class="muted mono"
                    title={n.where_fp ? `stored target fingerprint: ${n.where_fp}` : "no stored target fingerprint"}>
                stored secret · fingerprint {n.where_fp?.slice(-16) ?? "unavailable"}
              </span>
              {#if canEditProject}
                <button class="btn tiny" disabled={busy || permissionsPending || project.archived_at != null}
                  onclick={() => void removeNotice(i)}>Remove notice</button>
              {/if}
            </div>
          {:else}<p class="muted">No notice targets.</p>{/each}
          {#if canEditProject}
            <form class="liner" onsubmit={addNotice}>
              <select bind:value={noticeApp} aria-label="notice application"
                disabled={project.archived_at != null}>
                {#each APPS as a}<option value={a}>{a}</option>{/each}
              </select>
              <input type="password" autocomplete="new-password" required
                disabled={project.archived_at != null}
                bind:value={noticeURL} aria-label="notice URL (hidden)" placeholder="Target URL · hidden" />
              <button class="btn" disabled={busy || permissionsPending || project.archived_at != null}>Add notice</button>
            </form>
          {/if}
        </details>

        {#if canReadBoard || canEditProject}
          <details class="panel part fold">
            <summary>Outside hub and read-only mirror</summary>
            {#if canEditProject}
              <form onsubmit={saveMirror}>
                <label>Outside project id <input bind:value={mirrorSource}
                  disabled={project.archived_at != null}
              pattern={"[A-Za-z0-9_\-]+"} aria-label="outside project id" /></label>
                <button class="btn" disabled={busy || permissionsPending || project.archived_at != null}>Save mirror source</button>
              </form>
            {:else}
              <p>Mirror source: <span class="mono">{mirrorSource || "this project"}</span>.</p>
            {/if}
          <p class="muted">Empty means this project's own tasks. The mirror is read-only;
            connection credentials stay in the hub wiring, not in the browser.</p>
            {#if canReadBoard}
              {#each mirrorTasks as t}<div class="row"><b>{t.title}</b><span>{t.status}</span></div>
              {:else}<p class="muted">No mirrored tasks stored.</p>{/each}
            {:else}
              <p class="muted">Project tasks are not available with this project's current permissions.</p>
            {/if}
          </details>
        {/if}
      {/if}
    </div>
  </div>
</section>

<style>
  .pa { display: flex; flex-direction: column; gap: 14px; }
  .panel { background: var(--raised); border-radius: var(--r-panel); padding: 18px 20px;
           box-shadow: var(--shadow), 0 0 0 1px var(--line); min-width: 0;
           display: flex; flex-direction: column; gap: 12px; }
  .panel h3 { margin: 0; font-size: 15px; font-weight: 600; }
  .panel h4 { margin: 0; font-size: 13.5px; font-weight: 600; }
  .msg { margin: 0; font-size: 13px; padding: 8px 12px; border-radius: var(--r-ctl); }
  .msg.err { color: var(--err); background: color-mix(in oklch, var(--err) 10%, transparent); }
  .msg.okay { color: var(--ok-text); background: color-mix(in oklch, var(--ok) 12%, transparent); }
  .err { color: var(--err); font-size: 12.5px; }
  .muted { color: var(--muted); }
  .mono { font-family: var(--mono); font-size: 12px; }
  .chip { font-size: 11px; height: 20px; padding: 0 7px; }
  .chip.warn { color: var(--text); background: color-mix(in oklch, var(--warn) 22%, transparent); }

  .new { flex-direction: row; flex-wrap: wrap; align-items: flex-end; gap: 12px; }
  .new h3 { flex-basis: 100%; }
  .new label { display: flex; flex-direction: column; gap: 6px; }
  .new input { width: 240px; }

  .pgrid { display: grid; grid-template-columns: minmax(180px, 240px) minmax(0, 1fr); gap: 14px; align-items: start; }
  .picker { gap: 2px; padding: 14px 10px; }
  .picker h3 { padding: 0 8px 6px; }
  .project { text-align: start; display: flex; align-items: center; gap: 10px; padding: 8px;
             background: none; border-radius: var(--r-ctl); cursor: pointer; color: var(--text-2); }
  .project:hover { background: var(--hover); color: var(--text); }
  .project.active { background: var(--press); color: var(--text); }
  /* on the pressed row the muted grey falls under 4.5:1 — the second text colour keeps it readable */
  .project:hover .muted, .project.active .muted { color: var(--text-2); }
  .pdot { width: 8px; height: 8px; border-radius: 3px; flex: none; }
  .pname { display: flex; flex-direction: column; min-width: 0; font-size: 13.5px; }
  .pname .mono { font-size: 11.5px; }
  .details { display: flex; flex-direction: column; gap: 14px; min-width: 0; }

  .part form { display: flex; flex-direction: column; gap: 12px; }
  .part label { display: flex; flex-direction: column; gap: 6px; }
  .part form.liner, .liner { flex-direction: row; align-items: center; flex-wrap: wrap; }
  .liner { display: flex; gap: 8px; flex-wrap: wrap; }
  .stack { flex-direction: column; }
  .modules { display: flex; flex-wrap: wrap; gap: 8px; padding-top: 12px; border-top: 1px solid var(--line); }
  .modules h4 { flex-basis: 100%; }
  .modules label.switch { flex-direction: row; align-items: center; gap: 8px; padding: 6px 10px;
                          border-radius: 99px; background: var(--raised-2); color: var(--text); cursor: pointer; }
  .foot { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end;
          padding-top: 12px; border-top: 1px solid var(--line); }

  .member { display: grid; grid-template-columns: auto minmax(0, 1fr) 200px auto; gap: 10px;
            align-items: center; padding: 8px 0; border-top: 1px solid var(--line); }
  .member .sel { width: 100%; }
  .mname { font-size: 13.5px; font-weight: 600; }

  .status { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 6px 0;
            border-top: 1px solid var(--line); }
  .status input:not([type]) { flex: 1; min-width: 140px; }
  .status .pos { width: 64px; }
  .status .swatch { width: 32px; height: 32px; padding: 2px; border-radius: 8px; cursor: pointer; }
  .status .ib:disabled { opacity: .35; }

  .fold { padding: 0; gap: 0; }
  .fold > summary { list-style: none; cursor: pointer; padding: 14px 20px; font-size: 15px; font-weight: 600;
                    display: flex; align-items: center; gap: 8px; }
  .fold > summary::-webkit-details-marker { display: none; }
  .fold > summary::after { content: "›"; margin-left: auto; color: var(--muted); transition: transform var(--t-fast); }
  .fold[open] > summary::after { transform: rotate(90deg); }
  .fold[open] { padding-bottom: 16px; }
  .fold > :not(summary) { margin: 0 20px 10px; }
  .secret { overflow-wrap: anywhere; }
  .row { display: flex; gap: 8px; }
  .readonly-meta, .readonly-name, .mono, .row { overflow-wrap: anywhere; }
  .readonly-meta p { min-width: 0; }
  @media (max-width: 860px) {
    .pgrid { grid-template-columns: minmax(0, 1fr); }
    .member { grid-template-columns: auto minmax(0, 1fr); }
    .member .sel, .member .btn { grid-column: 2; }
    .new input { width: 100%; }
    .new label { flex: 1 1 100%; }
  }
</style>
