#!/usr/bin/env python3
"""webページをフルページ（またはビューポート1枚）でキャプチャする。

撮影エンジンは2段構え:
    1. Playwright CLI（`npx playwright screenshot --full-page`）… フルページ撮影
    2. ヘッドレスChrome（--headless=new --screenshot）… Playwright不在時の
       フォールバック。ビューポート1枚のみ（fallback_single: true）

⚠️ npx は cwd から playwright を解決する。対象リポジトリが devDependency で
playwright を持つ場合、本スクリプトは**必ず対象リポジトリを cwd にして**実行する
こと（別の cwd から実行すると無言で Chrome フォールバックに落ちる）。

iOS backend の paged_capture.py と同じ出力契約を満たす。webのフルページ撮影は
1枚の縦長PNGになるため pages は常に1（フルページが撮れていれば全域カバー済み）。

使い方:
    python3 capture.py --url <URL> --screen-id <id> \
        --out-dir <dir> --prefix before|after \
        [--viewport 1280x800] [--settle-ms 2000] [--wait-ready-sec 60]

出力: 撮影結果のJSONをstdoutに1行で出す。
    {"screen_id": ..., "pages": 1, "files": [...], "truncated": false,
     "fallback_single": bool, "paging_failed": false}
- fallback_single: Playwright不在でビューポート1枚のみ撮影した（スクロール領域は未撮影）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


def wait_ready(url: str, timeout_sec: int) -> int | None:
    """serve_command 起動直後のURLがHTTP応答を返すまでポーリングし、ステータスコードを返す。

    4xx/5xxでも「サーバは起動している」とみなして返す（起動判定と成否判定は分ける）。
    接続拒否/タイムアウトのみ待ち、期限切れは None。
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as res:
                return res.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(1)
    return None


def playwright_available() -> bool:
    if shutil.which("npx") is None:
        return False
    try:
        r = subprocess.run(
            ["npx", "--no-install", "playwright", "--version"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def capture_playwright(url: str, out: Path, viewport: str, full_page: bool, settle_ms: int) -> bool:
    w, h = viewport.split("x")
    cmd = [
        "npx", "--no-install", "playwright", "screenshot",
        f"--viewport-size={w},{h}",
        f"--wait-for-timeout={settle_ms}",
    ]
    if full_page:
        cmd.append("--full-page")
    cmd += [url, str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("WARN: playwright screenshot timed out (120s)", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"WARN: playwright screenshot failed: {r.stderr.strip()[-300:]}", file=sys.stderr)
    return r.returncode == 0 and out.exists()


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        if os.path.sep in candidate:
            if Path(candidate).exists():
                return candidate
        elif shutil.which(candidate):
            return candidate
    return None


def capture_chrome(chrome: str, url: str, out: Path, viewport: str, settle_ms: int) -> bool:
    w, h = viewport.split("x")
    try:
        r = subprocess.run([
            chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={w},{h}",
            f"--virtual-time-budget={settle_ms}",
            f"--screenshot={out}",
            url,
        ], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("WARN: chrome screenshot timed out (120s)", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"WARN: chrome screenshot failed: {r.stderr.strip()[-300:]}", file=sys.stderr)
    return r.returncode == 0 and out.exists()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="撮影対象のフルURL")
    parser.add_argument("--screen-id", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--prefix", required=True, help="before / after")
    parser.add_argument("--viewport", default="1280x800", help="WxH（CSS px）")
    parser.add_argument("--no-full-page", action="store_true", help="ビューポート1枚のみ撮影する")
    parser.add_argument("--settle-ms", type=int, default=2000, help="描画待ち（ms）")
    parser.add_argument("--wait-ready-sec", type=int, default=60,
                        help="URLがHTTP応答を返すまでの待機上限。0で疎通確認をスキップ")
    parser.add_argument("--allow-http-error", action="store_true",
                        help="対象URLが4xx/5xxでも撮影する（エラーページ自体を比較したい場合）")
    args = parser.parse_args()

    # \d はPythonでは全角数字にもマッチするため [0-9] を明示し、値域も検証する
    vp = re.fullmatch(r"([0-9]+)x([0-9]+)", args.viewport, re.ASCII)
    if not vp or not (1 <= int(vp.group(1)) <= 10000 and 1 <= int(vp.group(2)) <= 10000):
        print(f"ERROR: --viewport は WxH 形式（各1〜10000）で指定してください（例 1280x800）: {args.viewport}",
              file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.prefix}-{args.screen_id}-p1.png"
    # 前回実行の同名PNGが残っていると「撮影失敗なのに既存ファイルで成功判定」になるため必ず消す
    out.unlink(missing_ok=True)

    if args.wait_ready_sec > 0:
        status = wait_ready(args.url, args.wait_ready_sec)
        if status is None:
            print(f"ERROR: {args.url} が {args.wait_ready_sec}s 以内に応答しません（serve_command の起動失敗？）",
                  file=sys.stderr)
            return 1
        if status >= 400 and not args.allow_http_error:
            print(f"ERROR: {args.url} が HTTP {status} を返しました。エラーページを正常キャプチャとして"
                  "扱わないため中断します（意図的なら --allow-http-error）", file=sys.stderr)
            return 1

    full_page = not args.no_full_page
    fallback_single = False

    # Playwrightは「パッケージはあるがブラウザ未インストール（要 playwright install）」で
    # 失敗することがあるため、失敗時もChromeフォールバックに落とす
    ok = False
    if playwright_available():
        ok = capture_playwright(args.url, out, args.viewport, full_page, args.settle_ms)
    if not ok:
        chrome = find_chrome()
        if chrome is None:
            print("ERROR: Playwright での撮影に失敗し、ヘッドレスChromeも見つかりません。"
                  "`npm i -D playwright && npx playwright install chromium` を推奨", file=sys.stderr)
            return 1
        ok = capture_chrome(chrome, args.url, out, args.viewport, args.settle_ms)
        fallback_single = full_page  # フルページ指定だったのにビューポート1枚に落ちた場合のみ警告扱い

    if not ok:
        print(f"ERROR: screenshot failed: {args.url}", file=sys.stderr)
        return 1

    print(json.dumps({
        "screen_id": args.screen_id,
        "pages": 1,
        "files": [str(out)],
        "truncated": False,
        "fallback_single": fallback_single,
        "paging_failed": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
