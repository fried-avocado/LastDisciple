#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 app/ui/index.html 提取颜色与特效样式，生成「赛博朋克」主题包。

用法：
    python dev-tools/extract_theme.py

处理逻辑：
1. 解析 <style> 内的 CSS（支持 @media 嵌套）；
2. 将 :root 中颜色/特效 token 与所有 [data-theme=...] 规则移入主题包 CSS；
   布局/排版/过渡（--font-*、--font-size-*、--line-height-*、
   --letter-spacing-*、--transition-*）保留在基础样式中；
3. 基础样式中剩余的颜色字面量统一替换为 var(--token, 原值) 形式，
   已知语义色板映射到 --neon-* 等既有 token，其余生成 --cp-N token；
4. 为基础样式中已迁移 token 的 var() 引用注入字面量回退值；
5. 生成 app/ui/themes/cyberpunk/{theme.json, theme.css}。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(ROOT, "app", "ui")
HTML_FILE = os.path.join(UI_DIR, "index.html")
THEME_DIR = os.path.join(UI_DIR, "themes", "cyberpunk")

# 保留在基础样式（布局/排版/动效）中的 :root token
BASE_TOKENS_EXACT = {"--font-tech", "--font-main"}
BASE_TOKEN_PREFIXES = ("--font-size", "--line-height", "--letter-spacing", "--transition")

# 颜色字面量正则：#hex(3/4/6/8)、rgba()/rgb()、hsla()/hsl()（不含嵌套括号）
COLOR_RE = re.compile(
    r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{4}\b|#[0-9a-fA-F]{3}\b"
    r"|rgba?\([^()]*\)|hsla?\([^()]*\)"
)
VAR_NOFALLBACK_RE = re.compile(r"var\(\s*(--[A-Za-z0-9-]+)\s*\)")


def norm_color(value):
    """颜色字面量归一化（用于映射匹配）：去空白、转小写。"""
    return re.sub(r"\s+", "", value).lower()


def skip_comment(css, i):
    """返回 comment 结束位置（含结尾），若不是注释返回 i。"""
    if css.startswith("/*", i):
        end = css.find("*/", i + 2)
        return len(css) if end == -1 else end + 2
    return i


