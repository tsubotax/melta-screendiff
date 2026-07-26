#!/usr/bin/env python3
"""validate_resolved.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_validate_resolved.py

守っているのは「route_map で潰した silent 事故が resolver_command 経路で再開通
しないこと」。load_config.py の path 必須化は route_map にしか効かないため、
adapter の出力はここで止める。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'validate_resolved.py'

WEB_CONFIG = {"backend": "web"}
IOS_CONFIG = {"backend": "ios"}

OK_PAYLOAD = {
    "resolved": [{"screen_id": "home", "title": "ホーム", "path": "/", "source_file": "a.tsx"}],
    "unresolved": [],
}


def _run(payload, config=None) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / 'config.json'
        cfg.write_text(json.dumps(config if config is not None else WEB_CONFIG), encoding='utf-8')
        data = Path(tmp) / 'resolved.json'
        data.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                        encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--config-json', str(cfg), str(data)],
            capture_output=True, text=True,
        )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    return proc.returncode, {'json': out, 'stderr': proc.stderr}


class TestSilentAccidentGuard(unittest.TestCase):
    """空 path を通すと serve_url そのもの（トップページ）を撮ってしまう"""

    def test_missing_path_is_rejected(self):
        code, res = _run({"resolved": [{"screen_id": "home", "title": "ホーム"}]})
        self.assertEqual(code, 1)
        self.assertIn('path が必要です', json.dumps(res['json'], ensure_ascii=False))

    def test_empty_path_is_rejected(self):
        code, res = _run({"resolved": [{"screen_id": "home", "path": ""}]})
        self.assertEqual(code, 1)
        self.assertIn('path が必要です', json.dumps(res['json'], ensure_ascii=False))

    def test_null_path_is_rejected(self):
        """JS の undefined は JSON.stringify でキーごと消えるが、null は残る"""
        code, res = _run({"resolved": [{"screen_id": "home", "path": None}]})
        self.assertEqual(code, 1)

    def test_relative_path_is_rejected(self):
        code, res = _run({"resolved": [{"screen_id": "home", "path": "mocks/home/"}]})
        self.assertEqual(code, 1)
        self.assertIn('始まりです', json.dumps(res['json'], ensure_ascii=False))

    def test_same_screen_id_with_different_paths_is_rejected(self):
        """同じ画面IDに違うURLを与える矛盾を入力順で黙って採用しない"""
        code, res = _run({"resolved": [
            {"screen_id": "home", "path": "/"},
            {"screen_id": "home", "path": "/other"},
        ]})
        self.assertEqual(code, 1)
        self.assertIn('異なる path', json.dumps(res['json'], ensure_ascii=False))

    def test_ios_does_not_require_path(self):
        """iOS は screen_id が deeplink に入るため path 不要"""
        code, _ = _run({"resolved": [{"screen_id": "home", "title": "ホーム"}]}, IOS_CONFIG)
        self.assertEqual(code, 0)


class TestContractShape(unittest.TestCase):

    def test_valid_payload_passes_through(self):
        code, res = _run(OK_PAYLOAD)
        self.assertEqual(code, 0)
        self.assertEqual(res['json']['resolved'][0]['screen_id'], 'home')

    def test_resolver_error_is_surfaced(self):
        """resolver 自身がエラーを返したら成功扱いにしない"""
        code, res = _run({"error": "manifest.ts が見つかりません"})
        self.assertEqual(code, 1)
        self.assertIn('manifest.ts', json.dumps(res['json'], ensure_ascii=False))

    def test_broken_json_is_reported(self):
        code, res = _run('{"resolved": ')
        self.assertEqual(code, 1)
        self.assertIn('JSONとして不正', json.dumps(res['json'], ensure_ascii=False))

    def test_non_object_output_is_rejected(self):
        code, _ = _run('[1,2,3]')
        self.assertEqual(code, 1)

    def test_missing_screen_id_is_rejected(self):
        code, res = _run({"resolved": [{"path": "/"}]})
        self.assertEqual(code, 1)
        self.assertIn('screen_id', json.dumps(res['json'], ensure_ascii=False))

    def test_unresolved_without_source_file_is_rejected(self):
        code, _ = _run({"resolved": [], "unresolved": [{"reason": "なぜか"}]})
        self.assertEqual(code, 1)

    def test_empty_result_is_valid(self):
        """0件は「対象なし」として正当（呼び出し元がユーザー確認する）"""
        code, _ = _run({"resolved": [], "unresolved": []})
        self.assertEqual(code, 0)

    def test_no_traceback_on_any_input(self):
        for payload in ('null', '"string"', '{"resolved": "notalist"}', '{"resolved": [1,2]}'):
            with self.subTest(payload=payload):
                code, res = _run(payload)
                self.assertEqual(code, 1)
                self.assertNotIn('Traceback', res['stderr'])


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ validate_resolved self-test PASSED' if ok else '❌ validate_resolved self-test FAILED')
    sys.exit(0 if ok else 1)
