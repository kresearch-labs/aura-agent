# Security

Aura runs commands and edits files on your machine, so security reports matter a lot to us.

## Report a vulnerability

Please report privately — not in a public issue:

- **GitHub:** this repository's **Security** tab → **Report a vulnerability**, or
- **Email:** the.king.researcher@gmail.com

Include what an attacker can do, the steps to reproduce, and the Aura version (`aura --version`). We will confirm receipt, keep you informed, and credit you in the release notes if you want.

## What counts

Especially welcome:

- any way a tool call runs without passing the mediator, or outside the project scope
- an irreversible action that runs without the user's approval, or runs something other than what was approved
- secrets reaching the model, the logs or a receipt
- prompt injection from a web page, file, command output or MCP server that gets Aura to act against the user
- a skill pack acting beyond what its manifest declares, or loading without being trusted
- a release package or installer that can be swapped without the checksum check catching it

## Supported versions

Fixes go into the newest release. Update with `aura update`.
