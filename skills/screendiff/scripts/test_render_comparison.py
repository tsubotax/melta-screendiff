#!/usr/bin/env python3
"""render_comparison.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_render_comparison.py

ここで見ているのは **manifest の任意キーが未記録でもHTMLが壊れないこと**。
プレースホルダー方式（str.replace）は置換漏れが `{{...}}` の文字列として
そのまま画面に出るため、後方互換の manifest でも必ず全部置換されることを確かめる。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT = SCRIPT_DIR / 'render_comparison.py'
TEMPLATE = SCRIPT_DIR / 'template.html'

BASE_MANIFEST = {
    "pr_number": 42,
    "pr_title": "ホーム画面の更新",
    "pr_url": "https://github.com/example/app/pull/42",
    "base_ref": "main",
    "head_ref": "feature/home",
    "build_status": "SUCCEEDED",
    "generated_at": "2026-07-27T10:00:00+09:00",
    "screens": [{"screen_id": "home", "title": "ホーム", "kind": "new", "after_paths": []}],
}


def _render(manifest: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / 'manifest.json'
        out_path = Path(tmp) / 'comparison.html'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--manifest', str(manifest_path),
             '--output', str(out_path)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return out_path.read_text(encoding='utf-8')


class TestCommitChip(unittest.TestCase):
    """撮ったコミットOIDを比較HTML自体に残す（manifest.json は共有相手に渡らない）"""

    def test_oids_are_shown_when_recorded(self):
        html = _render(dict(BASE_MANIFEST, base_oid='9f1c2b4e5a6d7c8b9a0f1e2d3c4b5a6978890123',
                            head_oid='1a2b3c4d5e6f7890abcdef1234567890abcdef12'))
        self.assertIn('比較コミット', html)
        self.assertIn('9f1c2b4e5a6d', html)
        self.assertIn('1a2b3c4d5e6f', html)

    def test_absent_oids_render_nothing(self):
        """OID未記録の manifest（従来スキーマ）でもチップごと消えるだけ"""
        html = _render(BASE_MANIFEST)
        self.assertNotIn('比較コミット', html)

    def test_partial_oids_render_nothing(self):
        """片側だけでは何と何を比べたか分からないので出さない"""
        html = _render(dict(BASE_MANIFEST, base_oid='9f1c2b4e5a6d7c8b9a0f1e2d3c4b5a6978890123'))
        self.assertNotIn('比較コミット', html)

    def test_non_string_oid_does_not_crash(self):
        html = _render(dict(BASE_MANIFEST, base_oid=123, head_oid=456))
        self.assertNotIn('比較コミット', html)

    def test_before_oid_wins_over_base_oid(self):
        """merge_base モードでは撮ったのは base 先端ではない。撮っていない方を出さない"""
        html = _render(dict(BASE_MANIFEST,
                            base_oid='aaaaaaaaaaaa1111111111111111111111111111',
                            before_oid='bbbbbbbbbbbb2222222222222222222222222222',
                            head_oid='cccccccccccc3333333333333333333333333333',
                            before_base='merge_base'))
        self.assertIn('bbbbbbbbbbbb', html)
        self.assertNotIn('aaaaaaaaaaaa', html)

    def test_merge_base_mode_is_stated_in_html(self):
        """HTMLだけ受け取った人に「今のbaseに載せた姿ではない」と伝わること"""
        html = _render(dict(BASE_MANIFEST,
                            before_oid='bbbbbbbbbbbb2222222222222222222222222222',
                            head_oid='cccccccccccc3333333333333333333333333333',
                            before_base='merge_base'))
        self.assertIn('merge-base', html)

    def test_branch_tip_mode_has_no_extra_chip(self):
        html = _render(dict(BASE_MANIFEST,
                            before_oid='bbbbbbbbbbbb2222222222222222222222222222',
                            head_oid='cccccccccccc3333333333333333333333333333',
                            before_base='branch_tip'))
        self.assertIn('比較コミット', html)
        self.assertNotIn('merge-base', html)


class TestNoUnreplacedPlaceholders(unittest.TestCase):
    """置換漏れは `{{FOO}}` の文字列として利用者の画面に出る"""

    def _assert_fully_rendered(self, manifest):
        html = _render(manifest)
        leftovers = [line.strip() for line in html.splitlines() if '{{' in line]
        self.assertEqual(leftovers, [], msg=f"未置換のプレースホルダー: {leftovers}")

    def test_minimal_manifest(self):
        self._assert_fully_rendered(BASE_MANIFEST)

    def test_full_manifest(self):
        self._assert_fully_rendered(dict(
            BASE_MANIFEST,
            base_oid='9f1c2b4e5a6d7c8b9a0f1e2d3c4b5a6978890123',
            head_oid='1a2b3c4d5e6f7890abcdef1234567890abcdef12',
            validation_status='PASSED (ERROR 0)',
        ))


class TestEscaping(unittest.TestCase):
    """PR本文やAIの自由記述に由来する文字列がHTMLを壊さない"""

    def test_title_is_escaped(self):
        html = _render(dict(BASE_MANIFEST, pr_title='<script>alert(1)</script>'))
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ render_comparison self-test PASSED' if ok else '❌ render_comparison self-test FAILED')
    sys.exit(0 if ok else 1)
