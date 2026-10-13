"""vt-api-try: start one job on a running API and save the file."""

from __future__ import annotations

import argparse
import os
import time
from email.message import Message
from pathlib import Path

import httpx
from dotenv import find_dotenv, load_dotenv


def _problem(reply: httpx.Response) -> str:
    try:
        detail = reply.json().get("detail")
    except Exception:
        detail = None

    if isinstance(detail, dict):
        return "\n".join(
            part for part in (detail.get("title"), detail.get("message"))
            if part
        )

    if isinstance(detail, list) and detail:
        return (
            "The server refused the request: "
            f"{detail[0].get('msg', 'the input is not valid')}"
        )

    return f"The server answered with code {reply.status_code}."


def _server_name(reply: httpx.Response, fallback: str) -> str:
    """The file name the server chose, cut down to a bare name."""

    note = Message()
    note["content-disposition"] = reply.headers.get(
        "content-disposition", ""
    )
    name = Path(note.get_filename() or "").name.strip()

    return name or fallback


def _free_name(folder: Path, name: str, tag: str) -> Path:
    """Return a path in ``folder`` that does not overwrite a file."""

    target = folder / name

    if target.exists():
        target = folder / f"{target.stem} ({tag}){target.suffix}"

    return target


def run(
    link: str,
    client: httpx.Client,
    out_dir: Path,
    *,
    fmt: str = "MP4",
    pause: float = 2.0,
) -> int:
    start = client.post(
        "/v1/jobs",
        json={"tool": "basic", "url": link, "format": fmt},
    )

    if start.status_code != 202:
        print(_problem(start))
        return 1

    job = start.json()
    last = -1

    while job["status"] in ("queued", "running"):
        percent = int(job["progress"] * 100)

        if percent != last:
            print(f"  {job['status']}: {percent}%")
            last = percent

        time.sleep(pause)
        job = client.get(f"/v1/jobs/{job['id']}").json()

    if job["status"] == "failed":
        error = job["error"]
        print(error["title"])
        print(f"  {error['message']}")

        if error.get("hint"):
            print(f"  What to do: {error['hint']}")

        return 1

    reply = client.get(job["file_url"])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    target = _free_name(
        out_dir,
        _server_name(reply, f"video-tool-{job['id'][:8]}.{fmt.lower()}"),
        job["id"][:8],
    )
    target.write_bytes(reply.content)
    print(f"Saved: {target}")

    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True), override=True)

    parser = argparse.ArgumentParser(
        description="Try the Video Tool API with one link."
    )
    parser.add_argument("link", help="A YouTube link")
    parser.add_argument(
        "--server",
        default=os.environ.get("VT_API_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--key", default=os.environ.get("VT_API_KEY", ""))
    parser.add_argument("--user", default="tester")
    parser.add_argument("--format", default="MP4", choices=["MP4", "MP3"])
    parser.add_argument("--out", default=".", help="Folder for the file")
    args = parser.parse_args(argv)

    if not args.key:
        print("No API key. Put VT_API_KEY in the .env file, or pass --key.")
        return 1

    client = httpx.Client(
        base_url=args.server,
        headers={"X-API-Key": args.key, "X-User-Id": args.user},
        timeout=60,
    )

    try:
        return run(args.link, client, Path(args.out), fmt=args.format)
    except httpx.ConnectError:
        print(f"Cannot reach the API at {args.server}.")
        print("Start it in another window with: vt-api")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
