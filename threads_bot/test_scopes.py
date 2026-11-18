"""権限の確認（scopes）の説明文のテスト。"""

import unittest

import bot


class DescribeScopesTest(unittest.TestCase):
    def test_all_present(self):
        text, missing = bot.describe_scopes(
            {"data": {"is_valid": True, "scopes": list(bot.REQUIRED_SCOPES), "expires_at": 1798761600}}
        )
        self.assertEqual(missing, [])
        self.assertIn("そろっています", text)
        self.assertIn("2027-01-01", text)

    def test_reply_scope_missing(self):
        text, missing = bot.describe_scopes(
            {"data": {"scopes": ["threads_basic", "threads_content_publish"]}}
        )
        self.assertEqual(missing, ["threads_manage_replies"])
        self.assertIn("足りない権限: threads_manage_replies", text)


if __name__ == "__main__":
    unittest.main()
