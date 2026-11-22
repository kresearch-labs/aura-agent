<!-- ChangePassword.svelte — second card. law: current +
     new twice; the hub flips must_change away. The words: plain, short
     sentences, no apology. -->
<script lang="ts">
  import { call } from "../../lib/api/client";
  import { route, meStore, bootHome, me } from "../frame/shell_state";
  // the first sign-in of an account the admin made: this page is the way in
  const first = $derived(!!$me.value?.must_change);

  let currentPw = $state(""); let newPw = $state(""); let again = $state("");
  let busy = $state(false); let error = $state(""); let done = $state(false);

  function cancel() { route.go("now"); }

  async function go(e: Event) {
    e.preventDefault();
    error = ""; done = false;
    if (newPw !== again) { error = "The two new passwords aren't the same."; return; }
    busy = true;
    try {
      await call("/me/password",
                 { current: currentPw, new: newPw },
                 "POST");
      done = true;
      currentPw = newPw = again = "";              // cleared, never kept
      await meStore.load();                        // must_change's flip shows
      await bootHome();                            // the team's pages load now
      route.go("now");
    } catch (err) {
      error = err instanceof Error ? err.message : "the hub refused that";
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Change the password · Aura Teams</title></svelte:head>

<section class="wrap fade-in" aria-label="change the password">
  <h2>{first ? "Choose your own password" : "Change the password"}</h2>
  {#if first}<p class="muted">Your admin gave you a first password. Choose your own to continue — only you will know it.</p>{/if}

  {#if done}<div class="ok-bg card" role="status">Your password is changed.</div>{/if}

  <form class="card" onsubmit={go}>
    <div class="field">
      <label for="cw">Current password</label>
      <input id="cw" type="password" bind:value={currentPw} required
             autocomplete="current-password" />
    </div>
    <div class="field">
      <label for="nw">The new password — 8 to 64 characters</label>
      <input id="nw" type="password" bind:value={newPw} required
             minlength="8" maxlength="64" autocomplete="new-password" />
    </div>
    <div class="field">
      <label for="ag">The same new password again</label>
      <input id="ag" type="password" bind:value={again} required
             autocomplete="new-password" />
    </div>
    {#if error}<div class="err" role="alert">{error}</div>{/if}
    <div class="row">
      <button class="btn primary" type="submit" disabled={busy}>Save</button>
      {#if !first}<button class="btn" type="button" onclick={cancel}>Back</button>{/if}
    </div>
  </form>
</section>

<style>
  /* the same card and fields as the sign-in page (it had none: bare browser
     boxes with the labels running into them) */
  .wrap { max-width: 400px; margin: 8vh auto 0; padding: var(--sp-16);
          display: flex; flex-direction: column; gap: var(--sp-16); }
  form.card { display: flex; flex-direction: column; gap: 14px; padding: 24px; }
  .field { display: flex; flex-direction: column; gap: 6px; }
  label { font-size: 12.5px; font-weight: 500; color: var(--text-2); }
  input { width: 100%; height: 40px; padding: 0 12px; border-radius: var(--r-ctl); border: 0; outline: none;
          box-sizing: border-box; background: var(--raised-2); color: var(--text); font-size: 14px;
          box-shadow: 0 0 0 1px var(--line); transition: box-shadow var(--t-fast); }
  input:focus { box-shadow: 0 0 0 2px color-mix(in oklch, var(--acc) 60%, transparent); }
  .row { display: flex; gap: var(--sp-8); margin-top: 4px; }
  .err { font-size: 13px; color: var(--err); }
</style>
