---
name: shell-script
description: Write a portable shell script: POSIX sh (or PowerShell for Windows), safe quoting, fails loudly, works on every OS asked.
license: Apache-2.0
---
1. Ask or decide which shells must run it (sh on Linux/macOS, PowerShell on Windows); write one per family if needed.
2. sh: `set -eu`, quote every variable ("$x"), no bashisms in #!/bin/sh; PowerShell: $ErrorActionPreference = 'Stop'.
3. Check inputs and print a usage line; never `rm -rf` a variable that could be empty.
4. Test it on this machine (run_command) and with a bad input; say which OSes were not tested.
5. Make it re-runnable (doesn't fail or duplicate work the second time).
