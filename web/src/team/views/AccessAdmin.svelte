<!-- AccessAdmin.svelte — one person, one project: their role there and their
     own switches per area (Board, Now, Tree, Flow, History, Chat, Aura work).
     A switch is for this person in this project only; the role stays the same
     for everyone else (Roles changes a role for everyone). The hub enforces
     every switch: an area turned off is refused there, not only hidden here. -->
<script lang="ts">
  import { call } from "../../lib/api/client";
  import { me } from "../frame/shell_state";
  import { isTeamAdministrator } from "../../lib/admin_users";
  import { User, Users } from "@lucide/svelte";

  type Member = { person: number; name: string; role: string; overrides?: Record<string, boolean> };
  type Project = { id: number; name: string; slug: string; archived_at?: number | null;
                   modules?: Record<string, boolean>; members?: Member[] };
  type Role = { id: number; name: string; builtin?: boolean;
                permissions?: Record<string, string[] | Record<string, boolean>> };
  type Person = { id: number; name: string; display?: string | null; team_role?: string | null;
                  role?: string | null };

  let { people, projects, roles, changed }:
    { people: Person[]; projects: Project[]; roles: Role[]; changed: () => Promise<void> } = $props();

  const AREAS: { key: string; label: string }[] = [
    { key: "board", label: "Board" }, { key: "now", label: "Now" }, { key: "tree", label: "Tree" },
    { key: "flow", label: "Flow" }, { key: "history", label: "History" }, { key: "chat", label: "Chat" },
    { key: "aura_work", label: "Aura work" },
  ];

  let whoId = $state<number | "">("");
  let pick = $state<string>("");          // a project id, "all", "add" or ""
  let busy = $state("");
  let error = $state("");
  let note = $state("");
  let addPid = $state<number | "">("");
  let addRole = $state("Member");

  const who = $derived(people.find((p) => p.id === whoId));
  const isBoss = (p: Person | undefined) => !!p && isTeamAdministrator(p as never);
  const theirs = $derived(whoId === "" ? [] : projects
    .filter((pr) => !pr.archived_at && (pr.members ?? []).some((m) => m.person === whoId))
    .map((pr) => ({ pr, m: (pr.members ?? []).find((m) => m.person === whoId)! })));
  const notTheirs = $derived(projects.filter((pr) => !pr.archived_at &&
    !(pr.members ?? []).some((m) => m.person === whoId)));
  const shown = $derived(pick === "all" ? theirs
    : theirs.filter((t) => String(t.pr.id) === pick));

  // keep the project choice valid when the person changes
  $effect(() => {
    void whoId;
    if (pick && pick !== "all" && pick !== "add" && !theirs.some((t) => String(t.pr.id) === pick))
      pick = "";
  });

  function roleGives(roleName: string, area: string): boolean {
    const r = roles.find((x) => x.name === roleName);
    const g = r?.permissions?.[area];
    return Array.isArray(g) ? g.length > 0 : !!g && Object.values(g).some(Boolean);
  }
  function areaState(pr: Project, m: Member, area: string) {
    const projectOff = pr.modules?.[area] === false;
    const own = m.overrides?.[area];
    const fromRole = roleGives(m.role, area);
    return { projectOff, changed: own !== undefined, on: !projectOff && (own ?? fromRole), fromRole };
  }
  const changedCount = (pr: Project, m: Member) =>
    AREAS.filter((a) => areaState(pr, m, a.key).changed).length;

  async function act(label: string, work: () => Promise<unknown>, done: string) {
    busy = label; error = ""; note = "";
    try { await work(); await changed(); note = done; }
    catch (e) { error = e instanceof Error ? e.message : "That didn't go through."; }
    finally { busy = ""; }
  }
  function flip(pr: Project, m: Member, area: string) {
    const s = areaState(pr, m, area);
    const want = !s.on;
    // back to the role when the new value is what the role gives
    const value = want === s.fromRole ? null : want;
    void act(`${pr.id}:${area}`, () => call(`/projects/${pr.id}/members/${m.person}/access`,
      { overrides: { [area]: value } }, "PUT"),
      `${AREAS.find((a) => a.key === area)?.label} ${want ? "on" : "off"} for ${m.name} in ${pr.name}.`);
  }
  function backToRole(pr: Project, m: Member) {
    const reset = Object.fromEntries(Object.keys(m.overrides ?? {}).map((k) => [k, null]));
    void act(`${pr.id}:reset`, () => call(`/projects/${pr.id}/members/${m.person}/access`,
      { overrides: reset }, "PUT"), `${m.name} in ${pr.name}: back to the role.`);
  }
  function setRole(pr: Project, m: Member, role: string) {
    void act(`${pr.id}:role`, () => call(`/projects/${pr.id}/members/${m.person}`, { role }, "PUT"),
      `${m.name} is ${role} in ${pr.name}.`);
  }
  function remove(pr: Project, m: Member) {
    if (!confirm(`Take ${m.name} out of ${pr.name}? Their work and history stay.`)) return;
    void act(`${pr.id}:remove`, () => call(`/projects/${pr.id}/members/${m.person}`, {}, "DELETE"),
      `${m.name} is no longer in ${pr.name}.`).then(() => { pick = ""; });
  }
  function add() {
    if (addPid === "" || whoId === "") return;
    const pid = addPid;
    void act("add", () => call(`/projects/${pid}/members/${whoId}`, { role: addRole }, "PUT"),
      `${who?.name} added to ${projects.find((p) => p.id === pid)?.name} as ${addRole}.`)
      .then(() => { pick = String(pid); addPid = ""; });
  }
