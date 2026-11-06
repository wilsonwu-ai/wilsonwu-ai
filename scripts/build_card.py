#!/usr/bin/env python3
"""Build the profile card and refresh the README's live sections.

Writes assets/card-dark.svg and assets/card-light.svg (an ASCII portrait next
to a neofetch-style panel) and rewrites the marked regions of README.md.

Runs every day in GitHub Actions with the built-in GITHUB_TOKEN, so there is
no personal token to expire. Locally:

    GITHUB_TOKEN=$(gh auth token) python3 scripts/build_card.py

Standard library only. Set CARD_DATE=YYYY-MM-DD to render a fixed day.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.request
from collections import Counter
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
LOGIN = os.environ.get("PROFILE_LOGIN", "wilsonwu-ai")
TODAY = (dt.date.fromisoformat(os.environ["CARD_DATE"]) if os.environ.get("CARD_DATE")
         else dt.datetime.now(dt.timezone.utc).date())

# ---------------------------------------------------------------- hand-written facts
# Everything here is static on purpose; every number on the card comes from the API.
ABOUT = [
    ("OS", "macOS · zsh · Claude Code"),
    ("Uptime", None),  # time since joining GitHub, computed daily
    ("Host", "Georgia Tech · M.S. CS, AI (in progress)"),
    ("Firmware", "Duke MBA · TMU BComm"),
    ("Code", "Python · C · JS/TS · SQL · Bash"),
    ("Silicon", "Verilog · VHDL (working)"),
    ("Build", "evals · agent fleets · 3D web explainers"),
    ("Hack", "Sundai · Hackachusetts"),
]
DAY_JOBS = [("Founder & CEO", "Dubbs Capital"),
            ("CRO", "Snappy"),
            ("Owner", "Union Made Apparel")]
CERTS = [("Anthropic", "Claude Certified Architect, Professional"),
         ("Google Cloud", "Professional Machine Learning Engineer"),
         ("AWS", "Solutions Architect, Professional")]

# The `fortune` line quotes the first sentence of one of these repo descriptions,
# a different one each day, so the quote is always my own words, verbatim.
FORTUNES = ["sequential-at-inference", "plan-then-cull", "pyramid-principle-for-operators",
            "whetstone", "the-brain-is-a-folder", "commodity-scan", "rising-repos",
            "graph-engineering-for-operators", "eval-engineering-for-operators",
            "ai-engineering-field-notes", "the-video-is-a-program", "diy-terminal"]

HITS = [("🔌", "vibe-silicon"), ("🧪", "smoleval"), ("🧠", "reasoning-over-recall"),
        ("🕸️", "graph-engineering-kit"), ("⚡", "local-llm-decode-ceiling"), ("💧", "why-ai-needs-water")]

# Public, but not portfolio pieces (client sites, internal tools, course prep, placeholders).
SKIP_LATEST = {LOGIN, f"{LOGIN}.github.io", "braintree-warehouse", "cs6210-p3-prep",
               "bdr-performance-review", "bdr-probation-review", "sts-website",
               "fieldstock-airsoft-wholesale", "social-onboarding", "social-onboarding-dashboard",
               "basenotes", "websites", "counter-demo", "matchmaker", "grillme", "taobao-finds-oct-2026"}
LATEST_N = 5

RAMP = " .:;c" + "lisunow" + "XdKDRWMN"  # must match scripts/ascii_portrait.py

THEMES = {
    "dark": dict(
        bg="#0d1117", bar="#161b22", border="#30363d", title="#8b949e", text="#e6edf3",
        key="#79c0ff", dots="#30363d", head="#d2a8ff", user="#7ee787", host="#79c0ff",
        stat="#7ee787", quote="#e3b341", muted="#8b949e",
        tones=["#4a525c", "#6e7681", "#9da5ae", "#d6dde5", "#ffffff"],
        swatches=["#f85149", "#ffa657", "#e3b341", "#7ee787", "#56d4dd", "#79c0ff", "#d2a8ff", "#f0f6fc"]),
    "light": dict(
        bg="#ffffff", bar="#f6f8fa", border="#d0d7de", title="#57606a", text="#1f2328",
        key="#0969da", dots="#afb8c1", head="#8250df", user="#1a7f37", host="#0969da",
        stat="#1a7f37", quote="#9a6700", muted="#57606a",
        tones=["#c7ced6", "#9aa3ad", "#6e7781", "#424a53", "#1f2328"],
        swatches=["#cf222e", "#bc4c00", "#9a6700", "#1a7f37", "#1b7c83", "#0969da", "#8250df", "#24292f"]),
}

# ---------------------------------------------------------------- layout (px)
W = 1060
BAR_H = 34
MARGIN = 28
P_CW, P_LH, P_FS = 5.7, 11.09, 9.5      # portrait cell: same 0.514 aspect as ascii_portrait.py
I_CW, I_LH, I_FS = 9.0, 19.0, 15.0      # info panel: 0.6em advance at 15px
KEY_COL = 17                             # values start at this column
MAX_COLS = 58
FONT = "'SF Mono','Cascadia Code','JetBrains Mono',Menlo,Consolas,'DejaVu Sans Mono','Liberation Mono',monospace"


# ---------------------------------------------------------------- GitHub API
def gql(query: str, **variables) -> dict:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN (locally: GITHUB_TOKEN=$(gh auth token)).")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json",
                 "User-Agent": f"{LOGIN}-profile-card"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body["data"]


USER_Q = """query($login: String!) { user(login: $login) {
  createdAt
  contributionsCollection { totalCommitContributions contributionCalendar { totalContributions } }
  repositoriesContributedTo(first: 1, privacy: PUBLIC,
      contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, PULL_REQUEST_REVIEW]) { totalCount }
  publicRepos: repositories(ownerAffiliations: OWNER, privacy: PUBLIC) { totalCount }
} }"""

REPOS_Q = """query($login: String!, $after: String) { user(login: $login) {
  repositories(ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false, first: 100, after: $after,
               orderBy: {field: CREATED_AT, direction: DESC}) {
    pageInfo { hasNextPage endCursor }
    nodes { name description url homepageUrl createdAt stargazerCount primaryLanguage { name } }
  } } }"""


def fetch() -> tuple[dict, list[dict]]:
    user = gql(USER_Q, login=LOGIN)["user"]
    repos, after = [], None
    while True:
        page = gql(REPOS_Q, login=LOGIN, after=after)["user"]["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return user, repos
        after = page["pageInfo"]["endCursor"]


# ---------------------------------------------------------------- facts from data
def uptime(start: dt.date, end: dt.date) -> str:
    years, months, days = end.year - start.year, end.month - start.month, end.day - start.day
    if days < 0:
        months -= 1
        days += (end.replace(day=1) - dt.timedelta(days=1)).day
    if months < 0:
        years, months = years - 1, months + 12
    unit = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
    return f"{unit(years, 'year')}, {unit(months, 'month')}, {unit(days, 'day')}"


def first_sentence(text: str, limit: int = 104) -> str | None:
    """Leading sentence(s) of a description, kept short enough for two card lines."""
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    out = ""
    for part in parts:
        candidate = f"{out} {part}".strip()
        if len(candidate) > limit:
            break
        out = candidate
        if len(out) >= 30:
            break
    return out or None


def fortune(repos: list[dict]) -> tuple[str, str]:
    by_name = {r["name"]: r for r in repos}
    pool = [(n, s) for n in FORTUNES if n in by_name and (s := first_sentence(by_name[n]["description"]))]
    if not pool:
        return "Ship it, then measure it.", LOGIN
    name, sentence = pool[TODAY.toordinal() % len(pool)]
    return sentence, name


def wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + ([cur] if cur else [])


# ---------------------------------------------------------------- SVG
Seg = tuple[str, str]  # (text, css class)


def kv(key: str, value: str, value_cls: str = "v") -> list[Seg]:
    prefix = f"{key}: "
    dots = "." * max(1, KEY_COL - 1 - len(prefix))
    return [(key, "k"), (": ", "m"), (dots, "d"), (" ", "v"), (value, value_cls)]


def section(title: str) -> list[Seg]:
    head = f"- {title} "
    return [("- ", "m"), (title, "h"), (" " + "-" * (MAX_COLS - len(head) - 1), "m")]


def panel_lines(user: dict, repos: list[dict]) -> list[tuple[str, object]]:
    """('gap', None) = half-line spacer, ('line', segs), ('swatches', None)."""
    created = dt.date.fromisoformat(user["createdAt"][:10])
    langs = Counter(r["primaryLanguage"]["name"] for r in repos if r.get("primaryLanguage"))
    short = {"JavaScript": "JS", "TypeScript": "TS", "Jupyter Notebook": "Jupyter"}
    top = " · ".join(f"{short.get(name, name)} {n}" for name, n in langs.most_common(3))
    contrib = user["contributionsCollection"]
    quote, source = fortune(repos)

    out: list[tuple[str, object]] = [
        ("line", [("wilson", "u"), ("@", "m"), ("wu-ai", "o")]),
        ("line", [("-" * len("wilson@wu-ai"), "m")]),
    ]
    for key, value in ABOUT:
        out.append(("line", kv(key, value if value else f"{uptime(created, TODAY)} on GitHub")))
    out += [("gap", None), ("line", section("Day jobs"))]
    out += [("line", kv(k, v)) for k, v in DAY_JOBS]
    out += [("gap", None), ("line", section("Certs"))]
    out += [("line", kv(k, v)) for k, v in CERTS]
    out += [("gap", None), ("line", section("GitHub Stats"))]
    out += [
        ("line", kv("Repos", f"{len(repos)} original · {user['publicRepos']['totalCount']} public · "
                             f"{user['repositoriesContributedTo']['totalCount']} contributed", "s")),
        ("line", kv("Contributions", f"{contrib['contributionCalendar']['totalContributions']:,} in the last year"
                                     f" · {contrib['totalCommitContributions']:,} commits", "s")),
        ("line", kv("Top languages", f"{top} (by repo)", "s")),
        ("gap", None), ("swatches", None), ("gap", None),
        ("line", [("wilson@wu-ai", "u"), (" ~ ", "o"), ("% ", "m"), ("fortune", "v")]),
    ]
    quoted = wrap(f'"{quote}"', MAX_COLS - 2)
    for i, text in enumerate(quoted):
        segs: list[Seg] = [(text, "q")]
        if i == len(quoted) - 1:
            if len(text) + len(source) + 5 <= MAX_COLS:
                segs.append(("  -- " + source, "m"))
            else:
                out.append(("line", segs))
                segs = [("  -- " + source, "m")]
        out.append(("line", segs))
    out.append(("line", [("wilson@wu-ai", "u"), (" ~ ", "o"), ("% ", "m"), ("CURSOR", "cursor")]))
    return out


def text_el(x: float, y: float, segs: list[Seg], cw: float, cls: str, delay: float) -> str:
    plain = "".join(t for t, _ in segs)
    if cls == "l" and len(plain) > MAX_COLS:
        raise SystemExit(f"card line is {len(plain)} chars (max {MAX_COLS}): {plain!r}")
    spans = "".join(f'<tspan class="{c}">{escape(t)}</tspan>' for t, c in segs)
    return (f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" textLength="{len(plain) * cw:.1f}" '
            f'lengthAdjust="spacing" style="animation-delay:{delay:.2f}s">{spans}</text>')


def portrait_rows(lines: list[str], x0: float, y0: float) -> list[str]:
    rows = []
    levels = len(RAMP) - 1
    for r, line in enumerate(lines):
        stripped = line.rstrip()
        lead = len(stripped) - len(stripped.lstrip())
        body = stripped[lead:]
        if not body:
            continue
        runs: list[Seg] = []
        for ch in body:
            tone = None if ch == " " else f"t{min(4, (RAMP.index(ch) - 1) * 5 // levels)}"
            if runs and (tone is None or runs[-1][1] == tone):
                runs[-1] = (runs[-1][0] + ch, runs[-1][1])
            else:
                runs.append((ch, tone or "t0"))
        rows.append(text_el(x0 + lead * P_CW, y0 + (r + 1) * P_LH - 2, runs, P_CW, "p", 0.10 + r * 0.02))
    return rows


def render(theme: str, user: dict, repos: list[dict]) -> str:
    t = THEMES[theme]
    portrait = (ASSETS / f"portrait-{theme}.txt").read_text().splitlines()
    p_w, p_h = len(max(portrait, key=len)) * P_CW, len(portrait) * P_LH
    info_x = MARGIN + p_w + 30
    top = BAR_H + 56

    body, y, n = [], top + 14, 0
    for kind, segs in panel_lines(user, repos):
        if kind == "gap":
            y += I_LH / 2
            continue
        delay = 0.35 + n * 0.055
        if kind == "swatches":
            for i, color in enumerate(t["swatches"]):
                body.append(f'<rect x="{info_x + i * 30:.1f}" y="{y - 12:.1f}" width="26" height="14" rx="2" '
                            f'fill="{color}" class="l" style="animation-delay:{delay:.2f}s"/>')
        elif segs[-1][1] == "cursor":
            prompt = segs[:-1]
            body.append(text_el(info_x, y, prompt, I_CW, "l", delay))
            cx = info_x + len("".join(s for s, _ in prompt)) * I_CW + 2
            body.append(f'<rect x="{cx:.1f}" y="{y - 13:.1f}" width="9" height="17" fill="{t["user"]}" class="c"/>')
        else:
            body.append(text_el(info_x, y, segs, I_CW, "l", delay))
        y += I_LH
        n += 1
    info_bottom = y
    height = max(top + p_h, info_bottom) + MARGIN
    p_y = top + max(0.0, (info_bottom - top - p_h) / 2)

    css = f"""
