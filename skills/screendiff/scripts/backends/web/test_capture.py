#!/usr/bin/env python3
"""capture.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/backends/web/test_capture.py

撮影そのもの（Playwright / Chrome）は環境依存のため対象外。ここで守るのは
**「要求した画面と違うものを撮っていないか」の判定ロジック**:
  - 別URLへリダイレクトされた状態で撮ると、ログイン画面等を「その画面」として
    記録し、Before/After が一致して「差分なし」と誤結論する
  - 一方で末尾スラッシュの正規化リダイレクトで止まると、正当な設定が動かない

ローカルHTTPサーバを立てるテストは、リダイレクトを実際に踏ませて検証する。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture import extract_title, find_chrome, playwright_available, same_url  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / 'capture.py'

# 撮影まで進むテストはブラウザが要る。無い環境（クリーンなCI等）では skip する。
# 「撮影エンジン不要」と謳って実は必要、という状態にしないための判定
HAS_ENGINE = playwright_available() or find_chrome() is not None
NO_ENGINE_REASON = '撮影エンジン（Playwright / Chrome）が無い環境のためスキップ'


def _page(title: str) -> bytes:
    return f'<html><head><title>{title}</title></head><body>{title}</body></html>'.encode()


class _Handler(BaseHTTPRequestHandler):
    """/protected は /login へ302。/dir は末尾スラッシュへ301（正規化リダイレクト）。"""

    def do_GET(self):
        if self.path == '/protected':
            self.send_response(302)
            self.send_header('Location', '/login')
            self.end_headers()
            return
        if self.path == '/dir':
            self.send_response(301)
            self.send_header('Location', '/dir/')
            self.end_headers()
            return
        titles = {'/login': 'ログイン', '/dir/': 'ディレクトリ'}
        body = _page(titles.get(self.path, 'ホーム'))
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestUrlIdentity(unittest.TestCase):
    """純関数レベルの判定"""

    def test_trailing_slash_only_difference_is_same(self):
        self.assertTrue(same_url('http://h:1/a', 'http://h:1/a/'))

    def test_different_path_is_not_same(self):
        self.assertFalse(same_url('http://h:1/a', 'http://h:1/b'))

    def test_different_host_is_not_same(self):
        """localhost と 127.0.0.1 は別origin（cookie/storageの意味が変わる）"""
        self.assertFalse(same_url('http://localhost:1/a', 'http://127.0.0.1:1/a'))

    def test_query_value_ending_in_slash_is_not_normalized(self):
        """URL全体に rstrip("/") をかけるとクエリ値の変化を末尾スラッシュ差と誤認する"""
        self.assertFalse(same_url('http://h/page?tab=/', 'http://h/page?tab='))

    def test_query_change_is_not_same(self):
        self.assertFalse(same_url('http://h/a?x=1', 'http://h/a?x=2'))

    def test_query_dropped_is_not_same(self):
        """クエリが落ちるのは別状態（一覧のフィルタが外れる等）"""
        self.assertFalse(same_url('http://h/a?x=1', 'http://h/a'))

    def test_trailing_slash_with_query_is_same(self):
        """path側の正規化はクエリがあっても効く"""
        self.assertTrue(same_url('http://h/dir?x=1', 'http://h/dir/?x=1'))

    def test_different_scheme_is_not_same(self):
        self.assertFalse(same_url('http://h/a', 'https://h/a'))

    def test_different_port_is_not_same(self):
        self.assertFalse(same_url('http://h:1/a', 'http://h:2/a'))

    def test_extract_title_collapses_whitespace(self):
        self.assertEqual(extract_title(b'<title>\n  ho ge \n</title>'), 'ho ge')

    def test_extract_title_missing_returns_none(self):
        self.assertIsNone(extract_title(b'<html><body>no title</body></html>'))

    def test_extract_title_empty_returns_none(self):
        """空タイトルを空文字で返すと「取得できた」と誤読されるため None にする"""
        self.assertIsNone(extract_title(b'<title></title>'))


class TestRedirectDetection(unittest.TestCase):
    """実サーバを立ててリダイレクト時の中断/継続を検証する"""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _capture(self, path: str, *extra: str) -> tuple[int, dict, str, Path]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT),
             '--url', f'http://127.0.0.1:{self.port}{path}',
             '--screen-id', 'target', '--out-dir', str(tmp), '--prefix', 'after',
             '--viewport', '400x300', '--settle-ms', '100', '--wait-ready-sec', '10', *extra],
            capture_output=True, text=True,
        )
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = {}
        return proc.returncode, out, proc.stderr, tmp

    def test_redirect_to_other_screen_aborts(self):
        """認証ガード等で別画面に飛ばされたら撮らない"""
        code, _, stderr, tmp = self._capture('/protected')
        self.assertEqual(code, 1)
        self.assertIn('リダイレクト', stderr)
        # 撮影前に止まるのでPNGを残さない（残すと後段が成功と誤認する）
        self.assertEqual(list(tmp.glob('*.png')), [])

    @unittest.skipUnless(HAS_ENGINE, NO_ENGINE_REASON)
    def test_redirect_allowed_explicitly_records_final_url(self):
        """明示許可した場合は撮るが、何を撮ったかは記録に残す"""
        code, out, _, _ = self._capture('/protected', '--allow-redirect')
        self.assertEqual(code, 0)
        self.assertTrue(out['final_url'].endswith('/login'))
        self.assertEqual(out['title'], 'ログイン')

    @unittest.skipUnless(HAS_ENGINE, NO_ENGINE_REASON)
    def test_trailing_slash_redirect_does_not_abort(self):
        """ディレクトリindexへの正規化リダイレクトで止まると正当な設定が動かない"""
        code, out, stderr, _ = self._capture('/dir')
        self.assertEqual(code, 0, msg=stderr)
        self.assertEqual(out['title'], 'ディレクトリ')

    @unittest.skipUnless(HAS_ENGINE, NO_ENGINE_REASON)
    def test_records_what_was_captured(self):
        code, out, _, _ = self._capture('/')
        self.assertEqual(code, 0)
        self.assertIn('requested_url', out)
        self.assertIn('final_url', out)
        self.assertEqual(out['title'], 'ホーム')


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ capture self-test PASSED' if ok else '❌ capture self-test FAILED')
    sys.exit(0 if ok else 1)
