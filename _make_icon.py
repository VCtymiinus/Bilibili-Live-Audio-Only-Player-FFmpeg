"""生成应用图标 —— 深色圆角方块 + 有起伏的音柱。

    python bililive/_make_icon.py

产出（在 bililive/_iconout/ 下）：
    bililive.ico        多尺寸图标，16/20/24/32/40/48/64/128/256 共 9 档
    bililive-256.png    单张 PNG（256），给需要图片的场合用

图标怎么用
----------
1. exe 的文件图标：打包时 `--icon bililive.ico`（已写进打包命令）
2. 窗口标题栏 / 任务栏：ui.py 的 _set_window_icon() 运行时加载同一个 ico
   两者都要设 —— `--icon` 只管**资源管理器里那个文件**长什么样，
   窗口自己的图标是运行时取的，不设就显示 Tk 的默认空图标。

设计原则（仿微软 Fluent / 苹果 macOS 的「节俭又高级」）
-------------------------------------------------------
这不是「画个好看的图」，而是几条能验证的规则：

1. **一个主体，零杂物。** Apple 的图标基本都只有一个形状；Fluent 是
   「一个几何形 + 一个强调色」。这里就是「圆角方块 + 五根柱子」。

2. **留白充足。** 底座四周留 7.5%，底座内部再给柱子留白。抠掉留白的
   图标会显得拥挤廉价 —— 留白是「高级感」最主要的来源。

3. **不用细线、不用渐变高光。** 那些在 16px 会变成噪点，正是廉价感的来源。

4. **16px 必须还能读懂。** 这是微软/苹果图标的基本要求，也是本文件里
   参数反复调整的真正原因（详见下面 _render_simple 的说明）。

配色取自项目前端（bililive/ui.py），两边保持一致：
    INK_2   #26232c  主窗口卡片色
    BAR     #f0eef2  近白（不用纯白，纯白压在深色上偏硬）
为什么图标里**没有**品牌粉：中间一根染成粉色会产生两个视觉中心
（一根粉条 + 四根灰白柱互相抢注意力），而「节俭」要求的正是单一主体。
品牌色交给应用界面本身表达。

调参时注意
----------
本文件曾出现过**同一个常量被赋值两次**（BAR_H），后一次把前一次盖掉，
导致改参数毫无效果、排查很久。改参数前先确认每个常量只定义一次。
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

# ---- 配色 ----
INK_2 = (38, 35, 44, 255)        # #26232c
BAR = (240, 238, 242, 255)       # #f0eef2

SS = 4                           # 超采样倍数：先在 size*SS 上画，再缩回来

# Windows .ico 的标准尺寸集合
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

# ---- 几何比例（都按画布边长的比例，所以任何尺寸等比）----
PAD = 0.075          # 底座到画布边缘的留白
RADIUS = 0.235       # 底座圆角半径 / 底座宽
BAR_W = 0.108        # 单根柱子宽 / 底座宽
BAR_GAP = 0.034      # 柱子间距 / 底座宽
MARK_W = 0.76        # 音柱整体宽度 / 底座宽
MARK_H = 0.62        # 最高那根柱子的高度 / 底座高
BAR_H = [0.55, 0.82, 1.00, 0.82, 0.55]   # 各柱相对高度（平滑单峰）
# 平滑单峰是通用的「音频波形」记号，不模仿任何具体品牌；
# 完全等高则会读成「条形码/栅栏」（实测过），落差太大又像数据图表。


def render(size: int) -> Image.Image:
    """渲染指定尺寸，返回 RGBA 图。

    16px 走**单独简化**的版本。真正讲究的图标会为极小尺寸单独出图，
    而不是把大图缩下去 —— 这是 iOS / Windows 图标资源的常规做法。
    """
    if size <= 16:
        return _render_simple(size)
    return _render_bars(size)


def _base(size: int):
    """画出底座，返回 (图, 画笔, 底座边长)。"""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = s * PAD
    box = (pad, pad, s - pad, s - pad)
    side = box[2] - box[0]
    d.rounded_rectangle(box, radius=side * RADIUS, fill=INK_2)
    return img, d, side


def _render_simple(size: int) -> Image.Image:
    """小尺寸专用：3 根更粗的柱，去掉一切多余细节。

    *** 为什么必须单独做一版 ***
    把常规版缩到 16px 实测：5 根柱每根只有约 1.2 个像素宽，缩略图里糊成
    一团黑块。改成 3 根更粗的柱之后，16px 仍能读出「音频」。
    「16px 还能读懂」是这一版存在的全部理由。
    """
    img, d, side = _base(size)
    s = img.width
    cx, cy = s / 2.0, s / 2.0
    heights = (0.46, 0.74, 0.46)
    bw, gap = side * 0.155, side * 0.070
    total = len(heights) * bw + (len(heights) - 1) * gap
    x0 = cx - total / 2.0
    for i, ratio in enumerate(heights):
        h = side * 0.82 * ratio
        left = x0 + i * (bw + gap)
        d.rounded_rectangle((left, cy - h / 2.0, left + bw, cy + h / 2.0),
                            radius=bw / 2.0, fill=BAR)
    return img.resize((size, size), Image.LANCZOS)


def _render_bars(size: int) -> Image.Image:
    """常规尺寸：5 根柱的有起伏波形。"""
    img, d, side = _base(size)
    s = img.width
    cx, cy = s / 2.0, s / 2.0

    bw, gap = side * BAR_W, side * BAR_GAP
    n = len(BAR_H)
    # 按 MARK_W 归一化整体宽度：改 BAR_W/BAR_GAP 时不必反复试凑总宽
    k = (side * MARK_W) / (n * bw + (n - 1) * gap)
    bw, gap = bw * k, gap * k
    x0 = cx - (side * MARK_W) / 2.0

    for i, ratio in enumerate(BAR_H):
        h = side * MARK_H * ratio
        left = x0 + i * (bw + gap)
        # 圆角半径取半宽 = 胶囊形。胶囊比直角柔和，也更符合 Fluent 的圆润语言
        d.rounded_rectangle((left, cy - h / 2.0, left + bw, cy + h / 2.0),
                            radius=bw / 2.0, fill=BAR)
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, "_iconout")
    os.makedirs(outdir, exist_ok=True)

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
    # 而这种情况「看起来像是做了多尺寸」其实没有，必须验证。
    with Image.open(ico) as ic:
        print(f"  .ico 内含尺寸: {sorted(ic.info.get('sizes', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
