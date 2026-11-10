# Video Tool API

The API lets a website start Video Tool jobs and fetch the finished files.
You run the server. Your website calls it. Visitors never talk to it
directly.

There are two tools:

- `basic`: one YouTube link in, one file out.
- `merge`: two to six videos in, one joined MP4 out, in the order you send
  them. Each video is a YouTube link or a file the user handed over (see
  "Merging files from the user's computer" below).

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

   To merge, send `tool` `merge` and a list of `urls` instead:

```
   {"tool": "merge", "urls": ["https://youtu.be/...", "https://youtu.be/..."], "quality": "720p"}
```

   `quality` is optional (`Best`, `1080p`, `720p`, `480p` or `360p`). The
   result is always an MP4 named `merged.mp4`. The server joins the clips
   without re-encoding when they match, and re-encodes only when they do not.
   Merge jobs take longer, so `progress` moves in steps: the downloads fill
   most of the bar and the join fills the rest.

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

When a `merge` job fails because one link could not be fetched, the error
block also holds `clip`: the position of that source in your list, counting
from 1. Use it to tell the visitor which link to fix.

## Merging files from the user's computer

A merge may use files the visitor chose on your site. The API cannot read
files from your server or from anyone's computer by name. Instead, your
backend hands each file over first, then names it in the merge.

1. **Hand over each file:** `POST /v1/uploads` with the file as the whole
   request body (`Content-Type: application/octet-stream`). Send the same two
   headers as for jobs. The reply holds the upload `id`:

```
   curl -X POST http://127.0.0.1:8000/v1/uploads \
     -H "X-API-Key: YOUR_KEY" -H "X-User-Id: user-42" \
     -H "Content-Type: application/octet-stream" \
     --data-binary @first-half.mp4
```

   ```
   {"id": "k3Jx...22 characters", "size": 1873645231, "expires_in": 3600}
   ```

2. **Start the merge** with `sources`, in the order you want. You may mix
   uploads and YouTube links:

```
   {"tool": "merge", "sources": [{"upload": "ID_OF_FIRST_HALF"}, {"upload": "ID_OF_SECOND_HALF"}]}
```

   `quality` is optional and only affects YouTube links. Use `urls` instead
   of `sources` when every video is a link. Never send both.

3. **Poll and fetch** the result as for any job.

Rules for uploads:

- The server checks every file. It must be a real video in one of these
  containers: MP4, MOV, MKV, WebM, AVI, FLV or TS. Anything else is refused
  with 415, whatever the file is called. The server never uses the name the
  sender gave the file.
- An upload belongs to the `X-User-Id` that sent it. Another user cannot use
  it.
- The merge deletes its uploads when it ends, whether it worked or not. An
  upload nobody uses is deleted after `VT_API_TTL` seconds (one hour).
- `DELETE /v1/uploads/{id}` removes an upload you no longer want. It cannot
  remove a file a running job is using.
- A file is limited in size, and one user may hold only so many files and so
  many bytes at once. See the limits table.
- A merge that names an upload that is missing, expired or not the user's is
  refused with 404. `detail.source` says which source, counting from 1.
- Uploads are only kept in memory and on disk for the life of the server. If
  the server restarts, every upload is gone and must be sent again.

Testing in `/docs`: open `POST /v1/uploads`, press "Try it out", choose a
file, fill in `x-api-key` and `x-user-id`, and press Execute. Copy the `id`
from the reply. Do it again for the second file, then use both IDs in
`POST /v1/jobs`. Use the same `x-user-id` every time.

## Replies

| Code | Meaning |
|---|---|
| 201 | Upload accepted. |
| 202 | Job accepted. |
| 400 | `X-User-Id` is missing or has bad characters. |
| 401 | The key is missing or wrong. |
| 404 | No such job or upload for this user, or it expired. |
| 409 | The file is not ready yet, or an upload is in use. |
| 413 | The request body is too big. For an upload, the file is over the size limit. |
| 415 | The upload is not a video the server can read. |
| 422 | The request body is wrong, for example not a YouTube link. An empty upload also gets 422. |
| 507 | The server's disk is full. |
| 429 | A limit applies. Read `detail.message`. The `Retry-After` header gives the wait in seconds. |

