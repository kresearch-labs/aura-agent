---
name: build-cli-tool
description: Build a command-line tool: clear commands and --help, sensible exit codes, errors to stderr, tests.
license: Apache-2.0
---
1. Write the --help text first: commands, arguments, options, one example each.
2. Use the language's standard parser (argparse, click if already a dependency, commander/yargs in Node).
3. Exit 0 on success, non-zero on failure; errors to stderr, data to stdout (so it can be piped); a --quiet/--json if useful.
4. Read config from flags > environment > a config file, in that order.
5. Test the main paths and a bad input (run_command); include the help output in the README.
