"""投稿の宛先（ユーザーID）が空のときの確認。

GitHub のシークレット THREADS_USER_ID を登録していないと、環境変数が空文字で
届く。以前はそれをそのまま宛先に使い、"/threads" という存在しない場所へ
送って毎回失敗していた（HTTP 400, Object with ID 'threads' does not exist）。
"""

import os
import unittest
from unittest import mock

import bot


class RecordingClient(bot.ThreadsClient):
    """通信のかわりに、どこへ何を送ろうとしたかを覚えておく。"""

    def __init__(self, user_id="me", me_id="1234567890"):
        super().__init__("token", user_id, wait=0)
        self.me_id = me_id
        self.sent = []

    def _request(self, method, path, params):
        self.sent.append((method, path))
        if (method, path) == ("GET", "me"):
            return {"id": self.me_id} if self.me_id else {}
        if path.endswith("/threads"):
            return {"id": "container-1"}
        if path.endswith("/threads_publish"):
            return {"id": "published-1"}
        if method == "GET" and path == "container-1":
            return {"status": "FINISHED"}
        raise AssertionError(f"思いがけない宛先: {method} {path}")


class UserIdTest(unittest.TestCase):
    def test_empty_string_is_treated_as_unset(self):
        self.assertEqual(bot.ThreadsClient("t", "").user_id, "me")
        self.assertEqual(bot.ThreadsClient("t", None).user_id, "me")

    def test_empty_env_var_does_not_break_destination(self):
        # シークレット未登録 → 環境変数が空文字で届く、という状況を再現する
        client = RecordingClient(user_id="")
        client.publish("本文")
        posted_to = [path for method, path in client.sent if method == "POST"]
        self.assertEqual(posted_to, ["1234567890/threads", "1234567890/threads_publish"])
        self.assertNotIn("/threads", posted_to)  # 先頭にIDの無い宛先は使わない

    def test_explicit_user_id_is_used_without_asking(self):
        client = RecordingClient(user_id="999")
        client.publish("本文")
        self.assertNotIn(("GET", "me"), client.sent)
        posted_to = [path for method, path in client.sent if method == "POST"]
        self.assertEqual(posted_to, ["999/threads", "999/threads_publish"])

    def test_id_is_looked_up_only_once(self):
        client = RecordingClient()
        client.publish("1つ目")
        client.publish("2つ目")
        self.assertEqual(client.sent.count(("GET", "me")), 1)

    def test_missing_id_stops_before_sending_anything(self):
        client = RecordingClient(me_id=None)
        with self.assertRaises(bot.ThreadsError):
            client.publish("本文")
        # IDが分からなければ、投稿の下書きも作らない
        self.assertEqual([m for m, _ in client.sent if m == "POST"], [])

    def test_recent_posts_uses_resolved_id(self):
        client = RecordingClient()
        client._request = lambda method, path, params: (
            {"id": "1234567890"} if path == "me" else {"data": [{"id": "x"}]}
        )
        self.assertEqual(client.recent_posts(), [{"id": "x"}])
        self.assertEqual(client.user_id, "1234567890")


class MainEnvTest(unittest.TestCase):
    def test_main_builds_client_with_me_when_env_is_empty(self):
        captured = {}

        class Spy(bot.ThreadsClient):
            def __init__(self, token, user_id="me", wait=0):
                super().__init__(token, user_id, wait)
                captured["user_id"] = self.user_id

            def me(self):
                return {"id": "1", "username": "テスト"}

            def _request(self, method, path, params):
                raise AssertionError("テストで本物の通信をしてはいけません")

        env = {"THREADS_ACCESS_TOKEN": "t", "THREADS_USER_ID": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(bot, "ThreadsClient", Spy):
            # 公開する投稿が無い状態で止めるため、ドライランで呼ぶ
            bot.main(["post", "--dry-run"])
        self.assertEqual(captured["user_id"], "me")


if __name__ == "__main__":
    unittest.main()
