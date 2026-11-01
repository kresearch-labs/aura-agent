"""tui — Aura's terminal chat interface.

Inline renderer: finished output goes to the terminal's own scrollback
(native scroll, search, select and copy keep working); only a small live
area at the bottom — activity, the input box and the status line — is
redrawn in place. No scroll regions (they lose scrollback on Windows
Terminal and VS Code) and no alternate screen.

Modules
  text      terminal-cell width, ANSI-aware wrap/cut
  style     colour roles, themes, truecolor / 256 / 16 / no-colour
  keys      key events + the VT input parser
  term      the real terminal: raw input on Windows and POSIX, size, VT
  editor    the input box's text model (pure, testable)
  history   prompt history per project + reverse search
  complete  / commands and @file completion
  markdown  streaming markdown → styled lines (code colours, tables)
  highlight small, dependency-free syntax colouring
  diff      coloured unified diffs with line numbers
  cells     what the transcript is made of (messages, tool calls…)
  live      the live area at the bottom + printing above it
  app       the chat: input, the agent run, approvals, status line
"""
