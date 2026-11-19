#!/usr/bin/env python3
"""分析入口包装：以隔离模式运行（python3 -I analyze.py ...）。

-I 不读取 PYTHONPATH 与当前目录，因此在脚本内显式登记仓库根目录；
数据路径由命令行 --data 传入，不依赖工作目录。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from shuttle.cli import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1:])
