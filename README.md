# Aura — local-first autonomous AI agent that proves its work

**Every action through one audited gate, every write verified on disk, every run a tamper-evident receipt — any model, no cloud, scope enforced in code. And with Persona inside, it keeps what it learns.**

Aura is a local-first autonomous agent. Point it at any OpenAI-compatible
endpoint (Ollama, vLLM, LM Studio, llama.cpp, or a cloud API), give it a
scoped workspace, and it works like a teammate that has to *prove*
everything it claims.

## Persona: experience that stays

Most agents start every session from zero. Aura has **Persona** built in:
pblocks — small trained units of experience (real cases with their
confirmed outcomes, plus calibrated judgment heads) that brief the model
before each task with what worked and what failed, cite the cases behind
every judgment, and learn from every confirmed outcome. The experience is
plain data on your machine: readable, correctable, and kept when you
switch models. It runs on a CPU; no model is fine-tuned.

```text
/persona                           the pblocks in this chat, and what to do with each
/persona train [what you work on]  Aura plans new pblocks with you and trains them
/persona add <path>                add a trained pblock (a store, a folder or a .zip)
/persona use | stop <name>         use one in this chat, or stop using it
/persona learned [name]            the newest lessons, in the pblock's own words
/persona review [name]             open the lessons page: tick, edit, then /persona apply <file>
/persona off                       plain Aura, no pblocks
```

Every chat starts with your own blank personal pblock, which grows from
what you confirm. `discard` takes a pblock out of a chat and keeps its
files; `delete` removes its files after asking.

