"""Offline conversation tests with real CAMEL agents and a temporary SQLite DB."""
import base64
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from camel.models import ModelFactory, StubModel
from camel.types import ModelType
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class RecordingModel(StubModel):
    """Replace inference only; CAMEL still builds and serializes agent memory."""

    def __init__(self):
        super().__init__(ModelType.STUB)
        self.requests = []

    def _run(self, messages, response_format=None, tools=None):
        self.requests.append(copy.deepcopy(messages))
        stage = (len(self.requests) - 1) % 3
        if stage == 0:
            content = "Provide support in a conversation with a university student."
        elif stage == 1:
            content = "Instruction: Respond to the student.\nInput: Please continue."
        else:
            remembers = any(m["content"] == "我的代号是风筝74。" for m in messages)
            content = "Solution: " + ("你的代号是风筝74。" if remembers else "请继续分享。")
        response = super()._run(messages, response_format, tools)
        response.choices[0].message.content = content
        return response


class ConversationHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use the application's PROJECT_BASE override, never the user's config/DB.
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.addClassCleanup(os.chdir, os.getcwd())
        cls.env = patch.dict(os.environ, {"PROJECT_BASE": cls.temp.name, "MPLBACKEND": "Agg"})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        original_path = sys.path[:]
        cls.addClassCleanup(sys.path.__setitem__, slice(None), original_path)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        os.chdir(cls.temp.name)
        conf = Path("conf")
        conf.mkdir()
        # A synthetic encrypted API key exercises real startup/decryption without credentials.
        key, iv = b"0" * 32, b"1" * 16
        padder = padding.PKCS7(128).padder()
        padded = padder.update(b"offline-test-key") + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypted = base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()
        (conf / "public.pem").write_bytes(key)
        (conf / "service_conf.yaml").write_text(
            f"llm:\n  api_key: '{encrypted}'\n  model_type: 'offline'\n"
            "  model_url: 'http://unused.invalid/v1'\n"
            "admin:\n  username: 'test-admin'\n  password: 'test-password'\n"
            "  name_nick: 'Test'\n", encoding="utf-8"
        )
        with patch.object(ModelFactory, "create", return_value=RecordingModel()):
            from api.apps import conversation_app
        from api import settings
        from api.db.db_models import Conversation, User
        import matplotlib.pyplot as plt
        from loguru import logger

        cls.app, cls.settings = conversation_app, settings
        cls.conversations = Conversation
        cls.users = [User.create(username=f"student{i}", name_nick="Student", password="unused") for i in range(2)]
        cls.addClassCleanup(settings.databaseConn.db.close)
        cls.addClassCleanup(logger.remove)
        cls.addClassCleanup(plt.close, "all")

    def setUp(self):
        self.model = RecordingModel()
        self.model_patch = patch.object(self.app, "CHAT_MDL", self.model)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.settings.EMOTION_RECORDS.clear()
        self.conversations.delete().execute()

    def send(self, text, history, user_index=0):
        import matplotlib.pyplot as plt
        result, chart = self.app.process_user_input({"id": self.users[user_index].id}, text, history)
        plt.close(chart)
        return result

    def test_second_turn_restores_both_agents_history_without_duplicates(self):
        welcome = [{"role": "assistant", "content": "欢迎开始对话。"}]
        first = self.send("我的代号是风筝74。", welcome)
        original = copy.deepcopy(first)
        second = self.send("我刚才说的代号是什么？", first)
        self.assertEqual(second[-1]["content"], "你的代号是风筝74。")
        self.assertEqual(first, original)
        self.assertEqual(second[:-2], first)
        self.assertEqual(len(second), 5)
        self.assertEqual(len(self.model.requests), 6)  # No extra model calls for replay.
        for requests, reverse_roles in ((self.model.requests[4], True), (self.model.requests[5], False)):
            for message in first:
                matches = [m for m in requests if m["content"] == message["content"]]
                self.assertEqual(len(matches), 1)
                role = message["role"]
                expected = ("assistant" if role == "user" else "user") if reverse_roles else role
                self.assertEqual(matches[0]["role"], expected)
        self.assertEqual(self.conversations.select().count(), 2)

    def test_new_sessions_do_not_inherit_other_conversations(self):
        self.send("我的代号是风筝74。", [])
        for user_index in (0, 1):
            with self.subTest(user_index=user_index):
                start = len(self.model.requests)
                result = self.send("我刚才说的代号是什么？", [], user_index)
                self.assertEqual(result[-1]["content"], "请继续分享。")
                self.assertEqual(len(result), 2)
                for messages in self.model.requests[start:]:
                    self.assertFalse(any("风筝74" in m["content"] for m in messages))

    def test_existing_history_is_preserved_in_order_in_model_requests(self):
        history = [
            {"role": "user", "content": "第一条学生消息"},
            {"role": "assistant", "content": "第一条助手回复"},
            {"role": "user", "content": "第二条学生消息"},
            {"role": "assistant", "content": "第二条助手回复"},
        ]
        # A coarse clock must not let CAMEL's score tie-breaker reverse replayed turns.
        now_ns = time.time_ns()
        with patch("time.time_ns", return_value=now_ns), patch("time.time", return_value=now_ns / 1e9):
            self.send("继续", history)
        expected = [m["content"] for m in history]
        for messages in self.model.requests[1:]:
            actual = [m["content"] for m in messages if m["content"] in expected]
            self.assertEqual(actual, expected)
            self.assertNotIn(messages[-1]["content"], expected)


if __name__ == "__main__":
    unittest.main()
