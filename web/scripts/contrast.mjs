// contrast.mjs — proves every text colour in tokens.css is readable. Exit code 1 if any pair
// fails. Rule: WCAG 2 AA — text 4.5:1, accent used as text 4.5:1, text on accent buttons 4.5:1.
// Usage: node contrast.mjs [path/to/tokens.css] (default src/theme/tokens.css)
import { readFileSync } from "node:fs";

const file = process.argv[2] ?? "src/theme/tokens.css";
const css = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

function block(selectorStart) {
  const i = css.indexOf(selectorStart);
  if (i < 0) throw new Error(`no block starting "${selectorStart}" in ${file}`);
  const body = css.slice(css.indexOf("{", i) + 1, css.indexOf("}", i));
  const vars = {};
  for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) vars[m[1]] = m[2].trim();
  return vars;
}
function parse(c) {
  c = c.trim();
  let m = c.match(/^#([0-9a-f]{6})$/i);
  if (m) return [0, 2, 4].map((k) => parseInt(m[1].slice(k, k + 2), 16)).concat(1);
  m = c.match(/^rgba?\(([^)]+)\)$/i);
  if (m) { const p = m[1].split(",").map(Number); return [p[0], p[1], p[2], p[3] ?? 1]; }
  throw new Error(`can't read colour "${c}"`);
}
const over = (fg, bg) => [0, 1, 2].map((k) => Math.round(fg[k] * fg[3] + bg[k] * (1 - fg[3]))).concat(1);
const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const L = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
const ratio = (a, b) => { const x = L(a), y = L(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };

const looks = {
  graphite: { vars: block(':root, html[data-look="graphite"]'), dark: true },
  soft: { vars: block('html[data-look="soft"]'), dark: false },
  ink: { vars: block('html[data-look="ink"]'), dark: true },
};
const accents = {};
for (const a of ["teal", "violet", "amber", "blue"]) accents[a] = block(`html[data-accent="${a}"]`);
const onLight = "#FFFFFF";

let fails = 0;
const rows = [];
function check(what, fg, bg, min) {
  const r = ratio(fg, bg);
  const ok = r >= min;
  if (!ok) fails++;
  rows.push(`${ok ? "ok  " : "FAIL"} ${r.toFixed(2).padStart(5)}:1 (need ${min}) ${what}`);
}
for (const [name, { vars, dark }] of Object.entries(looks)) {
  const win = parse(vars["--win"]);
  const sheet = over(parse(vars["--sheet"]), win);
  const surfaces = {
    sheet,
    raised: over(parse(vars["--raised"]), sheet),
    "raised-2": over(parse(vars["--raised-2"]), sheet),
  };
  for (const [sn, s] of Object.entries(surfaces))
    for (const t of ["--text", "--text-2", "--muted"])
      check(`${name}: ${t} on ${sn}`, parse(vars[t]), s, 4.5);
  for (const [an, a] of Object.entries(accents)) {
    const acc = parse(dark ? a["--acc-dark"] : a["--acc-light"]);
    const on = parse(dark ? a["--on-acc-dark"] : onLight);
    check(`${name}/${an}: button text on accent`, on, acc, 4.5);
    check(`${name}/${an}: accent as text on sheet`, acc, sheet, 4.5);
  }
}
console.log(rows.join("\n"));
console.log(fails ? `\n${fails} pair(s) fail` : "\nall pairs pass");
process.exit(fails ? 1 : 0);
