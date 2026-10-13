# -*- coding: utf-8 -*-
"""按 ECHO 面板「折叠边条」的 logo 生成 Android 启动图标（**可重复生成，别手工改 PNG**）。

设计来源 = `web/rail.html` 里的 `.logo` 规则，逐条照抄：

    width/height 1:1、border-radius 10px
    background: linear-gradient(160deg, #4f8cff 0%, #3a6fe0 100%)
    color: #fff、font-weight 800、font-family "Segoe UI"
    字母按 **2×2 网格**排：E C / H O

Android 自适应图标是**两层**（背景 + 前景），所以要拆开产：
  * 背景层 = 蓝色渐变铺满 108×108（形状交给启动器去裁）→ 那份 XML 是手工写的常量
    （`res/drawable/ic_launcher_background.xml`），不在这里生成；
  * 前景层 = 白色 EC/HO 字母 → 本脚本生成 PNG，**落在中央安全区里**
    （自适应图标只有中间 66/108 一定可见，边角会被各种形状的遮罩切掉）。

用法：
    <repo>\\venv\\Scripts\\python.exe android\\tools\\make_launcher_icon.py
"""

import os

from PIL import Image, ImageDraw, ImageFont

#: 折叠边条那份 logo 的字面量（改这里之前先回去看 web/rail.html）
GRADIENT_FROM = "#4f8cff"
GRADIENT_TO = "#3a6fe0"
LETTER_COLOR = (255, 255, 255, 255)
LETTERS = ("E", "C", "H", "O")          # 2×2：E C / H O

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\segoeuib.ttf",   # Segoe UI Bold —— 面板字体就是 Segoe UI
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
)

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.normpath(os.path.join(HERE, "..", "app", "src", "main", "res"))

#: 自适应图标的画布（dp）。108 是标准值，安全区 66。
CANVAS = 108
#: 只放中间这一块（比安全区略小，圆/方形遮罩都切不到）
CONTENT = 62
#: 每个字母的目标高度（dp）
GLYPH_H = 25
#: 超采样倍数：先大画再缩，边缘才不毛糙
SS = 4

DENSITIES = {
    "mdpi": 1.0,
    "hdpi": 1.5,
    "xhdpi": 2.0,
    "xxhdpi": 3.0,
    "xxxhdpi": 4.0,
}


def _font_path():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("找不到可用的粗体字体，请改 FONT_CANDIDATES")


def _tight_glyph(ch, font):
    """把一个字母渲染成"贴边的"图（去掉左右上下空白），方便精确居中。"""
    tmp = Image.new("RGBA", (900, 900), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    d.text((200, 200), ch, font=font, fill=LETTER_COLOR)
    bbox = tmp.getbbox()
    if not bbox:
        raise SystemExit("字母 %r 渲染不出来" % ch)
    return tmp.crop(bbox)


def build_foreground(px):
    """前景层：白色 2×2 字母，内容落在中央 CONTENT 区域内。

    坐标一律用 **dp×SS**（超采样画布 = CANVAS*SS），最后再缩到目标像素 `px`。
    别把 dp 和"像素 = dp×密度"搞混 —— 混了会得到一个 16×16 的空图
    （2026-10-09 第一版就是这么错的，生成了一个什么都没有的图标）。
    """
    size = CANVAS * SS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    font = ImageFont.truetype(_font_path(), int(GLYPH_H * SS * 1.6))
    glyphs = [_tight_glyph(ch, font) for ch in LETTERS]

    # 统一缩放：让最高的那个字母正好等于 GLYPH_H（保持四个字母一样大）
    target_h = GLYPH_H * SS
    k = target_h / float(max(g.height for g in glyphs))

    # 2×2 网格中心
    cell = CONTENT / 2.0
    centers = [
        (CANVAS / 2 - cell / 2, CANVAS / 2 - cell / 2),   # E 左上
        (CANVAS / 2 + cell / 2, CANVAS / 2 - cell / 2),   # C 右上
        (CANVAS / 2 - cell / 2, CANVAS / 2 + cell / 2),   # H 左下
        (CANVAS / 2 + cell / 2, CANVAS / 2 + cell / 2),   # O 右下
    ]

    for glyph, (cx, cy) in zip(glyphs, centers):
        w = max(1, int(round(glyph.width * k)))
        h = max(1, int(round(glyph.height * k)))
        g = glyph.resize((w, h), Image.LANCZOS)
        img.paste(
            g,
            (int(round(cx * SS - w / 2.0)), int(round(cy * SS - h / 2.0))),
            g,
        )

    out = img.resize((px, px), Image.LANCZOS)

    # **响亮失败**：绝不写出一个"什么都没有"的图标（第一版就是这么错的）
    hist = out.getchannel("A").histogram()
    opaque = sum(hist[17:])
    if opaque < (px * px) // 200:
        raise SystemExit("生成的前景几乎是空的（不透明像素只有 %d）—— 别写出这种图标" % opaque)
    return out


def main():
    written = []
    for name, factor in DENSITIES.items():
        px = int(round(CANVAS * factor))
        out_dir = os.path.join(RES, "mipmap-%s" % name)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "ic_launcher_foreground.png")
        build_foreground(px).save(out, "PNG", optimize=True)
        written.append((out, px))

    for out, px in written:
        print("wrote %s (%dx%d)" % (out.replace(RES, "res"), px, px))


if __name__ == "__main__":
    main()
