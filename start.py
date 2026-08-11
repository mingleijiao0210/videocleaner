if __name__ == "__main__":
    import os
    import sys

    if "--vsr-worker" in sys.argv:
        # PyInstaller 的 windowed 应用在 Finder 启动时可能把 Python 的标准流
        # 设为 None；AI worker 仍需通过管道向主界面报告进度。
        if sys.stdout is None:
            sys.stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = os.fdopen(os.dup(2), "w", encoding="utf-8", buffering=1)
        sys.argv.remove("--vsr-worker")
        from tools.vsr.bridge.videocleaner_vsr_worker import main
    else:
        from app.main import main

    raise SystemExit(main())
