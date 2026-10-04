"""入口脚本。

独立运行时行为和 `python -m bililive` 完全一致。

这里显式关闭字节码写入，原因有三：
  1. 发行包是个绿色文件夹，运行它不该在里面生成 __pycache__；
     否则「只含 6 个在用模块」的说明就变成假话。
  2. 开发时「构建完再跑一次自测」会把 .pyc 留进发行产物里 —— 实际发生过。
  3. 关了之后可以从只读介质（U 盘、只读目录）直接运行。
start.bat 里也设了 PYTHONDONTWRITEBYTECODE，这里是双保险。
"""

import multiprocessing
import sys

# 必须在 import 本项目的包之前设置，否则 import 时已经写出 .pyc 了
sys.dont_write_bytecode = True

from bililive.play import main  # noqa: E402

if __name__ == "__main__":
    # 冻成 exe 后若没有这句，任何用到子进程的路径都可能反复重启自己
    # （PyInstaller 的经典坑）。当前不做 exe，但留着无害。
    multiprocessing.freeze_support()
    sys.exit(main())
