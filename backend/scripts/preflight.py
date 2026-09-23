"""发布前预检：可脱离服务运行，直接复用启动自检的判据。

比"起服务看日志"更省资源，也避免"自检失败但服务已占用端口"。
返回 0 表示通过，1 表示有违规项（stderr 打印全部违规项），可直接进 CI。
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> int:
    from app.core.startup_check import run_startup_checks

    try:
        asyncio.run(run_startup_checks())
    except Exception as e:  # noqa: BLE001  预检失败原因全部原样呈现给运维
        print(str(e), file=sys.stderr)
        return 1
    print("preflight 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
