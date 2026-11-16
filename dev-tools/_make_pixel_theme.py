# -*- coding: utf-8 -*-
"""生成像素复古主题包 pixel-retro：
1. 读取 cyberpunk/theme.css 的 :root 与 [data-theme="day"] token，按像素调色板映射转换
2. 拼接手写像素风附加规则（按钮/卡片/纹理/字体/图标）
3. 打包 zip（根目录布局：theme.json + theme.css）
"""
import io, json, os, re, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CP_CSS = os.path.join(HERE, '..', 'app', 'ui', 'themes', 'cyberpunk', 'theme.css')
OUT_DIR = os.path.join(HERE, '..', 'app', 'ui', 'themes', 'pixel-retro')
ZIP_PATH = os.path.join(os.environ['TEMP'], 'pixel-retro.zip')

# ---------- 像素调色板映射（cyberpunk 色 -> 像素色，保持 alpha） ----------
RGBA_MAP = {
    (0, 245, 255): (95, 205, 228),    # 青 -> 像素青 #5fcde4
    (0, 102, 255): (65, 166, 246),    # 蓝 -> 像素蓝 #41a6f6
    (59, 130, 246): (65, 166, 246),   # 蓝变体 -> 像素蓝
    (147, 197, 253): (143, 195, 243), # 浅蓝 -> 像素浅蓝 #8fc3f3
    (191, 0, 255): (215, 123, 186),   # 紫 -> 像素粉紫 #d77bba
    (102, 0, 255): (181, 80, 136),    # 深紫 -> #b55088
    (255, 0, 110): (255, 0, 77),      # 品红 -> 像素红 #ff004d
    (255, 51, 102): (255, 0, 77),     # 红 -> 像素红
    (255, 60, 90): (255, 0, 77),      # 红 -> 像素红
    (0, 255, 136): (56, 183, 100),    # 绿 -> 像素绿 #38b764
    (0, 230, 118): (56, 183, 100),    # 绿 -> 像素绿
    (16, 185, 129): (56, 183, 100),   # 日间绿 -> 像素绿
    (255, 170, 0): (255, 163, 0),     # 橙 -> 像素橙 #ffa300
    (255, 165, 0): (255, 163, 0),     # 橙 -> 像素橙
    (245, 158, 11): (255, 163, 0),    # 日间橙 -> 像素橙
    (239, 68, 68): (255, 0, 77),      # 日间红 -> 像素红
    (10, 15, 25): (26, 28, 44),       # 深底 rgba(10,15,25) -> rgba(26,28,44)
    (13, 13, 20): (34, 35, 58),       # 卡片底 rgba(13,13,20) -> rgba(34,35,58)
}

HEX_MAP = {
    '#00f5ff': '#5fcde4', '#0066ff': '#41a6f6', '#3b82f6': '#41a6f6',
    '#60a5fa': '#8fc3f3', '#93c5fd': '#8fc3f3',
    '#bf00ff': '#d77bba', '#a977ff': '#b55088', '#c39bff': '#d77bba',
    '#ff006e': '#ff004d', '#ff3366': '#ff004d', '#ef4444': '#ff004d',
    '#ff6b81': '#ff7096', '#ff8aad': '#ff7096',
    '#00ff88': '#38b764', '#00e676': '#38b764', '#10b981': '#38b764',
    '#ffaa00': '#ffa300', '#ffae42': '#ffcd75', '#f59e0b': '#ffa300',
    '#ffff00': '#ffec27', '#ff8800': '#ffa300',
    '#0a0a0f': '#1a1c2c', '#0d0d14': '#16181f', '#0f172a': '#16181f', '#1e293b': '#29366f',
    '#001016': '#16181f',
    '#e0e0e0': '#f4f4f4', '#888899': '#94a1b2', '#555566': '#566c86',
    '#94a3b8': '#566c86',
    '#f8fafc': '#e8e0d0', '#ffffff': '#f4efe0',
    '#475569': '#5a6988',
    '#fff': '#f4f4f4',
}