def find_matching_brace(css, open_idx):
    """返回与 css[open_idx]=='{' 匹配的 '}' 下标（跳过注释）。"""
    depth = 0
    i = open_idx
    n = len(css)
    while i < n:
        i = skip_comment(css, i)
        if i >= n:
            break
        c = css[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("花括号不匹配")


def parse_blocks(css):
    """解析顶层 CSS 块列表。

    返回 [(kind, sel, body, raw)]，kind ∈ {'rule','media'}；
    media 的 body 为子块列表（递归结构），rule 的 body 为字符串。
    raw 包含块前导空白。
    """
    blocks = []
    i = 0
    n = len(css)
    while i < n:
        seg_start = i
        sel_start = None
        while i < n:
            j = skip_comment(css, i)
            if j != i:
                i = j
                continue
            if css[i] == "{":
                break
            if sel_start is None and not css[i].isspace():
                sel_start = i
            i += 1
        if i >= n:
            if css[seg_start:].strip():
                blocks.append(("text", "", "", css[seg_start:]))
            break
        open_idx = i
        close_idx = find_matching_brace(css, open_idx)
        selector = css[sel_start or seg_start:open_idx].strip()
        leading = css[seg_start:sel_start or seg_start]
        inner = css[open_idx + 1:close_idx]
        raw = leading + selector + " {" + inner + "}"
        if selector.startswith("@media"):
            blocks.append(("media", selector, parse_blocks(inner), raw))
        else:
            blocks.append(("rule", selector, inner, raw))
        i = close_idx + 1
    return blocks


def extract_blocks(blocks, pred, media=None, out=None):
    """按谓词抽取规则块；media 上下文一并保留。

    返回 [(media_sel_or_None, sel, body, raw)]。
    """
    if out is None:
        out = []
    for kind, sel, body, raw in blocks:
        if kind == "rule":
            if pred(sel):
                out.append((media, sel, body, raw))
        elif kind == "media":
            extract_blocks(body, pred, media=media or sel, out=out)
    return out


def remove_blocks(blocks, pred):
    """返回移除命中规则后的新块树（media 内空则整个 media 移除）。"""
    result = []
    for kind, sel, body, raw in blocks:
        if kind == "rule":
            if pred(sel):
                continue
            result.append((kind, sel, body, raw))
        elif kind == "media":
            new_body = remove_blocks(body, pred)
            if new_body:
                result.append((kind, sel, new_body, raw))
        else:
            result.append((kind, sel, body, raw))
    return result


def render_blocks(blocks):
    """按原始文本拼装块列表。"""
    return "".join(raw for _, _, _, raw in blocks)


def parse_declarations(body):
    """按 ';' 拆分声明体为 [name, value] 列表（保留原始缩进风格重建）。"""
    decls = []
    for part in body.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, value = part.split(":", 1)
        decls.append((name.strip(), value.strip()))
    return decls


def build_root_block(decls, indent="            "):
    lines = [":root {"]
    for name, value in decls:
        lines.append(f"{indent}{name}: {value};")
    lines[-1] = lines[-1].rstrip(";")  # 保持原样式，最后一项也带分号则回填
    lines.append("        }")
    return "\n".join(l if i == 0 else l for i, l in enumerate(lines))


def iter_colors_outside_comments_and_urls(css):
    """产出 (match, text) 颜色匹配，跳过注释与 url(...) 内容。"""
    n = len(css)
    i = 0
    while i < n:
        j = skip_comment(css, i)
        if j != i:
            i = j
            continue
        m = COLOR_RE.search(css, i)
        if not m:
            return
        # 检查是否位于 url( ... ) 内：向前找最近的 url(
        url_open = css.rfind("url(", 0, m.start())
        if url_open != -1:
            url_close = css.find(")", url_open)
            if url_close != -1 and url_close > m.start():
                i = url_close + 1
                continue
        yield m
        i = m.end()


def replace_colors(css, mapping, counter):
    """把颜色字面量替换为 var(--token, 原文)。mapping/counter 原地更新。"""
    out = []
    last = 0
    for m in iter_colors_outside_comments_and_urls(css):
        literal = m.group(0)
        # 选择器位置保护：后随字符为 '{' 或 ':'（如伪类/id 选择器）则跳过
        k = m.end()
        while k < len(css) and css[k] in " \t\r\n":
            k += 1
        if k < len(css) and css[k] in "{:":
            continue
        key = norm_color(literal)
        token = mapping.get(key)
        if token is None:
            counter[0] += 1
            token = f"--cp-{counter[0]}"
            mapping[key] = token
        out.append(css[last:m.start()])
        out.append(f"var({token}, {literal})")
        last = m.end()
    out.append(css[last:])
    return "".join(out)


def main():
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    style_open = html.index("<style>")
    style_close = html.index("</style>")
    css = html[style_open + len("<style>"):style_close]

    blocks = parse_blocks(css)

    # ---- 1. 拆分 :root ----
    root_rules = [b for b in blocks if b[0] == "rule" and b[1].strip() == ":root"]
    if len(root_rules) != 1:
        sys.exit(f"预期 1 个 :root 块，实际 {len(root_rules)} 个")
    decls = parse_declarations(root_rules[0][2])
    base_decls, theme_decls = [], []
    theme_token_values = {}  # token -> (value, kind)  kind: root/day
    for name, value in decls:
        if name in BASE_TOKENS_EXACT or name.startswith(BASE_TOKEN_PREFIXES):
            base_decls.append((name, value))
        else:
            theme_decls.append((name, value))
            theme_token_values[name] = value

    # ---- 2. 抽取 [data-theme=...] 规则 ----
    is_theme_rule = lambda sel: "[data-theme=" in sel
    theme_rules = extract_blocks(blocks, is_theme_rule)
    base_blocks = remove_blocks(blocks, is_theme_rule)

    # ---- 3. 语义色板映射（仅纯颜色值的 theme token）----
    semantic = {}
    for name, value in theme_decls:
        if COLOR_RE.fullmatch(value.strip()):
            semantic[norm_color(value)] = name

    # ---- 4. 重建基础样式 ----
    base_root_raw = "\n        :root {\n" + "".join(
        f"            {n}: {v};\n" for n, v in base_decls
    ) + "        }\n"
    out_blocks = []
    root_replaced = False
    for kind, sel, body, raw in base_blocks:
        if kind == "rule" and sel.strip() == ":root":
            out_blocks.append(base_root_raw)
            root_replaced = True
        else:
            out_blocks.append(raw)
    if not root_replaced:
        sys.exit("未能重建 :root 块")
    base_css = "".join(out_blocks)

    # ---- 5. 颜色字面量 → var(--token, 原值) ----
    counter = [0]
    token_of = {}  # norm-literal -> token
    token_value = {}  # token -> original literal（用于回退注入）
    pieces = []
    last = 0
    for m in iter_colors_outside_comments_and_urls(base_css):
        literal = m.group(0)
        k = m.end()
        while k < len(base_css) and base_css[k] in " \t\r\n":
            k += 1
        if k < len(base_css) and base_css[k] in "{:":
            continue
        key = norm_color(literal)
        token = token_of.get(key)
        if token is None:
            token = semantic.get(key)
        if token is None:
            counter[0] += 1
            token = f"--cp-{counter[0]}"
            semantic[key] = token
        token_of[key] = token
        token_value[token] = literal
        pieces.append(base_css[last:m.start()])
        pieces.append(f"var({token}, {literal})")
        last = m.end()
    pieces.append(base_css[last:])
    base_css = "".join(pieces)

    # ---- 6. 为已迁移 token 的 var() 引用注入回退值 ----
    def inject_fallback(match):
        token = match.group(1)
        value = theme_token_values.get(token) or token_value.get(token)
        if value is None:
            return match.group(0)
        return f"var({token}, {value})"

    base_css = VAR_NOFALLBACK_RE.sub(inject_fallback, base_css)

    # ---- 7. 生成主题 CSS ----
    day_decls = []
    themed_rule_parts = []  # [(media_sel_or_None, raw)]
    for media, sel, body, raw in theme_rules:
        # 日间 token 覆盖块单独重建（追加粒子色板），不重复输出
        if sel.strip() == '[data-theme="day"]':
            for name, value in parse_declarations(body):
                day_decls.append((name, value))
                theme_token_values.setdefault(name, value)
            continue
        themed_rule_parts.append((media, raw))

    theme_css_parts = [
        "/* ============================================\n",
        " * LastDisciple 主题包：赛博朋克 (Cyberpunk)\n",
        " * 由 dev-tools/extract_theme.py 从默认样式提取生成\n",
        " * 颜色与特效定义；日间模式通过 [data-theme=\"day\"] 覆盖\n",
        " * ============================================ */\n\n",
        ":root {\n",
    ]
    for name, value in theme_decls:
        theme_css_parts.append(f"    {name}: {value};\n")
    # 生成的调色板 token
    generated = [(t, v) for t, v in token_value.items() if t.startswith("--cp-")]
    generated.sort(key=lambda kv: int(kv[0][5:]))
    if generated:
        theme_css_parts.append("\n    /* 派生调色板（按基础样式首次出现顺序） */\n")
        for token, value in generated:
            theme_css_parts.append(f"    {token}: {value};\n")
    theme_css_parts.append("\n    /* 背景粒子色板（逗号分隔，供 JS 读取） */\n")
    theme_css_parts.append(
        "    --particle-colors-night: #00f5ff, #0066ff, #bf00ff, #00ff88;\n"
    )
    theme_css_parts.append("}\n\n")

    # 日间模式 token 覆盖（追加粒子日间色板）
    if day_decls:
        theme_css_parts.append('[data-theme="day"] {\n')
        for name, value in day_decls:
            theme_css_parts.append(f"    {name}: {value};\n")
        theme_css_parts.append(
            "    --particle-colors-day: #0066ff, #3b82f6, #60a5fa, #93c5fd;\n"
        )
        theme_css_parts.append("}\n\n")

    theme_css_parts.append("/* 日间/夜间模式专属规则（自基础样式迁出） */\n\n")
    cur_media = None
    for media, raw in themed_rule_parts:
        if media != cur_media:
            if cur_media is not None:
                theme_css_parts.append("}\n\n")
            cur_media = media
            if media is not None:
                theme_css_parts.append(f"{media} {{\n")
        themed_raw = raw if raw.lstrip().startswith("\n") else "\n" + raw
        themed_raw = themed_raw.replace("\n", "\n    ") if media is not None else themed_raw
        theme_css_parts.append(themed_raw)
    if cur_media is not None:
        theme_css_parts.append("}\n\n")
    theme_css = "".join(theme_css_parts)

    # ---- 8. 写回 index.html ----
    new_html = html[:style_open] + "<style>" + base_css + html[style_close:]
    with open(HTML_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(new_html)

    # ---- 9. 写主题包 ----
    os.makedirs(THEME_DIR, exist_ok=True)
    with open(os.path.join(THEME_DIR, "theme.css"), "w", encoding="utf-8", newline="") as f:
        f.write(theme_css)
    meta = {
        "id": "cyberpunk",
        "version": "1.0.0",
        "name": {"zh-CN": "赛博朋克", "en": "Cyberpunk"},
        "description": {
            "zh-CN": "霓虹光效与暗色玻璃拟态的赛博朋克主题（内置默认）",
            "en": "Neon-glow dark glassmorphism cyberpunk theme (built-in default)",
        },
        "author": "LastDisciple",
    }
    with open(os.path.join(THEME_DIR, "theme.json"), "w", encoding="utf-8", newline="") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"语义 token（迁入主题）：{len(theme_decls)}")
    print(f"布局/动效 token（保留基础）：{len(base_decls)}")
    print(f"迁移 [data-theme] 规则块：{len(theme_rules)}")
    print(f"生成派生 token：--cp-1 .. --cp-{counter[0]}（共 {counter[0]}）")
    print(f"日间 token 覆盖：{len(day_decls)}")
    print("完成：index.html 与 themes/cyberpunk 已更新")


if __name__ == "__main__":
    main()