## Limits

| Rule | Default | Setting in `.env` |
|---|---|---|
| Jobs running per user | 1 | |
| Jobs a user may start per hour | 10 | `VT_API_PER_HOUR` |
| Failures in a row before a pause | 3 | `VT_API_FAIL_STREAK` |
| Length of that pause (seconds) | 300 | `VT_API_FAIL_PAUSE` |
| Time a failed link is remembered (seconds) | 300 | `VT_API_FAIL_MEMORY` |
| Longest a `basic` job may run (seconds) | 900 | `VT_API_JOB_TIMEOUT` |
| Longest a `merge` job may run (seconds) | 1800 | `VT_API_MERGE_TIMEOUT` |
| Biggest single upload (MB) | 5120 | `VT_API_MAX_UPLOAD_MB` |
| All of one user's uploads together (MB) | 12288 | `VT_API_UPLOAD_QUOTA_MB` |
| Uploaded files one user may hold at once | 6 | `VT_API_MAX_UPLOADS` |
| Time files are kept (seconds) | 3600 | `VT_API_TTL` |
| Jobs running at once, all users | 2 | `VT_API_WORKERS` |

## Server settings

| Setting | Use |
|---|---|
| `VT_API_KEY` | Required. The secret key. |
| `VT_API_COOKIE_FILE` | Path to a `cookies.txt` on the server, for YouTube sign-in errors. |
| `VT_API_WORK` | Folder for job files. The server deletes its old job folders when it starts, so give it a folder used for nothing else. |

## Running it behind your own site's backend

This is the intended setup. Your site's backend is the only caller.

- **Who calls:** the backend, never the browser. The key stays on your
  server, and the API needs no CORS settings.
- **`X-User-Id`:** send the visitor's ID from your own database, for example
  their user row ID. The API uses it for the per-user limits and so that one
  user cannot read another user's jobs. The backend decides who the visitor
  is, so only it should set this header.
- **Keep the file yourself.** Finished files are deleted after `VT_API_TTL`
  seconds (one hour by default). When a job is `done`, download the file
  right away and store it in your own storage.
- **Jobs live in memory.** If the API restarts, running jobs are lost and
  you get 404 for their IDs. Treat 404 on a job you started as "lost" and
  let the visitor start it again.
- **Run one server process.** Job state and rate limits are kept in memory,
  so do not start the API with several worker processes.
- **Use a long timeout on your side for downloads** of finished files, and
  poll job status every 2 to 5 seconds, not faster.
- **Windows:** set `PYTHONUTF8=1` before `vt-api`. The merge step prints
  symbols that a legacy console encoding cannot show.
- **Send uploads as a stream.** Pass the visitor's file on from your
  backend without loading it into memory (a few GB will not fit). Most HTTP
  libraries accept an open file or a generator as the body. Send the same
  user ID for the uploads and for the merge.
- **A reverse proxy needs room for the file.** The default body limit of
  most proxies is far below a match recording. In nginx, for example, set
  `client_max_body_size` above `VT_API_MAX_UPLOAD_MB` for `/v1/uploads` only,
  turn off `proxy_request_buffering` there, and raise the send and read
  timeouts. Keep the small limit for every other path.
- **Plan the disk.** Put `VT_API_WORK` on a disk with room for the uploads,
  the downloads and the merged file of the jobs that run at once: roughly
  twice the size of the source videos for each running merge.

## Safety rules

- Keep `.env` out of git. The repo already ignores it.
- Never put the key in browser code.
- Put the server behind HTTPS before it faces the internet.
- Request bodies never name files or folders. The server decides those.
  An upload is named only by the ID the server gave it.
- Uploaded files come from strangers. Keep ffmpeg up to date, because it
  reads them. The server only accepts known video containers and checks each
  file with ffprobe before any job opens it.
