import sys


def main() -> None:
    # Hook fast path: skip importing typer/rich/pydantic (keeps hooks well under 100 ms).
    if len(sys.argv) == 3 and sys.argv[1] == "hook":
        from agentctl.hooks import run_hook

        run_hook(sys.argv[2])
    if len(sys.argv) == 2 and sys.argv[1] == "statusline":
        from agentctl.hooks import run_statusline

        run_statusline()
    from agentctl.cli import app

    app()


if __name__ == "__main__":
    main()
