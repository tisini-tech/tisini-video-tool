# Merge page demo (tms side)

A working example of the page a site like tms shows its visitors, and of the
small backend that sits between that page and the video API.

```
visitor's browser  ->  tms backend (server.py)  ->  video API (vt-api)
   index.html            holds the key,              /v1/uploads
                         knows who the user is       /v1/jobs
```

The browser never sees the API key. `server.py` is a stand-in for the tms
backend: it shows exactly which calls tms has to offer. `index.html` is the
page. Both use only the Python standard library and plain JavaScript.

## Try it without the real API

`fake_api.py` answers like the real API but joins nothing, so you can see
every state quickly (slow joins, failing links).

Window 1:

    python examples/tms_demo/fake_api.py

Window 2:

    VT_API_KEY=demo-key-demo-key-demo-key-demo-key python examples/tms_demo/server.py

Open <http://127.0.0.1:8080>. Pick two video files and press Merge videos.

Things to try: a YouTube link containing the word `private` fails and flags
that link; a `.txt` file renamed `.mp4` is refused by the server; reload the
page during the join and it picks the job back up; Cancel during an upload.

## Try it with the real API

Window 1: `vt-api`

Window 2, from the project folder (the key is read from `.env`):

    python examples/tms_demo/server.py

Set `VT_API_URL` if the API is not on `http://127.0.0.1:8000`.

## What tms's backend must offer

Nine routes: six for merging, three for saving one video. `server.py` shows
each one working.

| Route | What it does |
|---|---|
| `GET /api/config` | Limits for the page: biggest file, 2 to 6 videos. |
| `POST /api/uploads` | Streams the file body straight to `POST /v1/uploads`. Never load it into memory. Returns the upload `id`. |
| `DELETE /api/uploads/{id}` | Removes an upload the visitor dropped. |
| `POST /api/merges` | Takes `{"sources": [{"upload": id} or {"url": link}, ...]}` and starts a `merge` job. |
| `GET /api/merges/{id}` | Job status: `status`, `progress`, and a plain `error` block. |
| `GET /api/merges/{id}/download` | Streams the finished file to the visitor. |
| `POST /api/downloads` | Save one video: `{"url": link, "format": "MP4" or "MP3", "quality": "Best" ...}`. Starts a `basic` job. |
| `GET /api/downloads/{id}` | Same status reply as for a merge. |
| `GET /api/downloads/{id}/download` | Streams the file under the name the API gives it (the video's title). |

On every call to the video API, send `X-API-Key` and `X-User-Id`. In this
demo the user ID comes from a cookie so you can try it. **In tms, use the
logged-in user's real ID from your database, and refuse the call if nobody is
logged in.** Check that the job belongs to that user before showing it.

## The two tools

The page has two tabs. **Merge videos** joins two to six videos. **Save one
video** takes one YouTube link and saves it as MP4 (with a quality choice) or
as audio only (MP3). Each tab keeps its own job, so a merge keeps running
while the visitor looks at the other tab, and a reload brings back whichever
job was running. Try a link with the word `private` in the fake API to see a
failure.

The API allows one running job per user, so a visitor who starts a download
while a merge runs gets a "job already running" message. The page shows it as
is.

## How the page behaves

- Files are checked in the browser (type, size) before anything is sent.
- Each file uploads with its own progress; the stages are
  Upload, Fetch links (only if there are links), Join, Ready.
- After the upload the merge runs on the server. The visitor may close the
  tab; the job id is saved in the browser and the page resumes on return.
- The join step has no real percentage (ffmpeg gives none to the API), so the
  page shows time worked instead of a fake bar.
- Failures show the API's own title, message and hint, and mark the source
  that caused it.

## Limits to know about

- The visitor must keep the tab open while files upload. After that it does not
  matter. Resumable uploads are not part of this demo.
- Finished files are kept about an hour by the API. Download or copy to your
  own storage soon after `done`.
- Proxies in front of tms need a large body limit and no request buffering for
  the upload route only (see `docs/API.md`).
- The video API keeps jobs in memory. If it restarts, a job id returns 404;
  the page shows that as an error and the visitor starts again.

## Porting

`index.html` has no build step. To move it into tms, copy its `<style>` and
`<script>` into your own template, and point the nine `/api/...` paths at your
routes. Only the JavaScript names those paths.
