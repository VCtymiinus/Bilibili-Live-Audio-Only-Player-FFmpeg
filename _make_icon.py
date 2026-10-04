"""生成应用图标 —— 深色圆角方块 + 「声」字。

    python bililive/_make_icon.py

产出（bililive/_iconout/）：
    bililive.ico        多尺寸图标，16/20/24/32/40/48/64/128/256 共 9 档
    bililive-256.png    单张 PNG（256）

图标怎么用
----------
1. exe 的文件图标：打包时 `--icon bililive.ico`
2. 窗口标题栏 / 任务栏：ui.py 的 _set_window_icon() 运行时加载同一个 ico
   *** 两者必须都设 ***
   `--icon` 只管**资源管理器里那个文件**长什么样；窗口自己的图标是运行时
   取的，不设就显示 Tk 的默认空图标（一个空白方块）。

为什么是「声」字 —— 这条经过一次返工
------------------------------------
第一版做的是「圆角方块 + 波形柱」。用户反馈「一眼就是抄袭，而且和这个
直播间放声音的契合度不大」，两条都成立：

  1. **抄袭感**：波形柱不是我抄了某个具体品牌，而是**整个品类都在用** ——
     播客、语音备忘录、均衡器、SoundCloud、Audacity…… 换到任何一个
     音频 app 上都成立。模仿整个品类，就是"一眼抄袭"的真正来源。
  2. **契合度**：波形只说明"有声音"，说明不了这个项目的核心 ——
     **只听直播、把画面扔掉**。那个「只」字才是它的身份。

所以改成「声」：
  * 这个字就是「声音」本身，与「只听声音」直接对应
  * 面向中文用户（B 站），指向明确、不用解释
  * **没有任何音频工具拿它当图标** —— 别人拿不走，这是关键

为什么最后没加粉色直播点
------------------------
试过在右下角加一个粉色圆点表示「直播中」。实测两点不行：
  * 在 512px 下它读起来像「有新消息」的小红点，而不是设计元素
  * 在 16px 下它占了太大视觉比重，字反而更糊
纯白单色更「节俭」，小尺寸也更稳。品牌粉交给应用界面本身表达。

评判标准（照微软 Fluent / 苹果 macOS 那套，外加一条「识别度」）
--------------------------------------------------------------
  1. **一个主体，零杂物** —— 一个圆角方块 + 一个字
  2. **留白充足** —— 底座四周 7.5%，字只占底座边长约 62%
  3. **不用细线、不用渐变高光** —— 那些在 16px 会退化成噪点
  4. **16px 仍可辨认** —— 实测「声」在 16px 还能读出横画结构；
     纯几何的柱群反而做不到（每根柱只剩约 1.2px）
  5. **别人拿不走** —— 不是任何音频 app 都适用的通用符号

调参时注意
----------
本文件的前身出现过**同一个常量被赋值两次**（BAR_H），后一次盖掉前一次，
导致改参数毫无效果、排查很久。改参数前先确认每个常量只定义一次。
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

# ---- 配色（与 bililive/ui.py 一致）----
INK_2 = (38, 35, 44, 255)        # #26232c 主窗口卡片色
BAR = (240, 238, 242, 255)       # #f0eef2 近白（纯白压在深色上偏硬）

SS = 4                           # 超采样倍数
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

PAD = 0.075          # 底座到画布边缘的留白
RADIUS = 0.235       # 底座圆角半径 / 底座宽
GLYPH = 0.62         # 字的尺寸 / 底座宽（留白是否充足主要由它决定）
NUDGE_X = 0.005      # 水平视觉修正（汉字视觉重心略偏，纯几何居中会显得偏坠）

# 中文字体，按优先级找。用系统字体渲染而不是手画笔画：
# 手画汉字必然变形，而系统字体是专业字库设计的，笔画间距正确。
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",     # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\Deng.ttf",       # 等线
    r"C:\Windows\Fonts\SourceHanSansSC-Bold.otf",
]


def find_font() -> str | None:
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def render(size: int) -> Image.Image:
    """渲染指定尺寸，返回 RGBA 图。

    先在 size*SS 的画布上画，再 LANCZOS 缩回来 —— 直接在小画布上画圆角
    和文字会出现锯齿，这对小图标是致命的。
    """
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ---- 底座 ----
    pad = s * PAD
    box = (pad, pad, s - pad, s - pad)
    side = box[2] - box[0]
    d.rounded_rectangle(box, radius=side * RADIUS, fill=INK_2)

    # ---- 字 ----
    fp = find_font()
    if not fp:
        # 找不到中文字体时画一个明显的占位框，而不是静默画错一个空图标
        d.rectangle((s * 0.30, s * 0.30, s * 0.70, s * 0.70),
                    outline=BAR, width=max(1, int(s * 0.03)))
        return img.resize((size, size), Image.LANCZOS)

    # 二分找最接近目标尺寸的字号。
    # 不能用「字号 = 目标像素」直接猜：字体的实际墨迹尺寸与字号不成正比
    # （不同字、不同字库的留白不同），必须量 bbox。
    target = side * GLYPH
    lo, hi, best = 8, int(side), None
    for _ in range(24):
        mid = (lo + hi) // 2
        f = ImageFont.truetype(fp, mid)
        bb = d.textbbox((0, 0), "声", font=f)
        if max(bb[2] - bb[0], bb[3] - bb[1]) < target:
            best = (f, bb)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f = ImageFont.truetype(fp, int(side * 0.5))
        bb = d.textbbox((0, 0), "声", font=f)
    else:
        f, bb = best

    # *** 居中必须按 bbox 算，不能用 anchor='mm' ***
    # 不同字库的 bbox 偏移不同，用 anchor 会让字看起来偏上或偏左。
    x = s / 2 - (bb[0] + bb[2]) / 2 - side * NUDGE_X
    y = s / 2 - (bb[1] + bb[3]) / 2
    d.text((x, y), "声", font=f, fill=BAR)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, "_iconout")
    os.makedirs(outdir, exist_ok=True)

    fp = find_font()
    print(f"字体: {fp}")
    if not fp:
        print("!! 找不到中文字体，无法生成正确图标")
        return 1

    imgs = {sz: render(sz) for sz in SIZES}

    ico = os.path.join(outdir, "bililive.ico")
    # 每一档都显式给出，而不是只放一张 256 让系统去缩 ——
    # 后者在小图标（标题栏 16、Alt+Tab 32）下会明显发糊。
    imgs[256].save(ico, format="ICO", sizes=[(s, s) for s in SIZES],
                   append_images=[imgs[s] for s in SIZES])
    imgs[256].save(os.path.join(outdir, "bililive-256.png"))

    print(f"输出目录: {outdir}")
    for f in sorted(os.listdir(outdir)):
        p = os.path.join(outdir, f)
        print(f"  {f:22s} {os.path.getsize(p):>8,} bytes")

    # 自检：.ico 里到底装了哪几档。只装一档的话小图标会糊，
    # 而那种情况「看起来像是做了多尺寸」其实没有，必须验证。
    with Image.open(ico) as ic:
        print(f"  .ico 内含尺寸: {sorted(ic.info.get('sizes', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
