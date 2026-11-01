"""highlight.py — syntax colouring for code blocks and diffs.

Uses Pygments when it happens to be installed (best quality, hundreds of
languages); otherwise a small built-in highlighter covers the languages
an agent shows most (Python, JS/TS, JSON, shell, YAML/TOML, Go, Rust,
C-family, Java/C#, SQL, HTML/XML, CSS). No dependency either way.
"""
from __future__ import annotations

import re

from .style import Style

ALIASES = {
    "py": "python", "python3": "python", "py3": "python",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
    "ps1": "powershell", "pwsh": "powershell",
    "yml": "yaml", "rs": "rust", "golang": "go",
    "c++": "cpp", "cc": "cpp", "hpp": "cpp", "h": "c", "cs": "csharp",
    "kt": "kotlin", "rb": "ruby", "htm": "html", "svg": "xml",
    "jsonc": "json", "json5": "json",
}

_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".ts": "typescript", ".tsx": "typescript", ".json": "json", ".sh": "bash",
        ".bash": "bash", ".ps1": "powershell", ".yml": "yaml", ".yaml": "yaml",
        ".toml": "toml", ".go": "go", ".rs": "rust", ".c": "c", ".h": "c",
        ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".java": "java", ".cs": "csharp",
        ".sql": "sql", ".html": "html", ".htm": "html", ".xml": "xml", ".css": "css",
        ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".swift": "swift",
        ".md": "markdown", ".diff": "diff", ".patch": "diff", ".ini": "toml",
        ".cfg": "toml"}


def lang_for_path(path: str) -> str:
    p = path.lower()
    for ext, lang in _EXT.items():
        if p.endswith(ext):
            return lang
    if p.endswith(("dockerfile",)):
        return "bash"
    return ""


def norm(lang: str) -> str:
    lang = (lang or "").strip().lower()
    return ALIASES.get(lang, lang)


_KW = {
    "python": "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case",
    "javascript": "await break case catch class const continue debugger default delete do else export extends finally for from function if import in instanceof let new of return static super switch this throw try typeof var void while with yield async true false null undefined",
    "go": "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false nil",
    "rust": "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while",
    "c": "auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while bool true false NULL nullptr class public private protected virtual template typename namespace using new delete this throw try catch",
    "java": "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new package private protected public return short static super switch synchronized this throw throws try void volatile while true false null var record",
    "sql": "select from where and or not insert into values update set delete create table drop alter add join left right inner outer on group by order having limit offset as distinct union all null is in like between case when then else end primary key foreign references index view",
    "bash": "if then else elif fi for while until do done case esac function in return export local readonly echo exit set unset source",
    "powershell": "if else elseif foreach for while do until switch function param return begin process end try catch finally throw",
}
_KW["typescript"] = _KW["javascript"] + " interface type enum implements declare readonly public private protected abstract namespace keyof as is"
_KW["cpp"] = _KW["c"]
_KW["csharp"] = _KW["java"] + " using namespace string var async await get set"
_KW["kotlin"] = "fun val var class object interface if else when for while return import package true false null is in as"
_KW["swift"] = "func let var class struct enum protocol if else guard for while return import true false nil self"
_KW["ruby"] = "def end class module if elsif else unless while until do return yield true false nil self require"
_KW["php"] = "function class if else elseif foreach for while return echo new public private protected static true false null"

_BUILTINS = {
    "python": "print len range str int float dict list set tuple open isinstance enumerate zip map filter sorted sum min max any all super self cls Exception ValueError TypeError KeyError",
    "javascript": "console window document JSON Math Promise Array Object String Number Map Set Error require module process",
}
_BUILTINS["typescript"] = _BUILTINS["javascript"]

_COMMENT = {
    "python": "#", "bash": "#", "yaml": "#", "toml": "#", "ruby": "#",
    "powershell": "#", "sql": "--",
}
_C_LIKE = {"javascript", "typescript", "go", "rust", "c", "cpp", "java",
           "csharp", "kotlin", "swift", "php", "css"}

_NUM = r"\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b"
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_OPS = r"[+\-*/%=<>!&|^~?:]+"


