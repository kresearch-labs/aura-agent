import os
import sys

# Windows consoles default to cp1252/cp437; ✓/✗/→ crash with
# UnicodeEncodeError. Reconfigure to UTF-8 before anything prints
# (Python 3.7+; harmless no-op elsewhere).
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8",
                                errors="replace")
        sys.stderr.reconfigure(encoding="utf-8",
                                errors="replace")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from .cli import main

main()
