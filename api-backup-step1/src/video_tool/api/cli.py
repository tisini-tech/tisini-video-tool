"""The vt-api command."""

from __future__ import annotations

import argparse

from .security import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Video Tool API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--test-page",
        action="store_true",
        help="Serve a test page at /test. For testing only.",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()

    if not settings.api_key:
        print("Set VT_API_KEY to a long secret text first, then run again.")
        return 1

    try:
        import uvicorn

        from .app import create_app
    except ImportError:
        print('The API needs extra packages. Run: pip install -e ".[api]"')
        return 1

    if args.test_page:
        print("Test page is on. Open /test in a browser. Testing only.")

    uvicorn.run(
        create_app(settings, test_page=args.test_page),
        host=args.host,
        port=args.port,
    )

    return 0