def map_value(v):
    """转换单个颜色值"""
    def rgba_sub(m):
        key = tuple(int(m.group(i)) for i in (1, 2, 3))
        rgb = RGBA_MAP.get(key, key)
        return 'rgba(%d, %d, %d, %s)' % (rgb[0], rgb[1], rgb[2], m.group(4))
    v = re.sub(r'rgba\((\d+),\s*(\d+),\s*(\d+),\s*([^)]*)\)', rgba_sub, v)
    def hex_sub(m):
        return HEX_MAP.get(m.group(0).lower(), m.group(0))
    v = re.sub(r'#[0-9a-fA-F]{3,6}\b', hex_sub, v)
    return v

def convert_block(block_text):
    return map_value(block_text)

# ---------- 读取并转换 token ----------
src = io.open(CP_CSS, encoding='utf-8').read()

root_m = re.search(r':root\s*\{([^{}]*)\}', src)
root_body = root_m.group(1)

day_m = re.search(r'\[data-theme="day"\]\s*\{([^{}]*)\}', src)
day_body = day_m.group(1)

# 特殊 token：阴影改为像素硬阴影，字体改为等宽像素字体
SPECIAL = {
    '--shadow-neon': '4px 4px 0 rgba(0, 0, 0, 0.85)',
    '--shadow-card': '0 6px 0 rgba(0, 0, 0, 0.55), inset 0 -3px 0 rgba(0, 0, 0, 0.25)',
    '--shadow-glow': '0 0 0 3px rgba(95, 205, 228, 0.35)',
    '--font-tech': "Consolas, 'Courier New', 'Lucida Console', monospace",
    '--font-main': "'Courier New', Consolas, monospace",
}
DAY_SPECIAL = {
    '--shadow-neon': '4px 4px 0 rgba(42, 42, 58, 0.55)',
    '--shadow-card': '0 6px 0 rgba(42, 42, 58, 0.28), inset 0 -3px 0 rgba(42, 42, 58, 0.12)',
    '--shadow-glow': '0 0 0 3px rgba(65, 166, 246, 0.35)',
}

def rebuild_tokens(body, specials):
    out = []
    for line in body.splitlines():
        m = re.match(r'^(\s*)(--[\w-]+)\s*:\s*(.+?);?\s*$', line)
        if not m:
            continue
        indent, name, value = m.group(1), m.group(2), m.group(3).rstrip(';').strip()
        if name in specials:
            value = specials[name]
        else:
            value = map_value(value)
        out.append('%s%s: %s;' % (indent, name, value))
    return '\n'.join(out)

root_css = rebuild_tokens(root_body, SPECIAL)
day_css = rebuild_tokens(day_body, DAY_SPECIAL)

# 日间粒子四色去重（像素蓝/粉/黄/绿各一）
day_css = re.sub(r'--particle-colors-day:[^;]+;',
                 '--particle-colors-day: #41a6f6, #ff7096, #ffcd75, #38b764;', day_css)

# 字体 token 在基础层定义（cyberpunk :root 中没有），像素主题需追加覆盖
root_css += "\n    /* 像素等宽字体覆盖 */\n    --font-tech: %s;\n    --font-main: %s;\n" % (SPECIAL['--font-tech'], SPECIAL['--font-main'])

# 像素语义边框（border-* 从透明霓虹改实色像素描边）
root_css = root_css.replace('--border-color: %s' % map_value('rgba(0, 245, 255, 0.15)'),
                            '--border-color: #3b4a6b')
root_css = root_css.replace('--border-glow: %s' % map_value('rgba(0, 245, 255, 0.4)'),
                            '--border-glow: #5fcde4')
day_css = day_css.replace('--border-color: %s' % map_value('rgba(0, 102, 255, 0.2)'),
                          '--border-color: #b8a890')
day_css = day_css.replace('--border-glow: %s' % map_value('rgba(0, 102, 255, 0.5)'),
                          '--border-glow: #41a6f6')

