"""
code-demo — 持久化 AI 编码代理入口

用法:
    python -m code_demo                                    # CLI 模式
    python -m code_demo --voice                            # CLI + 语音模式
    python -m code_demo --mode auto                        # CLI + 自动权限
    python -m code_demo --server                           # Web 服务模式
    python -m code_demo --server --port 9000 --tts --asr   # Web + TTS + ASR
"""

import sys
from code_demo.core.config import SERVER_HOST, SERVER_PORT


def _parse_args():
    """解析命令行参数。"""
    args = {
        "permission_mode": "default",
        "voice_mode": False,
        "server_mode": False,
        "host": SERVER_HOST,
        "port": SERVER_PORT,
        "enable_asr": False,
        "enable_tts": False,
    }

    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg == "--mode" and i + 1 < len(argv):
            args["permission_mode"] = argv[i + 1]
            i += 2
        elif arg == "--voice":
            args["voice_mode"] = True
            i += 1
        elif arg == "--server":
            args["server_mode"] = True
            i += 1
        elif arg == "--host" and i + 1 < len(argv):
            args["host"] = argv[i + 1]
            i += 2
        elif arg == "--port" and i + 1 < len(argv):
            args["port"] = int(argv[i + 1])
            i += 2
        elif arg == "--asr":
            args["enable_asr"] = True
            i += 1
        elif arg == "--tts":
            args["enable_tts"] = True
            i += 1
        elif arg in ("--no-asr",):
            args["enable_asr"] = False
            i += 1
        elif arg in ("--no-tts",):
            args["enable_tts"] = False
            i += 1
        elif arg in ("-h", "--help"):
            _print_usage()
            sys.exit(0)
        else:
            i += 1

    return args


def _print_usage():
    print(__doc__)


def _run_cli(args):
    """CLI 交互模式。"""
    from code_demo.core.agent import Agent

    agent = Agent(
        name="agent",
        permission_mode=args["permission_mode"],
        voice_mode=args["voice_mode"],
    )
    try:
        agent.start()
    except KeyboardInterrupt:
        print("\n\n👋 代理已安全关闭")
    except Exception as e:
        print(f"\n💥 运行时错误: {e}")
        import traceback
        traceback.print_exc()


def _run_server(args):
    """Web 服务模式 (FastAPI + WebSocket)。"""
    try:
        import uvicorn
    except ImportError:
        print("错误：服务器模式需要安装 uvicorn 和 fastapi")
        print("请执行: pip install fastapi uvicorn python-multipart")
        sys.exit(1)

    from code_demo.core.server import create_app

    app = create_app(
        enable_asr=args["enable_asr"],
        enable_tts=args["enable_tts"],
    )

    host = args["host"]
    port = args["port"]
    asr_status = "已启用" if args["enable_asr"] else "未启用"
    tts_status = "已启用" if args["enable_tts"] else "未启用"

    print(f"""
========================================
  芙芙 -- Web 服务
========================================
  地址:      http://{host}:{port}
  WebSocket: ws://{host}:{port}/ws
  ASR:       {asr_status}
  TTS:       {tts_status}
========================================
""")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )


def main():
    args = _parse_args()

    if args["server_mode"]:
        _run_server(args)
    else:
        _run_cli(args)


if __name__ == "__main__":
    main()
