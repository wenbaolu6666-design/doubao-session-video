import base64
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path


CLIENT = Path(__file__).parents[1] / "scripts" / "doubao_session_video.py"
SPEC = importlib.util.spec_from_file_location("doubao_session_video", CLIENT)
CLIENT_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT_MODULE)


def sse(*events):
    return "\n\n".join(events) + "\n\n"


def event(payload):
    return "event: message\ndata: " + json.dumps(payload, ensure_ascii=False)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.original_appdata = os.environ.get("APPDATA")
        self.original_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["APPDATA"] = self.config.name
        os.environ["XDG_CONFIG_HOME"] = self.config.name
        CLIENT_MODULE.ACTIVE_TASK_LOG = None

    def tearDown(self):
        if self.original_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self.original_appdata
        if self.original_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.original_xdg
        CLIENT_MODULE.ACTIVE_TASK_LOG = None
        self.config.cleanup()

    def save(self, cookies=None):
        session = CLIENT_MODULE._merge_saved_session(
            CLIENT_MODULE._coerce_session(
                {
                    "cookies": cookies or {"sessionid": "session-secret", "ttwid": "tt-value"},
                    "label": "test account",
                }
            )
        )
        CLIENT_MODULE.save_session(session)
        return session

    def test_save_session_is_private_on_unix(self):
        self.save()
        path = CLIENT_MODULE.config_path()
        self.assertEqual(json.loads(path.read_text())["cookies"]["sessionid"], "session-secret")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_replacing_cookies_keeps_device_identity(self):
        first = self.save()
        second = self.save({"sessionid": "replacement"})
        self.assertEqual(second["cookies"]["sessionid"], "replacement")
        self.assertEqual(second["device_id"], first["device_id"])
        self.assertEqual(second["web_id"], first["web_id"])
        self.assertEqual(second["fp"], first["fp"])

    def test_parse_cookie_header(self):
        cookies = CLIENT_MODULE.parse_cookie_header(
            "Cookie: sessionid=abc; passport_csrf_token=xyz; ttwid=value=with-equals"
        )
        self.assertEqual(cookies["sessionid"], "abc")
        self.assertEqual(cookies["passport_csrf_token"], "xyz")
        self.assertEqual(cookies["ttwid"], "value=with-equals")

    def test_plain_sessionid_is_accepted(self):
        session = CLIENT_MODULE._coerce_session("session-value")
        self.assertEqual(session["cookies"], {"sessionid": "session-value"})

    def test_session_without_sessionid_is_rejected(self):
        with self.assertRaises(CLIENT_MODULE.ClientError):
            CLIENT_MODULE._coerce_session({"cookies": {"ttwid": "only"}})

    def test_video_payload_uses_agent_mode(self):
        payload = CLIENT_MODULE.build_video_payload("a running dog", "9:16", "5 秒", "seedance-2.0-mini")
        self.assertEqual(payload["client_meta"]["bot_id"], CLIENT_MODULE.VIDEO_AGENT_BOT_ID)
        message = payload["messages"][0]
        text = message["content_block"][0]["content"]["text_block"]["text"]
        self.assertIn("Seedance 2.0 Mini", text)
        self.assertIn("9:16", text)
        self.assertIn("a running dog", text)
        self.assertEqual(payload["option"]["agent_mode"], 1)
        self.assertEqual(payload["option"]["conversation_mode"], 2)

    def test_video_payload_includes_camera_movement(self):
        payload = CLIENT_MODULE.build_video_payload("animate", "16:9", "", "doubao-video", "slow push")
        text = payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"]
        self.assertIn("slow push", text)

    def test_extract_async_task_id(self):
        raw = sse(
            event(
                {
                    "event_type": 2001,
                    "event_data": json.dumps(
                        {"fin_reason": {"reason": 1, "async_task": {"id": "task_123"}}}
                    ),
                }
            )
        )
        self.assertEqual(CLIENT_MODULE.extract_task_id(raw), "task_123")

    def test_extract_agent_task_id(self):
        raw = sse(
            "event: STREAM_CHUNK\ndata: "
            + json.dumps(
                {
                    "patch_op": [
                        {
                            "patch_value": {
                                "ext": {
                                    "ai_creation_tool_list": "[{\"task_id\":\"26061228235778\",\"file_type\":\"video\"}]"
                                }
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(CLIENT_MODULE.extract_task_id(raw), "26061228235778")

    def test_parse_agent_video_response(self):
        encoded = base64.b64encode(b"https://cdn.example/agent.mp4").decode()
        raw = sse(
            "event: STREAM_CHUNK\ndata: "
            + json.dumps(
                {
                    "patch_op": [
                        {
                            "patch_value": {
                                "content_block": [
                                    {
                                        "block_type": 10084,
                                        "content": {
                                            "video_model": {
                                                "video_list": {
                                                    "video_1": {"main_url": encoded, "vwidth": 1280, "vheight": 720}
                                                }
                                            },
                                            "poster_url": "https://cdn.example/poster.jpg",
                                        },
                                    }
                                ]
                            }
                        },
                        {
                            "patch_value": {
                                "ext": {
                                    "ai_creation_tool_list": "[{\"task_id\":\"agent_task_1\",\"file_type\":\"video\"}]"
                                }
                            }
                        },
                    ]
                },
                ensure_ascii=False,
            )
        )
        videos, _answer = CLIENT_MODULE.parse_video_response(raw)
        self.assertEqual(videos[0]["video_url"], "https://cdn.example/agent.mp4")
        self.assertEqual(videos[0]["cover_url"], "https://cdn.example/poster.jpg")
        self.assertEqual(videos[0]["width"], 1280)
        self.assertEqual(CLIENT_MODULE.extract_task_id(raw), "agent_task_1")

    def test_parse_video_url_response(self):
        content = {
            "data": [
                {
                    "video_url": "https://cdn.example/video.mp4",
                    "cover_url": "https://cdn.example/cover.jpg",
                    "duration": 5.2,
                }
            ]
        }
        raw = sse(
            event(
                {
                    "event_type": 2001,
                    "event_data": {
                        "message": {
                            "content_type": 2021,
                            "content": json.dumps(content),
                        }
                    },
                }
            )
        )
        videos, answer = CLIENT_MODULE.parse_video_response(raw)
        self.assertEqual(answer, "")
        self.assertEqual(videos[0]["video_url"], "https://cdn.example/video.mp4")
        self.assertEqual(videos[0]["cover_url"], "https://cdn.example/cover.jpg")

    def test_parse_base64_video_model_response(self):
        encoded = base64.b64encode(b"https://cdn.example/encoded.mp4").decode()
        content = {
            "data": [
                {
                    "video_model": json.dumps({"video_list": {"q": {"main_url": encoded}}}),
                }
            ]
        }
        raw = sse(
            event(
                {
                    "event_type": 2001,
                    "event_data": {
                        "message": {"content_type": 2021, "content": json.dumps(content)}
                    },
                }
            )
        )
        videos, _answer = CLIENT_MODULE.parse_video_response(raw)
        self.assertEqual(videos[0]["video_url"], "https://cdn.example/encoded.mp4")

    def test_gateway_error_is_reported(self):
        raw = "event: gateway-error\ndata: {\"code\":710012001,\"message\":\"expired\"}\n\n"
        with self.assertRaises(CLIENT_MODULE.ClientError):
            CLIENT_MODULE.parse_video_response(raw)

    def test_ratio_and_model_validation(self):
        self.assertEqual(CLIENT_MODULE.validate_ratio("1:1"), "1:1")
        self.assertEqual(CLIENT_MODULE.validate_model("seedance-2.0"), "seedance-2.0")
        with self.assertRaises(CLIENT_MODULE.ClientError):
            CLIENT_MODULE.validate_ratio("4:3")
        with self.assertRaises(CLIENT_MODULE.ClientError):
            CLIENT_MODULE.validate_model("kling-video-v3")

    def test_config_status_does_not_print_cookie_values(self):
        self.save()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            CLIENT_MODULE.config_status()
        text = output.getvalue()
        self.assertIn("configured", text)
        self.assertNotIn("session-secret", text)
        self.assertNotIn("tt-value", text)

    def test_task_log_does_not_include_cookies(self):
        with tempfile.TemporaryDirectory() as output:
            log = CLIENT_MODULE.TaskLog(output, "generate")
            log.event("request_validated", ratio="16:9")
            text = log.path.read_text(encoding="utf-8")
        self.assertIn("request_validated", text)
        self.assertNotIn("session-secret", text)


if __name__ == "__main__":
    unittest.main()
