# videotool: the video tools inside tms (Django)

A small Django app. It adds one page (`/video/`) and nine routes under
`/video/api/`. Signed-in visitors merge videos or save one YouTube video. Your
Django server passes each call to `vt-api` on the same machine.

```
visitor's browser -> Django (this app) -> vt-api on 127.0.0.1:8000
   session cookie      checks sign-in,       does the work with ffmpeg
                       adds the key + user ID
```

The browser never sees the key. Each visitor's ID is `tms-<user pk>`, so users
only ever see their own jobs and files.

## 1. Add the app

Copy the `videotool` folder next to `manage.py`.

`settings.py`:

```python
import os

INSTALLED_APPS = [
    # ...
    "videotool",
]

VIDEO_TOOL_URL = "http://127.0.0.1:8000"          # where vt-api listens
VIDEO_TOOL_KEY = os.environ["VIDEO_TOOL_KEY"]     # same value as vt-api's VT_API_KEY
# Optional:
# VIDEO_TOOL_MAX_UPLOAD_MB = 5120                 # keep at or below vt-api's VT_API_MAX_UPLOAD_MB
# VIDEO_TOOL_USER_ID = lambda request: f"tms-{request.user.pk}"
```

`urls.py` of the project:

```python
from django.urls import include, path

urlpatterns = [
    # ...
    path("video/", include("videotool.urls")),
]
```

The page needs the usual `LOGIN_URL`. Visitors who are not signed in are sent
there. The API routes answer a plain 401 instead.

No database tables, no migrations, no new packages. Only the standard library
is used to talk to vt-api.

## 2. Run vt-api on the same server

Use one process, and keep it on localhost (the default). Give both programs the
same key. Example `systemd` unit:

```
[Unit]
Description=Video tool API
After=network.target

[Service]
User=tms
WorkingDirectory=/srv/tisini-video-tool
Environment=PYTHONUTF8=1
EnvironmentFile=/srv/tisini-video-tool/.env
Environment=VT_API_WORK=/srv/video-work
ExecStart=/srv/tisini-video-tool/.venv/bin/vt-api
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`VT_API_WORK` is a folder used for nothing else. It needs room: roughly twice
the size of the source videos for each merge running at once.

## 3. Check it works

```
python manage.py test videotool
```

Then sign in to tms and open `/video/`. Try two small clips first.

## 4. Settings for big files (a football match)

These matter for multi-GB uploads and downloads.

- **nginx:** give the upload route its own block. Keep your normal small limit
  everywhere else.

  ```
  location = /video/api/uploads {
      client_max_body_size 6g;           # above VIDEO_TOOL_MAX_UPLOAD_MB
      proxy_request_buffering off;       # pass the file on as it arrives
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      proxy_pass http://django;          # your usual upstream
  }

  location ~ ^/video/api/(merges|downloads)/[^/]+/download$ {
      proxy_buffering off;               # stream the file, do not store it
      proxy_read_timeout 3600s;
      proxy_pass http://django;
  }
  ```

- **gunicorn:** an upload or a download holds one worker for as long as it
  takes. Use threads so other visitors are not blocked, and a long timeout:
  `--worker-class gthread --threads 8 --timeout 3600`.
- **ASGI (uvicorn, daphne):** Django saves the whole request body to a temporary
  file before the view runs. Uploads still work, but they use disk twice and
  progress on the server side starts late. WSGI avoids that.
- **Disk:** the upload goes straight on to vt-api, so Django itself stores
  nothing. The disk that matters is `VT_API_WORK`.

## 5. Things to decide

- **Who may use it.** Any signed-in user can. To limit it, add
  `@permission_required(...)` to the views, or check a field in
  `endpoint()` in `views.py`.
- **One job at a time per user.** vt-api allows one running job per user. A
  visitor who starts a second one sees "A job is already running".
- **Finished files last about an hour** (`VT_API_TTL` in vt-api). The page tells
  the visitor to download straight away. If you want to keep results, copy the
  file into your own storage in `stream_file()`.
- **The page is a full HTML document.** To place it inside tms's own layout,
  move the `<style>` and `<script>` blocks into your base template and keep the
  two `<meta name="vt-...">` tags. The script only needs those two tags and the
  page elements.
- **If vt-api restarts,** jobs and uploads are lost. The page shows "no longer
  available" and the visitor starts again.

## Files

| File | What it does |
|---|---|
| `client.py` | Talks to vt-api. No Django in it. |
| `views.py` | The nine routes and the page. |
| `urls.py` | Their addresses. |
| `templates/videotool/index.html` | The page. |
| `tests.py` | Tests for the views, with a stand-in for vt-api. |
