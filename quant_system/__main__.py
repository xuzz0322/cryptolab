import argparse

from .api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Quant Starter dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    server, url = serve(args.host, args.port)
    print("Quant Starter is running at %s" % url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

