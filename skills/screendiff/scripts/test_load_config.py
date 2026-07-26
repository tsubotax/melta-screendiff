#!/usr/bin/env python3
"""load_config.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_load_config.py

このテストが守っているのは主に **silent 事故の再発防止**:
  - web の route_map に path が無いと撮影URLが serve_url そのもの（トップページ）に
    なり、「変更された画面」としてトップを撮ったまま Before/After が一致して
    「差分なし」と誤結論する。よって path 必須は絶対に緩めない
  - 設定の型不正で traceback を出さない（利用者に「プラグインが壊れている」と
    誤認させる。設定の手書きが必要な現設計では最も踏まれやすい経路）
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'load_config.py'
EXAMPLES = Path(__file__).resolve().parents[3] / 'examples'

WEB_OK = {
    "backend": "web",
    "target_file_patterns": ["^src/"],
    "web": {"serve_command": "npm run dev", "serve_url": "http://localhost:5173"},
    "screens": {"route_map": [{"file_pattern": "^src/pages/about", "id": "about", "path": "/about"}]},
}

IOS_OK = {
    "backend": "ios",
    "target_file_patterns": [r"\.swift$"],
    "ios": {"build_command": "make build", "app_path": "a.app", "bundle_id": "com.example",
            "deeplink": "x://s/{id}", "simulator_device": "iPhone 17 Pro"},
    "screens": {"route_map": [{"file_pattern": "Home", "id": "home"}]},
}


def _run_config(config) -> tuple[int, dict]:
    """設定（dict または生文字列）を一時ファイルに書いて load_config.py にかける。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'screendiff.json'
        path.write_text(config if isinstance(config, str) else json.dumps(config), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--config', str(path)],
            capture_output=True, text=True,
        )
    # stderr に traceback が出ていないこと自体が検証対象なので、ここで持ち回る
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    return proc.returncode, {'json': out, 'stderr': proc.stderr}


def _with(base: dict, **overrides) -> dict:
    merged = json.loads(json.dumps(base))
    for key, value in overrides.items():
        merged[key] = value
    return merged


class TestSilentAccidentGuards(unittest.TestCase):
    """撮影対象がズレたまま気づかず動く経路を塞いでいるか"""

    def test_web_route_without_path_is_rejected(self):
        """path 欠落 → serve_url そのもの（トップページ）を撮る事故。確定前に落とす"""
        config = _with(WEB_OK, screens={"route_map": [{"file_pattern": "^src/", "id": "about"}]})
        code, res = _run_config(config)
        self.assertEqual(code, 1)
        self.assertIn('path が必要です', json.dumps(res['json'], ensure_ascii=False))

    def test_web_route_path_must_be_absolute(self):
        """相対パスは serve_url との連結結果が壊れる"""
        config = _with(WEB_OK, screens={
            "route_map": [{"file_pattern": "^src/", "id": "about", "path": "about"}]})
        code, res = _run_config(config)
        self.assertEqual(code, 1)
        self.assertIn('始まりです', json.dumps(res['json'], ensure_ascii=False))

    def test_ios_route_without_path_is_allowed(self):
        """iOS は id が deeplink に入るため path 不要（web の必須化を巻き込まない）"""
        code, _ = _run_config(IOS_OK)
        self.assertEqual(code, 0)

    def test_invalid_regex_rejected_before_use(self):
        """不正な正規表現は Phase 1 の適用範囲判定まで露見しないため確定前に落とす"""
        code, res = _run_config(_with(WEB_OK, target_file_patterns=["^src/(unclosed"]))
        self.assertEqual(code, 1)
        self.assertIn('正規表現が不正', json.dumps(res['json'], ensure_ascii=False))

    def test_serve_url_trailing_slash_normalized(self):
        """serve_url + path の連結が二重スラッシュにならないよう末尾を正規化する"""
        config = _with(WEB_OK, web={"serve_command": "npm run dev",
                                    "serve_url": "http://localhost:5173/"})
        code, res = _run_config(config)
        self.assertEqual(code, 0)
        self.assertEqual(res['json']['web']['serve_url'], 'http://localhost:5173')


class TestNoTracebackOnMalformedConfig(unittest.TestCase):
    """型不正でも整形されたエラーを返し、traceback を出さないか"""

    def _assert_clean_error(self, config, expected_fragment):
        code, res = _run_config(config)
        self.assertEqual(code, 1)
        self.assertNotIn('Traceback', res['stderr'])
        self.assertIn(expected_fragment, json.dumps(res['json'], ensure_ascii=False))

    def test_web_section_as_string(self):
        self._assert_clean_error(_with(WEB_OK, web="npm run dev"), 'web はオブジェクトです')

    def test_screens_section_as_string(self):
        self._assert_clean_error(_with(WEB_OK, screens="none"), 'screens はオブジェクトです')

    def test_file_pattern_as_number(self):
        """re.compile は非文字列で TypeError（re.error では捕まらない）"""
        self._assert_clean_error(
            _with(WEB_OK, screens={"route_map": [{"file_pattern": 5, "id": "a", "path": "/a"}]}),
            '文字列です')

    def test_top_level_array(self):
        self._assert_clean_error('[1, 2, 3]', 'JSONオブジェクトです')

    def test_broken_json(self):
        self._assert_clean_error('{"backend": ', 'JSONが不正')

    def test_serve_url_without_host(self):
        """prefix 一致だけの検証では "http://" が通ってしまう"""
        self._assert_clean_error(
            _with(WEB_OK, web={"serve_command": "npm run dev", "serve_url": "http://"}),
            'ホストがありません')

    def test_serve_url_wrong_scheme(self):
        self._assert_clean_error(
            _with(WEB_OK, web={"serve_command": "npm run dev", "serve_url": "ftp://example.com"}),
            'スキーム')

    def test_resolver_command_as_number(self):
        self._assert_clean_error(
            _with(WEB_OK, screens={"resolver_command": 42}), 'resolver_command は文字列です')


class TestShippedExamples(unittest.TestCase):
    """同梱サンプルが検証を通る（サンプルが動かないのは導入時の信頼を落とす）"""

    def test_examples_are_valid(self):
        for name in ('screendiff.web.json', 'screendiff.ios.json'):
            with self.subTest(example=name):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), '--config', str(EXAMPLES / name)],
                    capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stdout)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ load_config self-test PASSED' if ok else '❌ load_config self-test FAILED')
    sys.exit(0 if ok else 1)
