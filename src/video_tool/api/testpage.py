"""A test page for the API. Off unless started with --test-page.

The page asks for the API key when it is used. The key is never written
into the page and is never saved. Use this page for testing only. On a real
site the key stays on the site's server.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Tool API: test page</title>
<style>
  body { font-family: system-ui, sans-serif; background: #1e1e1e; color: #eee; margin: 0; }
  main { max-width: 34rem; margin: 2rem auto; padding: 0 1rem; }
  label { display: block; margin: 0.8rem 0 0.2rem; font-size: 0.9rem; }
  input, select { width: 100%; box-sizing: border-box; padding: 0.55rem; border-radius: 6px;
                  border: 1px solid #555; background: #2b2b2b; color: #eee; font-size: 1rem; }
  button { margin-top: 1rem; padding: 0.65rem 1.4rem; border: 0; border-radius: 6px;
           background: #1f6aa5; color: #fff; font-size: 1rem; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: wait; }
  progress { width: 100%; margin-top: 1rem; }
  #status { white-space: pre-line; margin-top: 1rem; }
  .bad { color: #ff8080; }
  .note { color: #aaa; font-size: 0.85rem; }
  a { color: #7cc4ff; }
</style>
</head>
<body>
<main>
  <h1>Video Tool API: test page</h1>
  <p class="note">For testing only. The key stays in this page and is never saved.</p>

  <label for="key">API key</label>
  <input id="key" type="password" autocomplete="off">

  <label for="user">User name</label>
  <input id="user" value="tester" autocomplete="off">

  <label for="url">YouTube link</label>
  <input id="url" placeholder="https://youtu.be/..." autocomplete="off">

  <label for="fmt">Format</label>
  <select id="fmt"><option>MP4</option><option>MP3</option></select>

  <button id="go">Start</button>
  <progress id="bar" max="1" value="0" hidden></progress>
  <p id="status" role="status"></p>
  <p id="out"></p>
</main>

<script>
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
let busy = false;

function say(text, bad) {
  const line = $("status");
  line.textContent = text;
  line.className = bad ? "bad" : "";
}

function headers() {
  return { "X-API-Key": $("key").value.trim(), "X-User-Id": $("user").value.trim() };
}

async function problem(reply) {
  let detail = null;
  try { detail = (await reply.json()).detail; } catch (e) { /* no body */ }

  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return [detail.title, detail.message].filter(Boolean).join(": ");
  }
  if (Array.isArray(detail) && detail.length) {
    return "The server refused the request: " + (detail[0].msg || "the input is not valid");
  }
  return "The server answered with code " + reply.status + ".";
}

function fileName(reply, fallback) {
  const note = reply.headers.get("content-disposition") || "";
  let found = note.match(/filename\*=utf-8''([^;]+)/i);

  if (found) {
    try { return decodeURIComponent(found[1]); } catch (e) { /* use next rule */ }
  }

  found = note.match(/filename="?([^";]+)"?/i);

  return found ? found[1] : fallback;
}

async function run() {
  if (busy) return;

  busy = true;
  $("go").disabled = true;
  $("out").textContent = "";
  $("bar").hidden = true;

  try {
    const start = await fetch("/v1/jobs", {
      method: "POST",
      headers: { ...headers(), "Content-Type": "application/json" },
      body: JSON.stringify({ tool: "basic", url: $("url").value.trim(), format: $("fmt").value }),
    });

    if (start.status !== 202) {
      say(await problem(start), true);
      return;
    }

    let job = await start.json();
    $("bar").hidden = false;

    while (job.status === "queued" || job.status === "running") {
      $("bar").value = job.progress;
      say("Job " + job.status + ": " + Math.round(job.progress * 100) + "%");
      await sleep(2000);

      const check = await fetch("/v1/jobs/" + job.id, { headers: headers() });

      if (!check.ok) {
        say(await problem(check), true);
        return;
      }

      job = await check.json();
    }

    if (job.status === "failed") {
      const e = job.error;
      say([e.title, e.message, e.hint ? "What to do: " + e.hint : ""].filter(Boolean).join("\n"), true);
      return;
    }

    const file = await fetch(job.file_url, { headers: headers() });

    if (!file.ok) {
      say(await problem(file), true);
      return;
    }

    const blob = await file.blob();
    const name = fileName(file, "video-tool-" + job.id.slice(0, 8) + "." + $("fmt").value.toLowerCase());
    const link = document.createElement("a");

    link.href = URL.createObjectURL(blob);
    link.download = name;
    link.textContent = "Save " + name;
    $("out").replaceChildren(link);
    say("Done.");
  } catch (e) {
    say("Cannot reach the API. Is vt-api running?", true);
  } finally {
    busy = false;
    $("go").disabled = false;
  }
}

$("go").addEventListener("click", run);
</script>
</body>
</html>
"""


def add_test_page(app: FastAPI) -> None:
    """Add GET /test to ``app``."""

    @app.get("/test", response_class=HTMLResponse, include_in_schema=False)
    def test_page() -> HTMLResponse:
        return HTMLResponse(PAGE)
