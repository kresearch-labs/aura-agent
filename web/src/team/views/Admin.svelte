<!-- Admin.svelte — four plain pages. All names, no teasing: Users, Roles, Projects, Hub.
-->
<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { call } from "../../lib/api/client";
  import { route, me } from "../frame/shell_state";
  import { sessionIsActive, type AccountSession } from "../../lib/account_profile";
  import ProjectAdmin from "./ProjectAdmin.svelte";
  import RolesAdmin from "./RolesAdmin.svelte";
  import AccessAdmin from "./AccessAdmin.svelte";
  import type { Project } from "../frame/shell_state";
  import type { Role } from "../../lib/role_permissions";
  import { projectRolesFor, canAdministerSessions, adminTabsFor,
           canManageAccount, isFixedOwner, isTeamAdministrator,
           GLOBAL_STAFF_MANAGER, type AdminPerson,
           isLinkedApp,
           type AdminModule } from "../../lib/admin_users";
  import Avatar from "../../lib/parts/Avatar.svelte";
  import { ChevronDown, Ellipsis, UserPlus } from "@lucide/svelte";
  const TAB_NAMES: Record<string, string> = { users: "Users", access: "Access", roles: "Roles", projects: "Projects", hub: "Hub" };
  type SessionRow = AccountSession & {
    token: string; person: number; agent: string | null; approved_by: string;
  };
  type Person = AdminPerson & { name: string; display: string; nbrow?: boolean;
                  password_set?: boolean; must_change?: boolean; locked_until?: number | null;
                  disabled_at?: number | null };
  type Hub = { fingerprint?: string; address?: string; build?: string; chain_head?: string };
  type ChainHead = { chain: string; seq: number; hash: string };
  type Backup = { name: string; size: number; ts: number };
  type HookSub = { id: number; url_fp: string; types: string[];
                   projects: number[]; created: number; failures: number };
  type HookDelivery = { sub: string; url_fp: string; type: string; ok: boolean;
                        at: number; wid: string; attempts: number;
                        next_retry: number | null; last_error: string };
  type HookList = { subscriptions: HookSub[]; deliveries: HookDelivery[] };
  type HeadAttempt = Readonly<{
    adminLease: number; hubLease: number; generation: number; actorId: number | null;
  }>;
  type CopyAttempt = Readonly<HeadAttempt & { id: number; hash: string }>;
  type HubLifecycle = {
    adminLease: number; hubLease: number; generation: number;
    actorId: number | null; allowed: boolean; hubVisible: boolean;
    attempted: boolean; head: ChainHead | null;
    freshness: "unknown" | "stale" | "fresh";
    read: HeadAttempt | null; headError: string; copySequence: number;
    activeCopy: CopyAttempt | null; copyError: string; copyNote: string;
  };
  let headLifecycle = $state<HubLifecycle>({
    adminLease: 0, hubLease: 0, generation: 0,
    actorId: null, allowed: false, hubVisible: false,
    attempted: false, head: null, freshness: "unknown", read: null,
    headError: "", copySequence: 0, activeCopy: null, copyError: "", copyNote: "",
  });
  let backups = $state<Backup[]>([]);
  let backupLoaded = $state(false);
  let backupHasData = $state(false);
  let backupBusy = $state(false);
  let backupError = $state("");
  let backupNote = $state("");
  let backupEpoch = 0;
  let hooks = $state<HookList>({ subscriptions: [], deliveries: [] });
  let hookLoaded = $state(false);
  let hookHasData = $state(false);
  let hookListBusy = $state(false);
  let hookListError = $state("");
  let hookEpoch = 0;
  let hookProjects = $state<number[]>([]);
  let hookCreateBusy = $state(false);
  let hookCreateError = $state("");
  let freshHook = $state<{ id: number; secret: string } | null>(null);
  let hookSecretShown = $state(false);
  let hookSecretCopy = $state<number | null>(null);
  let hookSecretSequence = 0;
  let hookSecretNote = $state("");
  let hookSecretError = $state("");
  let hookDeleteBusy = $state<number | null>(null);
  let hookRemoveError = $state("");
  let metricKey = $state<{ key: string; note: string } | null>(null);
  let metricBusy = $state(false);
  let metricShown = $state(false);
  let metricEpoch = 0;
  let metricCopy = $state<number | null>(null);
  let metricSequence = 0;
  let metricNote = $state("");
  let metricError = $state("");
  let moveOpen = $state(false);
  let moveDevices = $state<{ id: string; name: string; person_name: string;
                             revoked_at: number | null }[]>([]);
  let moveDevicesBusy = $state(false);
  let moveDevicesError = $state("");
  let moveBusyId = $state<string | null>(null);
  let moveError = $state("");
  let moveNote = $state("");
  let moveEpoch = 0;

  let tab = $state<AdminModule>("users");
  let people = $state<Person[]>([]);
  let roles = $state<Role[]>([]);
  let projects = $state<Project[]>([]);
  let hub = $state<Hub>({});
  let loading = $state(false);
  let error = $state("");
  let refreshEpoch = 0;
  let roleBusy = $state<number | null>(null);
  let roleError = $state("");
  let roleNote = $state("");
  let sessionFor = $state<number | null>(null);
  let sessionRows = $state<SessionRow[]>([]);
  let sessionLoading = $state<number | null>(null);
  let sessionDeleting = $state("");
  let sessionError = $state("");
  let sessionNote = $state("");
  let sessionEpoch = 0;

  $effect(() => {
    const m = $route.sub;
    const available = adminTabsFor($me.value);
    if (m === "users" || m === "access" || m === "roles" || m === "projects" || m === "hub") {
      if (available.includes(m)) tab = m;
      else { tab = "users"; route.go("admin", "users"); }
    }
    if (!available.includes(tab)) { tab = "users"; route.go("admin", "users"); }
    syncHubVisibility();
  });

  async function refresh() {
    // Refreshing the surrounding data must not unmount the editor being used.
    // Only the first visit has a page-level loading screen. An older read may
    // finish after a newer write/read; it must not roll back either their rows
    // or the newer read's loading, error and hub states.
    const seq = ++refreshEpoch;
    loading = !loadedOnce; error = "";
    try {
      const [a, b, c] = await Promise.all([
        call<{ people: Person[] }>("/people"),
        call<{ roles: Role[] }>("/roles"),
        call<{ projects: Project[] }>("/projects"),
      ]);
      if (seq !== refreshEpoch) return;
      people = a.people.filter((p) => !isLinkedApp(p)); roles = b.roles; projects = c.projects;
      if (isTeamAdministrator($me.value) && (!loadedOnce || tab === "hub")) {
        try {
          const latest = await call<Hub>("/info");
          if (seq === refreshEpoch)
            hub = { ...latest, address: window.location.origin };
        } catch { if (seq === refreshEpoch) hub = {}; }
      }
    } catch (e) {
      if (seq === refreshEpoch) error = e instanceof Error ? e.message : "no answer";
    } finally {
      if (seq === refreshEpoch) { loading = false; loadedOnce = true; }
    }
  }
  let loadedOnce = false;
  onMount(() => { void refresh(); });
  onDestroy(() => {
    // A head or copy belongs to this mounted actor and its actual Hub entry.
    // Retire eligibility on every boundary; never pretend a native write ended.
    ++refreshEpoch;
    ++sessionEpoch;
    ++backupEpoch;
    ++hookEpoch;
    // A one-time secret is a handoff to the admin on this page; leaving the
    // page ends the handoff, wherever the awaits stood.
    freshHook = null;
    hookSecretShown = false;
    hookSecretNote = "";
    hookSecretError = "";
    ++metricEpoch;
    metricKey = null;
    metricShown = false;
    metricNote = "";
    metricError = "";
    ++moveEpoch;
    moveOpen = false;
    moveDevices = [];
    moveNote = "";
    moveError = "";
    ++headLifecycle.adminLease;
    ++headLifecycle.hubLease;
    ++headLifecycle.generation;
    headLifecycle.actorId = null;
    headLifecycle.allowed = false;
    headLifecycle.hubVisible = false;
    headLifecycle.attempted = false;
    headLifecycle.read = null;
    headLifecycle.headError = "";
    headLifecycle.copyError = "";
    headLifecycle.copyNote = "";
  });

  function syncHubVisibility() {
    const life = headLifecycle;
    const actorId = $me.value?.id ?? null;
    const allowed = isTeamAdministrator($me.value);
    const visible = tab === "hub" && allowed;
    const actorChanged = actorId !== life.actorId;
    const authorityRevoked = life.allowed && !allowed;
    if (actorChanged || allowed !== life.allowed || visible !== life.hubVisible) {
      ++life.hubLease;
      ++life.generation;
      life.read = null;
      life.attempted = false;
      life.actorId = actorId;
      life.allowed = allowed;
      life.hubVisible = visible;
      life.headError = ""; life.copyError = ""; life.copyNote = "";
      if (actorChanged || authorityRevoked) {
        life.head = null; life.freshness = "unknown";
      } else if (life.head !== null) {
        life.freshness = "stale";
      }
    }
    if (visible && !life.attempted && life.read === null) void beginHeadRead();
  }

  $effect(() => {
    if (tab === "hub" && isTeamAdministrator($me.value)) {
      if (!backupLoaded && !backupBusy) void loadBackups();
      if (!hookLoaded && !hookListBusy) void loadHooks();
    }
  });

  async function readBackups(owner: number) {
    if (owner !== backupEpoch) return;
    const result = await call<{ backups: Backup[] }>("/backups");
    if (owner !== backupEpoch) return;
    backups = result.backups;
    backupHasData = true;
  }

  async function loadBackups() {
    if (backupBusy) return;
    const owner = ++backupEpoch;
    backupBusy = true; backupError = ""; backupNote = "";
    try {
      await readBackups(owner);
    } catch (e) {
      if (owner === backupEpoch)
        backupError = e instanceof Error ? e.message : "no answer";
    } finally {
      if (owner === backupEpoch) { backupBusy = false; backupLoaded = true; }
    }
  }

  async function createBackup() {
    if (backupBusy) return;
    const owner = ++backupEpoch;
    let wrote = false;
    backupBusy = true; backupError = ""; backupNote = "";
    try {
      await call<{ ok: boolean; detail: string }>("/backups", {}, "POST");
      wrote = true;
      if (owner !== backupEpoch) return;
      invalidateHeadAfterBackup();
      // The ancillary read owns its own errors and never holds list/status.
      void beginHeadRead();
      await readBackups(owner);
      if (owner !== backupEpoch) return;
      backupNote = "Backup created.";
    } catch (e) {
      if (owner === backupEpoch) {
        const reason = e instanceof Error ? e.message : "no answer";
        backupError = wrote
          ? `Backup created, but its list could not be refreshed: ${reason}`
          : reason;
      }
    } finally {
      if (owner === backupEpoch) { backupBusy = false; backupLoaded = true; }
    }
  }

  function invalidateHeadAfterBackup() {
    const life = headLifecycle;
    ++life.generation;
    life.read = null;
    life.attempted = false;
    life.headError = ""; life.copyError = ""; life.copyNote = "";
    life.freshness = life.head === null ? "unknown" : "stale";
    // Native clipboard activity is not cleared until its own await settles.
  }

  function canReadHead(): boolean {
    const life = headLifecycle;
    return life.hubVisible && life.allowed && tab === "hub" &&
      isTeamAdministrator($me.value) && life.actorId === ($me.value?.id ?? null) &&
      life.read === null;
  }

  function ownsHeadAttempt(attempt: HeadAttempt): boolean {
    const life = headLifecycle;
    return life.read?.generation === attempt.generation &&
      life.generation === attempt.generation &&
      life.adminLease === attempt.adminLease && life.hubLease === attempt.hubLease &&
      life.actorId === attempt.actorId &&
      life.hubVisible && life.allowed && tab === "hub" &&
      isTeamAdministrator($me.value) && life.actorId === ($me.value?.id ?? null);
  }

  async function beginHeadRead(): Promise<void> {
    const life = headLifecycle;
    if (!canReadHead()) return;
    const attempt: HeadAttempt = {
      adminLease: life.adminLease, hubLease: life.hubLease,
      generation: ++life.generation, actorId: life.actorId,
    };
    life.attempted = true;
    life.read = attempt;
    life.headError = ""; life.copyError = ""; life.copyNote = "";
    if (life.head !== null) life.freshness = "stale";
    try {
      const next = await call<ChainHead>("/log/head");
      if (!ownsHeadAttempt(attempt)) return;
      life.head = next;
      life.freshness = "fresh";
      life.headError = "";
      life.read = null;
    } catch (e) {
      if (!ownsHeadAttempt(attempt)) return;
      // A discarded attempt never consumes a later Hub admission.
      life.headError = e instanceof Error ? e.message : "Could not read the chain head.";
      life.freshness = life.head === null ? "unknown" : "stale";
      life.read = null;
    }
  }

  function chainHashLabel(): string {
    if (headLifecycle.freshness === "fresh") return "Current hash";
    if (headLifecycle.headError) return "Last known hash (refresh failed)";
    return "Last known hash (refresh needed)";
  }

  function canCopyChainHead(): boolean {
    const life = headLifecycle;
    return life.head !== null && !!life.head.hash && life.freshness === "fresh" &&
      life.read === null && life.activeCopy === null &&
      life.hubVisible && life.allowed && tab === "hub" &&
      isTeamAdministrator($me.value) && life.actorId === ($me.value?.id ?? null);
  }

  function ownsCopyAttempt(attempt: CopyAttempt): boolean {
    const life = headLifecycle;
    return life.activeCopy?.id === attempt.id &&
      life.generation === attempt.generation &&
      life.adminLease === attempt.adminLease && life.hubLease === attempt.hubLease &&
      life.actorId === attempt.actorId &&
      life.hubVisible && life.allowed && tab === "hub" &&
      isTeamAdministrator($me.value) && life.actorId === ($me.value?.id ?? null) &&
      life.read === null && life.freshness === "fresh" && life.head?.hash === attempt.hash;
  }

  async function copyChainHead(event: MouseEvent) {
    if (!canCopyChainHead()) return;
    const life = headLifecycle;
    const hash = life.head!.hash;
    const attempt: CopyAttempt = {
      id: ++life.copySequence, adminLease: life.adminLease, hubLease: life.hubLease,
      generation: life.generation, actorId: life.actorId, hash,
    };
    const button = event.currentTarget as HTMLButtonElement;
    life.activeCopy = attempt;
    life.copyError = ""; life.copyNote = "";
    try {
      if (!window.isSecureContext || !navigator.clipboard ||
          typeof navigator.clipboard.writeText !== "function")
        throw new Error("Clipboard unavailable");
      // Capture before awaiting: only current ownership may publish completion UI.
      await navigator.clipboard.writeText(hash);
      if (ownsCopyAttempt(attempt)) life.copyNote = "Chain head copied.";
    } catch {
      if (ownsCopyAttempt(attempt)) {
        life.copyError = "Clipboard write did not complete. The readonly hash is selected for manual copying.";
        const field = button.closest("section")?.querySelector<HTMLTextAreaElement>(
          "[data-chain-hash]");
        field?.focus(); field?.select();
      }
    } finally {
      // This release proves the awaited native operation actually finished.
      if (life.activeCopy?.id === attempt.id) life.activeCopy = null;
    }
  }

  function formatBytes(size: number): string {
    if (size < 1024) return `${size} bytes`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
  }

  async function readHooks(owner: number) {
    if (owner !== hookEpoch) return;
    const data = await call<HookList>("/webhooks");
    if (owner !== hookEpoch) return;
    hooks = { subscriptions: data.subscriptions, deliveries: data.deliveries };
    hookHasData = true;
  }

  async function loadHooks() {
    if (hookListBusy) return;
    const owner = ++hookEpoch;
    hookListBusy = true; hookListError = "";
    try {
      await readHooks(owner);
    } catch (e) {
      // A failed read never becomes a fake "no webhooks" result: last-known
      // rows stay on the card next to the error.
      if (owner === hookEpoch)
        hookListError = e instanceof Error ? e.message : "Could not load webhooks.";
    } finally {
      // The mutex belongs to this view instance, not the list generation.
      hookListBusy = false;
      hookLoaded = true;
    }
  }

  async function addHook(e: Event) {
    e.preventDefault();
    if (hookCreateBusy) return;
    const form = e.currentTarget as HTMLFormElement;
    const f = new FormData(form);
    const url = String(f.get("hook_url") ?? "").trim();
    const types = String(f.get("hook_types") ?? "")
      .split(",").map((t) => t.trim()).filter(Boolean).slice(0, 20);
    if (!/^https?:\/\//.test(url)) {
      hookCreateError = "Give the webhook URL (http or https).";
      return;
    }
    if (!hookProjects.length) {
      // an empty selection is not a team-wide hook; the hub refuses one
      hookCreateError = "Choose at least one project the hook listens to.";
      return;
    }
    const owner = ++hookEpoch;
    hookCreateBusy = true; hookCreateError = ""; hookListError = "";
    try {
      const made = await call<{ id: number; secret: string }>(
        "/webhooks", { url, types, projects: [...hookProjects] }, "POST");
      // The one-time handoff belongs to the admin on THIS page, not to the list generation. Row
      // publication stays epoch-owned below.
      freshHook = { id: made.id, secret: made.secret };
      hookSecretShown = false; hookSecretNote = ""; hookSecretError = "";
      form.reset(); hookProjects = [];
      try {
        await readHooks(owner);
      } catch (le) {
        if (owner === hookEpoch)
          hookListError = le instanceof Error ? le.message : "Could not load webhooks.";
      }
    } catch (e2) {
      if (owner === hookEpoch)
        hookCreateError = e2 instanceof Error ? e2.message : "The hook was not saved.";
    } finally {
      // the busy mutex belongs to this view instance: a superseded add
      // still has to release it, or the form latches until a reload
      hookCreateBusy = false;
    }
  }

  function clearFreshHook(): void {
    freshHook = null;
    hookSecretShown = false;
    hookSecretNote = "";
    hookSecretError = "";
  }

  async function copyHookSecret(): Promise<void> {
    if (!freshHook || hookSecretCopy !== null) return;
    const pinned = { id: freshHook.id, secret: freshHook.secret };
    const attempt = ++hookSecretSequence;
    hookSecretCopy = attempt; hookSecretNote = ""; hookSecretError = "";
    try {
      if (!window.isSecureContext || !navigator.clipboard ||
          typeof navigator.clipboard.writeText !== "function")
        throw new Error("Clipboard unavailable");
      // Capture before awaiting: only the current secret may publish success.
      await navigator.clipboard.writeText(pinned.secret);
      if (freshHook && freshHook.id === pinned.id &&
          freshHook.secret === pinned.secret)
        hookSecretNote = "Webhook secret copied.";
    } catch {
      if (freshHook && freshHook.id === pinned.id) {
        hookSecretError = "Clipboard write did not complete. The readonly secret is selected for manual copying.";
        const field = document.getElementById("hook-fresh-secret") as
          HTMLInputElement | null;
        field?.focus(); field?.select();
      }
    } finally {
      // This release proves the awaited native operation actually finished.
      if (hookSecretCopy === attempt) hookSecretCopy = null;
    }
  }

  async function removeHook(id: number): Promise<void> {
    if (hookDeleteBusy !== null) return;
    const sub = hooks.subscriptions.find((s) => s.id === id);
    if (!sub) return;
    // Every removal is its own deliberate confirmation for the exact row.
    if (!confirm(`Remove webhook ${sub.url_fp}? Its pending retries stop.`)) return;
    const owner = ++hookEpoch;
    hookDeleteBusy = id; hookRemoveError = "";
    try {
      await call(`/webhooks/${id}`, {}, "DELETE");
      // The removal happened: retire its secret panel before any epoch decision — row
      // publication below stays epoch-owned.
      if (freshHook?.id === id) clearFreshHook();
      if (owner !== hookEpoch) return;
      await readHooks(owner);
    } catch (e) {
      if (owner === hookEpoch)
        hookRemoveError = e instanceof Error ? e.message : "The webhook was not removed.";
    } finally {
      // unconditional: while hookDeleteBusy is set no second removal can
      // start, so the flag is always this removal's own to release
      hookDeleteBusy = null;
    }
  }

  function hookProjectNames(ids: number[]): string {
    return ids.map((id) => projects.find((p) => p.id === id)?.name ?? `#${id}`)
      .join(", ");
  }

  async function makeMetricsKey(): Promise<void> {
    if (metricBusy) return;
    // Replacement is always real: the server keeps only the sha256, so no
    // page can ever show the old key again — the confirm says so.
    if (!confirm("Show a new metrics key? Any key made before stops working."))
      return;
    const owner = ++metricEpoch;
    metricBusy = true; metricError = ""; metricNote = "";
    try {
      const made = await call<{ key: string; note: string }>(
        "/metrics/key", {}, "POST");
      // The one-time handoff belongs to the admin on THIS page: publish before any epoch
      // decision; only Clear, a newer key or unmount ends it.
      metricKey = made;
      metricShown = false;
      metricNote = "Metrics key shown once — store it now.";
    } catch (e) {
      if (owner === metricEpoch)
        metricError = e instanceof Error ? e.message : "The key was not made.";
    } finally {
      metricBusy = false;
    }
  }

  function clearMetricsKey(): void {
    metricKey = null;
    metricShown = false;
    metricNote = "";
    metricError = "";
  }

  async function toggleMove(): Promise<void> {
    if (moveOpen) { moveOpen = false; return; }
    moveOpen = true; moveError = ""; moveNote = "";
    const owner = ++moveEpoch;
    moveDevicesBusy = true; moveDevicesError = "";
    try {
      const data = await call<{ devices: { id: string; name: string;
        person_name: string; revoked_at: number | null }[] }>("/devices");
      if (owner !== moveEpoch) return;
      moveDevices = data.devices.filter((d) => !d.revoked_at);
    } catch (e) {
      if (owner === moveEpoch)
        moveDevicesError = e instanceof Error ? e.message : "Could not load devices.";
    } finally {
      if (owner === moveEpoch) moveDevicesBusy = false;
    }
  }

  async function prepareMoveBundle(device: { id: string; name: string;
    person_name: string }): Promise<void> {
    if (moveBusyId !== null) return;
    // One deliberate confirm for the exact device; the bundle carries the
    // whole sealed team — anyone without that device's key reads nothing.
    if (!confirm(`Seal the move bundle for ${device.name} (${device.person_name})? ` +
        "Carry the downloaded file to the new machine yourself.")) return;
    const owner = ++moveEpoch;
    moveBusyId = device.id; moveError = ""; moveNote = "";
    try {
      const data = await call<{ bundle: string }>(
        `/hub/bundle?device=${encodeURIComponent(device.id)}`);
      // decode + hand off as a real download; the page itself cannot move
      // the hub — the receiver's Aura runs the restore (card law)
      const bin = atob(data.bundle);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      if (owner !== moveEpoch) return;
      const blob = new Blob([bytes], { type: "application/octet-stream" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "aura-hub-bundle.zip";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      moveNote = `Bundle sealed for ${device.name}. Move the file and start `
        + "the take-over there; the fingerprint is unchanged.";
    } catch (e) {
      if (owner === moveEpoch)
        moveError = e instanceof Error ? e.message : "The bundle was not sealed.";
    } finally {
      moveBusyId = null;
    }
  }

  async function copyMetricsKey(): Promise<void> {
    if (!metricKey || metricCopy !== null) return;
    const pinned = { key: metricKey.key };
    const attempt = ++metricSequence;
    metricCopy = attempt;
    metricNote = ""; metricError = "";
    try {
      if (!window.isSecureContext || !navigator.clipboard ||
          typeof navigator.clipboard.writeText !== "function")
        throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(pinned.key);
      if (metricKey && metricKey.key === pinned.key)
        metricNote = "Metrics key copied.";
    } catch {
      if (metricKey && metricKey.key === pinned.key) {
        metricError = "Clipboard write did not complete. The readonly key is selected for manual copying.";
        const field = document.getElementById("metrics-fresh-key") as
          HTMLInputElement | null;
        field?.focus(); field?.select();
      }
    } finally {
      if (metricCopy === attempt) metricCopy = null;
    }
  }

  async function addUser(e: Event) {
    e.preventDefault();
    const form = e.currentTarget as HTMLFormElement;
    const f = new FormData(form);
    const name = String(f.get("name") ?? "").trim();
    const password = String(f.get("pw") ?? "");
    if (!name || !password) return;
    await call("/people", { name, password, must_change: true }, "POST");
    form.reset();
    // a message to send them: where, who, what to do (the password is only
    // in this message, never kept by the page)
    let where = location.origin;
    try {
      const i = await call<{ address?: string }>("/hub/lan");
      if (i.address && /^(127\.|localhost|\[::1\])/.test(location.hostname)) where = i.address;
    } catch { /* the page's own address */ }
    invite = { name, text:
      `You're on our Aura team.\n\n` +
      `1. Open ${where}/team/ and sign in as "${name}" with the password: ${password}\n` +
      `   You'll choose your own password right away.\n` +
      `   (The page warns about its certificate the first time — that's expected for our own hub.)\n` +
      `2. Connect your Aura: open Aura and say "connect me to my team at ${where}".\n` +
      `   It asks your name ("${name}") and your new password.\n` +
      `3. Then link your work by naming a board task in your request, for example: #3 fix the login.` };
    inviteNote = "";
    void refresh();
  }
  let invite = $state<{ name: string; text: string } | null>(null);
  let inviteNote = $state("");
  async function copyInvite() {
    if (!invite) return;
    try { await navigator.clipboard.writeText(invite.text); inviteNote = "Copied."; }
    catch { inviteNote = "Select the text and copy it."; }
  }
  async function setPassword(id: number) {
    const row = people.find((p) => p.id === id);
    if (!row || !canManageAccount(row, $me.value)) return;
    const pw = window.prompt(`new password for ${row.name} (hidden once set — pry once more)`);
    if (!pw) return;
    await call(`/people/${id}`, { password: pw, must_change: true }, "PATCH");
    void refresh();
  }
  async function disableUser(id: number, disable: boolean) {
    const person = people.find((p) => p.id === id);
    if (!person || !canManageAccount(person, $me.value)) return;
    await call(`/people/${id}`, { disabled: disable }, "PATCH");
    void refresh();
  }
  async function setMustChange(id: number, on: boolean) {
    const person = people.find((p) => p.id === id);
    if (!person || !canManageAccount(person, $me.value)) return;
    await call(`/people/${id}`, { must_change: on }, "PATCH");
    void refresh();
  }
  async function unlockUser(id: number) {
    const person = people.find((p) => p.id === id);
    if (!person || !canManageAccount(person, $me.value)) return;
    await call(`/people/${id}`, { unlock: true }, "PATCH");
    void refresh();
  }

  async function setTeamRole(p: Person, chooser: HTMLSelectElement) {
    const before = isFixedOwner(p) ? "owner" : isTeamAdministrator(p) ? "admin" : "user";
    const value = chooser.value;
    if (roleBusy !== null || !isTeamAdministrator($me.value) || isFixedOwner(p)) {
      chooser.value = before; return;
    }
    if (!confirm(`Change ${p.name}'s team role to ${value}?`)) {
      chooser.value = before;
      return;
    }
    roleBusy = p.id; roleError = ""; roleNote = "";
    try {
      await call(`/people/${p.id}`, { team_role: value }, "PATCH");
      await refresh();
      roleNote = `${p.name}: team role ${value} saved`;
    } catch (e) {
      chooser.value = before;
      roleError = e instanceof Error ? e.message : "team role was not changed";
    } finally { roleBusy = null; }
  }

  async function setGlobalRole(p: Person, chooser: HTMLSelectElement) {
    const before = p.global_role || "";
    const value = chooser.value;
    if (roleBusy !== null || !isTeamAdministrator($me.value) || isFixedOwner(p)) {
      chooser.value = before; return;
    }
    if (!confirm(`${value ? `Assign ${value}` : "Clear the global role"} for ${p.name}?`)) {
      chooser.value = before;
      return;
    }
    roleBusy = p.id; roleError = ""; roleNote = "";
    try {
      await call(`/people/${p.id}`, { global_role: value }, "PATCH");
      await refresh();
      roleNote = value ? `${p.name}: global role saved` : `${p.name}: global role cleared`;
    } catch (e) {
      chooser.value = before;
      roleError = e instanceof Error ? e.message : "global role was not changed";
    } finally { roleBusy = null; }
  }

  async function toggleSessions(p: Person) {
    if (sessionLoading !== null || sessionDeleting) return;
    if (sessionFor === p.id) {
      sessionFor = null; sessionRows = []; ++sessionEpoch;
      sessionError = ""; sessionNote = "";
      return;
    }
    sessionFor = p.id; sessionRows = []; sessionNote = "";
    await loadSessions(p);
  }

  async function loadSessions(p: Person): Promise<boolean> {
    if (!canAdministerSessions(p, $me.value)) return false;
    const seq = ++sessionEpoch;
    sessionLoading = p.id; sessionError = ""; sessionNote = "";
    try {
      const data = await call<{sessions: SessionRow[]}>("/sessions");
      if (seq !== sessionEpoch || sessionFor !== p.id) return false;
      sessionRows = data.sessions.filter((s) =>
        s.person === p.id && !s.current && sessionIsActive(s));
      return true;
    } catch (e) {
      if (seq === sessionEpoch && sessionFor === p.id)
        sessionError = e instanceof Error ? e.message : "Could not load sessions.";
      return false;
    } finally {
      if (seq === sessionEpoch) sessionLoading = null;
    }
  }

  async function revokeUserSession(p: Person, session: SessionRow) {
    if (sessionDeleting || sessionLoading !== null) return;
    if (!canAdministerSessions(p, $me.value) ||
        !confirm(`Sign out ${p.name}'s session?`)) return;
    sessionDeleting = session.token; sessionError = ""; sessionNote = "";
    try {
      await call(`/sessions/${encodeURIComponent(session.token)}`, {}, "DELETE");
      if (sessionFor !== p.id) return;
      if (await loadSessions(p)) sessionNote = `Signed out session for ${p.name}.`;
    } catch (e) {
      if (sessionFor === p.id)
        sessionError = e instanceof Error ? e.message : "Could not sign out the session.";
    } finally {
      if (sessionDeleting === session.token) sessionDeleting = "";
    }
  }

  function addToPerson(p: Person): string {
    return p.password_set ? "" : "no password";
  }
</script>

<svelte:head><title>Admin · Aura Teams</title></svelte:head>
<!-- a person's "…" menu closes on Esc, on a click outside it, and after its action -->
<svelte:window
  onkeydown={(e) => { if (e.key === "Escape") document.querySelectorAll("details.rowmenu[open]").forEach((d) => d.removeAttribute("open")); }}
  onpointerdown={(e) => { const t = e.target as HTMLElement;
    document.querySelectorAll("details.rowmenu[open]").forEach((d) => { if (!d.contains(t)) d.removeAttribute("open"); }); }} />

<div class="wrap">
  <header class="ph">
    <h1>Admin</h1>
    <div class="seg" role="tablist" aria-label="Administration sections">
      {#each adminTabsFor($me.value) as t}
        <button role="tab" aria-selected={tab === t} class:on={tab === t}
                onclick={() => { tab = t; route.go("admin", t);
                                 if (t === "hub") { void refresh(); void loadBackups(); void loadHooks(); }}}>{TAB_NAMES[t] ?? t}</button>
      {/each}
    </div>
  </header>

  {#if loading}<p class="muted">Loading…</p>
  {:else if error}<p class="err card">{error}</p>
  {:else if tab === "users"}
    <section class="panel" aria-labelledby="people-heading">
      <div class="panel-h">
        <div><h3 id="people-heading">People</h3>
          <p class="muted">{people.length} {people.length === 1 ? "person" : "people"} on this team</p></div>
      </div>
      {#if invite}
        <section class="invite" aria-label={`invite for ${invite.name}`}>
          <div class="invite-h"><b>{invite.name} is added — send them this</b><span class="spacer"></span>
            <button class="btn tiny" type="button" onclick={() => void copyInvite()}>Copy</button>
            <button class="btn tiny" type="button" onclick={() => (invite = null)}>Done</button></div>
          <textarea readonly rows="8" aria-label="the invite message">{invite.text}</textarea>
          {#if inviteNote}<p class="muted" role="status">{inviteNote}</p>{/if}
        </section>
      {/if}
      <form class="addrow" onsubmit={addUser} aria-label="add a person">
        <input name="name" placeholder="Username (lowercase)" aria-label="new username" required />
        <input name="pw" type="password" placeholder="Their first password" aria-label="their first password"
               required autocomplete="new-password" />
        <button class="btn pri" type="submit"><UserPlus size={14} strokeWidth={1.75} />Add person</button>
      </form>
      {#if roleError}<p class="err" role="alert">{roleError}</p>{/if}
      {#if roleNote}<p class="okay" role="status">{roleNote}</p>{/if}
      <div class="ptable" role="presentation">
        <div class="phead" aria-hidden="true"><span>Person</span><span>Team role</span><span>Global role</span><span></span></div>
      <ul class="people">
        {#each people as p}
          <li class="person">
            <div class="who">
              <Avatar name={p.name} display={p.display} size="md" />
              <div class="who-t">
                <div class="who-n"><b>{p.display && p.display !== p.name ? p.display : p.name}</b>
                  {#if p.display && p.display !== p.name}<span class="muted">@{p.name}</span>{/if}
                  {#if isFixedOwner(p)}<span class="chip owner">owner</span>{/if}
                  {#if p.disabled_at}<span class="chip warn">disabled</span>{/if}
                  {#if p.must_change}<span class="chip" title="they must change it on sign in">new password first</span>{/if}
                  {#if p.locked_until}<span class="chip warn" title="locked after too many wrong tries">locked</span>{/if}
                  {#if addToPerson(p)}<span class="muted late">{addToPerson(p)}</span>{/if}
                </div>
                <ul class="member-projects" aria-label={`Projects and roles for ${p.name}`}>
                  {#each projectRolesFor(p, projects) as membership (`${membership.projectId}:${membership.source}`)}
                    <li>
                      <span>{membership.projectName}</span>
                      <span class="muted"> · {membership.role}</span>
                      {#if membership.archived}<span class="chip warn">archived</span>{/if}
                    </li>
                  {:else}
                    <li class="muted">No visible project memberships</li>
                  {/each}
                </ul>
              </div>
            </div>
            <span class="sel">
              <select aria-label={`team role for ${p.name}`}
                      value={isFixedOwner(p) ? "owner" : isTeamAdministrator(p) ? "admin" : "user"}
                      disabled={roleBusy !== null || isFixedOwner(p) || !isTeamAdministrator($me.value)}
                      onchange={(e: Event) => void setTeamRole(p,
                        e.currentTarget as HTMLSelectElement)}>
                {#if isFixedOwner(p)}
                  <option value="owner">Owner</option>
                {:else}
                  <option value="user">User</option>
                  <option value="admin">Admin</option>
                {/if}
              </select>
              <ChevronDown size={14} strokeWidth={1.75} />
            </span>
            <span class="sel">
              <select aria-label={`Global role for ${p.name}`}
                      title={p.global_role || "No global role"} value={p.global_role || ""}
                      disabled={roleBusy !== null || isFixedOwner(p) || !isTeamAdministrator($me.value)}
                      onchange={(e: Event) => void setGlobalRole(p,
                        e.currentTarget as HTMLSelectElement)}>
                <option value="">None</option>
                <option value={GLOBAL_STAFF_MANAGER}>Staff and projects manager</option>
              </select>
              <ChevronDown size={14} strokeWidth={1.75} />
            </span>
            <div class="acts">
              {#if canAdministerSessions(p, $me.value)}
                <button class="btn ghost tiny" aria-label={`Sessions for ${p.name}`}
                        aria-expanded={sessionFor === p.id}
                        disabled={sessionLoading !== null || !!sessionDeleting}
                        onclick={() => void toggleSessions(p)}>Sessions</button>
              {/if}
              {#if canManageAccount(p, $me.value)}
              <details class="rowmenu" aria-label={`actions for ${p.name}`}>
                <summary aria-label={`open actions for ${p.name}`}><Ellipsis size={16} strokeWidth={1.75} /></summary>
                <div class="rowmenu-card menu" role="presentation"
                     onclick={(e) => { if ((e.target as HTMLElement).closest("button")) (e.currentTarget as HTMLElement).closest("details")?.removeAttribute("open"); }}>
                  <button class="mi ghost" onclick={() => setPassword(p.id)}>Set a new password</button>
                  <button class="mi ghost" onclick={() => setMustChange(p.id, !p.must_change)}>
                    {#if p.must_change}Don't require a new password{:else}Require a new password at sign-in{/if}
                  </button>
                  {#if p.locked_until}
                    <button class="mi ghost" onclick={() => unlockUser(p.id)}>Unlock</button>
                  {/if}
                  <button class="mi ghost" class:danger={!p.disabled_at} onclick={() => disableUser(p.id, !p.disabled_at)}>
                    {p.disabled_at ? "Bring them back" : "Disable"}
                  </button>
                </div>
              </details>
              {/if}
            </div>
            {#if sessionFor === p.id}
              <section class="user-sessions" aria-label={`Sessions of ${p.name}`}>
                <h4>Sessions</h4>
                {#if sessionError}<p class="err" role="alert">{sessionError}</p>{/if}
                {#if sessionNote}<p class="okay" role="status">{sessionNote}</p>{/if}
                {#if sessionLoading !== null}
                  <p class="muted">Loading sessions…</p>
                {:else if !sessionRows.length}
                  <p class="muted">No other active sessions.</p>
                {:else}
                  {#each sessionRows as session (session.token)}
                    <div class="session-row" data-session-token={session.token}>
                      <div>
                        <div>{session.agent || (session.approved_by === "signin" ?
                          "Browser sign-in" : session.approved_by || "Browser session")}</div>
                        <span class="muted">Last used {session.last_used ?
                          new Date(session.last_used * 1000).toLocaleString() : "Not recorded"}</span>
                        <code>{session.token}</code>
                      </div>
                      <button class="btn tiny" disabled={!!sessionDeleting}
                              onclick={() => void revokeUserSession(p, session)}>Sign out</button>
                    </div>
                  {/each}
                {/if}
              </section>
            {/if}
          </li>
        {/each}
      </ul>
      </div>
    </section>
  {:else if tab === "access"}
    <AccessAdmin {people} {projects} {roles} changed={refresh} />
  {:else if tab === "roles"}
    <RolesAdmin {roles} changed={refresh} />
  {:else if tab === "projects"}
    <ProjectAdmin {projects} {people} {roles} changed={refresh} />
  {:else if tab === "hub"}
    <section class="hubgrid part" aria-labelledby="hub-heading">
      <h3 id="hub-heading" class="sr">Hub</h3>
      <section class="panel hubid wide" aria-label="this hub">
        <div class="panel-h"><div><h4>This hub</h4>
          <p class="muted">Where your team signs in, and the key fingerprint people check once.</p></div></div>
        <dl class="kv">
          <dt>Address</dt><dd class="hub-connection"><span>{hub.address ?? ""}</span></dd>
          <dt>Fingerprint</dt><dd class="hub-connection mono"><span>{hub.fingerprint ?? ""}</span></dd>
        </dl>
      </section>
      <section aria-label="chain head" class="panel chain-panel">
        <div class="chain-actions">
          <div class="ttl"><h4>Chain head</h4>
            <p class="muted">The latest link of the team's history. Matching it proves nothing was changed.</p></div>
          <span class="spacer"></span>
          <button class="btn ghost tiny" type="button" disabled={headLifecycle.read !== null}
                  onclick={() => void beginHeadRead()}>Refresh chain head</button>
        </div>
        {#if headLifecycle.headError}<p class="err" role="alert">{headLifecycle.headError}</p>{/if}
        {#if !headLifecycle.head && (!headLifecycle.attempted || headLifecycle.read !== null)}
          <p class="muted" role="status">Loading chain head…</p>
        {:else if !headLifecycle.head}
          <p class="muted">Chain head unavailable.</p>
        {/if}
        {#if headLifecycle.head !== null}
          <div class="chain-heading">
            <span class="muted chain-kind">{headLifecycle.head.chain}</span>
            <span class="muted">Sequence <code class="chain-sequence">{headLifecycle.head.seq}</code></span>
            {#if headLifecycle.read !== null}<span class="muted" role="status">Refreshing…</span>{/if}
          </div>
          <label class="chain-hash-label" for="team-chain-hash">{chainHashLabel()}</label>
          <textarea id="team-chain-hash" class="chain-hash" data-chain-hash
                    aria-label="Chain head hash" readonly rows="3"
                    autocomplete="off" spellcheck="false"
                    value={headLifecycle.head.hash}></textarea>
          <div class="chain-actions">
            <button class="btn pri" type="button"
                    disabled={!canCopyChainHead()}
                    onclick={(event) => void copyChainHead(event)}>Copy chain head</button>
          </div>
        {/if}
        {#if headLifecycle.copyError}<p class="err" role="alert">{headLifecycle.copyError}</p>{/if}
        {#if headLifecycle.copyNote}<p class="okay" role="status">{headLifecycle.copyNote}</p>{/if}
      </section>
      <section aria-label="backups" class="panel backup-panel">
        <div class="backup-actions">
          <div class="ttl"><h4>Backups</h4>
            <p class="muted">Sealed copies of the whole team, kept on this machine.</p></div>
          <span class="spacer"></span>
          <button class="btn ghost tiny" type="button" disabled={backupBusy}
                  onclick={() => void loadBackups()}>Refresh backups</button>
          <button class="btn pri" type="button" disabled={backupBusy}
                  onclick={() => void createBackup()}>Back up now</button>
        </div>
        {#if backupError}<p class="err" role="alert">{backupError}</p>{/if}
        {#if backupNote}<p class="okay" role="status">{backupNote}</p>{/if}
        {#if !backupHasData && (!backupLoaded || backupBusy)}
          <p class="muted" role="status">Loading backups…</p>
        {:else if !backupHasData}
          <p class="muted">Backup list unavailable.</p>
        {:else if backups.length === 0 && !backupError}
          <p class="muted">No backups yet.</p>
        {:else}
          <ul class="backups">
            {#each backups as backup}
              <li class="backup-row" data-backup-name={backup.name}>
                <strong class="backup-file">{backup.name}</strong>
                <span class="backup-size">{formatBytes(backup.size)}</span>
                <time datetime={new Date(backup.ts * 1000).toISOString()}>
                  {new Date(backup.ts * 1000).toLocaleString()}
                </time>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
      <section aria-label="webhooks" class="panel hook-panel wide">
        <div class="hook-actions">
          <div class="ttl"><h4>Webhooks</h4>
            <p class="muted">Tell another service when something happens here. Each is signed with its own secret.</p></div>
          <span class="spacer"></span>
          <button class="btn ghost tiny" type="button" disabled={hookListBusy}
                  onclick={() => void loadHooks()}>Refresh webhooks</button>
        </div>
        {#if hookListError}<p class="err" role="alert">{hookListError}</p>{/if}
        {#if !hookHasData && (!hookLoaded || hookListBusy)}
          <p class="muted" role="status">Loading webhooks…</p>
        {:else if !hookHasData}
          <p class="muted">Webhook list unavailable.</p>
        {:else}
          {#if hooks.subscriptions.length === 0}
            <p class="muted">No webhooks yet.</p>
          {:else}
            <ul class="hook-subs">
              {#each hooks.subscriptions as sub (sub.id)}
                <li class="hook-row" data-hook-id={sub.id}>
                  <code class="hook-url">{sub.url_fp}</code>
                  <span class="muted">{sub.types.length ? sub.types.join(", ")
                    : "all events"}</span>
                  <span class="muted">{hookProjectNames(sub.projects)}</span>
                  {#if sub.failures > 0}<span class="chip warn">{sub.failures} failures</span>{/if}
                  <span class="spacer"></span>
                  <button class="btn ghost tiny" disabled={hookDeleteBusy !== null}
                          onclick={() => void removeHook(sub.id)}>Remove</button>
                </li>
              {/each}
            </ul>
          {/if}
          {#if hooks.deliveries.length}
            <h5>Recent deliveries</h5>
            <ul class="hook-deliveries">
              {#each hooks.deliveries as d, i (`${d.wid}:${i}`)}
                <li class="hook-delivery">
                  <span class={d.ok ? "chip" : "chip warn"}>
                    {d.ok ? "delivered" : "failed"}</span>
                  <code>{d.url_fp}</code>
                  <span class="muted">{d.type}</span>
                  <span class="muted">{d.attempts}
                    attempt{d.attempts === 1 ? "" : "s"}</span>
                  {#if !d.ok && d.next_retry !== null}
                    <span class="muted">retry {new Date(d.next_retry * 1000).toLocaleString()}</span>
                  {/if}
                  {#if d.last_error}<span class="muted hook-err">{d.last_error}</span>{/if}
                </li>
              {/each}
            </ul>
          {/if}
        {/if}
        {#if hookRemoveError}<p class="err" role="alert">{hookRemoveError}</p>{/if}
        <form class="hook-add" onsubmit={addHook} aria-label="add a webhook">
          <h5>Add a webhook</h5>
          <div class="two">
          <label>Receiver URL
            <input name="hook_url" type="url" required autocomplete="off"
                   placeholder="https://receiver.example/hook" />
          </label>
          <label>Event types (optional, comma separated)
            <input name="hook_types" type="text" autocomplete="off"
                   placeholder="task.updated, file.changed" />
          </label>
          </div>
          <fieldset class="hook-projects">
            <legend>Only these projects</legend>
            {#each projects as p (p.id)}
              <label class="hook-project">
                <input type="checkbox" bind:group={hookProjects} value={p.id} />
                <span>{p.name}</span>
              </label>
            {/each}
          </fieldset>
          {#if hookCreateError}<p class="err" role="alert">{hookCreateError}</p>{/if}
          <div><button class="btn pri" type="submit" disabled={hookCreateBusy}>
            Add webhook</button></div>
        </form>
        {#if freshHook}
          <section class="hook-secret" aria-label="one-time webhook secret">
            <h5>One-time secret for webhook #{freshHook.id}</h5>
            <p class="muted">Shown once. Copy it to the receiver now; the hub
              keeps only the sealed value.</p>
            <input id="hook-fresh-secret" data-hook-secret
                   type={hookSecretShown ? "text" : "password"} readonly
                   autocomplete="off" spellcheck="false"
                   value={freshHook.secret}
                   aria-label="Webhook one-time secret" />
            <div class="chain-actions">
              <button class="btn tiny" type="button"
                      onclick={() => void copyHookSecret()}
                      disabled={hookSecretCopy !== null}>Copy secret</button>
              <button class="btn tiny" type="button"
                      onclick={() => hookSecretShown = !hookSecretShown}>
                {hookSecretShown ? "Hide" : "Reveal"}</button>
              <button class="btn tiny" type="button"
                      onclick={() => clearFreshHook()}>Clear</button>
            </div>
            {#if hookSecretError}<p class="err" role="alert">{hookSecretError}</p>{/if}
            {#if hookSecretNote}<p class="okay" role="status">{hookSecretNote}</p>{/if}
          </section>
        {/if}
      </section>
      <section aria-label="metrics key" class="panel hook-panel">
        <div class="hook-actions">
          <div class="ttl"><h4>Metrics</h4></div>
          <span class="spacer"></span>
          <button class="btn sec2 tiny" type="button" disabled={metricBusy}
                  onclick={() => void makeMetricsKey()}>
            Make or replace the key</button>
        </div>
        <p class="muted">The key lets an outside monitor read
          <code>/metrics</code>. Only its sha256 is stored; the key itself is
          shown once.</p>
        {#if metricError}<p class="err" role="alert">{metricError}</p>{/if}
        {#if metricNote}<p class="okay" role="status">{metricNote}</p>{/if}
        {#if metricKey}
          <section class="hook-secret" aria-label="one-time metrics key">
            <h5>Your key — shown once</h5>
            <input id="metrics-fresh-key" data-metrics-key
                   type={metricShown ? "text" : "password"} readonly
                   autocomplete="off" spellcheck="false"
                   value={metricKey.key}
                   aria-label="Metrics one-time key" />
            <div class="chain-actions">
              <button class="btn tiny" type="button"
                      onclick={() => void copyMetricsKey()}
                      disabled={metricCopy !== null}>Copy key</button>
              <button class="btn tiny" type="button"
                      onclick={() => metricShown = !metricShown}>
                {metricShown ? "Hide" : "Reveal"}</button>
              <button class="btn tiny" type="button"
                      onclick={() => clearMetricsKey()}>Clear</button>
            </div>
          </section>
        {/if}
      </section>
      <section aria-label="Vikunja import" class="panel hook-panel">
        <div class="hook-actions"><div class="ttl"><h4>Import from Vikunja</h4></div></div>
        <p class="muted">Bring a Vikunja export into this team: projects,
          tasks (done stays done, labels kept), comments and attachments.
          A dry-run summary comes first — nothing is written until you say
          go. The import runs through your own Aura on the machine that
          holds the export zip; the page cannot read files.</p>
        <ol class="hook-steps">
          <li>Put the export zip on the machine where your Aura runs.</li>
          <li>Tell your Aura the path — it answers with the dry-run counts
            (projects, tasks, comments, labels).</li>
          <li>Say go for the real import; the counts are the receipt.</li>
        </ol>
        <label class="hook-copyline">Reminder to paste to your Aura
          <input type="text" readonly value="import the Vikunja export zip at &lt;path&gt; — dry run first"
                 aria-label="Vikunja import reminder" autocomplete="off"
                 spellcheck="false" data-vikunja-reminder />
        </label>
      </section>
      <section aria-label="add-ons" class="panel hook-panel">
        <div class="hook-actions"><div class="ttl"><h4>Add-ons</h4></div></div>
        <p class="muted">Optional, never bundled. Installing is always your
          yes: your Aura downloads the add-on into the server's addons
          folder with its licence and a versions note; the views then offer
          it. Removing one returns every view to our own drawing.</p>
        <ul class="hook-subs addons">
          <li class="hook-row" data-addon="mermaid">
            <b>Mermaid diagrams</b>
            <span class="muted">richer diagram rendering beside our own SVG;
              the diagram routes always draw ours first.</span>
            <span class="chip">not bundled</span>
          </li>
          <li class="hook-row" data-addon="gantt">
            <b>Frappe Gantt</b>
            <span class="muted">richer Gantt charts beside our own bars;
              Flow keeps working without it.</span>
            <span class="chip">not bundled</span>
          </li>
          <li class="hook-row" data-addon="vikunja">
            <b>Vikunja</b>
            <span class="muted">the outside hub this team can migrate FROM
              (see the import card) or wire as a read-only mirror.</span>
            <span class="chip">not bundled</span>
          </li>
        </ul>
      </section>
      {#if isFixedOwner($me.value)}
      <section aria-label="move the hub" class="panel hook-panel">
        <div class="hook-actions">
          <div class="ttl"><h4>Move the hub</h4></div>
          <span class="spacer"></span>
          <button class="btn tiny" type="button" onclick={() => void toggleMove()}>
            {moveOpen ? "Close" : "Prepare a move"}</button>
        </div>
        <p class="muted">Owner only. The sealed bundle carries the whole team
          for the chosen device's key; the page cannot move the hub — run the
          take-over on the new machine.</p>
        {#if moveOpen}
          {#if moveDevicesError}<p class="err" role="alert">{moveDevicesError}</p>{/if}
          {#if moveDevicesBusy}
            <p class="muted" role="status">Loading devices…</p>
          {:else if moveDevices.length === 0}
            <p class="muted">No open devices to seal for.</p>
          {:else}
            <ul class="hook-subs">
              {#each moveDevices as d (d.id)}
                <li class="hook-row" data-move-device={d.id}>
                  <b>{d.name}</b>
                  <span class="muted">{d.person_name}</span>
                  <span class="spacer"></span>
                  <button class="btn sec2 tiny" disabled={moveBusyId !== null}
                          onclick={() => void prepareMoveBundle(d)}>
                    Seal bundle</button>
                </li>
              {/each}
            </ul>
          {/if}
        {/if}
        {#if moveError}<p class="err" role="alert">{moveError}</p>{/if}
        {#if moveNote}<p class="okay" role="status">{moveNote}</p>{/if}
      </section>
      {/if}
    </section>
  {/if}
</div>

<style>
  .invite { display: flex; flex-direction: column; gap: 8px; padding: 12px; margin-bottom: 12px;
            border-radius: var(--r-ctl); background: color-mix(in oklch, var(--acc) 10%, transparent); }
  .invite-h { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .invite-h .spacer { flex: 1; }
  .invite textarea { width: 100%; box-sizing: border-box; resize: vertical; font: inherit; font-size: 12.5px;
                     line-height: 1.5; padding: 8px 10px; border-radius: var(--r-ctl); border: 1px solid var(--line);
                     background: var(--raised); color: var(--text); }
  /* ---- the admin page in the app's own look ---- */
  .wrap { padding: 0 0 24px; display: flex; flex-direction: column; gap: 14px; }
  .wrap > :global(*:not(.ph)) { margin-left: 20px; margin-right: 20px; }
  .ph { flex-wrap: wrap; height: auto; min-height: 52px; padding-top: 8px; padding-bottom: 8px; }
  .panel { background: var(--raised); border-radius: var(--r-panel); padding: 18px 20px;
           box-shadow: var(--shadow), 0 0 0 1px var(--line);
           display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .panel-h { display: flex; align-items: flex-start; gap: 12px; }
  .panel-h h3 { margin: 0 0 2px; font-size: 15px; font-weight: 600; }
  .panel-h p { margin: 0; font-size: 12.5px; }
  .muted { color: var(--muted); }
  /* the shared form controls: the admin page and its Roles / Projects parts */
  .wrap :global(input:not([type="checkbox"]):not([type="radio"]):not([type="file"])),
  .wrap :global(textarea), .wrap :global(select) {
    min-height: 32px; padding: 0 10px; border-radius: var(--r-ctl); border: 0; outline: none;
    background: var(--raised-2); color: var(--text); font: inherit; font-size: 13px;
    box-shadow: 0 0 0 1px var(--line); transition: box-shadow var(--t-fast); max-width: 100%; }
  .wrap :global(textarea) { padding: 8px 10px; line-height: 1.5; }
  .wrap :global(input:focus), .wrap :global(textarea:focus), .wrap :global(select:focus) {
    box-shadow: 0 0 0 2px color-mix(in oklch, var(--acc) 60%, transparent); }
  .wrap :global(input:disabled), .wrap :global(select:disabled) { opacity: .6; }
  .wrap :global(input[type="checkbox"]), .wrap :global(input[type="radio"]) {
    accent-color: var(--acc); width: 15px; height: 15px; }
  .wrap :global(label) { font-size: 12.5px; color: var(--text-2); }
  .wrap :global(.sel) { position: relative; display: inline-flex; align-items: center; min-width: 0; }
  .wrap :global(.sel select) { appearance: none; padding-right: 28px; width: 100%; cursor: pointer; }
  .wrap :global(.sel svg) { position: absolute; right: 8px; pointer-events: none; color: var(--muted); }
  .wrap :global(h3) { font-size: 15px; font-weight: 600; }
  .wrap :global(h4) { font-size: 13.5px; font-weight: 600; }
  .okay { color: var(--ok-text); font-size: 13px; margin: 0; }
  .err { color: var(--err); font-size: 13px; margin: 0; }

  /* users */
  .addrow { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .addrow input { width: 220px; }
  .ptable { display: flex; flex-direction: column; min-width: 0; }
  .phead, .person { display: grid; grid-template-columns: minmax(0, 1fr) 130px 210px 128px;
                    gap: 6px 12px; align-items: center; }
  .phead { padding: 0 4px 6px; font-size: 11.5px; color: var(--muted); font-weight: 500;
           border-bottom: 1px solid var(--line); }
  .people { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; }
  .person { padding: 10px 4px; border-bottom: 1px solid var(--line); }
  .person:last-child { border-bottom: 0; }
  .person .sel { width: 100%; }
  .who { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .who-t { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .who-n { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 13.5px; }
  .who-n .muted { font-size: 12.5px; }
  .acts { display: flex; align-items: center; justify-content: flex-end; gap: 4px; }
  .row { display: flex; gap: var(--sp-8); align-items: baseline; }
  .member-projects { display: flex; flex-wrap: wrap; gap: 2px 10px;
                     list-style: none; margin: 0; padding: 0; font-size: 12px; }
  .member-projects li { min-width: 0; overflow-wrap: anywhere; }
  .user-sessions { grid-column: 1 / -1; background: var(--raised-2); padding: 12px 14px;
                   border-radius: var(--r-ctl); }
  .user-sessions h4 { margin: 0 0 var(--sp-8); }
  .session-row { display: flex; flex-wrap: wrap; align-items: center;
                 gap: var(--sp-8); padding-bottom: var(--sp-8); }
  .session-row > div { flex: 1; min-width: 18ch; }
  .session-row code { display: block; font-size: 11px; }
  .spacer { flex: 1; }
  .chip { font-size: 11px; height: 20px; padding: 0 7px; }
  .chip.owner { color: var(--acc); }
  .chip.warn { color: var(--text); background: color-mix(in oklch, var(--warn) 22%, transparent); }
  .muted.late { color: var(--text-2); font-size: 12px; }
  .btn.tiny { padding: 0 var(--sp-8); height: 24px; font-size: 12px; }

  /* hub */
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .hubgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; align-items: stretch; }
  .hook-secret p { margin: 0; font-size: 12.5px; color: var(--text-2); }
  .hook-secret .btn { background: var(--raised); }
  .hubgrid > .wide { grid-column: 1 / -1; }
  .hubgrid .panel { gap: 12px; }
  .hubgrid h4 { margin: 0; font-size: 15px; font-weight: 600; }
  .hubgrid h5 { margin: 0; font-size: 12px; font-weight: 600; color: var(--text-2); }
  .ttl { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .ttl p, .hubgrid .panel-h p { margin: 0; font-size: 12.5px; }
  .hubgrid .panel > p.muted { margin: 0; font-size: 13px; line-height: 1.5; }
  .kv { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 8px 12px; margin: 0; font-size: 13px; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
  .kv dd.mono { font-family: var(--mono, monospace); font-size: 12px; }
  .chain-panel, .backup-panel, .hook-panel { min-width: 0; }
  .chain-actions, .chain-heading, .backup-actions, .hook-actions {
    display: flex; flex-wrap: wrap; align-items: flex-start; gap: 8px; }
  .chain-heading { align-items: center; font-size: 12px; }
  .chain-hash-label { font-size: 12px; color: var(--muted); }
  .wrap .chain-hash { box-sizing: border-box; display: block; width: 100%; min-width: 0;
                max-width: 100%; resize: vertical; white-space: pre-wrap;
                overflow-wrap: anywhere; word-break: break-all;
                font-family: var(--mono, monospace); font-size: 12px; line-height: 1.5; }
  .backups { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; min-width: 0; }
  .backup-row { display: grid; grid-template-columns: minmax(0, 1fr) auto;
                gap: 2px 8px; min-width: 0; padding: 8px 0; border-top: 1px solid var(--line); }
  .backup-row:first-child { border-top: 0; }
  .backup-file { min-width: 0; overflow-wrap: anywhere; font-size: 13px; font-weight: 500; }
  .backup-size, .backup-row time { color: var(--muted); font-size: 12px; }
  .backup-row time { grid-column: 1 / -1; min-width: 0; }
  .hook-subs, .hook-deliveries { list-style: none; margin: 0; padding: 0;
                                 display: flex; flex-direction: column; min-width: 0; }
  .hook-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px;
              padding: 8px 0; border-top: 1px solid var(--line); font-size: 13px; }
  .hook-row:first-child { border-top: 0; }
  .addons .hook-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; }
  .addons .hook-row .muted { grid-column: 1; grid-row: 2; font-size: 12.5px; }
  .addons .hook-row .chip { grid-column: 2; grid-row: 1; }
  .hook-url { min-width: 0; overflow-wrap: anywhere; }
  .hook-deliveries { gap: 6px; }
  .hook-delivery { display: flex; flex-wrap: wrap; align-items: center;
                   gap: 4px 8px; font-size: 12px; min-width: 0; }
  .hook-delivery code { min-width: 0; overflow-wrap: anywhere; }
  .hook-delivery .hook-err { flex: 1 0 100%; overflow-wrap: anywhere; }
  .hook-add { display: flex; flex-direction: column; gap: 10px; padding-top: 12px; border-top: 1px solid var(--line); }
  .two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .hook-add label:not(.hook-project) { display: flex; flex-direction: column; gap: 6px; }
  .hook-projects { border: 0; border-radius: var(--r-ctl); background: var(--raised-2);
                   padding: 10px 12px; display: flex; flex-wrap: wrap;
                   gap: 6px 14px; margin: 0; min-width: 0; }
  .hook-projects legend { float: left; width: 100%; font-size: 12px; font-weight: 600;
                          color: var(--text); padding: 0 0 4px; }
  .hook-project { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
  .hook-secret { background: var(--raised-2); box-shadow: inset 3px 0 0 var(--warn); border-radius: var(--r-ctl);
                 padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
  .wrap .hook-secret input { width: 100%; box-sizing: border-box;
                       font-family: var(--mono, monospace); font-size: 12px; }
  .hook-steps { margin: 0; padding-left: 18px; display: flex;
                flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-2); }
  .hook-copyline { display: flex; flex-direction: column; gap: 6px; }
  .wrap .hook-copyline input { width: 100%; box-sizing: border-box;
                         font-family: var(--mono, monospace); font-size: 12px;
                         color: var(--text-2); }
  .rowmenu { position: relative; }
  .rowmenu summary { list-style: none; cursor: pointer; width: 28px; height: 28px; border-radius: 7px;
                     display: grid; place-items: center; color: var(--muted); }
  .rowmenu summary:hover { background: var(--hover); color: var(--text); }
  .rowmenu summary::-webkit-details-marker { display: none; }
  .rowmenu-card { right: 0; top: calc(100% + 4px); min-width: 240px; }
  .rowmenu-card .mi { width: 100%; text-align: left; }
  .rowmenu-card .mi.danger { color: var(--err); }

  /* narrow screens: room for the ☰ button at the top left */
  /* (the menu button has its own strip above every page now) */
  @media (max-width: 1100px) { .hubgrid { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 860px) {
    .two { grid-template-columns: minmax(0, 1fr); }
    .phead { display: none; }
    .person { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
    .who { grid-column: 1 / -1; }
    .acts { grid-column: 1 / -1; justify-content: flex-start; }
    .addrow input { width: 100%; }
  }
</style>