</script>

<section class="panel access" aria-label="access per person and project">
  <div class="panel-h"><div><h3>Access</h3>
    <p class="muted">Choose a person, then one of their projects, to set what they can use there.</p></div></div>

  <div class="pickers">
    <label class="pick">Person
      <select aria-label="person" bind:value={whoId}>
        <option value="">Choose a person…</option>
        {#each people as p (p.id)}
          <option value={p.id}>{p.display && p.display !== p.name ? `${p.display} (@${p.name})` : p.name}{isBoss(p) ? " — admin" : ""}</option>
        {/each}
      </select>
    </label>
    <label class="pick">Project
      <select aria-label="project" bind:value={pick} disabled={whoId === ""}>
        {#if whoId === ""}
          <option value="">Choose a person first</option>
        {:else}
          <option value="">Choose a project…</option>
          {#each theirs as t (t.pr.id)}
            <option value={String(t.pr.id)}>{t.pr.name} · {t.m.role}{changedCount(t.pr, t.m) ? " · changed" : ""}</option>
          {/each}
          {#if theirs.length > 1}<option value="all">All {who?.name}'s projects</option>{/if}
          <option value="add">+ Add to a project…</option>
        {/if}
      </select>
    </label>
  </div>

  {#if error}<p class="err" role="alert">{error}</p>{/if}
  {#if note}<p class="muted" role="status">{note}</p>{/if}

  {#if whoId === ""}
    <p class="empty muted">Choose a person, then a project.</p>
  {:else if isBoss(who)}
    <p class="empty muted">{who?.name} is a team admin: they can use everything in every project. Change their team role under Users to limit them.</p>
  {:else if pick === "add"}
    <div class="proj">
      <div class="ph"><b>Add {who?.name} to a project</b></div>
      {#if notTheirs.length}
        <div class="row">
          <select aria-label="project to add to" bind:value={addPid}>
            <option value="">Choose a project</option>
            {#each notTheirs as pr (pr.id)}<option value={pr.id}>{pr.name}</option>{/each}
          </select>
          <select aria-label="role in that project" bind:value={addRole}>
            {#each roles as r (r.id)}<option value={r.name}>{r.name}</option>{/each}
          </select>
          <button class="btn primary" type="button" disabled={addPid === "" || busy !== ""} onclick={add}>Add</button>
        </div>
      {:else}<p class="muted">{who?.name} is already in every project.</p>{/if}
    </div>
  {:else if pick === ""}
    <p class="empty muted">{theirs.length ? `Choose one of ${who?.name}'s projects.` : `${who?.name} isn't in any project yet — add them to one.`}</p>
  {:else}
    <div class="note"><span class="ico" aria-hidden="true">{#if pick === "all"}<Users size={15} strokeWidth={1.75} />{:else}<User size={15} strokeWidth={1.75} />{/if}</span>
      <span>Changes here are for <b>{who?.name}{pick === "all" ? "" : ` in ${shown[0]?.pr.name}`} only</b>. To change a role for everyone, use Roles.</span></div>
    {#each shown as t (t.pr.id)}
      {@const n = changedCount(t.pr, t.m)}
      <div class="proj">
        <div class="ph">
          <b>{t.pr.name}</b>
          <span class="muted">Role</span>
          <select aria-label={`${who?.name}'s role in ${t.pr.name}`} value={t.m.role} disabled={busy !== ""}
                  onchange={(e) => setRole(t.pr, t.m, (e.currentTarget as HTMLSelectElement).value)}>
            {#each roles as r (r.id)}<option value={r.name}>{r.name}</option>{/each}
          </select>
          <span class="muted">{n ? `${n} changed for ${t.m.name}` : "as the role gives"}</span>
          <span class="spacer"></span>
          {#if n}<button class="btn tiny" type="button" disabled={busy !== ""} onclick={() => backToRole(t.pr, t.m)}>Back to role</button>{/if}
          <button class="btn tiny" type="button" disabled={busy !== ""} onclick={() => remove(t.pr, t.m)}>Remove from {t.pr.name}</button>
        </div>
        <div class="areas">
          {#each AREAS as a (a.key)}
            {@const s = areaState(t.pr, t.m, a.key)}
            <div class="ar" class:ch={s.changed} class:off={s.projectOff}>
              <button class="sw" class:on={s.on} type="button" role="switch" aria-checked={s.on}
                      aria-label={`${a.label} for ${t.m.name} in ${t.pr.name}`}
                      disabled={s.projectOff || busy !== ""}
                      title={s.projectOff ? `${a.label} is off for the whole project (Projects › Settings)` : ""}
                      onclick={() => flip(t.pr, t.m, a.key)}></button>
              <span>{a.label}</span>
              {#if s.projectOff}<span class="tag muted">off for the project</span>
              {:else if s.changed}<span class="tag">changed</span>{/if}
            </div>
          {/each}
        </div>
      </div>
    {/each}
  {/if}
</section>

<style>
  .access { display: flex; flex-direction: column; gap: 12px; }
  .pickers { display: flex; gap: 14px; flex-wrap: wrap; align-items: flex-end; }
  .pick { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
  .pick select { min-width: 220px; height: 34px; font-size: 13px; }
  .empty { padding: 24px; text-align: center; }
  .note { display: flex; align-items: center; gap: 8px; font-size: 12.5px; padding: 8px 12px;
          border-radius: var(--radius-control); background: color-mix(in oklab, var(--accent) 12%, transparent);
          color: var(--text); }
  .note .ico { display: inline-flex; color: var(--accent); }
  .proj { border: 1px solid var(--line); border-radius: var(--radius-control); padding: 12px; }
  .ph { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; font-size: 13px; }
  .ph b { font-weight: 600; font-size: 14px; }
  .ph select, .row select { height: 30px; font-size: 12.5px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .areas { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 6px; }
  .ar { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 7px 9px;
        border-radius: 6px; background: color-mix(in oklab, var(--raised), var(--text) 4%); }
  .ar.ch { background: color-mix(in oklab, var(--warn) 18%, transparent); }
  .ar.off { opacity: .6; }
  .tag { margin-left: auto; font-size: 11px; color: var(--warn); }
  .tag.muted { color: var(--muted); }
  .sw { position: relative; width: 32px; height: 18px; border-radius: 9px; border: 0; padding: 0; flex: none;
        background: var(--line-strong); cursor: pointer; }
  .sw::after { content: ""; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 50%;
               background: var(--raised); transition: left 120ms ease; }
  .sw.on { background: var(--ok); }
  .sw.on::after { left: 16px; }
  .sw:disabled { cursor: not-allowed; }
  .sw:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .muted { color: var(--muted); }
  .err { color: var(--err); margin: 0; }
</style>
