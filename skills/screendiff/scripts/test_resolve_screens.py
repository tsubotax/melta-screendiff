#!/usr/bin/env python3
"""resolve_screens.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_resolve_screens.py

ここで守るのは「推測せず unresolved に倒す」振る舞い。解決できないファイルを
黙って近そうな画面に割り当てると、間違った画面を撮ってレビュアーを誤誘導する。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'resolve_screens.py'

CONFIG = {
    "backend": "web",
    "screens": {
        "route_map": [
            {"file_pattern": r"^src/pages/index\.", "id": "home", "path": "/", "title": "ホーム"},
            {"file_pattern": "^src/pages/about/", "id": "about", "path": "/about", "title": "About"},
            {"file_pattern": "^src/components/Header", "id": "home", "path": "/", "title": "ホーム"},
        ]
    },
}


def _run(config: dict, *changed_files: str) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--config-json', str(path), *changed_files],
            capture_output=True, text=True,
        )
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {'_stdout': proc.stdout, '_stderr': proc.stderr}


class TestResolveScreens(unittest.TestCase):

    def test_resolves_each_file_to_its_screen(self):
        code, out = _run(CONFIG, 'src/pages/index.tsx', 'src/pages/about/page.tsx')
        self.assertEqual(code, 0)
        self.assertEqual([s['screen_id'] for s in out['resolved']], ['home', 'about'])
        self.assertEqual([s['path'] for s in out['resolved']], ['/', '/about'])
        self.assertEqual(out['unresolved'], [])

    def test_same_screen_from_multiple_files_appears_once(self):
        """共通コンポーネントと画面ファイルの両方が変わっても撮影は1回"""
        code, out = _run(CONFIG, 'src/pages/index.tsx', 'src/components/Header.tsx')
        self.assertEqual(code, 0)
        self.assertEqual(len(out['resolved']), 1)
        self.assertEqual(out['resolved'][0]['screen_id'], 'home')

    def test_unmatched_file_goes_to_unresolved_not_guessed(self):
        """マッチしないファイルは推測で割り当てず unresolved に倒す"""
        code, out = _run(CONFIG, 'src/pages/misc.tsx')
        self.assertEqual(code, 0)
        self.assertEqual(out['resolved'], [])
        self.assertEqual(len(out['unresolved']), 1)
        self.assertEqual(out['unresolved'][0]['source_file'], 'src/pages/misc.tsx')

    def test_conflicting_path_for_same_id_is_an_error(self):
        """同一idに異なるpathを与える設定矛盾を入力順で黙って採用しない"""
        config = json.loads(json.dumps(CONFIG))
        config['screens']['route_map'].append(
            {"file_pattern": "^src/other", "id": "home", "path": "/other", "title": "ホーム"})
        code, out = _run(config, 'src/pages/index.tsx')
        self.assertEqual(code, 1)
        self.assertIn('error', out)

    def test_empty_route_map_reports_all_as_unresolved(self):
        """route_map が空なら全ファイルを unresolved にする（黙って0件成功にしない）"""
        code, out = _run({"backend": "web", "screens": {"route_map": []}}, 'src/pages/index.tsx')
        self.assertEqual(code, 0)
        self.assertEqual(out['resolved'], [])
        self.assertEqual(len(out['unresolved']), 1)

    def test_path_is_carried_through_verbatim(self):
        """path の欠落は load_config 側で落とす契約。ここでは値をそのまま運ぶ"""
        code, out = _run(CONFIG, 'src/pages/about/page.tsx')
        self.assertEqual(code, 0)
        self.assertEqual(out['resolved'][0]['path'], '/about')
        self.assertNotEqual(out['resolved'][0]['path'], '')


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ resolve_screens self-test PASSED' if ok else '❌ resolve_screens self-test FAILED')
    sys.exit(0 if ok else 1)