text{{font-family:{FONT};white-space:pre}}
.p{{font-size:{P_FS}px}} .l{{font-size:{I_FS}px}}
.t0{{fill:{t['tones'][0]}}} .t1{{fill:{t['tones'][1]}}} .t2{{fill:{t['tones'][2]}}} .t3{{fill:{t['tones'][3]}}} .t4{{fill:{t['tones'][4]}}}
.k{{fill:{t['key']}}} .v{{fill:{t['text']}}} .d{{fill:{t['dots']}}} .h{{fill:{t['head']};font-weight:700}}
.u{{fill:{t['user']};font-weight:700}} .o{{fill:{t['host']};font-weight:700}} .m{{fill:{t['muted']}}}
.s{{fill:{t['stat']}}} .q{{fill:{t['quote']};font-style:italic}}
@keyframes in{{from{{opacity:.3}}to{{opacity:1}}}}
@keyframes blink{{50%{{opacity:0}}}}
.p,.l{{animation:in .4s ease-out both}}
.c{{animation:blink 1.1s steps(1) infinite}}
@media (prefers-reduced-motion:reduce){{.p,.l,.c{{animation:none}}}}
"""
    title = f"{LOGIN}: ~ (zsh)"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height:.0f}" viewBox="0 0 {W} {height:.0f}" '
        f'role="img" aria-labelledby="ttl dsc" xml:space="preserve">',
        f'<title id="ttl">Wilson Wu, GitHub profile card</title>',
        f'<desc id="dsc">An ASCII portrait of Wilson Wu made of letters, beside a neofetch-style panel: '
        f'Georgia Tech M.S. CS, Founder and CEO of Dubbs Capital, CRO of Snappy, certifications, and GitHub stats '
        f'refreshed {TODAY.isoformat()}.</desc>',
        f"<style>{css}</style>",
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{height - 1:.0f}" rx="12" fill="{t["bg"]}" stroke="{t["border"]}"/>',
        f'<path d="M0.5 {BAR_H} V12.5 A12 12 0 0 1 12.5 0.5 H{W - 12.5} A12 12 0 0 1 {W - 0.5} 12.5 V{BAR_H} Z" '
        f'fill="{t["bar"]}" stroke="{t["border"]}"/>',
        *[f'<circle cx="{20 + i * 20}" cy="{BAR_H / 2}" r="6" fill="{c}"/>'
          for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840"))],
        f'<text x="{W / 2}" y="{BAR_H / 2 + 5}" text-anchor="middle" style="font-size:13px;fill:{t["title"]}">'
        f"{escape(title)}</text>",
        f'<text x="{W - 16}" y="{BAR_H / 2 + 5}" text-anchor="end" style="font-size:11px;fill:{t["muted"]}">'
        f"refreshed {TODAY.isoformat()}</text>",
        text_el(MARGIN, BAR_H + 30, [("wilson@wu-ai", "u"), (" ~ ", "o"), ("% ", "m"), ("neofetch", "v")],
                I_CW, "l", 0.0),
        *portrait_rows(portrait, MARGIN, p_y),
        *body,
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- README
def md_escape(text: str) -> str:
    return (text or "").replace("|", "/").replace("\n", " ").strip()


def clip(text: str, limit: int = 120) -> str:
    """Whole sentences when they fit, otherwise a word-boundary cut with an ellipsis."""
    text = md_escape(text)
    if len(text) <= limit:
        return f"{text}." if text[-1:].isalnum() else text  # some descriptions skip the final period
    kept = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    if len(kept) >= 60:
        return kept
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def link_cell(repo: dict) -> str:
    cell = f"[**{repo['name']}**]({repo['url']})"
    if repo.get("homepageUrl"):
        cell += f" · [live]({repo['homepageUrl']})"
    return cell


def latest_md(repos: list[dict]) -> str:
    rows = [r for r in repos if r["name"] not in SKIP_LATEST and r.get("description")][:LATEST_N]
    lines = ["| Shipped | Project | What it is |", "|:--|:--|:--|"]
    day = lambda r: dt.date.fromisoformat(r["createdAt"][:10])
    lines += [f"| {day(r):%b} {day(r).day} | {link_cell(r)} | {clip(r['description'])} |" for r in rows]
    return "\n".join(lines)


def hits_md(repos: list[dict]) -> str:
    by_name = {r["name"]: r for r in repos}
    lines = ["| | Project | What it is |", "|:-:|:--|:--|"]
    for emoji, name in HITS:
        if name in by_name and by_name[name].get("description"):
            r = by_name[name]
            lines.append(f"| {emoji} | {link_cell(r)} | {clip(r['description'], 150)} |")
    return "\n".join(lines)


def replace_block(doc: str, tag: str, content: str) -> str:
    pattern = re.compile(rf"(<!-- {tag}:START -->).*?(<!-- {tag}:END -->)", re.S)
    if not pattern.search(doc):
        raise SystemExit(f"README.md is missing the {tag} markers")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(2)}", doc)


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    return True


def main() -> None:
    user, repos = fetch()
    changed = [str(p.relative_to(ROOT)) for p, text in (
        (ASSETS / "card-dark.svg", render("dark", user, repos)),
        (ASSETS / "card-light.svg", render("light", user, repos)),
    ) if write_if_changed(p, text)]
    doc = README.read_text()
    doc = replace_block(doc, "LATEST", latest_md(repos))
    doc = replace_block(doc, "HITS", hits_md(repos))
    if write_if_changed(README, doc):
        changed.append("README.md")
    print(f"{TODAY}: {len(repos)} original repos; changed: {', '.join(changed) or 'nothing'}")


if __name__ == "__main__":
    main()