# ---------- 手写像素风附加规则 ----------
PIXEL_RULES = """
/* ============================================
 * 像素复古附加规则（Pixel Retro Overlays）
 * 方角、硬阴影、棋盘格纹理、等宽像素字体
 * ============================================ */

/* 像素核心：全元素方角 + 像素渲染 */
* {
    border-radius: 0 !important;
}
img, svg, video, canvas {
    image-rendering: pixelated;
    image-rendering: crisp-edges;
}

/* 背景棋盘格纹理（8px 像素格） */
body {
    background-image:
        repeating-conic-gradient(rgba(255, 255, 255, 0.022) 0% 25%, transparent 0% 50%) !important;
    background-size: 16px 16px !important;
    letter-spacing: 0.3px;
}
body::before {
    background:
        repeating-linear-gradient(0deg, transparent 0 15px, rgba(255, 255, 255, 0.03) 15px 16px),
        repeating-linear-gradient(90deg, transparent 0 15px, rgba(255, 255, 255, 0.03) 15px 16px) !important;
}

/* 像素网格覆盖层 */
.grid-overlay {
    background-image:
        linear-gradient(rgba(95, 205, 228, 0.05) 2px, transparent 2px),
        linear-gradient(90deg, rgba(95, 205, 228, 0.05) 2px, transparent 2px) !important;
    background-size: 24px 24px !important;
}

/* 扫描线 -> CRT 像素扫描带 */
.scan-line {
    background: linear-gradient(90deg, transparent 0%, rgba(95, 205, 228, 0.55) 45%, rgba(244, 244, 244, 0.85) 50%, rgba(95, 205, 228, 0.55) 55%, transparent 100%) !important;
}

/* 卡片：实心底 + 3px 像素描边 + 硬投影 */
.card {
    background: var(--bg-card) !important;
    border: 3px solid var(--border-color) !important;
    box-shadow: 6px 6px 0 rgba(0, 0, 0, 0.6) !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
    transition: transform 0.08s steps(2), box-shadow 0.08s steps(2) !important;
}
.card:hover {
    transform: translate(-2px, -2px) !important;
    box-shadow: 8px 8px 0 rgba(0, 0, 0, 0.65) !important;
}
.card::before {
    background: linear-gradient(90deg, var(--neon-cyan), var(--neon-cyan)) !important;
    opacity: 0.9;
}

/* 按钮：像素台阶式立体按键 */
.btn,
button {
    box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.85) !important;
    border: 2px solid rgba(0, 0, 0, 0.65) !important;
    text-shadow: none !important;
    transition: transform 0.06s steps(2), box-shadow 0.06s steps(2) !important;
    font-weight: 700 !important;
}
.btn:hover,
button:hover {
    transform: translate(-2px, -2px) !important;
    box-shadow: 6px 6px 0 rgba(0, 0, 0, 0.9) !important;
}
.btn:active,
button:active {
    transform: translate(3px, 3px) !important;
    box-shadow: 1px 1px 0 rgba(0, 0, 0, 0.85) !important;
}
.btn-primary {
    background: var(--neon-green) !important;
    border-color: rgba(0, 0, 0, 0.7) !important;
    color: #16181f !important;
}
.btn-primary::before {
    display: none !important;
}
.btn-danger {
    background: var(--error-color) !important;
    color: #fff !important;
    border-color: rgba(0, 0, 0, 0.7) !important;
}

/* 输入控件：方角 + 内嵌像素凹槽 */
input, select, textarea, .form-field input, .form-field select {
    box-shadow: inset 3px 3px 0 rgba(0, 0, 0, 0.45) !important;
    border: 2px solid var(--border-color) !important;
}
input:focus, select:focus, textarea:focus {
    border-color: var(--neon-cyan) !important;
    box-shadow: inset 3px 3px 0 rgba(0, 0, 0, 0.45), 0 0 0 2px var(--neon-cyan) !important;
    outline: none !important;
}

/* 状态点：方形像素灯 + 闪烁 steps 动画 */
.status-dot {
    box-shadow: 0 0 0 2px rgba(0, 0, 0, 0.5) !important;
}

/* 顶部标题：像素横幅描边 */
.header, header {
    border-bottom: 4px solid var(--neon-cyan) !important;
    box-shadow: 0 4px 0 rgba(0, 0, 0, 0.5) !important;
}

/* 标题文字：像素描边效果（text-shadow 硬偏移） */
h1, h2, h3 {
    text-shadow: 2px 2px 0 rgba(0, 0, 0, 0.8) !important;
}

/* 滚动条像素化 */
::-webkit-scrollbar {
    width: 14px;
}
::-webkit-scrollbar-thumb {
    background: var(--neon-cyan);
    border: 3px solid var(--bg-secondary);
    box-shadow: inset -3px -3px 0 rgba(0, 0, 0, 0.4);
}
::-webkit-scrollbar-track {
    background: var(--bg-secondary);
}

/* 日间模式纹理调暗 */
[data-theme="day"] body {
    background-image:
        repeating-conic-gradient(rgba(42, 42, 58, 0.05) 0% 25%, transparent 0% 50%) !important;
    background-size: 16px 16px !important;
}
[data-theme="day"] body::before {
    background:
        repeating-linear-gradient(0deg, transparent 0 15px, rgba(42, 42, 58, 0.045) 15px 16px),
        repeating-linear-gradient(90deg, transparent 0 15px, rgba(42, 42, 58, 0.045) 15px 16px) !important;
}
[data-theme="day"] .card {
    box-shadow: 6px 6px 0 rgba(42, 42, 58, 0.3) !important;
}
[data-theme="day"] .btn,
[data-theme="day"] button {
    box-shadow: 4px 4px 0 rgba(42, 42, 58, 0.5) !important;
}
"""

