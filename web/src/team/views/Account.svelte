<script lang="ts">
import { onDestroy, onMount } from "svelte";
import { call } from "../../lib/api/client";
import { me, meStore, completeSignOut } from "../frame/shell_state";
import { profilePatch, sessionIsActive } from "../../lib/account_profile";

type PersonSession = {
  token: string; person: number; created: number; last_used: number | null;
  expires: number; ended: number | null; agent: string | null;
  approved_by: string; revoked_at: number | null; current: boolean;
};
type Device = {
  id: string; person: number; name: string; created: number;
  last_seen: number | null; revoked_at: number | null;
};
type ResourceRead = {
  sessions: PersonSession[]; devices: Device[]; error: string;
};
type MeResponse = { person: { id: number; name: string; display: string } };

let life = 0;
let accountId: number | undefined;
let readEpoch = 0;
let profileEpoch = 0;
let sessionEpoch = 0;
let deviceEpoch = 0;
let loading = true;
let readError = "";
let readNotice = "";
let sessions: PersonSession[] = [];
let devices: Device[] = [];
let nameDraft = $me.value?.name ?? "";
let displayDraft = $me.value?.display ?? "";
let originalName = nameDraft;
let originalDisplay = displayDraft;
let profileBusy = false;
let profileError = "";
let profileNotice = "";
let draftEpoch = 0;
let sessionBusy = "";
let sessionError = "";
let deviceBusy = "";
let deviceError = "";

function owns(person: number | undefined, epoch: number): boolean {
  return epoch === life && person !== undefined && $me.value?.id === person;
}
function message(error: unknown): string {
  return error instanceof Error ? error.message : "The hub could not complete the request.";
}
function when(seconds: number | null | undefined): string {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "Not recorded";
}
async function readResources(): Promise<ResourceRead | null> {
  const person = accountId, epoch = life, seq = ++readEpoch;
  loading = true; readError = ""; readNotice = "";
  try {
    const [profile, listed, linked] = await Promise.all([
      call<MeResponse>("/me"),
      call<{ sessions: PersonSession[] }>("/sessions"),
      call<{ devices: Device[] }>("/devices"),
    ]);
    if (!owns(person, epoch) || seq !== readEpoch || profile.person.id !== person)
      return null;
    if (draftEpoch === 0) {              // idle reload; never erase a pending draft
      nameDraft = profile.person.name;
      displayDraft = profile.person.display ?? "";
      originalName = nameDraft;
      originalDisplay = displayDraft;
    }
    const fresh: ResourceRead = {
      sessions: listed.sessions.filter(s => s.person === person && sessionIsActive(s)),
      devices: linked.devices.filter(d => d.person === person && d.revoked_at === null),
      error: "",
    };
    sessions = fresh.sessions;
    devices = fresh.devices;
    return fresh;
  } catch (error) {
    const failure = message(error);
    if (owns(person, epoch) && seq === readEpoch) {
      readError = failure;
      return { sessions: [], devices: [], error: failure };
    }
    return null;
  } finally {
    if (owns(person, epoch) && seq === readEpoch) loading = false;
  }
}
onMount(() => {
  accountId = $me.value?.id;
  ++life;
  void readResources();
});
onDestroy(() => {
  ++life; ++readEpoch; ++profileEpoch; ++sessionEpoch; ++deviceEpoch;
});

async function saveProfile() {
  if (profileBusy) return;
  const person = accountId, epoch = life, seq = ++profileEpoch;
  const patch = profilePatch(
    {name: originalName, display: originalDisplay},
    {name: nameDraft, display: displayDraft},
  );
  const draft = draftEpoch;
  if (!Object.keys(patch).length) {
    profileError = ""; profileNotice = "No changes to save.";
    return;
  }
  profileBusy = true; profileError = ""; profileNotice = ""; readNotice = "";
  try {
    await call("/people/me", patch, "PATCH");
    await meStore.load();                     // actual persisted frame identity
    if (!owns(person, epoch) || seq !== profileEpoch) return;
    originalName = $me.value?.name ?? originalName;
    originalDisplay = $me.value?.display ?? originalDisplay;
    if (draftEpoch === draft) {
      nameDraft = originalName;
      displayDraft = originalDisplay;
      profileNotice = "Profile saved.";
    } else {
      profileNotice = "Profile saved. Your newer edits have not been saved.";
    }
  } catch (error) {
    if (owns(person, epoch) && seq === profileEpoch) profileError = message(error);
  } finally {
    if (owns(person, epoch) && seq === profileEpoch) profileBusy = false;
  }
}

