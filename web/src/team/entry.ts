// entry.ts — the Team page: sign in at the door, then the frame + the
// module screens. Boot → render in one component so the hash lane and the
// /me lane agree on what's on screen.
import { mount } from "svelte";
import Root from "./Root.svelte";
import "../theme/tokens.css";
import "../theme/parts.css";
import "../lib/prefs";

const target = document.getElementById("app") as HTMLElement;
mount(Root, { target });

// lab-proof: ?debug in the URL leaves the stores on window (debug builds
// the sane lane in test runs, unreachable to a LAN user without it)
if (new URLSearchParams(location.search).has("debug")) {
  import("./frame/shell_state").then((m) => { (window as any).__aura = m; });
}
