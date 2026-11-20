import argparse

import wsd.routes  # noqa: F401  registers API routes
from .server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="wsd", description="Workstation dashboard server")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server("127.0.0.1", args.port)
    host, port = server.server_address[:2]
    print(f"http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
