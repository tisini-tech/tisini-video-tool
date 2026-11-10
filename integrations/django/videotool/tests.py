"""Run with: python manage.py test videotool

These use Django's test client and a stand-in for the video API, so they need
neither vt-api nor ffmpeg.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import client

SETTINGS = {"VIDEO_TOOL_KEY": "k" * 32, "VIDEO_TOOL_URL": "http://127.0.0.1:9"}


class FakeReply:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self._headers = headers or {}

    def read(self, n=-1):
        data, self._body = (self._body, b"") if n < 0 else (self._body[:n], self._body[n:])
        return data

    def getheader(self, name):
        return self._headers.get(name)


class FakeConn:
    closed = False

    def close(self):
        self.closed = True


@override_settings(**SETTINGS)
class VideoToolTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ann", password="pw")
        self.client.force_login(self.user)

    def url(self, name, *args):
        return reverse(f"videotool:{name}", args=args)

    # ------------------------------------------------------------ access

    def test_page_needs_login(self):
        anon = Client()
        reply = anon.get(self.url("index"))
        self.assertEqual(reply.status_code, 302)

    def test_page_has_token_and_base(self):
        reply = self.client.get(self.url("index"))
        self.assertContains(reply, 'name="vt-csrf"')
        self.assertContains(reply, f'name="vt-api" content="{self.url("config").rsplit("/", 1)[0]}"')

    def test_routes_answer_json_401_when_signed_out(self):
        anon = Client()

        for name, args in (("config", ()), ("merge_status", ("abc",)), ("download_file", ("abc",))):
            reply = anon.get(self.url(name, *args))
            self.assertEqual(reply.status_code, 401, name)
            self.assertIn("error", reply.json())

    def test_post_needs_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        reply = strict.post(self.url("start_download"), "{}", content_type="application/json")
        self.assertEqual(reply.status_code, 403)

    def test_wrong_method(self):
        self.assertEqual(self.client.post(self.url("config")).status_code, 405)

    # ------------------------------------------------------------ merging

    def test_merge_sends_only_known_fields_and_the_users_id(self):
        sent = {}

        def fake_call(api, method, path, user, payload=None):
            sent.update(method=method, path=path, user=user, payload=payload, key=api.key)
            return 202, {"id": "J1", "status": "queued", "progress": 0.0, "secret": "x"}

        body = {"sources": [{"upload": "A" * 22, "evil": 1}, {"url": "https://youtu.be/x"}], "tool": "basic"}

        with mock.patch.object(client, "call_json", fake_call):
            reply = self.client.post(self.url("start_merge"), body, content_type="application/json")

        self.assertEqual(reply.status_code, 202)
        self.assertNotIn("secret", reply.json())
        self.assertEqual(sent["user"], f"tms-{self.user.pk}")
        self.assertEqual(sent["key"], "k" * 32)
        self.assertEqual(
            sent["payload"],
            {"tool": "merge", "sources": [{"upload": "A" * 22}, {"url": "https://youtu.be/x"}]},
        )

    def test_merge_needs_two_to_six(self):
        for sources in ([], [{"url": "https://youtu.be/x"}], [{"url": "u"}] * 7, "nope", [5, 6]):
            reply = self.client.post(self.url("start_merge"), {"sources": sources}, content_type="application/json")
            self.assertEqual(reply.status_code, 422, sources)

    def test_api_error_shows_plain_words(self):
        detail = {"title": "A job is already running", "message": "Wait.", "hint": "Soon."}

        with mock.patch.object(client, "call_json", lambda *a, **k: (429, {"detail": detail})):
            reply = self.client.post(
                self.url("start_merge"),
                {"sources": [{"url": "a"}, {"url": "b"}]},
                content_type="application/json",
            )

        self.assertEqual(reply.status_code, 429)
        self.assertEqual(reply.json()["error"]["title"], "A job is already running")

    def test_refused_key_is_not_shown_to_the_visitor(self):
        with mock.patch.object(client, "call_json", lambda *a, **k: (401, {})):
            reply = self.client.post(
                self.url("start_merge"),
                {"sources": [{"url": "a"}, {"url": "b"}]},
                content_type="application/json",
            )

        self.assertEqual(reply.status_code, 502)
        self.assertNotIn("key", reply.content.decode().lower())

    def test_unreachable_api(self):
        def down(*a, **k):
            raise client.Unreachable

        with mock.patch.object(client, "call_json", down):
            reply = self.client.get(self.url("merge_status", "abc"))

        self.assertEqual(reply.status_code, 502)

    def test_a_download_job_is_not_shown_as_a_merge(self):
        with mock.patch.object(client, "call_json", lambda *a, **k: (200, {"id": "J", "tool": "basic", "status": "done"})):
            self.assertEqual(self.client.get(self.url("merge_status", "J")).status_code, 404)
            self.assertEqual(self.client.get(self.url("download_status", "J")).status_code, 200)

    # ------------------------------------------------------------ single video

    def test_download_validates_and_forwards(self):
        sent = {}

        def fake_call(api, method, path, user, payload=None):
            sent["payload"] = payload
            return 202, {"id": "J2", "status": "queued", "progress": 0}

        with mock.patch.object(client, "call_json", fake_call):
            ok = self.client.post(
                self.url("start_download"),
                {"url": " https://youtu.be/x ", "format": "MP3", "quality": "720p", "tool": "merge"},
                content_type="application/json",
            )
            bad_format = self.client.post(
                self.url("start_download"), {"url": "https://youtu.be/x", "format": "AVI"}, content_type="application/json"
            )
            no_link = self.client.post(self.url("start_download"), {"format": "MP4"}, content_type="application/json")

        self.assertEqual(ok.status_code, 202)
        self.assertEqual(
            sent["payload"], {"tool": "basic", "url": "https://youtu.be/x", "format": "MP3", "quality": "720p"}
        )
        self.assertEqual(bad_format.status_code, 422)
        self.assertEqual(no_link.status_code, 422)

    # ------------------------------------------------------------ files

    def test_upload_streams_the_body_to_the_api(self):
        got = {}

        def fake_upload(api, user, read, size):
            data = b""

            while len(data) < size:
                data += read(min(1000, size - len(data)))

            got.update(user=user, data=data)
            return 201, {"id": "U" * 22, "size": size}

        payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000

        with mock.patch.object(client, "send_upload", fake_upload):
            reply = self.client.post(self.url("upload"), payload, content_type="application/octet-stream")

        self.assertEqual(reply.status_code, 201)
        self.assertEqual(got["data"], payload)
        self.assertEqual(got["user"], f"tms-{self.user.pk}")

    @override_settings(VIDEO_TOOL_MAX_UPLOAD_MB=0)
    def test_upload_over_the_limit_is_refused_before_sending(self):
        with mock.patch.object(client, "send_upload", side_effect=AssertionError("must not be called")):
            reply = self.client.post(self.url("upload"), b"abc", content_type="application/octet-stream")

        self.assertEqual(reply.status_code, 413)

    def test_download_passes_the_file_and_its_name(self):
        conn = FakeConn()
        reply = FakeReply(
            200,
            b"filedata",
            {"Content-Disposition": 'attachment; filename="Match.mp3"', "Content-Type": "audio/mpeg", "Content-Length": "8"},
        )

        with mock.patch.object(client, "open_file", lambda *a: (conn, reply)):
            response = self.client.get(self.url("download_file", "J2"))
            body = b"".join(response.streaming_content)

        self.assertEqual(body, b"filedata")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="Match.mp3"')
        self.assertTrue(conn.closed)

    def test_merge_file_is_always_named_merged_mp4(self):
        conn, reply = FakeConn(), FakeReply(200, b"v", {"Content-Disposition": "inline"})

        with mock.patch.object(client, "open_file", lambda *a: (conn, reply)):
            response = self.client.get(self.url("merge_file", "J1"))
            b"".join(response.streaming_content)

        self.assertEqual(response["Content-Disposition"], 'attachment; filename="merged.mp4"')

    def test_missing_file_passes_the_api_error(self):
        conn, reply = FakeConn(), FakeReply(409, b'{"detail": {"title": "No file yet", "message": "Wait."}}')

        with mock.patch.object(client, "open_file", lambda *a: (conn, reply)):
            response = self.client.get(self.url("merge_file", "J1"))

        self.assertEqual(response.status_code, 409)
        self.assertTrue(conn.closed)

    def test_odd_ids_never_reach_the_api(self):
        with mock.patch.object(client, "call_json", side_effect=AssertionError("must not be called")):
            self.assertEqual(self.client.get(self.url("merge_status", "a.b")).status_code, 404)
            self.assertEqual(self.client.delete(self.url("delete_upload", "a b")).status_code, 404)
