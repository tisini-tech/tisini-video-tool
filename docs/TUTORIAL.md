# Video Tool API: tutorial

This guide shows how to start the API and try it in your browser. You need
no coding. For the full list of calls, read `API.md`.

## What the API does

The API runs Video Tool on a server. A website asks it for a job, and the
API does the work and keeps the file for one hour.

A job takes minutes, so the website cannot wait on one call. It works in
three steps:

1. **Start** a job. The API replies at once with a job ID.
2. **Check** the job now and then, using the ID.
3. **Fetch** the file when the job says `done`.

## Which tools work today

| Tool | Does | State |
|---|---|---|
| `basic` | One YouTube link in, one video or MP3 out. | Works |
| `pro` | One link, with a start and end time. | Planned |
| `merge` | Several links joined into one video. | Planned |
| `compiler` | A clip list from your database, made into one video. | Planned |

Part 5 shows what each planned tool will take. A request for a planned
tool is refused with code 422.

## Part 1: Start the server

Do this once:

1. Open a terminal in the project folder and run
   `source .venv/bin/activate`.
2. Install the extras: `pip install -e ".[api]"`
3. Make your secret key. This makes the file `.env` only if it is missing:

   ```
   [ -f .env ] || python -c "import secrets; print('VT_API_KEY=' + secrets.token_urlsafe(32))" > .env
   ```

4. Optional but advised: pick a fixed folder for job files. Add this line
   to `.env`, and use a folder you use for nothing else:

   ```
   VT_API_WORK=/home/lab/Downloads/Video-Tool-API
   ```

   Without it, files go to `/tmp`, which Linux may clear on reboot.

Every time:

```
vt-api
```

Leave that window open. It prints a line for every call. Press Ctrl+C to
stop the server.

## Part 2: Try one link from the terminal

Open a second terminal, activate the environment, and run:

```
vt-api-try "https://youtu.be/jNQXAC9IVRw"
```

It shows progress and saves the video in your current folder, under the
video's title. If the job fails, it prints a plain reason and what to do.

## Part 3: Try it in the browser

1. Show your key: `cat .env`. Copy the text after `VT_API_KEY=`.
2. Open <http://127.0.0.1:8000/docs>. Each call is a coloured bar. Click a
   bar to open it.
3. **Check the server.** Open `GET /v1/health`, click **Try it out**, then
   **Execute**. You should see `"status": "ok"`. `"ffmpeg": true` means
   ffmpeg is installed. This call needs no key.
4. **Start a job.** Open `POST /v1/jobs` and click **Try it out**. Fill in:
   - `X-API-Key`: your key
   - `X-User-Id`: any name, for example `tester`
   - the request body:

     ```
     {"tool": "basic", "url": "https://youtu.be/jNQXAC9IVRw", "format": "MP4"}
     ```

   Click **Execute**. The code should be `202`. Copy the `id` from the
   reply.
5. **Check the job.** Open `GET /v1/jobs/{job_id}`, click **Try it out**,
   and paste the ID, the key and the same user name. Click **Execute** every
   few seconds. `status` goes `queued`, `running`, `done`. `progress` goes
   from 0 to 1.
6. **Fetch the file.** Open `GET /v1/jobs/{job_id}/file`, fill in the same
   three fields and click **Execute**. Click **Download file**. The browser
   keeps the video's title as the name.
7. **Clean up.** `DELETE /v1/jobs/{job_id}` removes the job and its file.

The page asks for the key and user name on every call. That is normal. The
API has no login, so each call carries its own proof.

## Part 4: Break it on purpose

Each of these should give a plain answer:

| Do this | You get |
|---|---|
| Use a wrong key | 401 |
| Leave `X-User-Id` empty | 400 |
| Send `"url": "https://example.com"` | 422, and no job starts |
| Start a second job while the first runs, as the same user | 429 |
| Ask for a job with another user name | 404 |
| Send the same private or missing video twice | The second answer is instant, since the server remembers the failure for 5 minutes |
| Fail 3 jobs in a row | 429, with a wait of 5 minutes |

## Part 5: The planned tools

These are the plans. The shapes may change when each tool is built, and
this file will change with them.

**pro**: one link and a time range.

```
{"tool": "pro", "url": "https://youtu.be/...", "start": "0:10", "end": "0:20", "format": "MP4"}
```

**merge**: two to ten links, joined in the order given.

```
{"tool": "merge", "urls": ["https://youtu.be/...", "https://youtu.be/..."]}
```

**compiler**: no link and no upload. The website sends an ID, the server
reads that request's clips from the database, and builds the video.

```
{"tool": "compiler", "request_id": "..."}
```

Planned rules for all of them: links only, never a file path on the server;
the same limits as `basic`; and the same plain `error` block when a job
fails.

## Where files go

| Where | Name |
|---|---|
| Server folder | `<work folder>/<random job ID>/` |
| File inside it | The video title, for example `Me at the zoo.mp4` |
| Your copy | Your browser's downloads folder, or the folder where you ran `vt-api-try` |

The server keeps a finished file for one hour, and deletes a failed job's
folder at once. The API is not storage. Your website must fetch the file
and keep it.

## If something goes wrong

| You see | Do this |
|---|---|
| `Cannot reach the API` | Start the server with `vt-api`. |
| `No API key` | Check that `.env` has a line `VT_API_KEY=...`. |
| 401 in the browser | The key you pasted is wrong. Copy it again from `.env`. |
| 429 | Read `detail.message`. It says how long to wait. |
| A job fails | Read `error.message` and `error.hint`. They are plain text. |
