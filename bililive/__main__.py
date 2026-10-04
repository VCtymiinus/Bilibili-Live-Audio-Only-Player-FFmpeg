"""支持 `python -m bililive 房间号`（走 ffplay 直连的 A 方案）。"""

import sys

from .play import main

if __name__ == "__main__":
    sys.exit(main())
