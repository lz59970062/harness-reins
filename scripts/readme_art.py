"""Render the README hero and workflow figures as light and dark SVGs.

Usage: python scripts/readme_art.py [out-dir]   (default: assets/readme)

One palette for every README figure: neutral ink, one blue accent, GitHub's own
page colours as the surface so the cards sit flush in either theme. The README
switches between the two files with <picture> and prefers-color-scheme.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SANS = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
THEMES = {
    "light": {"surface": "#ffffff", "card": "#f6f8fa", "border": "#d8dee4", "ink": "#1f2328", "ink2": "#59636e",
              "muted": "#818b98", "accent": "#2a78d6", "accent_wash": "#ddf4ff", "ok": "#1a7f37"},
    "dark": {"surface": "#0d1117", "card": "#151b23", "border": "#30363d", "ink": "#f0f6fc", "ink2": "#c3cbd4",
             "muted": "#8b949e", "accent": "#3987e5", "accent_wash": "#0c2d4f", "ok": "#3fb950"},
}


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def frame(t: dict, w: int, h: int, title: str, desc: str, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-labelledby="t d" font-family="{SANS}">\n<title id="t">{esc(title)}</title>\n<desc id="d">{esc(desc)}</desc>\n'
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="14" fill="{t["surface"]}" stroke="{t["border"]}"/>\n'
            f'{body}\n</svg>\n')


def hero(t: dict) -> str:
    lines = [  # a session's view of one long task, in plain ink with one accent
        ("ink", "$ reins run -- python train.py"),
        ("muted", "reins: submitted managed task 7f3a91c2"),
        ("muted", "… the agent ends its turn; nothing polls …"),
        ("accent", "[reins] 7f3a91c2-c1"),
        ("ink", "Result: exited 0 · 6h 12m 08s"),
        ("ink", "ACK after inspection: reins ack 7f3a91c2-c1"),
    ]
    term = [f'<rect x="604" y="58" width="556" height="244" rx="10" fill="{t["card"]}" stroke="{t["border"]}"/>',
            f'<text x="628" y="88" font-size="12" font-family="{MONO}" fill="{t["muted"]}">same session, six hours later</text>',
            f'<line x1="604" x2="1160" y1="102" y2="102" stroke="{t["border"]}"/>']
    y = 132
    for role, text in lines:
        if role == "accent":
            term.append(f'<rect x="618" y="{y - 17}" width="3" height="22" rx="1.5" fill="{t["accent"]}"/>')
        weight = ' font-weight="600"' if role == "accent" else ""
        term.append(f'<text x="632" y="{y}" font-size="15" font-family="{MONO}"{weight} fill="{t["ink" if role == "accent" else role]}">{esc(text)}</text>')
        y += 30 if role != "muted" or text.startswith("…") else 30
    body = f'''
<text x="56" y="88" font-size="13" font-weight="600" letter-spacing="1.6" fill="{t["accent"]}">A HARNESS FOR YOUR HARNESSES</text>
<text x="54" y="146" font-size="56" font-weight="700" fill="{t["ink"]}">Harness Reins</text>
<text x="56" y="190" font-size="19" fill="{t["ink2"]}">Hand long work and child agents to an independent</text>
<text x="56" y="216" font-size="19" fill="{t["ink2"]}">owner, then resume the exact session that asked.</text>
<text x="56" y="272" font-size="14" fill="{t["muted"]}">Codex · Claude Code · Pi   —   Linux · macOS · Windows</text>
{chr(10).join(term)}'''
    return frame(t, 1200, 360, "Harness Reins (reins)",
                 "Project title beside a terminal card: a long task is submitted, the agent stops, and six hours later the "
                 "same session receives the result and acknowledges it.", body)


def wrap(text: str, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + (1 if line else 0) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return lines + ([line] if line else [])


WORKFLOW = {
    "en": {
        "steps": [
            ("Submit", "The agent calls reins run or reins agent; the task is durable before it returns.", "session bound"),
            ("Own", "The OS starts a separate task service, not a child of the agent or the daemon.", "OS-native owner"),
            ("Run", "The job runs for minutes or days. The agent has ended its turn; nothing polls.", "no model calls"),
            ("Queue", "On exit, Reins stores the result and queues one callback for that session.", "at-least-once"),
            ("Resume", "The same session wakes, inspects the result and runs reins ack.", "explicit ACK"),
        ],
        "eyebrow": "HOW A CALLBACK WORKS",
        "sub": "Codex resumes with codex exec resume · Claude Code with a background reins wait · Pi through its extension",
        "loop": "more to do? the agent submits the next stage and the loop repeats",
    },
    "zh": {
        "steps": [
            ("提交", "Agent 调用 reins run 或 reins agent，返回之前任务已经落盘。", "绑定原会话"),
            ("托管", "由操作系统启动独立的任务服务，不是 Agent 或守护进程的子进程。", "系统原生托管"),
            ("运行", "任务跑几分钟到几天。Agent 已结束回合，没有任何轮询。", "零模型调用"),
            ("排队", "任务退出后，Reins 保存结果，为那个会话排入一条回调。", "至少投递一次"),
            ("恢复", "同一个会话醒来，检查结果，执行 reins ack。", "显式 ACK"),
        ],
        "eyebrow": "一次回调怎么走完",
        "sub": "Codex 通过 codex exec resume 恢复 · Claude Code 通过后台 reins wait · Pi 通过扩展",
        "loop": "还有下一步？Agent 再提交一次，循环继续",
    },
}


def text_width(text: str, size: float) -> float:
    return sum(size * (1.0 if ord(ch) > 0x2E80 else 0.55) for ch in text)


def wrap_width(text: str, size: float, limit: float) -> list[str]:
    """Wrap Latin text at spaces and CJK text between characters, by rendered width."""
    tokens = re.findall(r"[\u2e80-\uffff]|[^\s\u2e80-\uffff]+|\s+", text)
    lines, line = [], ""
    for token in tokens:
        candidate = line + token
        # Closing punctuation never starts a line; it stays with the text before it.
        if line and not token.isspace() and token not in "，。、；：？！）」』%" and text_width(candidate.rstrip(), size) > limit:
            lines.append(line.rstrip())
            line = token
        elif not (token.isspace() and not line):
            line = candidate
    return lines + ([line.rstrip()] if line.strip() else [])


def workflow(t: dict, lang: str = "en") -> str:
    words = WORKFLOW[lang]
    out, x, cw, gap = [], 40, 204, 25
    for index, (name, text, foot) in enumerate(words["steps"]):
        last = index == len(words["steps"]) - 1
        out.append(f'<rect x="{x}" y="92" width="{cw}" height="182" rx="10" fill="{t["accent_wash"] if last else t["card"]}" '
                   f'stroke="{t["accent"] if last else t["border"]}"/>')
        out.append(f'<text x="{x + 18}" y="124" font-size="12" font-weight="600" font-family="{MONO}" fill="{t["accent"]}">{index + 1:02d}</text>')
        out.append(f'<text x="{x + 46}" y="124" font-size="16" font-weight="650" fill="{t["ink"]}">{esc(name)}</text>')
        for row, line in enumerate(wrap_width(text, 13.5, cw - 36)):
            out.append(f'<text x="{x + 18}" y="{156 + row * 21}" font-size="13.5" fill="{t["ink2"]}">{esc(line)}</text>')
        out.append(f'<text x="{x + 18}" y="256" font-size="11.5" font-family="{MONO}" fill="{t["muted"]}">{esc(foot)}</text>')
        if not last:
            ax = x + cw + 4
            out.append(f'<path d="M{ax},183 L{ax + gap - 9},183" stroke="{t["muted"]}" stroke-width="1.5"/>')
            out.append(f'<path d="M{ax + gap - 10},178 L{ax + gap - 4},183 L{ax + gap - 10},188 Z" fill="{t["muted"]}"/>')
        x += cw + gap
    # The goal loop: from Resume back to Submit, solid hairline, its label on a surface patch.
    lx0, lx1 = 40 + cw / 2, 40 + 4 * (cw + gap) + cw / 2
    half = text_width(words["loop"], 13) / 2 + 14
    out.append(f'<path d="M{lx1},274 L{lx1},314 L{lx0},314 L{lx0},282" fill="none" stroke="{t["muted"]}" stroke-width="1.5"/>')
    out.append(f'<path d="M{lx0 - 5},288 L{lx0},281 L{lx0 + 5},288 Z" fill="{t["muted"]}"/>')
    out.append(f'<rect x="{(lx0 + lx1) / 2 - half:.1f}" y="303" width="{2 * half:.1f}" height="22" fill="{t["surface"]}"/>')
    out.append(f'<text x="{(lx0 + lx1) / 2}" y="319" font-size="13" fill="{t["ink2"]}" text-anchor="middle">{esc(words["loop"])}</text>')
    head = (f'<text x="40" y="56" font-size="13" font-weight="600" letter-spacing="1.6" fill="{t["accent"]}">{esc(words["eyebrow"])}</text>'
            f'\n<text x="40" y="76" font-size="13" fill="{t["muted"]}">{esc(words["sub"])}</text>')
    return frame(t, 1200, 350, "How a Harness Reins callback works",
                 "Five steps: submit, own, run, queue, resume; a loop returns from resume to submit for the next stage.",
                 head + "\n" + "\n".join(out))


TUI = {
    "en": {
        "eyebrow": "NEW IN 0.14 · REINS TUI",
        "sub": "Every Claude Code, Codex and Pi session on the machine, with the work it started, in one terminal",
        "rows": [
            ("●", "★ @trainer Sweep ViT lr", "here", "claude", "1"),
            ("●", "@reviewer API refactor", "background", "codex", ""),
            ("●", "Clean the survey data", "here", "pi", "2"),
            ("○", "Paper figures", "Desktop", "claude", ""),
            ("○", "Kimi benchmark round 5", "terminal", "codex", "1"),
            ("◌", "Release notes draft", "closed", "claude", ""),
            ("◌", "Literature survey", "DSH app", "dsh", "1"),
            ("◌", "Fix the data loader", "closed", "codex", ""),
        ],
        "pane": "@trainer · Sweep ViT lr",
        "chat": [
            ("prompt", "run the lr sweep and tell me which one wins"),
            ("ink", "● reins run --task \"lr sweep\" -- python sweep.py"),
            ("muted", "  reins: submitted managed task 7f3a91c2"),
            ("ink2", "  It runs on its own; I'll pick it up when it ends."),
            ("gap", ""),
            ("note", "… the turn ends; nothing polls for 3h 41m …"),
            ("gap", ""),
            ("callback", "[reins] 7f3a91c2-c1"),
            ("ink", "Result: exited 0 · 3h 41m 12s"),
            ("gap", ""),
            ("ink", "● lr 3e-4 wins: 82.4% top-1, +1.9 over 1e-3."),
            ("muted", "  reins ack 7f3a91c2-c1"),
        ],
        "bar": "Alt+arrows move · Alt+x close a pane · Alt+d leave, it keeps running · Alt+q asks first",
        "title": "reins tui: the Reins panel beside an open session",
        "desc": "A panel lists Claude Code, Codex, Pi and DeepSeek Harness sessions with where each runs and its running tasks; "
                "beside it one session is open in its own interface, where a long task was submitted and its "
                "result arrived in the same conversation hours later.",
    },
    "zh": {
        "eyebrow": "0.14 新功能 · REINS TUI",
        "sub": "本机所有 Claude Code、Codex、Pi 会话和它们发起的工作，集中在一个终端里",
        "rows": [
            ("●", "★ @trainer ViT 学习率扫描", "here", "claude", "1"),
            ("●", "@reviewer API 重构审查", "background", "codex", ""),
            ("●", "清洗调研数据", "here", "pi", "2"),
            ("○", "论文插图", "Desktop", "claude", ""),
            ("○", "Kimi 基准第 5 轮", "terminal", "codex", "1"),
            ("◌", "发版说明草稿", "closed", "claude", ""),
            ("◌", "文献调研", "DSH app", "dsh", "1"),
            ("◌", "修数据加载器", "closed", "codex", ""),
        ],
        "pane": "@trainer · ViT 学习率扫描",
        "chat": [
            ("prompt", "跑一下学习率扫描，告诉我哪个最好"),
            ("ink", "● reins run --task \"lr sweep\" -- python sweep.py"),
            ("muted", "  reins: submitted managed task 7f3a91c2"),
            ("ink2", "  任务独立运行，结束后我再接着看。"),
            ("gap", ""),
            ("note", "… 回合结束，3 小时 41 分钟里没有任何轮询 …"),
            ("gap", ""),
            ("callback", "[reins] 7f3a91c2-c1"),
            ("ink", "Result: exited 0 · 3h 41m 12s"),
            ("gap", ""),
            ("ink", "● 3e-4 最好：top-1 82.4%，比 1e-3 高 1.9 点。"),
            ("muted", "  reins ack 7f3a91c2-c1"),
        ],
        "bar": "Alt+方向键 切换窗格 · Alt+x 关闭窗格 · Alt+d 离开（会话照常运行）· Alt+q 先询问",
        "title": "reins tui：Reins 面板和旁边打开的会话",
        "desc": "左侧面板列出 Claude Code、Codex、Pi 和 DeepSeek Harness 会话、各自在哪里运行和正在跑的任务；右侧在会话自己的界面里打开其中一个，"
                "长任务提交后数小时，结果回到同一个对话。",
    },
}


def tui(t: dict, lang: str = "en") -> str:
    words = TUI[lang]
    mono = f'font-family="{MONO}"'
    out = [
        f'<text x="40" y="56" font-size="13" font-weight="600" letter-spacing="1.6" fill="{t["accent"]}">{esc(words["eyebrow"])}</text>',
        f'<text x="40" y="78" font-size="14" fill="{t["muted"]}">{esc(words["sub"])}</text>',
        # The terminal window, with the panel on the left and one session pane on the right.
        f'<rect x="40" y="100" width="1120" height="484" rx="10" fill="{t["card"]}" stroke="{t["border"]}"/>',
        f'<line x1="580" x2="580" y1="101" y2="548" stroke="{t["border"]}"/>',
        f'<line x1="41" x2="1159" y1="548" y2="548" stroke="{t["border"]}"/>',
        f'<text x="600" y="571" font-size="12" {mono} fill="{t["muted"]}" text-anchor="middle">{esc(words["bar"])}</text>',
        # Panel.
        f'<text x="62" y="138" font-size="16" font-weight="700" fill="{t["ink"]}">Reins</text>',
        f'<circle cx="446" cy="133.5" r="4" fill="{t["ok"]}"/>',
        f'<text x="562" y="138" font-size="12.5" {mono} fill="{t["ok"]}" text-anchor="end">Reins is ready</text>',
    ]
    x = 62
    for index, tab in enumerate(("Sessions", "Tasks", "Goals", "Messages")):
        width = len(tab) * 7.8
        weight = ' font-weight="600"' if index == 0 else ""
        out.append(f'<text x="{x}" y="174" font-size="13" {mono} fill="{t["ink" if index == 0 else "muted"]}"{weight}>{tab}</text>')
        if index == 0:
            out.append(f'<rect x="{x}" y="182" width="{width:.0f}" height="2.5" rx="1" fill="{t["accent"]}"/>')
        x += width + 22
    out.append(f'<line x1="41" x2="579" y1="190" y2="190" stroke="{t["border"]}"/>')
    cols = (62, 84, 340, 438, 524)
    for col, head in zip(cols[1:], ("Session", "Where", "Agent", "Tasks")):
        out.append(f'<text x="{col}" y="216" font-size="12" {mono} fill="{t["muted"]}">{head}</text>')
    y = 248
    for index, (mark, title, where, agent, tasks) in enumerate(words["rows"]):
        dim = where in ("closed", "DSH app")
        if index == 0:
            out.append(f'<rect x="50" y="{y - 20}" width="520" height="30" rx="6" fill="{t["accent_wash"]}"/>')
            out.append(f'<rect x="50" y="{y - 20}" width="3" height="30" rx="1.5" fill="{t["accent"]}"/>')
        ink = t["muted"] if dim else t["ink"]
        mark_fill = t["accent"] if mark == "●" else t["muted"]
        out.append(f'<text x="{cols[0]}" y="{y}" font-size="13" {mono} fill="{mark_fill}">{mark}</text>')
        out.append(f'<text x="{cols[1]}" y="{y}" font-size="13.5" {mono} fill="{ink}">{esc(title)}</text>')
        out.append(f'<text x="{cols[2]}" y="{y}" font-size="13" {mono} fill="{t["muted"] if dim else t["ink2"]}">{where}</text>')
        out.append(f'<text x="{cols[3]}" y="{y}" font-size="13" {mono} fill="{t["muted"] if dim else t["ink2"]}">{agent}</text>')
        out.append(f'<text x="{cols[4] + 12}" y="{y}" font-size="13" {mono} fill="{ink}" text-anchor="middle">{tasks}</text>')
        y += 34
    keys = [("Enter", "Open"), ("n", "New"), ("t", "Shell"), ("m", "Msg"), ("o", "Menu")]
    x = 62
    for key, label in keys:
        out.append(f'<text x="{x}" y="528" font-size="12.5" {mono}><tspan fill="{t["accent"]}" font-weight="600">{key}</tspan>'
                   f'<tspan fill="{t["ink2"]}"> {label}</tspan></text>')
        x += (len(key) + len(label) + 3) * 7.6
    # Session pane: its own interface, titled with the session's address and name.
    out.append(f'<text x="604" y="138" font-size="14.5" font-weight="650" fill="{t["ink"]}">{esc(words["pane"])}</text>')
    out.append(f'<text x="1138" y="138" font-size="12" {mono} fill="{t["muted"]}" text-anchor="end">claude attach · one conversation</text>')
    out.append(f'<line x1="581" x2="1159" y1="152" y2="152" stroke="{t["border"]}"/>')
    y = 186
    for role, text in words["chat"]:
        if role == "gap":
            y += 10
            continue
        if role == "prompt":
            out.append(f'<text x="604" y="{y}" font-size="13.5" {mono}><tspan fill="{t["muted"]}">&gt; </tspan>'
                       f'<tspan fill="{t["ink"]}">{esc(text)}</tspan></text>')
        elif role == "callback":
            out.append(f'<rect x="596" y="{y - 17}" width="3" height="22" rx="1.5" fill="{t["accent"]}"/>')
            out.append(f'<text x="608" y="{y}" font-size="13.5" font-weight="600" {mono} fill="{t["ink"]}">{esc(text)}</text>')
        elif role == "note":
            out.append(f'<text x="604" y="{y}" font-size="13" font-style="italic" fill="{t["muted"]}">{esc(text)}</text>')
        else:
            out.append(f'<text x="604" y="{y}" font-size="13.5" {mono} fill="{t[role]}">{esc(text)}</text>')
        y += 28
    return frame(t, 1200, 604, words["title"], words["desc"], "\n".join(out))


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "assets" / "readme")
    out.mkdir(parents=True, exist_ok=True)
    for mode, t in THEMES.items():
        (out / f"hero-{mode}.svg").write_text(hero(t))
        (out / f"workflow-{mode}.svg").write_text(workflow(t))
        (out / f"workflow-zh-{mode}.svg").write_text(workflow(t, "zh"))
        (out / f"tui-{mode}.svg").write_text(tui(t))
        (out / f"tui-zh-{mode}.svg").write_text(tui(t, "zh"))
    print(f"wrote hero, workflow and tui (light, dark) to {out}")


if __name__ == "__main__":
    main()
