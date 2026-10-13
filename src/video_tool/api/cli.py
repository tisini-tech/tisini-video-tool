"""The vt-api command."""

from __future__ import annotations

import argparse

from .security import MIN_KEY_LENGTH, Settings

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Video Tool API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--test-page",
        action="store_true",
        help="Serve a test page at /test. For testing only.",
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="Serve the try-it page at /docs. For testing only.",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()

    if not settings.api_key:
        print("Set VT_API_KEY to a long secret text first, then run again.")
        return 1

    for name, key in (
        ("VT_API_KEY", settings.api_key),
        ("VT_API_KEY_NEXT", settings.api_key_next),
    ):
        if key and len(key) < MIN_KEY_LENGTH:
            print(f"{name} is too short. Use at least {MIN_KEY_LENGTH} characters.")
            print('Make one with: python -c "import secrets; print(secrets.token_urlsafe(32))"')
            return 1

    if (args.test_page or args.docs) and args.host not in LOCAL_HOSTS:
        print("--test-page and --docs work only with --host 127.0.0.1.")
        print("They let anyone who reaches the server try the API.")
        return 1

    try:
        import uvicorn

        from .app import create_app
    except ImportError:
        print('The API needs extra packages. Run: pip install -e ".[api]"')
        return 1

    if args.test_page:
        print("Test page is on. Open /test in a browser. Testing only.")

    if args.docs:
        print("Docs page is on. Open /docs in a browser. Testing only.")

    uvicorn.run(
        create_app(settings, test_page=args.test_page, docs=args.docs),
        host=args.host,
        port=args.port,
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
    )

    return 0
