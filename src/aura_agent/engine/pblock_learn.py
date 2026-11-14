"""pblock_learn.py — Aura's hook to Persona's learner: this name IS the
engine's pblock_engine/pblock_learn.py (the same as the Persona project's
learn.py), one copy."""
import sys as _sys

from ..pblock_engine import pblock_learn as _engine

_sys.modules[__name__] = _engine
