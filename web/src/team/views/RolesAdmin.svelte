<!-- RolesAdmin.svelte — what each role may do, changed for everyone who has it.
     Every role can be edited (a built-in one keeps its name and can go back to
     its default); a custom one can also be renamed or deleted. The hub decides:
     this page only sends the ticks. One person's own exceptions are in Access. -->
<script lang="ts">
  import { call } from "../../lib/api/client";
  import { PERMISSION_GROUPS, type Role } from "../../lib/role_permissions";
  import { TriangleAlert, Plus } from "@lucide/svelte";

  type RoleRow = Role & { people?: number; projects?: number };
  let { roles, changed }: { roles: RoleRow[]; changed: () => Promise<void> } = $props();

  let selectedId = $state<number | "new" | null>(null);
  let name = $state("");
  let ticks = $state<Record<string, boolean>>({});
  let busy = $state(false);
  let error = $state("");
  let note = $state("");

  const selected = $derived(typeof selectedId === "number" ? roles.find((r) => r.id === selectedId) : undefined);

  function has(role: Role | undefined, module: string, tick: string): boolean {
    const g = role?.permissions?.[module];
    if (Array.isArray(g)) return g.includes(tick);
    return typeof g === "object" && (g as Record<string, boolean>)?.[tick] === true;
  }
  function load(role: Role | undefined) {
    const t: Record<string, boolean> = {};
    for (const g of PERMISSION_GROUPS) for (const k of g.ticks) t[`${g.module}:${k.key}`] = has(role, g.module, k.key);
    ticks = t;
    name = role?.name ?? "";
  }
  // a click clears the last message; moving on after a save keeps it
  function choose(id: number | "new", keep = false) {
    selectedId = id;
    if (!keep) { error = ""; note = ""; }
    load(id === "new" ? undefined : roles.find((r) => r.id === id));
  }
  // first visit: the first role is open
  $effect(() => { if (selectedId === null && roles.length) choose(roles[0].id, true); });

  // the role's ticks as the hub stores them: the boxes on screen, plus the
  // entries this page doesn't show (Flow's and Now's "view") kept or derived
  function permissionsFrom(base: Role | undefined): Record<string, string[]> {
    const out: Record<string, string[]> = {};
    for (const [mod, list] of Object.entries(base?.permissions ?? {}))
      if (Array.isArray(list)) out[mod] = [...list];
    for (const g of PERMISSION_GROUPS) {
      const shown = new Set(g.ticks.map((k) => k.key));
      const kept = (out[g.module] ?? []).filter((k) => !shown.has(k));
      out[g.module] = [...kept, ...g.ticks.filter((k) => ticks[`${g.module}:${k.key}`]).map((k) => k.key)];
    }
    const sees = (mod: string, key: string) => (out[mod] ?? []).includes(key);
    out.flow = (out.flow ?? []).filter((k) => k !== "view_work_packages");
    if (sees("board", "view_work_packages")) out.flow.push("view_work_packages");
    out.now = (out.now ?? []).filter((k) => k !== "view_activity");
    if (sees("history", "view_activity")) out.now.push("view_activity");
    return out;
  }
  const countIn = (module: string) =>
    PERMISSION_GROUPS.find((g) => g.module === module)!.ticks.filter((k) => ticks[`${module}:${k.key}`]).length;
  const dirty = $derived.by(() => {
    if (selectedId === "new") return !!name.trim();
    if (!selected) return false;
    if (!selected.builtin && name.trim() !== selected.name) return true;
    return PERMISSION_GROUPS.some((g) => g.ticks.some((k) => ticks[`${g.module}:${k.key}`] !== has(selected, g.module, k.key)));
  });

  async function save() {
    if (busy) return;
    busy = true; error = ""; note = "";
    try {
      if (selectedId === "new") {
        if (!name.trim()) { error = "Give the role a name."; return; }
        const made = await call<{ role: Role }>("/roles", {
          name: name.trim(), copy_from: null, permissions: permissionsFrom(undefined) }, "POST");
        await changed();
        note = "Role created.";
        choose(made.role.id, true);
      } else if (selected) {
        const body: Record<string, unknown> = { permissions: permissionsFrom(selected) };
        if (!selected.builtin && name.trim() !== selected.name) body.name = name.trim();
        await call(`/roles/${selected.id}`, body, "PATCH");
        await changed();
        note = `${name.trim() || selected.name} saved for everyone with it.`;
        load(roles.find((r) => r.id === selected.id));
      }
    } catch (e) {
      error = e instanceof Error ? e.message : "The role wasn't saved.";
    } finally { busy = false; }
  }
  async function resetDefault() {
    if (!selected?.builtin || busy) return;
    if (!confirm(`Put ${selected.name} back to its default for everyone with it?`)) return;
    busy = true; error = ""; note = "";
    try {
      await call(`/roles/${selected.id}`, { reset: true }, "PATCH");
      await changed();
      load(roles.find((r) => r.id === selected.id));
      note = `${selected.name} is back to its default.`;
    } catch (e) { error = e instanceof Error ? e.message : "It wasn't reset."; }
    finally { busy = false; }
  }
  async function deleteRole(role: Role) {
    if (role.builtin || busy) return;
    if (!confirm(`Delete the role ${role.name}?`)) return;
    busy = true; error = ""; note = "";
    try {
      await call(`/roles/${role.id}`, {}, "DELETE");
      await changed();
      note = "Role deleted.";
      selectedId = null;
    } catch (e) {
      error = e instanceof Error ? e.message : "This role cannot be deleted";
    } finally { busy = false; }
  }