def _builtin_highlight(line: str, lang: str, s: Style, state: dict) -> str:
    """One line; `state` carries open multi-line comments/strings."""
    if lang == "json":
        return re.sub(
            r'("(?:\\.|[^"\\])*")(\s*:)?|' + _NUM + r"|\b(true|false|null)\b",
            lambda m: (s("syn_fn" if m.group(2) else "syn_str", m.group(1)) + (m.group(2) or ""))
            if m.group(1) else s("syn_num" if m.group(3) is None else "syn_kw", m.group(0)),
            line)
    if lang in ("html", "xml"):
        def tag(m):
            inner = re.sub(r'(\s[\w:-]+)(=)("[^"]*"|\'[^\']*\')',
                           lambda a: s("syn_type", a.group(1)) + a.group(2) + s("syn_str", a.group(3)),
                           m.group(2))
            return s("syn_punct", m.group(1)) + s("syn_kw", m.group(3) or "") + inner[len(m.group(3) or ""):] + s("syn_punct", m.group(4))
        return re.sub(r"(</?)(([\w:-]+)[^<>]*?)(/?>)", tag, line)
    if lang in ("diff",):
        if line.startswith("+"):
            return s("diff_add", line)
        if line.startswith("-"):
            return s("diff_del", line)
        if line.startswith("@@"):
            return s("diff_hunk", line)
        return line
    kws = set(_KW.get(lang, "").split())
    builtins = set(_BUILTINS.get(lang, "").split())
    out: list[str] = []
    i, n = 0, len(line)
    c_like = lang in _C_LIKE
    line_comment = _COMMENT.get(lang, "//" if c_like else None)
    while i < n:
        # inside a block comment / triple-quoted string from earlier lines
        if state.get("block"):
            end_tok = state["block"]
            j = line.find(end_tok, i)
            role = state.get("block_role", "syn_com")
            if j < 0:
                out.append(s(role, line[i:]))
                return "".join(out)
            out.append(s(role, line[i:j + len(end_tok)]))
            i = j + len(end_tok)
            state["block"] = None
            continue
        ch = line[i]
        if c_like and line.startswith("/*", i):
            state["block"], state["block_role"] = "*/", "syn_com"
            continue
        if lang == "python" and line.startswith(('"""', "'''"), i):
            q = line[i:i + 3]
            j = line.find(q, i + 3)
            if j < 0:
                out.append(s("syn_str", line[i:]))
                state["block"], state["block_role"] = q, "syn_str"
                return "".join(out)
            out.append(s("syn_str", line[i:j + 3]))
            i = j + 3
            continue
        if line_comment and line.startswith(line_comment, i):
            out.append(s("syn_com", line[i:], italic=True))
            break
        if ch in "\"'`":
            j = i + 1
            while j < n and line[j] != ch:
                j += 2 if line[j] == "\\" else 1
            out.append(s("syn_str", line[i:j + 1]))
            i = j + 1
            continue
        m = re.match(_NUM, line[i:]) if (ch.isdigit()) else None
        if m and (i == 0 or not (line[i - 1].isalnum() or line[i - 1] == "_")):
            out.append(s("syn_num", m.group(0)))
            i += len(m.group(0))
            continue
        m = re.match(_IDENT, line[i:]) if (ch.isalpha() or ch == "_") else None
        if m:
            w = m.group(0)
            nxt = line[i + len(w):i + len(w) + 1]
            if w in kws or (lang == "sql" and w.lower() in kws):
                out.append(s("syn_kw", w))
            elif w in builtins:
                out.append(s("syn_builtin", w))
            elif nxt == "(":
                out.append(s("syn_fn", w))
            elif w[:1].isupper() and lang not in ("bash", "sql"):
                out.append(s("syn_type", w))
            else:
                out.append(w)
            i += len(w)
            continue
        if lang == "bash" and ch == "$":
            m = re.match(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\d|\$[@*#?$!]", line[i:])
            if m:
                out.append(s("syn_builtin", m.group(0)))
                i += len(m.group(0))
                continue
        if lang == "python" and ch == "@" and line[:i].strip() == "":
            out.append(s("syn_builtin", line[i:]))
            break
        m = re.match(_OPS, line[i:])
        if m:
            out.append(s("syn_op", m.group(0)))
            i += len(m.group(0))
            continue
        out.append(ch)
        i += 1
    if lang in ("yaml", "toml") and not state.get("block"):
        # keys before ':' / '=' in config files
        joined = "".join(out)
        return re.sub(r"^(\s*-?\s*)([\w.\-\"']+)(\s*[:=])",
                      lambda m: m.group(1) + s("syn_fn", m.group(2)) + m.group(3), joined, count=1)
    return "".join(out)


_PYG_ROLE = (("Comment", "syn_com"), ("String", "syn_str"), ("Number", "syn_num"),
             ("Keyword", "syn_kw"), ("Name.Function", "syn_fn"), ("Name.Class", "syn_type"),
             ("Name.Builtin", "syn_builtin"), ("Name.Decorator", "syn_builtin"),
             ("Name.Tag", "syn_kw"), ("Name.Attribute", "syn_type"),
             ("Operator", "syn_op"), ("Punctuation", "syn_punct"),
             ("Literal", "syn_num"), ("Generic.Inserted", "diff_add"),
             ("Generic.Deleted", "diff_del"), ("Generic.Subheading", "diff_hunk"))


def _pygments_lines(code: str, lang: str, s: Style) -> list[str] | None:
    try:                                    # pragma: no cover - optional
        from pygments import lex
        from pygments.lexers import get_lexer_by_name
        lexer = get_lexer_by_name(lang, stripnl=False, ensurenl=False)
    except Exception:                        # noqa: BLE001
        return None
    out, cur = [], ""
    for ttype, value in lex(code, lexer):
        tname = str(ttype).replace("Token.", "")
        role = next((r for prefix, r in _PYG_ROLE if tname.startswith(prefix)), None)
        parts = value.split("\n")
        for k, part in enumerate(parts):
            if k:
                out.append(cur)
                cur = ""
            cur += s(role, part) if role and part else part
    out.append(cur)
    return out


def highlight(code: str, lang: str, s: Style) -> list[str]:
    """Coloured lines for a code block (no trailing newline line)."""
    lang = norm(lang)
    lines = code.split("\n")
    if s.depth == 0 or not lang:
        return lines
    pyg = _pygments_lines(code, lang, s)
    if pyg is not None:
        return pyg[:len(lines)]
    state: dict = {}
    return [_builtin_highlight(ln, lang, s, state) for ln in lines]


class LineHighlighter:
    """Highlights a code block one line at a time while it streams in."""

    def __init__(self, lang: str, s: Style):
        self.lang = norm(lang)
        self.s = s
        self.state: dict = {}

    def line(self, ln: str) -> str:
        if self.s.depth == 0 or not self.lang:
            return ln
        return _builtin_highlight(ln, self.lang, self.s, self.state)
