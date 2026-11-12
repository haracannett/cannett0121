"""「出たかどうか分からない」ときに、同じ投稿を2回出さないことの確認。

Threads に公開を送ったあと、返事が来ないまま通信が切れることがある。
そのとき送り直すと同じ投稿が2つ並んでしまうので、bot は送り直さずに
記録へ「要確認」を残し、次の実行で Threads に問い合わせて確かめる。
"""

import unittest
from datetime import datetime, timedelta, timezone

import bot


NOW = datetime(2026, 11, 14, 11, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
AFF = {"text": "愛用のカート", "url": "https://example.com/a"}


def make_post(pid="a", affiliate=None):
    post = {"id": pid, "scheduled_at": "2026-11-14T20:00:00+09:00", "text": f"{pid} の本文"}
    if affiliate:
        post["affiliate"] = affiliate
    return post


class FakeClient:
    """返事の返し方を自由に決められる、にせ Threads。"""

    def __init__(self, unknown_on=None, status=None, recent=None):
        self.calls = []
        self.unknown_on = unknown_on  # "post" か "reply"。その公開で返事が来なかったことにする
        self.status = status  # container_status が返す下書きの状態
        self.recent = recent or []  # recent_posts が返す最近の投稿
        self.status_calls = 0

    def publish(self, text, image_url=None, reply_to_id=None):
        kind = "reply" if reply_to_id else "post"
        self.calls.append({"kind": kind, "text": text, "reply_to_id": reply_to_id})
        if kind == self.unknown_on:
            raise bot.UnknownOutcome("返事が来ませんでした", f"container-{kind}", text, kind)
        return f"id{len(self.calls)}"

    def container_status(self, container_id):
        self.status_calls += 1
        return {"status": self.status}

    def recent_posts(self, limit=25):
        return self.recent


def run(client, posts, state, now=NOW):
    return bot.run_post(client, posts, state, now, log=lambda *a: None)


class UnknownPostTest(unittest.TestCase):
    def test_marks_needs_check_and_does_not_resend(self):
        posts = [make_post()]
        state = {"posted": {}}
        client = FakeClient(unknown_on="post")
        with self.assertRaises(bot.NeedsCheck):
            run(client, posts, state)
        self.assertEqual(len(client.calls), 1, "公開は1回だけ")
        self.assertIn("pending", state["posted"]["a"])
        self.assertNotIn("media_id", state["posted"]["a"])

    def test_next_run_does_not_repost_while_unresolved(self):
        posts = [make_post()]
        state = {"posted": {}}
        with self.assertRaises(bot.NeedsCheck):
            run(FakeClient(unknown_on="post"), posts, state)

        # 次の実行。まだ処理中なので、投稿も送らないままにする
        later = FakeClient(status="IN_PROGRESS")
        with self.assertRaises(bot.NeedsCheck):
            run(later, posts, state, now=LATER)
        self.assertEqual(later.calls, [], "2回目は何も送らない")

    def test_resolves_when_the_post_actually_went_out(self):
        posts = [make_post(affiliate=AFF)]
        state = {"posted": {}}
        with self.assertRaises(bot.NeedsCheck):
            run(FakeClient(unknown_on="post"), posts, state)

        # 実は出ていた。本文が一致する投稿が見つかる
        later = FakeClient(recent=[{"id": "real-123", "text": "a の本文"}])
        run(later, posts, state, now=LATER)
        record = state["posted"]["a"]
        self.assertEqual(record["media_id"], "real-123")
        self.assertNotIn("pending", record)
        # 投稿は送り直さず、リプライだけを送っている
        self.assertEqual([c["kind"] for c in later.calls], ["reply"])
        self.assertEqual(later.calls[0]["reply_to_id"], "real-123")

    def test_retries_when_the_post_did_not_go_out(self):
        posts = [make_post()]
        state = {"posted": {}}
        with self.assertRaises(bot.NeedsCheck):
            run(FakeClient(unknown_on="post"), posts, state)

        # 下書きが失敗していた＝出ていない。やり直してよい
        later = FakeClient(status="ERROR")
        run(later, posts, state, now=LATER)
        self.assertEqual([c["kind"] for c in later.calls], ["post"])
        self.assertEqual(state["posted"]["a"]["media_id"], "id1")
        self.assertNotIn("pending", state["posted"]["a"])

    def test_interrupted_before_any_answer_is_checked_not_reposted(self):
        # 送る直前で止まったときの記録（下書きIDがない）
        posts = [make_post()]
        state = {"posted": {"a": {"attempted_at": NOW.isoformat()}}}

        # 最近の投稿に無い＝出ていないので、やり直す
        client = FakeClient(recent=[{"id": "other", "text": "ちがう投稿"}])
        run(client, posts, state, now=LATER)
        self.assertEqual([c["kind"] for c in client.calls], ["post"])
        self.assertEqual(state["posted"]["a"]["media_id"], "id1")

    def test_interrupted_but_actually_published_is_not_reposted(self):
        posts = [make_post()]
        state = {"posted": {"a": {"attempted_at": NOW.isoformat()}}}

        client = FakeClient(recent=[{"id": "real-9", "text": "a の本文"}])
        run(client, posts, state, now=LATER)
        self.assertEqual(client.calls, [], "出ていたので送り直さない")
        self.assertEqual(state["posted"]["a"]["media_id"], "real-9")


class UnknownReplyTest(unittest.TestCase):
    def setUp(self):
        self.posts = [make_post(affiliate=AFF)]
        self.state = {"posted": {}}
        with self.assertRaises(bot.NeedsCheck):
            run(FakeClient(unknown_on="reply"), self.posts, self.state)

    def test_parent_is_kept_and_reply_is_pending(self):
        record = self.state["posted"]["a"]
        self.assertEqual(record["media_id"], "id1", "親の投稿は成功として残る")
        self.assertEqual(record["pending"]["kind"], "reply")

    def test_reply_that_went_out_is_not_sent_again(self):
        later = FakeClient(status="PUBLISHED")
        run(later, self.posts, self.state, now=LATER)
        record = self.state["posted"]["a"]
        self.assertTrue(record["reply_done"])
        self.assertNotIn("pending", record)
        self.assertEqual(later.calls, [], "親もリプライも送り直さない")

    def test_reply_that_did_not_go_out_is_retried_without_reposting_parent(self):
        later = FakeClient(status="EXPIRED")
        run(later, self.posts, self.state, now=LATER)
        self.assertEqual([c["kind"] for c in later.calls], ["reply"], "リプライだけ送り直す")
        self.assertEqual(self.state["posted"]["a"]["media_id"], "id1")
        self.assertEqual(self.state["posted"]["a"]["reply_id"], "id1")


class ErrorKindTest(unittest.TestCase):
    def test_server_error_is_treated_as_unknown(self):
        self.assertTrue(issubclass(bot.ThreadsUnreachable, bot.ThreadsError))
        self.assertTrue(issubclass(bot.UnknownOutcome, bot.ThreadsError))

    def test_definite_failure_clears_the_record_so_it_can_retry(self):
        class Failing(FakeClient):
            def publish(self, text, image_url=None, reply_to_id=None):
                self.calls.append({"kind": "post", "text": text, "reply_to_id": reply_to_id})
                raise bot.ThreadsError("本文が長すぎます")

        posts = [make_post()]
        state = {"posted": {}}
        with self.assertRaises(bot.ThreadsError):
            run(Failing(), posts, state)
        self.assertNotIn("a", state["posted"], "出ていないので記録を残さない")


class DryRunTest(unittest.TestCase):
    def test_dry_run_leaves_pending_alone(self):
        posts = [make_post()]
        state = {"posted": {}}
        with self.assertRaises(bot.NeedsCheck):
            run(FakeClient(unknown_on="post"), posts, state)

        client = FakeClient()
        bot.run_post(client, posts, state, LATER, dry_run=True, log=lambda *a: None)
        self.assertEqual(client.calls, [])
        self.assertIn("pending", state["posted"]["a"])


if __name__ == "__main__":
    unittest.main()