</script>

<section aria-label="roles" class="roles">
  <div class="everyone" role="note">
    <TriangleAlert size={15} strokeWidth={1.75} />
    <span>Changes here apply to <b>everyone with the role</b>, in every project. One person's own switches are under Access, and stay as they are.</span>
  </div>
  {#if error}<p class="msg err" role="alert">{error}</p>{/if}
  {#if note}<p class="msg okay" role="status">{note}</p>{/if}

  <div class="cols">
    <div class="panel list" aria-label="the roles">
      {#each roles as role (role.id)}
        <button type="button" class="ritem" class:on={selectedId === role.id} data-role-id={role.id}
                aria-pressed={selectedId === role.id} onclick={() => choose(role.id)}>
          <span class="rname">{role.name}</span>
          <span class="muted small">{role.builtin ? "built-in" : "custom"} · {role.people ?? 0} {role.people === 1 ? "person" : "people"}</span>
        </button>
      {/each}
      <button type="button" class="btn tiny newbtn" class:on={selectedId === "new"} onclick={() => choose("new")}>
        <Plus size={13} strokeWidth={1.75} /> New role</button>
    </div>

    {#if selectedId !== null}
      <form class="panel editor" aria-label={selectedId === "new" ? "create a role" : `edit role ${selected?.name ?? ""}`}
            onsubmit={(e) => { e.preventDefault(); void save(); }}>
        <div class="ehead">
          {#if selectedId === "new" || (selected && !selected.builtin)}
            <label class="name">Name
              <input bind:value={name} maxlength={60} required aria-label="role name" placeholder="Reviewer" /></label>
          {:else}
            <h3>{selected?.name}</h3>
          {/if}
          {#if selected}
            <span class="muted small">{selected.builtin ? "built-in" : "custom"} · {selected.people ?? 0}
              {selected.people === 1 ? "person" : "people"} in {selected.projects ?? 0}
              {selected.projects === 1 ? "project" : "projects"}</span>
          {/if}
          <span class="spacer"></span>
          {#if selected?.builtin}
            <button class="btn tiny" type="button" disabled={busy} onclick={() => void resetDefault()}>Reset to default</button>
          {:else if selected}
            <button class="btn danger tiny" type="button" aria-label={`delete role ${selected.name}`}
                    disabled={busy} onclick={() => void deleteRole(selected)}>Delete role</button>
          {/if}
        </div>
        <div class="grid">
          {#each PERMISSION_GROUPS as group (group.module)}
            <fieldset data-module={group.module}>
              <legend>{group.title} <span class="muted small">{countIn(group.module)} of {group.ticks.length}</span></legend>
              {#each group.ticks as tick (tick.key)}
                <label class="tick">
                  <input type="checkbox" bind:checked={ticks[`${group.module}:${tick.key}`]} disabled={busy} />
                  <span>{tick.label}</span>
                </label>
              {/each}
            </fieldset>
          {/each}
        </div>
        {#if dirty && selected && (selected.people ?? 0) > 0}
          <p class="impact"><TriangleAlert size={14} strokeWidth={1.75} />
            Saving changes what {selected.people} {selected.people === 1 ? "person" : "people"} can do in
            {selected.projects} {selected.projects === 1 ? "project" : "projects"}.</p>
        {/if}
        <div class="foot">
          {#if selectedId !== "new"}<button class="btn" type="button" disabled={busy || !dirty} onclick={() => load(selected)}>Cancel</button>{/if}
          <button class="btn pri" type="submit" disabled={busy || !dirty}>
            {selectedId === "new" ? "Create role" : "Save for everyone"}</button>
        </div>
      </form>
    {/if}
  </div>
</section>

<style>
  .roles { display: flex; flex-direction: column; gap: 12px; }
  .everyone, .impact { display: flex; align-items: center; gap: 8px; font-size: 12.5px; padding: 8px 12px; margin: 0;
             border-radius: var(--r-ctl); background: color-mix(in oklch, var(--warn) 16%, transparent); color: var(--text); }
  .everyone :global(svg), .impact :global(svg) { color: var(--warn); flex: none; }
  .cols { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 12px; align-items: start; }
  @media (max-width: 760px) { .cols { grid-template-columns: minmax(0, 1fr); } }
  .panel { background: var(--raised); border-radius: var(--r-panel); padding: 14px 16px;
           box-shadow: var(--shadow), 0 0 0 1px var(--line); display: flex; flex-direction: column; gap: 12px; }
  .list { padding: 8px; gap: 2px; }
  .ritem { all: unset; display: flex; flex-direction: column; gap: 1px; padding: 8px 10px; border-radius: var(--r-ctl); cursor: pointer; }
  .ritem:hover { background: var(--hover); }
  .ritem.on { background: color-mix(in oklch, var(--acc) 14%, transparent); }
  .ritem:focus-visible { outline: 2px solid var(--acc); }
  .rname { font-size: 13.5px; font-weight: 600; }
  .newbtn { margin-top: 6px; justify-content: center; }
  .ehead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .ehead h3 { margin: 0; font-size: 15px; }
  .spacer { flex: 1; }
  .small { font-size: 12px; }
  .muted { color: var(--muted); }
  .msg { margin: 0; font-size: 13px; padding: 8px 12px; border-radius: var(--r-ctl); }
  .msg.err { color: var(--err); background: color-mix(in oklch, var(--err) 10%, transparent); }
  .msg.okay { color: var(--ok-text); background: color-mix(in oklch, var(--ok) 12%, transparent); }
  .name { display: flex; flex-direction: column; gap: 6px; max-width: 320px; font-size: 12px; color: var(--muted); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
  fieldset { min-width: 0; margin: 0; border: 0; border-radius: var(--r-ctl); background: var(--raised-2);
             padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; }
  legend { float: left; width: 100%; padding: 0 0 4px; font-size: 12px; font-weight: 600; color: var(--text); }
  .tick { display: flex; align-items: flex-start; gap: 8px; font-size: 13px; color: var(--text-2); line-height: 1.35; }
  .tick input { margin-top: 1px; flex: none; }
  .foot { display: flex; gap: 8px; justify-content: flex-end; }
</style>
