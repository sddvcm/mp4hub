import argparse
import os
import uvicorn
from app.main import app
from app import main as backend


def main():
    parser = argparse.ArgumentParser(description="MP4Hub local media service")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AVHUB_PORT", "8765")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    backend.SERVER_PORT = args.port
    # The service is driven exclusively by the MP4Hub desktop window: it never
    # opens a browser on its own.
    # Paused video streams and disconnected clients must not drain forever on
    # desktop shutdown. Finish writes, then cancel remaining HTTP tasks before
    # the lifespan stops scanning / FFmpeg workers.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                          log_level="warning", timeout_graceful_shutdown=5))
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