The engine on its own: [kresearch-labs/Persona](https://github.com/kresearch-labs/Persona).
Trained pblocks, including **Keel Guard** (a security pblock that judges
agent requests in tens of milliseconds on a CPU):
[Kresearch/persona-pblocks](https://huggingface.co/datasets/Kresearch/persona-pblocks).
The paper's runs and grade reports:
[kresearch-labs/persona-evidence](https://github.com/kresearch-labs/persona-evidence).

## Install

Aura is not on PyPI yet. Install it from this folder — the installer brings
its own Python (your system Python isn't touched), checks every download
against a checksum written into the installer, and changes nothing outside
`~/.aura` and the `aura` command unless you say so. `--dry-run` shows the
plan first.

macOS, Linux, WSL, containers:

```bash
sh scripts/install.sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

It ends in `aura setup`: the model first, then every other choice on one
board (`aura setup full`, or `/setup` in a chat). For scripts:
`--yes` / `-Yes`, then `aura setup --answers FILE --non-interactive`.
Later: `aura update --from <new folder>`; `aura uninstall` removes the
program and keeps your data (`--purge` removes that too, after asking).

Already have Python 3.11+ and a virtual environment? `pip install ".[vault]"`
works as well.

In a container (Docker):

```bash
docker build -t aura .
docker run -it --rm -v aura-data:/data -v "$PWD":/work -e AI_BASE_URL -e AI_MODEL -e AI_API_KEY aura
```

Your project is mounted at `/work`, Aura's data stays in the `aura-data`
volume, and keys come in as environment variables (a container has no
system keychain). A model server on your own machine is
`http://host.docker.internal:11434/v1` from inside (on Linux add
`--add-host=host.docker.internal:host-gateway`). Aura's sandbox works in the
container as it is; `--build-arg EXTRAS=vault,web` adds the browser tool.

Then, in any project folder:

```bash
cd my-project
aura init                      # optional: what it may reach on the network
aura chat                      # or: aura run "fix the failing test"
```

Something failed? Run `aura doctor`.

The chat screen keeps your terminal's own scrollback (scroll, search,
copy as usual), streams formatted answers, shows each tool call with its
real result and a diff for every edit, lets you type while Aura works and
stop it with Esc, and shows approvals as cards with the change previewed.
`?` lists the keys.

## The 60-second tour

*(illustrative)*

```text
you: fix the off-by-one in utils/dates.py and run its tests
· reading utils/dates.py
· editing utils/dates.py — verified on disk, snapshot saved for /undo
· running pytest tests/test_dates.py
▸ done — receipt written (hash-chained to the previous run)
   checks: final answer ✓ · work diff accounted ✓
```

## What makes it different

- **Keeps what it learns** — Persona's pblocks brief every task with past
  cases and grow from confirmed outcomes; the experience is yours, as plain
  data, and survives a model change.
- **Checks its own work** — every file write is re-read from disk and
  fails hard if the change didn't land; after each run an independent
  referee re-checks deliverables on disk and re-runs named scripts; if the
  model never shows its diff, the engine recovers it from `git diff`.
- **Tamper-evident receipts** — every run writes a receipt hash-chained to
  the previous one; `aura receipts-verify` (or `/receipt verify`) detects
  any edited or missing link.
- **Scope enforced outside the LLM** — the mediator checks every tool
  call: kill-switch → mode → scope → your hooks → approval → budget →
  execute → audit. Refusals are data the model sees; silence is a bug.
- **Any model, local-first** — works with whatever OpenAI-compatible
  endpoint you already run. No cloud in the critical path; your workspace
  data never leaves your box.
- **Human-grade interaction** — tool calls narrated in plain sentences
  ("reading src/auth.py"), receipts for every run, previously-on recaps,
  approvals you answer right in the chat.

## Core commands (the daily five)

| | |
|---|---|
| `/help` | every command, grouped, with usage |
| `/model` | hot-swap the model (live picker from your endpoint) |
| `/findings` | recorded results and their proof status |
| `/status` | mode, model, context, spend |
| `/exit` | save a checkpoint and leave |

Plus more self-documenting commands: `/persona`, `/audit` (the action
chain), `/receipt` (per-run receipt), `/undo`, `/rewind`, `/new`,
`/resume`, `/feedback`. Type `/` for the menu; TAB completes.

## How Aura keeps your work

- **Your project stays yours.** The folder you start Aura in is the
  project: files, commands and tests work there, and Aura writes none of
  its own files into it.
- **Its notes live in a notebook** per project, under
  `~/.aura/profiles/<profile>/projects/` — chats, results, todo, task
  list, rules, undo copies, checkpoints, audit log, receipts.
- **Profiles** keep separate work apart, like separate desks: each has
  its own model setups and keys, MCP servers, settings, skills, slash
  commands and projects. `default` always exists — you never have to make
  one. `aura profile create work` (`--copy-models` brings your models),
  `aura profile use work`, `aura -p work chat` for one launch.
  What is about **you** (your preferences, `USER.md`) is shared.
- **Every chat remembers what it runs with** — model, budget, feature
  switches, Build/Plan mode, assist level, and its pblocks. `/resume`
  brings it back as it was. A change in chat is for that chat; add
  `permanent` to make it the profile's default.
- **Keys** (model API keys, MCP logins, gateway tokens, credentials for
  commands) live in one store per profile: the OS keychain, or an AES-256
  vault with your password (`aura keys lock`), or — only with neither — a
  plain owner-only file, with a warning. Config files keep `@keystore`
  where a key was. `aura keys` lists names, never values.
- **Options** — `/settings` in chat or `aura settings`: every option with
  what it costs and what it needs, `find <words>` to look one up by
  describing it, `set` / `reset` / `undo`. You can also just ask Aura
  ("stop showing line hashes") — it shows old -> new and changes nothing
  until you confirm.
- **Network reach** is per project: `aura scope add docs.python.org`
  (or `/scope add …` in chat) allows a website or host; everything else
  is refused. For research, `/web all on` lets Aura read any public
  website in that project (never local or private addresses, and never
  for commands). Files are always limited to the project.
- **Servers and watchers** run in the background (`process_start`):
  Aura waits until they are ready (output, port or URL), never starts a
  second copy on a taken port, is told when one stops, and stops them all
  when it exits — even after a crash on Windows; leftovers of a crash are
  cleaned up next time.
- **Web search** works with the search services you connect with your
  own key (or your own SearXNG), and — if you switch them on — the
  official open public sources (`/web open on`). See "Services Aura can
  contact" below. PDFs are read with `pip install 'aura-agent[pdf]'`.
  Aura picks the engine by the question (papers, code, facts), skips
  engines that are failing, and before answering checks that every link
  it cites was really opened and every quote is really on the page.
- **Model setups** (endpoint + model + key): `aura model list`,
  `aura model use <name>`, `--model <name>` for one launch, `/model use`
  in chat. The context window is asked from the server; when a server
  says a prompt is too long, Aura uses the size it names and shrinks the
  conversation to fit. Every model gets Aura's own prompt; a model maker's
  recommended settings (temperature and the like) can be used instead of
  Aura's with `aura model settings`.

## Services Aura can contact

Everyday web research works with no setup: DuckDuckGo and the open APIs
below need no account (`/web open off` switches them off). Every other
service works with **your own** key or server, so its terms are between
you and that service. Nothing is used behind your back — the list is
complete. `/research basic|normal|deep` sets how far research goes:
basic uses only the listed sources, normal (the default) searches
anything but keeps it small, deep plans and checks two sources per
question.

| Service | Connect with | What it receives | Terms |
|---|---|---|---|
| your model server | `aura setup` | your conversation (that's the job) | yours / your provider's |
| Brave Search | `BRAVE_API_KEY` | search words | [terms](https://api-dashboard.search.brave.com/terms-of-service) |
| Tavily | `TAVILY_API_KEY` | search words | [terms](https://tavily.com/terms) |
| Exa | `EXA_API_KEY` | search words | [terms](https://exa.ai/assets/Exa_Labs_Terms_of_Service.pdf) |
| Serper (Google results) | `SERPER_API_KEY` | search words | [terms](https://serper.dev/terms) |
| Firecrawl | `FIRECRAWL_API_KEY` | search words | [terms](https://www.firecrawl.dev/terms-of-service) |
| Marginalia | `MARGINALIA_API_KEY` | search words — its data is non-commercial (CC-BY-NC-SA) | [API & license](https://about.marginalia-search.com/article/api/) |
| OpenAlex | `OPENALEX_API_KEY` | search words | [terms](https://openalex.org/terms) |
| your SearXNG | `SEARXNG_URL` | search words (your own server) | — |
| DuckDuckGo | nothing — on by default (`/web open off`) | search words | [terms](https://duckduckgo.com/terms) |
| Jina Reader | `JINA_API_KEY` | the address of a page that is all JavaScript | [Jina](https://jina.ai/reader) |
| Wikipedia, arXiv, PubMed, Stack Overflow, GitHub | nothing — official open APIs, on by default (`/web open off`) | search words | [Wikimedia](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) · [arXiv](https://info.arxiv.org/help/api/tou.html) · [NCBI](https://www.ncbi.nlm.nih.gov/home/about/policies/) · [Stack Exchange](https://stackoverflow.com/legal/api-terms-of-use) · [GitHub](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) |
| web pages | public pages can be read by default (`/web all off`); commands reach a site only after `/scope add` or `/web all on` | a normal page request | the site's own |
| MCP servers, gateway channels | only those you configure | what you send them | theirs |

Optional keys for higher limits on the open APIs: `GITHUB_TOKEN`,
`STACKEXCHANGE_KEY`, `NCBI_API_KEY`. Set any key with `aura keys set NAME`
(or an environment variable); `/web` shows what is connected. Requests
name Aura as the client; `AURA_CONTACT` adds your email or site, as
Wikimedia asks of API clients.

## Trust architecture

- **Mediator** — every tool call: kill-switch → unknown-tool → mode →
  skill-pack limits → scope → hooks → approval → budget → execute →
  audit line. Property-tested (executed ⟺ scope predicate, 200+
  randomized combinations).
- **State-file lock** — the model cannot write results/state directly.
- **Shadow-git** — every write snapshotted; `/undo` restores.
- **Artifact store** — sha256-addressed, tamper-evident.
- **Graded autonomy** — reads (G0) to irreversible/external (G3). Three
  levels: **L1** asks before every write or command; **L2** (default) asks
  only before actions that can't be undone or reach outside (sending
  messages, `git push`, publishing, deploys, `ssh`, posting data to
  non-local servers); **L3** never asks. When it asks, you see the real
  command or file and answer **y** (once), **a** (always for this exact
  file or command) or **n**; the run waits, and what runs is checked to be
  exactly what you approved. `/approvals` lists saved permissions, offers
  ones you keep saying yes to, and removes them. Irreversible actions can
  never be pre-approved by a saved permission. The agent can propose
  adding or removing one of your standing rules (`constraints.md`), but
  that **always** asks you — at every level, never pre-approvable — and a
  "no" isn't asked again that session. **Memory** saves facts and your
  preferences by itself (short, one line, capped, never auto-deleted);
  anything phrased as an order to the agent asks you first. Saving a
  **skill** always asks. Runs with nobody to ask (scheduled, piped)
  refuse instead of waiting.
- **Agent self-defense** — tool outputs are secret-scrubbed before they
  enter context, and the scrub fails closed (if it crashes, the output is
  withheld rather than shown raw); output from web pages, files, commands
  and MCP servers is flagged when it contains instruction-like text;
  receipts store secret NAMES, never values.
- **Reviewer** — after repeated failures, a separate model call that sees
  only the failed calls and the task graph (never the agent's reasoning)
  diagnoses the problem and recommends a next step (max 3 per run).
- **Proof before the answer** — when a run changed something, Aura
  re-runs the read-only steps its answer rests on (never anything that
  changes state), checks each result still matches, and runs the
  project's tests (`.aura/checks.json` overrides the detected command).
  Failing tests get one turn to fix; the outcome is recorded in the run
  receipt either way.
- **Report gate** — a finding can enter the report only with an attached
  proof (code-enforced).
- **Recorded results are proven.** When the agent records a result
  (`finding_add`), Aura re-runs the reads or tests behind it and answers
  "proven" or "not proven: why"; a result resting on a file that doesn't
  parse is never "proven". `aura report` writes `report.md` from proven
  results only.

## Running on its own

- `aura fleet dispatch "task one" "task two"` runs helpers in parallel on
  the project and stays until all finish, printing each result.
- `aura cron add "every morning: …"` saves a scheduled job;
  **`aura cron watch`** is what runs due jobs (every project of the
  profile, once a minute) — keep it running, or have your OS scheduler
  run `aura --dir <project> cron run`.

## Measuring

`aura eval run` scores the agent on a built-in suite of 20 small tasks
(bug fixes, features, refactors, tests, config edits, reading and
answering), each in a fresh temporary workspace, with whatever model you
configured, and saves a results file. `aura eval compare old.json new.json`
shows which tasks got fixed or broke — run it before and after a change.
`aura eval check` proves the suite itself is right without any model.
When you pick a model that was never measured (`aura setup`, `aura model`),
Aura offers once to run the eval — it always asks, because the tasks use
tokens; in chat, `/model` shows a one-line hint instead.

## Feature switches

`/features` lists the behaviour features and switches them for the
current chat (`/features off auto_lint`, `/features preset minimal`; add
`permanent` for the profile's default); `AURA_FEATURES=-proof,auto_lint`
does the same for one run. Presets: **core** (the default — the drift
check, the failure reviewer and the syntax note: an edit that leaves a
Python/JSON/TOML file unparseable is flagged at once), **minimal** (syntax
note + proof), **recommended** (all on), **off** (none). Safety —
approvals, skill-pack limits, protected files, scope and your hooks — is
never a switch.

## Ecosystem

Skill packs are installed **untrusted** and are never loaded into the
agent until you review one and run `aura skills trust <name>`. Trust
records a fingerprint of every file in the pack; if anything in it changes
afterwards, the pack stops loading until you trust it again. Skills you
write yourself in your workspace's `skills/` folder load as your own.

Packs are Ed25519-signed over **every file** and only verify against
publisher keys **you** trust (`aura skills trust-key <key>`) — never the
key a pack carries. Publishers use `aura skills keygen` and
`aura skills sign <pack> --key <file>`.

Once the agent loads a pack, **every later call in that conversation must
fit what the pack's `manifest.json` declares** (`capabilities`): where it
may write (`fs_scope`), which hosts and MCP servers it may reach
(`network_targets`), which saved secrets may reach commands
(`credential_access`) and, optionally, which shell commands it may run
(`commands`; none declared = no shell, and no chaining with `;` `&&` `|`
`>`). Nothing declared = reads only. `aura skills trust` prints what you
are allowing.

Skills you write yourself load from the profile (`~/.aura/profiles/
<profile>/skills/`) or, for one project only, from its notebook
(`aura skills install --local`).

## License

Apache 2.0 — see [LICENSE](LICENSE).
