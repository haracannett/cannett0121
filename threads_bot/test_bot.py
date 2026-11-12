import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import bot


def make_post(pid, when, affiliate=None):
    post = {"id": pid, "scheduled_at": when, "text": f"{pid} の本文"}
    if affiliate:
        post["affiliate"] = affiliate
    return post


class FakeClient:
    def __init__(self, fail_on_reply=False):
        self.calls = []
        self.fail_on_reply = fail_on_reply

    def publish(self, text, image_url=None, reply_to_id=None):
        self.calls.append({"text": text, "image_url": image_url, "reply_to_id": reply_to_id})
        if reply_to_id and self.fail_on_reply:
            raise bot.ThreadsError("reply failed")
        return f"id{len(self.calls)}"


NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
AFF = {"text": "愛用のおやつ", "url": "https://example.com/a"}


class AffiliateTextTest(unittest.TestCase):
    def test_adds_pr_label_when_missing(self):
        self.assertEqual(bot.affiliate_reply_text(AFF), "【PR】愛用のおやつ\nhttps://example.com/a")

    def test_keeps_existing_label(self):
        text = bot.affiliate_reply_text({"text": "#PR 愛用のおやつ", "url": "https://example.com/a"})
        self.assertEqual(text, "#PR 愛用のおやつ\nhttps://example.com/a")


class ValidateTest(unittest.TestCase):
    def test_requires_timezone(self):
        problems = bot.validate_post(make_post("a", "2026-11-03T20:00:00"))
        self.assertTrue(any("タイムゾーン" in p for p in problems))

    def test_rejects_long_text(self):
        post = make_post("a", "2026-11-03T20:00:00+09:00")
        post["text"] = "あ" * 501
        self.assertTrue(bot.validate_post(post))

    def test_rejects_too_many_links(self):
        post = make_post("a", "2026-11-03T20:00:00+09:00")
        post["text"] = " ".join(f"https://example.com/{i}" for i in range(6))
        self.assertTrue(bot.validate_post(post))

    def test_rejects_duplicate_ids(self):
        posts = [make_post("a", "2026-11-03T20:00:00+09:00")] * 2
        self.assertTrue(any("重複" in p for p in bot.validate_all(posts)))


def make_jpeg(with_gps):
    tag = 0x8825 if with_gps else 0x010F  # GPS 情報 / カメラのメーカー
    ifd = (1).to_bytes(2, "little") + tag.to_bytes(2, "little") + bytes(10) + bytes(4)
    tiff = b"II*\x00" + (8).to_bytes(4, "little") + ifd
    app1 = b"Exif\x00\x00" + tiff
    return b"\xff\xd8\xff\xe1" + (len(app1) + 2).to_bytes(2, "big") + app1 + b"\xff\xd9"


class ImageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(bot, "HERE", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.dir / "photos").mkdir()

    def test_accepts_photo_without_gps(self):
        (self.dir / "photos" / "ok.jpg").write_bytes(make_jpeg(with_gps=False))
        self.assertEqual(bot.validate_image("a", "photos/ok.jpg"), [])

    def test_rejects_photo_with_gps(self):
        (self.dir / "photos" / "home.jpg").write_bytes(make_jpeg(with_gps=True))
        self.assertTrue(any("位置情報" in p for p in bot.validate_image("a", "photos/home.jpg")))

    def test_rejects_missing_photo(self):
        self.assertTrue(bot.validate_image("a", "photos/none.jpg"))

    def test_builds_public_url_from_commit(self):
        env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "abc123"}
        with mock.patch.dict(os.environ, env):
            url = bot.image_url_for({"image": "photos/ゆず.jpg"})
        self.assertTrue(url.startswith("https://raw.githubusercontent.com/owner/repo/abc123/"))
        self.assertTrue(url.endswith("/photos/%E3%82%86%E3%81%9A.jpg"))

    def test_image_url_takes_priority(self):
        self.assertEqual(bot.image_url_for({"image_url": "https://x.test/a.jpg"}), "https://x.test/a.jpg")


class RunPostTest(unittest.TestCase):
    def test_skips_future_posts(self):
        posts = [make_post("later", "2026-11-06T20:00:00+09:00")]
        client = FakeClient()
        self.assertFalse(bot.run_post(client, posts, {"posted": {}}, NOW, log=lambda *a: None))
        self.assertEqual(client.calls, [])

    def test_posts_earliest_due_then_replies_with_affiliate(self):
        posts = [
            make_post("second", "2026-11-04T20:00:00+09:00", AFF),
            make_post("first", "2026-11-03T20:00:00+09:00"),
        ]
        state = {"posted": {}}
        client = FakeClient()
        bot.run_post(client, posts, state, NOW, log=lambda *a: None)
        self.assertEqual(list(state["posted"]), ["first"])
        self.assertEqual(len(client.calls), 1)

        bot.run_post(client, posts, state, NOW, log=lambda *a: None)
        self.assertEqual(client.calls[2]["reply_to_id"], state["posted"]["second"]["media_id"])
        self.assertTrue(client.calls[2]["text"].startswith("【PR】"))
        self.assertEqual(state["posted"]["second"]["reply_id"], "id3")

    def test_failed_reply_is_retried_without_reposting(self):
        posts = [make_post("a", "2026-11-03T20:00:00+09:00", AFF)]
        state = {"posted": {}}
        with self.assertRaises(bot.ThreadsError):
            bot.run_post(FakeClient(fail_on_reply=True), posts, state, NOW, log=lambda *a: None)
        self.assertIn("a", state["posted"])
        self.assertNotIn("reply_id", state["posted"]["a"])

        client = FakeClient()
        bot.run_post(client, posts, state, NOW, log=lambda *a: None)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["reply_to_id"], state["posted"]["a"]["media_id"])

    def test_dry_run_changes_nothing(self):
        posts = [make_post("a", "2026-11-03T20:00:00+09:00", AFF)]
        state = {"posted": {}}
        client = FakeClient()
        bot.run_post(client, posts, state, NOW, dry_run=True, log=lambda *a: None)
        self.assertEqual(state, {"posted": {}})
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