HEADER = """/* ============================================
 * LastDisciple 主题包：像素复古 (Pixel Retro)
 * 复古 8-bit 配色 + 方角硬阴影 + 棋盘格纹理
 * 日间模式通过 [data-theme="day"] 覆盖
 * ============================================ */

"""

css = HEADER + ':root {\n' + root_css + '\n}\n\n[data-theme="day"] {\n' + day_css + '\n}\n' + PIXEL_RULES

# ---------- theme.json ----------
theme_json = {
    "id": "pixel-retro",
    "version": "1.0.0",
    "name": {"zh-CN": "像素复古", "en": "Pixel Retro"},
    "description": {
        "zh-CN": "复古 8-bit 像素风格：方角硬阴影、棋盘格纹理、等宽像素字体与 PICO 风调色板",
        "en": "Retro 8-bit pixel style: squared corners, hard shadows, checkerboard texture, monospace pixel font and PICO-style palette"
    },
    "author": "LastDisciple"
}

# ---------- 写盘 + 打包 ----------
os.makedirs(OUT_DIR, exist_ok=True)
io.open(os.path.join(OUT_DIR, 'theme.css'), 'w', encoding='utf-8').write(css)
io.open(os.path.join(OUT_DIR, 'theme.json'), 'w', encoding='utf-8').write(
    json.dumps(theme_json, ensure_ascii=False, indent=2))

with zipfile.ZipFile(ZIP_PATH, 'w', zipfile.ZIP_DEFLATED) as z:
    z.write(os.path.join(OUT_DIR, 'theme.json'), 'theme.json')
    z.write(os.path.join(OUT_DIR, 'theme.css'), 'theme.css')

# 校验
import re as _re
opens = css.count('{')
closes = css.count('}')
tokens = _re.findall(r'--[a-z][\w-]*\s*:', root_css)
print('theme.css: %.1f KB, braces %d/%d balanced=%s' % (len(css.encode('utf-8')) / 1024, opens, closes, opens == closes))
print(':root tokens: %d' % len(tokens))
print('zip: %s (%d bytes)' % (ZIP_PATH, os.path.getsize(ZIP_PATH)))