async function revokeSession(token: string) {
  if (sessionBusy) return;
  const current = sessions.some(s => s.token === token && s.current);
  if (!confirm(current ? "Sign out this browser?" :
                 "Sign out this session? Your current session will stay signed in.")) return;
  const person = accountId, epoch = life, seq = ++sessionEpoch;
  sessionBusy = token; sessionError = ""; readNotice = ""; profileNotice = "";
  try {
    await call(`/sessions/${encodeURIComponent(token)}`, {}, "DELETE");
    if (!owns(person, epoch) || seq !== sessionEpoch) return;
    if (current) {
      completeSignOut();                    // DELETE already revoked this cookie
      return;
    }
    await readResources();
    if (owns(person, epoch) && seq === sessionEpoch) readNotice = "Signed out session.";
  } catch (error) {
    if (owns(person, epoch) && seq === sessionEpoch) sessionError = message(error);
  } finally {
    if (owns(person, epoch) && seq === sessionEpoch) sessionBusy = "";
  }
}
async function revokeOtherSessions() {
  if (sessionBusy) return;
  if (!confirm("Sign out all other sessions? This browser will stay signed in.")) return;
  const person = accountId, epoch = life, seq = ++sessionEpoch;
  sessionBusy = "others"; sessionError = ""; readNotice = ""; profileNotice = "";
  let completed = 0, failed = "";
  try {
    const fresh = await readResources();
    if (!owns(person, epoch) || seq !== sessionEpoch) return;
    if (!fresh) {
      sessionError = "A newer refresh completed. No other sessions were revoked. Try again.";
      return;
    }
    if (fresh.error) {
      sessionError = `Sessions could not be refreshed; no others were revoked. ${fresh.error}`;
      return;
    }
    const targets = fresh.sessions.filter(s => !s.current && sessionIsActive(s));
    if (!targets.length) {
      readNotice = "No other sessions remain to sign out.";
      return;
    }
    for (const target of targets) {
      if (!owns(person, epoch) || seq !== sessionEpoch) return;
      try {
        await call(`/sessions/${encodeURIComponent(target.token)}`, {}, "DELETE");
        ++completed;
      } catch (error) {
        failed = `Signed out ${completed}. ${message(error)}`;
        break;
      }
    }
    if (!owns(person, epoch) || seq !== sessionEpoch) return;
    await readResources();
    if (owns(person, epoch) && seq === sessionEpoch) {
      readNotice = `Signed out ${completed} other session${completed === 1 ? "" : "s"}.`;
      sessionError = failed;
    }
  } finally {
    if (owns(person, epoch) && seq === sessionEpoch) sessionBusy = "";
  }
}
async function revokeDevice(device: Device) {
  if (deviceBusy) return;
  if (!confirm(`Revoke ${device.name}? This device will no longer be able to sign in.`)) return;
  const person = accountId, epoch = life, seq = ++deviceEpoch;
  deviceBusy = device.id; deviceError = ""; readNotice = ""; profileNotice = "";
  try {
    await call(`/devices/${encodeURIComponent(device.id)}`, {}, "DELETE");
    if (!owns(person, epoch) || seq !== deviceEpoch) return;
    await readResources();
    if (owns(person, epoch) && seq === deviceEpoch) readNotice = "Device revoked.";
  } catch (error) {
    if (owns(person, epoch) && seq === deviceEpoch) deviceError = message(error);
  } finally {
    if (owns(person, epoch) && seq === deviceEpoch) deviceBusy = "";
  }
}

  // ---- Account › Appearance (bound to lib/prefs stores) ----
  import { look, accent, density, contrast, motion, enterKey } from "../../lib/prefs";
  import { CHOICES, LABELS } from "../../lib/prefs_logic";
  import Segmented from "../../lib/parts/Segmented.svelte";

  // ---- Your avatar: pick any drawing, see more, or use a photo ---------
  import { avatars, avatarSrc, avatarChoices, drawAvatar, drawReady, loadAvatars, type AvatarChoice } from "../../lib/avatars";
  $: myName = $me.value?.name ?? "";
  $: myAvatar = $avatars.get(myName);
  let avBusy = false;
  let avError = "";
  let avNote = "";
  async function avatarDo(work: () => Promise<unknown>, note: string) {
    avBusy = true; avError = ""; avNote = "";
    try { await work(); await loadAvatars(true); avNote = note; }
    catch (e) { avError = e instanceof Error ? e.message : "The avatar was not changed"; }
    finally { avBusy = false; }
  }
  let choicePage = 0;
  $: choices = avatarChoices(myName, choicePage);
  $: myStyle = myAvatar?.style ?? "lorelei";
  $: mySeed = myAvatar?.seed ?? "";
  const isMine = (c: AvatarChoice, style: string, seed: string) => c.style === style && c.seed === seed;
  // ready: pass $drawReady so the grid re-renders once drawings can load
  const drawChoice = (c: AvatarChoice, name: string, ready: boolean) => { void ready; return drawAvatar(c.style, c.seed || name); };
  const choose = (c: AvatarChoice) =>
    avatarDo(() => call("/people/me/avatar", { style: c.style, seed: c.seed }, "PATCH"), "Avatar chosen");
  async function uploadPhoto(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { avError = "The picture is over 2 MB."; return; }
    await avatarDo(async () => {
      const r = await fetch("/api/v1/people/me/avatar/photo", {
        method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d?.error?.message ?? "The photo was not saved");
      }
    }, "Your photo is now your avatar");
  }
  const removePhoto = () => avatarDo(() => call("/people/me/avatar/photo", undefined, "DELETE"), "Back to your drawing");
  const APPEARANCE: { store: any; key: keyof typeof CHOICES; title: string }[] = [
    { store: look,     key: "look",     title: "Look" },
    { store: accent,   key: "accent",   title: "Accent" },
    { store: density,  key: "density",  title: "Density" },
    { store: contrast, key: "contrast", title: "Contrast" },
    { store: motion,   key: "motion",   title: "Motion" },
    { store: enterKey, key: "enter",    title: "Enter key" },
  ];
  // each row's CURRENT value — the store itself never matched an option, so
  // no choice ever showed as picked
  $: picked = { look: $look, accent: $accent, density: $density, contrast: $contrast,
                motion: $motion, enter: $enterKey } as Record<string, string>;
