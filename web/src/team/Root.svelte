<!-- The law rules every screen: loading, empty, error, normal are four explicit states.
-->
<script lang="ts">
  import { onMount } from "svelte";
  import Frame from "./frame/App.svelte";
  import SignIn from "./SignIn.svelte";
  import ChangePassword from "./views/ChangePassword.svelte";
  import Account from "./views/Account.svelte";
  import Now from "./views/Now.svelte";
  import Board from "./views/Board.svelte";
  import Tree from "./views/Tree.svelte";
  import Flow from "./views/Flow.svelte";
  import History from "./views/History.svelte";
  import Chat from "./views/Chat.svelte";
  import Admin from "./views/Admin.svelte";
  import Stub from "./views/Stub.svelte";
  import Gallery from "../lib/parts/Gallery.svelte";
  import { boot, booting, me, route } from "./frame/shell_state";
  import { canOpenAdministration } from "../lib/admin_users";

  onMount(boot);

  import type { Component } from "svelte";
  // the screens must be REAL Svelte components — keep the map typed
  const SCREENS: Record<string, Component | undefined> = {
    now: Now,
    board: Board,
    tasks: Board,                                   // The pre-route alias
    tree: Tree,
    flow: Flow,
    history: History,
    activity: History,
    decisions: History,
    digest: History,
    chat: Chat,
    admin: Admin,
    password: ChangePassword,
    account: Account,
    // the parts gallery: development builds, or a test build made with VITE_PARTS_GALLERY=1
    ...(import.meta.env.DEV || import.meta.env.VITE_PARTS_GALLERY === "1" ? { gallery: Gallery } : {}),
    // the rest of hull matures in its own one-by-one order;
    // today they borrow shape (loading shown, real screen
    // lands at its own row in the phase).
  };
</script>

{#if $me.value}
  {#if $booting}
    <main class="loading card" aria-busy="true"><p>Loading permissions…</p></main>
  {:else if $route.module === "password"}
    <ChangePassword />
  {:else}
    <Frame>
      {#if $route.module === "admin" && !canOpenAdministration($me.value)}
        <section class="card" aria-labelledby="administration-denied">
          <h2 id="administration-denied">You do not have permission to access administration.</h2>
          <p>Ask an owner or team admin if you need this access.</p>
        </section>
      {:else if SCREENS[$route.module]}
        {@const C = SCREENS[$route.module] as NonNullable<Component>}
        {#key $route.module}<C />{/key}
      {:else}
        <Stub module={$route.module} />
      {/if}
    </Frame>
  {/if}
{:else if $me.loaded}
  <SignIn />
{:else}
  <main class="loading card fade-in" aria-busy="true" aria-live="polite">
    <p>Loading…</p>
  </main>
{/if}

<style>
  .loading { max-width: 300px; margin: 30vh auto; text-align: center;
             color: var(--muted); }
</style>
