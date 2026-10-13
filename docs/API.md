# Video Tool API

The API lets a website start Video Tool jobs and fetch the finished files.
You run the server. Your website calls it. Visitors never talk to it
directly.

Version 1 has one tool, `basic`: one YouTube link in, one file out.

## Start the server

Do this once:

1. Install: `pip install -e ".[api]"`
2. Make a secret key and save it in a file named `.env`:

```
   python -c "import secrets; print('VT_API_KEY=' + secrets.token_urlsafe(32))" > .env
```

3. Start the server: `vt-api`

Leave that window open. Open <http://127.0.0.1:8000/docs> in a browser. It
lists every call and lets you try them.

Next time, only step 3 is needed.

## Try it with one link

In a second window:

```
vt-api-try "https://youtu.be/jNQXAC9IVRw"
```

It shows progress and saves the file in the current folder. If it fails, it
prints a plain reason and what to do.

## Call it from a website

Run these calls from your site's server, never from the visitor's browser.
Code in a browser can be read, and the key must stay secret.

Send two headers with every call:

| Header | Value |
|---|---|
| `X-API-Key` | The secret key from `.env`. |
| `X-User-Id` | Your own ID for the visitor (1 to 64 letters, digits or `. _ @ -`). |

The API does not log anyone in. Your site does that, and the ID tells the
API who is who. Each user sees only their own jobs.

### The three calls

1. **Start a job:** `POST /v1/jobs` with this body:

```
   {"tool": "basic", "url": "https://youtu.be/...", "format": "MP4"}
```

   `format` is `MP4` or `MP3`. The reply holds the job `id`.

2. **Check the job:** `GET /v1/jobs/{id}` every 2 to 5 seconds. `status` is
   `queued`, `running`, `done` or `failed`. `progress` runs from 0 to 1.

3. **Fetch the file:** when `status` is `done`, `GET` the `file_url` from the
   reply.

`DELETE /v1/jobs/{id}` removes a finished job and its file.

### Example (Python, on your server)

```python
import time, requests

API = "http://127.0.0.1:8000"
HEAD = {"X-API-Key": "YOUR_KEY", "X-User-Id": "user-42"}

reply = requests.post(f"{API}/v1/jobs", headers=HEAD,
                      json={"tool": "basic", "url": link})
if reply.status_code != 202:          # for example 429: wait and try later
    print(reply.json()["detail"]["message"])
else:
    job = reply.json()
    while job["status"] in ("queued", "running"):
        time.sleep(2)
        job = requests.get(f"{API}/v1/jobs/{job['id']}", headers=HEAD).json()
    if job["status"] == "done":
        data = requests.get(API + job["file_url"], headers=HEAD).content
    else:
        e = job["error"]
        print(e["title"], e["message"], e["hint"])
```

## Failed jobs

A failed job has an `error` block with `level`, `title`, `message`, `hint`
and `code`. Show `title`, `message` and `hint` to the visitor. They are plain
text, the same words the desktop app shows. `code` is a short stable name,
such as `private` or `unavailable`, for your own logic.

## Replies

| Code | Meaning |
|---|---|
| 202 | Job accepted. |
| 400 | `X-User-Id` is missing or has bad characters. |
| 401 | The key is missing or wrong. |
| 404 | No such job for this user, or it expired. |
| 409 | The file is not ready yet. |
| 422 | The request body is wrong, for example not a YouTube link. |
| 429 | A limit applies. Read `detail.message`. The `Retry-After` header gives the wait in seconds. |

## Limits

| Rule | Default | Setting in `.env` |
|---|---|---|
| Jobs running per user | 1 | |
| Jobs a user may start per hour | 10 | `VT_API_PER_HOUR` |
| Failures in a row before a pause | 3 | `VT_API_FAIL_STREAK` |
| Length of that pause (seconds) | 300 | `VT_API_FAIL_PAUSE` |
| Time a failed link is remembered (seconds) | 300 | `VT_API_FAIL_MEMORY` |
| Longest a job may run (seconds) | 900 | `VT_API_JOB_TIMEOUT` |
| Time files are kept (seconds) | 3600 | `VT_API_TTL` |
| Jobs running at once, all users | 2 | `VT_API_WORKERS` |

## Server settings

| Setting | Use |
|---|---|
| `VT_API_KEY` | Required. The secret key. |
| `VT_API_COOKIE_FILE` | Path to a `cookies.txt` on the server, for YouTube sign-in errors. |
| `VT_API_WORK` | Folder for job files. The server deletes its old job folders when it starts, so give it a folder used for nothing else. |

## Safety rules

- Keep `.env` out of git. The repo already ignores it.
- Never put the key in browser code.
- Put the server behind HTTPS before it faces the internet.
- Request bodies never name files or folders. The server decides those.