</script>

<svelte:head><title>My account · Aura Teams</title></svelte:head>
<section class="wrap fade-in" aria-label="my account">
  <header class="head">
    <div><h2>My account</h2><p class="muted">Your profile, signed-in sessions and linked devices.</p></div>
    <button class="btn btn-quiet" onclick={() => void readResources()} disabled={loading}>Refresh</button>
  </header>

  {#if readError}<p class="error" role="alert">{readError}</p>{/if}
  {#if readNotice}<p class="notice" role="status">{readNotice}</p>{/if}
  {#if profileNotice}<p class="notice" role="status">{profileNotice}</p>{/if}

  <section class="card" aria-labelledby="profile-heading">
    <h3 id="profile-heading">Profile</h3>
    <form onsubmit={(event) => { event.preventDefault(); void saveProfile(); }}>
      <label for="account-name">Account name</label>
      <input id="account-name" bind:value={nameDraft} oninput={() => ++draftEpoch}
             autocomplete="username" required aria-describedby="account-name-help" disabled={profileBusy}>
      <p class="muted" id="account-name-help">Used to sign in. The hub validates names and duplicate names.</p>
      <label for="display-name">Display name (optional)</label>
      <input id="display-name" bind:value={displayDraft} oninput={() => ++draftEpoch}
             maxlength="80" autocomplete="nickname" disabled={profileBusy}>
      {#if profileError}<p class="error" role="alert">{profileError}</p>{/if}
      <button class="btn btn-primary" type="submit" disabled={profileBusy || loading}>
        {profileBusy ? "Saving profile…" : "Save profile"}
      </button>
    </form>
  </section>

    <section class="card" aria-labelledby="avatar-heading">
    <h3 id="avatar-heading">Your avatar</h3>
    <div class="avrow">
      <img class="avbig" src={avatarSrc(myName, $avatars, $drawReady) || undefined} alt="Your avatar" />
      <div class="avctl">
        <div class="avgrid" role="group" aria-label="Avatar choices">
          {#each choices as c, i (`${c.style}|${c.seed}`)}
            <button type="button" class="avpick" class:on={!myAvatar?.photo && isMine(c, myStyle, mySeed)}
                    aria-label={`Avatar choice ${i + 1}`}
                    aria-pressed={!myAvatar?.photo && isMine(c, myStyle, mySeed)}
                    disabled={avBusy || !!myAvatar?.photo}
                    onclick={() => void choose(c)}>
              <img src={drawChoice(c, myName, $drawReady) || undefined} alt="" />
            </button>
          {/each}
        </div>
        <div class="avbtns">
          <button class="btn sec2" type="button" disabled={avBusy || !!myAvatar?.photo}
                  onclick={() => choicePage += 1}>More choices</button>
          {#if choicePage > 0}
            <button class="btn ghost" type="button" disabled={avBusy || !!myAvatar?.photo}
                    onclick={() => choicePage -= 1}>Previous</button>
          {/if}
          <label class="btn sec2 upload" class:busy={avBusy}>
            {myAvatar?.photo ? "Change photo" : "Upload a photo"}
            <input type="file" accept="image/png,image/jpeg,image/webp" class="visually-hidden"
                   disabled={avBusy} onchange={(e) => void uploadPhoto(e)} />
          </label>
          {#if myAvatar?.photo}
            <button class="btn ghost" type="button" disabled={avBusy} onclick={() => void removePhoto()}>Remove photo</button>
          {/if}
        </div>
        <p class="muted">
          {#if myAvatar?.photo}Your photo shows everywhere. Remove it to use a drawing again.
          {:else}Pick any drawing, see more choices, or upload a photo (PNG, JPEG or WebP, up to 2 MB).{/if}
        </p>
        {#if avError}<p class="error" role="alert">{avError}</p>{/if}
        {#if avNote}<p class="notice" role="status">{avNote}</p>{/if}
      </div>
    </div>
  </section>

  <section class="card" aria-labelledby="appearance-heading">
    <h3 id="appearance-heading">Appearance</h3>
    {#each APPEARANCE as row (row.key)}
      <div class="aprop">
        <span class="ak">{row.title}</span>
        <Segmented options={(CHOICES[row.key] as readonly string[]).map((v) => ({ v, label: (LABELS[row.key] as Record<string, string>)[v] }))}
                   value={picked[row.key]}
                   onchange={(v: string) => row.store.set(v as never)} />
      </div>
    {/each}
    <p class="muted">These settings are kept in this browser.</p>
  </section>
<section class="card" aria-labelledby="sessions-heading">
    <div class="head"><div><h3 id="sessions-heading">Sessions</h3>
      <p class="muted">An active session signs in without a password.</p></div>
      <button class="btn btn-quiet" disabled={loading || !!sessionBusy || sessions.filter(s => !s.current).length === 0}
              onclick={() => void revokeOtherSessions()}>Sign out all others</button>
    </div>
    {#if sessionError}<p class="error" role="alert">{sessionError}</p>{/if}
    {#if loading}<p class="muted">Loading your sessions…</p>
    {:else if !sessions.length}<p class="muted">No active sessions.</p>
    {:else}<div class="list">
      {#each sessions as session (session.token)}
        <article class="entry" data-session-token={session.token} data-current={session.current}>
          <div><div class="title"><strong>{session.current ? "This browser" : "Browser session"}</strong>
            {#if session.current}<span class="badge">Current session</span>{/if}</div>
            <div class="muted">Last used {when(session.last_used)} · Expires {when(session.expires)}</div>
            <div class="muted">{session.agent || ({ signin: "Password sign-in", "password-change": "After a password change",
                                 invite: "Invite link", device: "Linked device" } as Record<string, string>)[session.approved_by]
                               || session.approved_by}</div>
            <code>{session.token}</code>
          </div>
          <button class="btn btn-quiet" disabled={!!sessionBusy}
                  onclick={() => void revokeSession(session.token)}>Sign out session</button>
        </article>
      {/each}
    </div>{/if}
  </section>

  <section class="card" aria-labelledby="devices-heading">
    <h3 id="devices-heading">Linked devices</h3>
    <p class="muted">Revoking a private device ends future signed device access. It does not revoke this browser's cookie.</p>
    {#if deviceError}<p class="error" role="alert">{deviceError}</p>{/if}
    {#if loading}<p class="muted">Loading your devices…</p>
    {:else if !devices.length}<p class="muted">No linked devices.</p>
    {:else}<div class="list">
      {#each devices as device (device.id)}
        <article class="entry" data-device-id={device.id}>
          <div><strong>{device.name || "Unnamed device"}</strong>
            <div class="muted">Last seen {when(device.last_seen)} · Linked {when(device.created)}</div>
            <code>{device.id}</code>
          </div>
          <button class="btn btn-quiet" disabled={!!deviceBusy}
                  onclick={() => void revokeDevice(device)}>Revoke device</button>
        </article>
      {/each}
    </div>{/if}
  </section>
</section>

<style>
  /* the account page in the app's own look (same panels as Admin) */
  .wrap { padding: 20px 20px 28px; display: flex; flex-direction: column; gap: 14px;
          max-width: 980px; overflow-x: clip; }
  .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  h2, h3, p { margin-top: 0; }
  h2 { font-size: 20px; font-weight: 600; margin-bottom: 2px; }
  h3 { font-size: 15px; font-weight: 600; margin-bottom: 0; }
  .muted { color: var(--muted); font-size: 12.5px; margin-bottom: 0; }
  .card { background: var(--raised); border: 0; border-radius: var(--r-panel); padding: 18px 20px;
          box-shadow: var(--shadow), 0 0 0 1px var(--line); display: grid; gap: 12px; min-width: 0; }
  form { display: grid; gap: 8px; max-width: 36rem; }
  form label { font-size: 12.5px; color: var(--text-2); margin-top: 4px; }
  input { width: 100%; min-height: 32px; padding: 0 10px; background: var(--raised-2); color: var(--text);
          border: 0; border-radius: var(--r-ctl); box-shadow: 0 0 0 1px var(--line); outline: none;
          font: inherit; font-size: 13px; box-sizing: border-box; transition: box-shadow var(--t-fast); }
  input:focus { box-shadow: 0 0 0 2px color-mix(in oklch, var(--acc) 60%, transparent); }
  .notice { color: var(--ok-text); font-size: 13px; margin-bottom: 0; }
  .error { color: var(--err); font-size: 13px; margin-bottom: 0; }
  .list { display: grid; }
  .entry { border-top: 1px solid var(--line); padding: 12px 0; display: flex;
           align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; }
  .entry:first-child { border-top: 0; padding-top: 4px; }
  .entry code { display: block; color: var(--muted); font-size: 11.5px; margin-top: 4px; overflow-wrap: anywhere; }
  .title { display: flex; align-items: center; gap: 8px; }
  .aprop { display: grid; grid-template-columns: 110px minmax(0,1fr); gap: 8px 10px; align-items: center; }
  .ak { color: var(--muted); font-size: 12.5px; }
  .badge { border-radius: 999px; padding: 2px 8px; font-size: 11px; white-space: nowrap;
           color: var(--ok-text); background: color-mix(in oklch, var(--ok) 12%, transparent); }
  button { max-width: fit-content; }
  .avrow { display: flex; gap: 20px; align-items: flex-start; }
  .avbig { width: 88px; height: 88px; border-radius: 50%; object-fit: cover; flex: none;
           background: var(--raised-2); box-shadow: 0 0 0 1px var(--line); }
  .avctl { display: grid; gap: 12px; min-width: 0; flex: 1; }
  .avgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(52px, 1fr)); gap: 8px; max-width: 560px; }
  .avpick { max-width: none; aspect-ratio: 1; padding: 0; border: 0; border-radius: 50%; cursor: pointer;
            background: var(--raised-2); box-shadow: 0 0 0 1px var(--line); overflow: hidden;
            transition: box-shadow var(--t-fast), transform var(--t-press); }
  .avpick:hover:not(:disabled) { transform: scale(1.06); }
  .avpick.on { box-shadow: 0 0 0 2px var(--acc); }
  .avpick:disabled { opacity: .45; cursor: default; }
  .avpick img { width: 100%; height: 100%; display: block; }
  .avbtns { display: flex; flex-wrap: wrap; gap: 8px; }
  .upload { cursor: pointer; position: relative; }
  .upload.busy { opacity: .6; pointer-events: none; }
  .upload:focus-within { outline: 2px solid var(--acc); outline-offset: 2px; }
  .visually-hidden { position: absolute; width: 1px; height: 1px; clip-path: inset(50%); overflow: hidden; white-space: nowrap; }
  @media (max-width: 40rem) {
    .head, .entry, .avrow { align-items: flex-start; flex-direction: column; }
    .wrap { padding: 16px 12px 24px; }
    /* the avatar choices and the hint use the card's width, never more */
    .avrow { align-items: stretch; }
    .avctl { width: auto; max-width: 100%; min-width: 0; }
    .avgrid { max-width: 100%; grid-template-columns: repeat(auto-fill, minmax(44px, 1fr)); }
    /* each setting's name above its choices; the choices wrap, never squeeze */
    .aprop { grid-template-columns: minmax(0, 1fr); gap: 6px; margin-bottom: 6px; }
    .aprop :global(.seg) { flex-wrap: wrap; height: auto; }
    .aprop :global(.seg button) { white-space: nowrap; }
  }
</style>
