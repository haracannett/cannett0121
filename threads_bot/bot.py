"""Threads 自動投稿ボット。

予約された投稿を Threads API で公開し、その投稿へのリプライ（コメント欄）に
PR表記付きのアフィリエイトリンクを書き込む。

使い方:
    python threads_bot/bot.py post      # 予定時刻を過ぎた投稿を1件公開
    python threads_bot/bot.py post --dry-run
    python threads_bot/bot.py check     # posts.json の内容を検証するだけ
    python threads_bot/bot.py refresh --token-file new_token.txt  # 長期アクセストークンを更新

環境変数:
    THREADS_ACCESS_TOKEN  長期アクセストークン（必須。check では不要）
    THREADS_USER_ID       Threads のユーザーID（省略時は "me"）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://graph.threads.net/v1.0"
HERE = Path(__file__).resolve().parent
POSTS_FILE = HERE / "posts.json"
STATE_FILE = HERE / "state.json"

MAX_TEXT_LENGTH = 500
MAX_LINKS = 5
# 公式ドキュメントが推奨する、コンテナ作成から公開までの待ち時間
PUBLISH_WAIT_SECONDS = 30
# ステマ規制（景品表示法）対応：アフィリエイトのリプライには必ず広告表記を入れる
PR_LABEL = "【PR】"
PR_MARKERS = ("PR", "広告", "プロモーション", "アフィリエイト")
URL_PATTERN = re.compile(r"https?://\S+")


class ThreadsError(Exception):
    pass


# ---------- 投稿データ ----------

def load_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def parse_time(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"scheduled_at にタイムゾーンがありません: {value}（例: 2026-11-03T20:00:00+09:00）")
    return dt


def affiliate_reply_text(affiliate):
    """アフィリエイトのリプライ本文。広告表記がなければ先頭に付ける。"""
    text = affiliate.get("text", "").strip()
    url = affiliate["url"].strip()
    if not any(marker in text for marker in PR_MARKERS):
        text = f"{PR_LABEL}{text}"
    return f"{text}\n{url}" if text else f"{PR_LABEL}\n{url}"


def validate_post(post):
    """投稿1件の問題点を文字列のリストで返す。"""
    problems = []
    pid = post.get("id")
    if not pid:
        problems.append("id がありません")
    if not post.get("text", "").strip():
        problems.append(f"{pid}: text が空です")
    try:
        parse_time(post.get("scheduled_at", ""))
    except ValueError as e:
        problems.append(f"{pid}: {e}")

    bodies = [("本文", post.get("text", ""))]
    affiliate = post.get("affiliate")
    if affiliate:
        if not affiliate.get("url", "").startswith("https://"):
            problems.append(f"{pid}: affiliate.url は https:// で始めてください")
        elif "example.com" in affiliate["url"]:
            problems.append(f"{pid}: affiliate.url がサンプルのままです。自分のアフィリエイトリンクに差し替えてください")
        else:
            bodies.append(("アフィリエイトのリプライ", affiliate_reply_text(affiliate)))
    for label, body in bodies:
        if len(body) > MAX_TEXT_LENGTH:
            problems.append(f"{pid}: {label}が{len(body)}文字です（上限{MAX_TEXT_LENGTH}文字）")
        if len(set(URL_PATTERN.findall(body))) > MAX_LINKS:
            problems.append(f"{pid}: {label}のリンクが{MAX_LINKS}個を超えています")

    image_url = post.get("image_url")
    if image_url and not image_url.startswith("https://"):
        problems.append(f"{pid}: image_url は公開されている https:// のURLにしてください")
    return problems


def validate_all(posts):
    problems = []
    seen = set()
    for post in posts:
        problems += validate_post(post)
        if post.get("id") in seen:
            problems.append(f"id が重複しています: {post.get('id')}")
        seen.add(post.get("id"))
    return problems


def next_work(posts, state, now):
    """次にやることを返す。

    ("reply", post) … 公開済みだがアフィリエイトのリプライが未完了
    ("post", post)  … 予定時刻を過ぎた未公開の投稿（予定時刻が早い順）
    None            … 何もしない
    """
    done = state.get("posted", {})
    for post in posts:
        record = done.get(post["id"])
        if record and post.get("affiliate") and not record.get("reply_id"):
            return ("reply", post)
    due = [p for p in posts if p["id"] not in done and parse_time(p["scheduled_at"]) <= now]
    if not due:
        return None
    due.sort(key=lambda p: parse_time(p["scheduled_at"]))
    return ("post", due[0])


# ---------- Threads API ----------

class ThreadsClient:
    def __init__(self, token, user_id="me", wait=PUBLISH_WAIT_SECONDS):
        self.token = token
        self.user_id = user_id
        self.wait = wait

    def _request(self, method, path, params):
        params = {**params, "access_token": self.token}
        url = f"{API_BASE}/{path}"
        data = None
        if method == "GET":
            url += "?" + urllib.parse.urlencode(params)
        else:
            data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise ThreadsError(f"{method} {path} が失敗しました（HTTP {e.code}）: {body}") from None

    def _wait_until_ready(self, container_id):
        time.sleep(self.wait)
        for _ in range(10):
            status = self._request("GET", container_id, {"fields": "status,error_message"})
            if status.get("status") == "FINISHED":
                return
            if status.get("status") in ("ERROR", "EXPIRED"):
                raise ThreadsError(f"コンテナ {container_id} の処理に失敗しました: {status}")
            time.sleep(10)
        raise ThreadsError(f"コンテナ {container_id} の処理が終わりませんでした")

    def publish(self, text, image_url=None, reply_to_id=None):
        """投稿（またはリプライ）を作成して公開し、公開された投稿のIDを返す。"""
        params = {"text": text}
        if image_url:
            params.update(media_type="IMAGE", image_url=image_url)
        else:
            params["media_type"] = "TEXT"
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        container = self._request("POST", f"{self.user_id}/threads", params)
        self._wait_until_ready(container["id"])
        published = self._request("POST", f"{self.user_id}/threads_publish", {"creation_id": container["id"]})
        return published["id"]

    def refresh_token(self):
        return self._request("GET", "refresh_access_token", {"grant_type": "th_refresh_token"})


# ---------- コマンド ----------

def run_post(client, posts, state, now, dry_run=False, log=print):
    work = next_work(posts, state, now)
    if work is None:
        log("公開する投稿はありません。")
        return False
    kind, post = work
    record = state.setdefault("posted", {}).get(post["id"])

    if kind == "post":
        log(f"投稿します: {post['id']}")
        if dry_run:
            log(post["text"])
        else:
            media_id = client.publish(post["text"], image_url=post.get("image_url"))
            record = {"media_id": media_id, "posted_at": now.isoformat()}
            state["posted"][post["id"]] = record
            log(f"公開しました: media_id={media_id}")

    affiliate = post.get("affiliate")
    if affiliate:
        reply = affiliate_reply_text(affiliate)
        log(f"アフィリエイトのリプライ: {post['id']}")
        if dry_run:
            log(reply)
        else:
            reply_id = client.publish(reply, reply_to_id=record["media_id"])
            record["reply_id"] = reply_id
            log(f"リプライしました: reply_id={reply_id}")
    return not dry_run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Threads 自動投稿ボット")
    sub = parser.add_subparsers(dest="command", required=True)
    post_cmd = sub.add_parser("post", help="予定時刻を過ぎた投稿を1件公開する")
    post_cmd.add_argument("--dry-run", action="store_true", help="APIを呼ばずに内容だけ表示する")
    sub.add_parser("check", help="posts.json を検証する")
    refresh_cmd = sub.add_parser("refresh", help="長期アクセストークンを更新する")
    refresh_cmd.add_argument("--token-file", help="新しいトークンを書き出すファイル（画面には表示しない）")
    args = parser.parse_args(argv)

    posts = load_json(POSTS_FILE, {"posts": []})["posts"]
    problems = validate_all(posts)
    if problems:
        print("posts.json に問題があります:", *problems, sep="\n- ", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"posts.json はOKです（{len(posts)}件）。")
        return 0

    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token and not (args.command == "post" and args.dry_run):
        print("THREADS_ACCESS_TOKEN が設定されていません。", file=sys.stderr)
        return 1
    client = ThreadsClient(token or "", os.environ.get("THREADS_USER_ID", "me"))

    if args.command == "refresh":
        result = client.refresh_token()
        days = result.get("expires_in", 0) // 86400
        if args.token_file:
            Path(args.token_file).write_text(result["access_token"], encoding="utf-8")
        print(f"トークンを更新しました（有効期限: 約{days}日）。")
        return 0

    state = load_json(STATE_FILE, {"posted": {}})
    try:
        run_post(client, posts, state, datetime.now(timezone.utc), dry_run=args.dry_run)
    finally:
        # 投稿の途中で失敗しても、公開できた分は記録して二重投稿を防ぐ
        if not args.dry_run:
            save_json(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
