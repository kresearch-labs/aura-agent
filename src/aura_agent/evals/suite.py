"""suite.py — the core task suite (aura-core-v1).

Each task: id, category, prompt (what a user would type), files (the
starting workspace), check (Python run in the workspace afterwards; exit 0
= pass, assertion message = why not) and solution (files that solve it —
used only by `aura eval check` to prove the checker is right).

Tasks are small, stdlib-only and independent of any model or provider.
Add tasks by appending to TASKS; keep ids stable (results are compared by
id across runs).
"""
from __future__ import annotations

SUITE = "aura-core-v1"

TASKS: list[dict] = [
    # ---------------------------------------------------------- bug fixes
    {
        "id": "bugfix-add",
        "category": "bugfix",
        "prompt": "add() in calc.py returns the wrong result (add(2, 3) gives -1). Fix it.",
        "files": {"calc.py": "def add(a, b):\n    return a - b\n"},
        "check": """
from calc import add
assert add(2, 3) == 5, "add(2, 3) should be 5"
assert add(-1, 1) == 0, "add(-1, 1) should be 0"
""",
        "solution": {"calc.py": "def add(a, b):\n    return a + b\n"},
    },
    {
        "id": "bugfix-off-by-one",
        "category": "bugfix",
        "prompt": "last_n(xs, n) in seqs.py should return the last n items, but it returns one too many. Fix it (n >= 1).",
        "files": {"seqs.py": "def last_n(xs, n):\n    return xs[-n - 1:]\n"},
        "check": """
from seqs import last_n
assert last_n([1, 2, 3, 4], 2) == [3, 4], last_n([1, 2, 3, 4], 2)
assert last_n([1, 2, 3], 3) == [1, 2, 3]
assert last_n([9], 1) == [9]
""",
        "solution": {"seqs.py": "def last_n(xs, n):\n    return xs[-n:]\n"},
    },
    {
        "id": "bugfix-empty-average",
        "category": "bugfix",
        "prompt": "average() in stats.py crashes on an empty list. It should return 0.0 for an empty list and keep working otherwise.",
        "files": {"stats.py": "def average(xs):\n    return sum(xs) / len(xs)\n"},
        "check": """
from stats import average
assert average([]) == 0.0, "empty list should give 0.0"
assert average([2, 4]) == 3
""",
        "solution": {"stats.py": "def average(xs):\n    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)\n"},
    },
    {
        "id": "bugfix-email-dot",
        "category": "bugfix",
        "prompt": "is_valid_email in validate.py accepts 'a@b', which has no dot in the domain. Domains must contain a dot; keep accepting normal addresses.",
        "files": {"validate.py": "import re\n\ndef is_valid_email(s):\n    return re.fullmatch(r\"[^@\\s]+@[^@\\s]+\", s) is not None\n"},
        "check": """
from validate import is_valid_email as v
assert not v("a@b"), "a@b must be rejected"
assert v("me@example.com") and v("x.y@mail.co.uk")
assert not v("no-at-sign.com") and not v("a@@b.com")
""",
        "solution": {"validate.py": "import re\n\ndef is_valid_email(s):\n    return re.fullmatch(r\"[^@\\s]+@[^@\\s]+\\.[^@\\s]+\", s) is not None\n"},
    },
    {
        "id": "bugfix-mutable-default",
        "category": "bugfix",
        "prompt": "add_item in cart.py remembers items between separate calls that don't pass a cart. Each call without a cart should start with an empty list.",
        "files": {"cart.py": "def add_item(item, cart=[]):\n    cart.append(item)\n    return cart\n"},
        "check": """
from cart import add_item
assert add_item("a") == ["a"]
assert add_item("b") == ["b"], "second call must not see the first item"
assert add_item("c", ["x"]) == ["x", "c"]
""",
        "solution": {"cart.py": "def add_item(item, cart=None):\n    if cart is None:\n        cart = []\n    cart.append(item)\n    return cart\n"},
    },
    # ------------------------------------------------------------ features
    {
        "id": "feature-palindrome",
        "category": "feature",
        "prompt": "Add is_palindrome(s) to text_utils.py. It ignores case and any character that is not a letter or digit, so 'A man, a plan, a canal: Panama' is a palindrome.",
        "files": {"text_utils.py": "def shout(s):\n    return s.upper()\n"},
        "check": """
from text_utils import is_palindrome, shout
assert is_palindrome("A man, a plan, a canal: Panama")
assert is_palindrome("") and is_palindrome("Aa")
assert not is_palindrome("hello")
assert shout("x") == "X", "existing function must keep working"
""",
        "solution": {"text_utils.py": "def shout(s):\n    return s.upper()\n\n\ndef is_palindrome(s):\n    t = [c.lower() for c in s if c.isalnum()]\n    return t == t[::-1]\n"},
    },
    {
        "id": "feature-cli-flag",
        "category": "feature",
        "prompt": "greet.py prints 'Hello, NAME'. Add a --shout flag that prints the whole greeting in upper case. Without the flag, output stays the same.",
        "files": {"greet.py": "import argparse\n\np = argparse.ArgumentParser()\np.add_argument('name')\na = p.parse_args()\nprint(f'Hello, {a.name}')\n"},
        "check": """
import subprocess, sys
run = lambda *a: subprocess.run([sys.executable, "greet.py", *a], capture_output=True, text=True).stdout.strip()
assert run("Ana") == "Hello, Ana", run("Ana")
assert run("Ana", "--shout") == "HELLO, ANA", run("Ana", "--shout")
""",
        "solution": {"greet.py": "import argparse\n\np = argparse.ArgumentParser()\np.add_argument('name')\np.add_argument('--shout', action='store_true')\na = p.parse_args()\nmsg = f'Hello, {a.name}'\nprint(msg.upper() if a.shout else msg)\n"},
    },
    {
        "id": "feature-format-bytes",
        "category": "feature",
        "prompt": "Write format_bytes(n) in units.py: under 1024 → '512 B'; then KB, MB, GB with one decimal, e.g. 1536 → '1.5 KB', 1048576 → '1.0 MB'.",
        "files": {"units.py": "# byte formatting helpers\n"},
        "check": """
from units import format_bytes as f
assert f(512) == "512 B", f(512)
assert f(1536) == "1.5 KB", f(1536)
assert f(1048576) == "1.0 MB", f(1048576)
assert f(3 * 1024 ** 3) == "3.0 GB", f(3 * 1024 ** 3)
""",
        "solution": {"units.py": "def format_bytes(n):\n    if n < 1024:\n        return f'{n} B'\n    for unit in ('KB', 'MB', 'GB'):\n        n /= 1024\n        if n < 1024 or unit == 'GB':\n            return f'{n:.1f} {unit}'\n"},
    },
    {
        "id": "feature-dedupe",
        "category": "feature",
        "prompt": "Add dedupe(items) to listy.py: remove duplicates but keep the first occurrence order.",
        "files": {"listy.py": ""},
        "check": """
from listy import dedupe
assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]
assert dedupe([]) == []
assert dedupe(["b", "a", "b"]) == ["b", "a"]
""",
        "solution": {"listy.py": "def dedupe(items):\n    seen, out = set(), []\n    for x in items:\n        if x not in seen:\n            seen.add(x)\n            out.append(x)\n    return out\n"},
    },
    {
        "id": "feature-env-timeout",
        "category": "feature",
        "prompt": "In settings.py, make get_timeout() read the APP_TIMEOUT environment variable as an int, and return 30 when it is missing or not a number.",
        "files": {"settings.py": "def get_timeout():\n    return 30\n"},
        "check": """
import os
from settings import get_timeout
os.environ.pop("APP_TIMEOUT", None); assert get_timeout() == 30
os.environ["APP_TIMEOUT"] = "5"; assert get_timeout() == 5, get_timeout()
os.environ["APP_TIMEOUT"] = "soon"; assert get_timeout() == 30
""",
        "solution": {"settings.py": "import os\n\n\ndef get_timeout():\n    try:\n        return int(os.environ.get('APP_TIMEOUT', ''))\n    except ValueError:\n        return 30\n"},
    },
    # ----------------------------------------------------------- refactors
    {
        "id": "refactor-rename",
        "category": "refactor",
        "prompt": "Rename the function calc_total to compute_total everywhere (shop.py and main.py). The old name must be gone and main.py must still work.",
        "files": {"shop.py": "def calc_total(prices):\n    return sum(prices)\n",
                  "main.py": "from shop import calc_total\n\nprint(calc_total([1, 2, 3]))\n"},
        "check": """
import pathlib, subprocess, sys
src = pathlib.Path("shop.py").read_text() + pathlib.Path("main.py").read_text()
assert "calc_total" not in src, "old name still present"
from shop import compute_total
assert compute_total([1, 2]) == 3
out = subprocess.run([sys.executable, "main.py"], capture_output=True, text=True)
assert out.stdout.strip() == "6", out.stdout + out.stderr
""",
        "solution": {"shop.py": "def compute_total(prices):\n    return sum(prices)\n",
                     "main.py": "from shop import compute_total\n\nprint(compute_total([1, 2, 3]))\n"},
    },
    {
        "id": "refactor-unused-imports",
        "category": "refactor",
        "prompt": "Remove the unused imports from util.py without changing what it does.",
        "files": {"util.py": "import os\nimport sys\nimport json\n\n\ndef dump(d):\n    return json.dumps(d, sort_keys=True)\n"},
        "check": """
import ast, pathlib
tree = ast.parse(pathlib.Path("util.py").read_text())
names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
assert names == {"json"}, f"imports left: {names}"
from util import dump
assert dump({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
""",
        "solution": {"util.py": "import json\n\n\ndef dump(d):\n    return json.dumps(d, sort_keys=True)\n"},
    },
    {
        "id": "refactor-typo-everywhere",
        "category": "refactor",
        "prompt": "The word 'recieve' is misspelled in several .py files here. Fix it to 'receive' everywhere (names, strings, comments) and keep the code working.",
        "files": {"net.py": "def recieve(sock):\n    # recieve one message\n    return sock.pop(0)\n",
                  "app.py": "from net import recieve\n\nMSG = 'could not recieve'\n\n\ndef run(q):\n    return recieve(q)\n",
                  "README.txt": "recieve is spelled wrong in the code\n"},
        "check": """
import pathlib
for f in ("net.py", "app.py"):
    assert "recieve" not in pathlib.Path(f).read_text(), f + " still misspelled"
from app import run, MSG
assert run([7]) == 7 and MSG == "could not receive"
""",
        "solution": {"net.py": "def receive(sock):\n    # receive one message\n    return sock.pop(0)\n",
                     "app.py": "from net import receive\n\nMSG = 'could not receive'\n\n\ndef run(q):\n    return receive(q)\n"},
    },
    {
        "id": "refactor-fast-fib",
        "category": "refactor",
        "prompt": "fib(n) in fib.py is far too slow for n = 80. Make it fast (well under a second) with the same results.",
        "files": {"fib.py": "def fib(n):\n    return n if n < 2 else fib(n - 1) + fib(n - 2)\n"},
        "check": """
import time
from fib import fib
assert [fib(i) for i in range(10)] == [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]
t = time.time(); v = fib(80)
assert v == 23416728348467685, v
assert time.time() - t < 1.0, "too slow"
""",
        "solution": {"fib.py": "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n"},
    },
    # ---------------------------------------------------------- write tests
    {
        "id": "tests-for-slugify",
        "category": "tests",
        "prompt": "Write tests for slugify() in slug.py as tests/test_slug.py (plain asserts, runnable with python). Cover at least spaces, case and punctuation.",
        "files": {"slug.py": "import re\n\n\ndef slugify(s):\n    s = re.sub(r'[^a-z0-9]+', '-', s.lower())\n    return s.strip('-')\n"},
        "check": """
import pathlib, subprocess, sys, shutil
t = pathlib.Path("tests/test_slug.py")
assert t.exists(), "tests/test_slug.py missing"
src = t.read_text()
assert src.count("assert") >= 3 and "slugify" in src, "needs at least 3 asserts on slugify"
env_ok = subprocess.run([sys.executable, str(t)], capture_output=True, text=True, cwd=".",
                        env={**__import__("os").environ, "PYTHONPATH": "."})
assert env_ok.returncode == 0, "tests fail on the correct code: " + env_ok.stderr[-300:]
# a broken slugify must make the tests fail (they really test something)
shutil.copy("slug.py", "slug.bak")
pathlib.Path("slug.py").write_text("def slugify(s):\\n    return s\\n")
try:
    bad = subprocess.run([sys.executable, str(t)], capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": "."})
    assert bad.returncode != 0, "tests still pass when slugify is broken"
finally:
    shutil.move("slug.bak", "slug.py")
""",
        "solution": {"tests/test_slug.py": "from slug import slugify\n\nassert slugify('Hello World') == 'hello-world'\nassert slugify('ABC') == 'abc'\nassert slugify('a, b!') == 'a-b'\nassert slugify('  x  ') == 'x'\nprint('ok')\n"},
    },
    # ------------------------------------------------------------- config
    {
        "id": "config-json-edit",
        "category": "config",
        "prompt": "In config.json, turn debug off and add \"port\": 8080. Keep every other setting as it is.",
        "files": {"config.json": "{\n  \"name\": \"svc\",\n  \"debug\": true,\n  \"workers\": 4\n}\n"},
        "check": """
import json
c = json.load(open("config.json"))
assert c == {"name": "svc", "debug": False, "workers": 4, "port": 8080}, c
""",
        "solution": {"config.json": "{\n  \"name\": \"svc\",\n  \"debug\": false,\n  \"workers\": 4,\n  \"port\": 8080\n}\n"},
    },
    {
        "id": "config-toml-version",
        "category": "config",
        "prompt": "Bump the project version in pyproject.toml to 1.2.0. Change nothing else.",
        "files": {"pyproject.toml": "[project]\nname = \"demo\"\nversion = \"1.1.3\"\ndependencies = [\"requests>=2\"]\n"},
        "check": """
import tomllib
d = tomllib.load(open("pyproject.toml", "rb"))
assert d["project"]["version"] == "1.2.0", d["project"]["version"]
assert d["project"]["name"] == "demo" and d["project"]["dependencies"] == ["requests>=2"]
""",
        "solution": {"pyproject.toml": "[project]\nname = \"demo\"\nversion = \"1.2.0\"\ndependencies = [\"requests>=2\"]\n"},
    },
    # ------------------------------------------------------- read + answer
    {
        "id": "read-count-failed",
        "category": "read",
        "prompt": "How many rows in jobs.csv have status 'failed'? Write just the number to answer.txt.",
        "files": {"jobs.csv": "id,status\n1,ok\n2,failed\n3,failed\n4,ok\n5,failed\n6,running\n"},
        "check": """
import pathlib
a = pathlib.Path("answer.txt")
assert a.exists(), "answer.txt missing"
assert a.read_text().strip() == "3", repr(a.read_text())
""",
        "solution": {"answer.txt": "3\n"},
    },
    {
        "id": "read-find-config-key",
        "category": "read",
        "prompt": "Which port does the 'api' service use according to the files in services/? Write just the number to answer.txt.",
        "files": {"services/web.ini": "[web]\nport = 8000\n",
                  "services/api.ini": "[api]\nhost = 0.0.0.0\nport = 9431\n",
                  "services/db.ini": "[db]\nport = 5432\n"},
        "check": """
import pathlib
assert pathlib.Path("answer.txt").read_text().strip() == "9431"
""",
        "solution": {"answer.txt": "9431\n"},
    },
    # --------------------------------------------------------- multi-file
    {
        "id": "multi-add-field",
        "category": "multi-file",
        "prompt": "Give User (models.py) an optional email field defaulting to an empty string, and include it in to_dict() in serializers.py.",
        "files": {"models.py": "class User:\n    def __init__(self, name):\n        self.name = name\n",
                  "serializers.py": "def to_dict(user):\n    return {'name': user.name}\n"},
        "check": """
from models import User
from serializers import to_dict
assert to_dict(User("a")) == {"name": "a", "email": ""}, to_dict(User("a"))
assert to_dict(User("b", email="b@x.io")) == {"name": "b", "email": "b@x.io"}
""",
        "solution": {"models.py": "class User:\n    def __init__(self, name, email=''):\n        self.name = name\n        self.email = email\n",
                     "serializers.py": "def to_dict(user):\n    return {'name': user.name, 'email': user.email}\n"},
    },
]
