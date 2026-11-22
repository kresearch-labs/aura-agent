<!-- SignIn.svelte — lane #1. Words per: verbs first, sentence case, no "please", no
     "successfully".
-->
<script lang="ts">
  import { signIn } from "../lib/api/client";
  import { bootHome, meStore, route } from "./frame/shell_state";
  import { onMount } from "svelte";
  import { Eye, EyeOff, LayoutGrid, MessagesSquare, ShieldCheck } from "@lucide/svelte";

  let name = $state("");
  let password = $state("");
  let busy = $state(false);
  let error = $state("");
  let showPw = $state(false);
  let caps = $state(false);
  // "where am I signing in?": the team's name (when the hub has one) and the
  // address in the bar, so a person can tell the real hub from a look-alike
  let team = $state("");
  const host = typeof location !== "undefined" ? location.host : "";
  onMount(() => {
    fetch("/api/v1/info").then((r) => (r.ok ? r.json() : null))
      .then((i) => { team = (i && i.team) || ""; })
      .catch(() => { /* the address alone still says where */ });
  });
  function capsFrom(e: KeyboardEvent) { caps = e.getModifierState?.("CapsLock") ?? false; }

  async function go(e: Event) {
    e.preventDefault();
    // the DOM's own states carry it (never bind:lives outside Svelte —
    // its answer on this element's read is the truth, kept through the
    // button's re-renders)
    const form = e.target as HTMLFormElement;
    const fName = (form.querySelector("#name") as HTMLInputElement).value;
    const fPw = (form.querySelector("#pass") as HTMLInputElement).value;
    if (!fName || !fPw) { error = "need both the name and the password"; return; }
    busy = true; error = "";
    try {
      await signIn(fName.trim(), fPw);
      password = "";                    // never kept anywhere 
      await bootHome();                 // projects + perms fill the shell
      if (!meStore.value?.must_change) route.go("now");
    } catch (err) {
      error = err instanceof Error ? err.message : "the sign-in failed";
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Sign in · Aura Teams</title></svelte:head>

<main class="wrap fade-in" aria-label="sign in">
  <section class="intro" aria-label="about Aura Teams">
    <div class="glow" aria-hidden="true"></div>
    <img class="logo" src="./aura-logo-128.png" alt="" aria-hidden="true" />
    <h1>Aura Teams</h1>
    <p class="lead">Your team's work, chat and Auras — on one page, on your own network.</p>
    <ul class="points">
      <li><LayoutGrid size={16} strokeWidth={1.75} /><span><b>Board and tasks</b> — who does what, and when</span></li>
      <li><MessagesSquare size={16} strokeWidth={1.75} /><span><b>Team chat</b> — rooms, pictures, files, #task cards</span></li>
      <li><img class="pt-logo" src="./aura-logo-64.png" alt="" /><span><b>Every Aura, visible</b> — what each person's Aura is working on</span></li>
    </ul>
  </section>

  <div class="panel">
    <div class="head">
      <h2>Sign in</h2>
      <p class="where" title="The team hub you are signing in to">
        <ShieldCheck size={14} strokeWidth={1.75} />
        {#if team}<b>{team}</b> · {/if}<span class="host">{host}</span>
      </p>
    </div>

    <form class="form" method="dialog" onsubmit={go}>
      <div class="field">
        <label for="name">Your name</label>
        <input id="name" bind:value={name} autocomplete="username" required />
      </div>
      <div class="field">
        <label for="pass">Your password</label>
        <div class="pw">
          <input id="pass" type={showPw ? "text" : "password"} bind:value={password}
                 autocomplete="current-password" required
                 onkeydown={capsFrom} onkeyup={capsFrom} onblur={() => (caps = false)} />
          <button type="button" class="eye" aria-label={showPw ? "Hide password" : "Show password"}
                  aria-pressed={showPw} onclick={() => (showPw = !showPw)}>
            {#if showPw}<EyeOff size={16} strokeWidth={1.75} />{:else}<Eye size={16} strokeWidth={1.75} />{/if}
          </button>
        </div>
        {#if caps}<p class="caps" role="status">Caps Lock is on</p>{/if}
      </div>
      {#if error}<div class="msg" role="alert">{error}</div>{/if}
      <button class="btn pri go" type="submit" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  </div>
</main>

<style>
  .wrap { min-height: 100dvh; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 400px);
          align-items: center; gap: 48px; padding: 40px max(24px, 6vw); background: var(--bg);
          position: relative; overflow: hidden; }
  /* the page's depth: two soft glows that echo the crystal */
  .wrap::before { content: ""; position: absolute; inset: -20%; pointer-events: none;
    background:
      radial-gradient(40% 45% at 22% 40%, color-mix(in oklch, var(--acc) 16%, transparent), transparent 70%),
      radial-gradient(35% 40% at 78% 65%, color-mix(in oklch, var(--violet-glow) 12%, transparent), transparent 70%); }
  .intro, .panel { position: relative; }
  .intro { max-width: 460px; justify-self: end; }
  .glow { position: absolute; left: -24px; top: -24px; width: 140px; height: 140px; border-radius: 50%;
          background: radial-gradient(circle, color-mix(in oklch, var(--acc) 30%, transparent), transparent 70%);
          pointer-events: none; }
  .logo { position: relative; width: 84px; height: 84px; object-fit: contain; }
  h1 { margin: 14px 0 6px; font-size: 20px; font-weight: 600; letter-spacing: -.01em; color: var(--text); }
  .lead { margin: 0 0 22px; font-size: 15px; line-height: 1.5; color: var(--text-2); }
  .points { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
  .points li { display: flex; align-items: flex-start; gap: 10px; font-size: 13.5px; color: var(--text-2); }
  .points li :global(svg), .pt-logo { flex: none; margin-top: 2px; color: var(--acc); }
  .pt-logo { width: 16px; height: 16px; object-fit: contain; }
  .points b { color: var(--text); font-weight: 600; }

  .panel { width: 100%; max-width: 400px; padding: 28px 26px 26px; border-radius: var(--r-panel);
           background: var(--raised); box-shadow: var(--pop), 0 0 0 1px var(--line); }
  .head { margin-bottom: 20px; }
  h2 { margin: 0 0 6px; font-size: 20px; font-weight: 600; color: var(--text); }
  .where { margin: 0; display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); }
  .where :global(svg) { color: var(--ok); flex: none; }
  .where b { color: var(--text-2); font-weight: 600; }
  .host { font-family: var(--mono); font-size: 12px; }
  .form { display: flex; flex-direction: column; gap: 14px; }
  .field { display: flex; flex-direction: column; gap: 6px; }
  label { font-size: 12.5px; font-weight: 500; color: var(--text-2); }
  input { width: 100%; height: 40px; padding: 0 12px; border-radius: var(--r-ctl); border: 0; outline: none;
          background: var(--raised-2); color: var(--text); font-size: 14px;
          box-shadow: 0 0 0 1px var(--line); transition: box-shadow var(--t-fast); }
  input:focus { box-shadow: 0 0 0 2px color-mix(in oklch, var(--acc) 60%, transparent); }
  .pw { position: relative; }
  .pw input { padding-right: 40px; }
  .eye { position: absolute; right: 4px; top: 4px; width: 32px; height: 32px; border-radius: 8px;
         display: grid; place-items: center; color: var(--muted); }
  .eye:hover { background: var(--hover); color: var(--text); }
  .caps { margin: 0; font-size: 12px; color: var(--warn); }
  .msg { font-size: 13px; padding: 8px 10px; border-radius: var(--r-ctl); color: var(--err);
         background: color-mix(in oklch, var(--err) 12%, transparent); }
  .go { height: 40px; justify-content: center; font-size: 14px; margin-top: 4px; }
  .go:disabled { opacity: .6; }

  /* narrow screens: the intro sits on top, shorter */
  @media (max-width: 860px) {
    .wrap { grid-template-columns: minmax(0, 1fr); gap: 24px; padding: 32px 16px; align-content: center; }
    .intro { justify-self: center; text-align: center; }
    .logo { width: 64px; height: 64px; }
    .glow { left: 50%; transform: translateX(-50%); }
    .points { display: none; }
    .lead { margin-bottom: 0; }
    .panel { justify-self: center; }
  }
</style>
