# Contributing to Aura

Thanks for helping. Bug reports, fixes, docs and ideas are all welcome.

## Report a bug

Open an issue with:

- what you ran (`aura --version`, your OS, and the model / endpoint you use)
- what you expected and what happened
- the output of `aura doctor`
- the run's receipt or log lines if you have them (`/receipt` in chat) — receipts keep secret *names*, never values, but read them before you paste

Security problems: please don't open a public issue — see [SECURITY.md](SECURITY.md).

## Set up

```bash
git clone https://github.com/kresearch-labs/aura-agent.git && cd aura-agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[test]"
python -m aura_agent selftest
```

`selftest` must pass before and after your change.

## Make a change

- **Keep safety out of switches.** Approvals, scope, protected files, skill-pack limits and your hooks are never behind a feature switch or a setting the model can change.
- **Every platform.** Aura runs on Windows, macOS, Linux, WSL and in containers. Don't add anything that works on only one.
- **No new outside services by default.** Anything that contacts a service must be listed in the guide's *Services Aura can contact* table and work only with the user's own key, or be off until the user turns it on.
- **A feature ships with its command, setting or notice** in the same change, so people can find and control it.
- **Small pull requests.** One change, with a short description of what it does and how you checked it.

## License

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
