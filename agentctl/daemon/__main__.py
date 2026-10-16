import asyncio
import sys

from agentctl.daemon.server import main

asyncio.run(main(debug="--debug" in sys.argv))
