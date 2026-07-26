#!/usr/bin/env python3
"""share_plan.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_share_plan.py

守っているのは **PRへの書き込みを止めるべき組合せで、確実に止まっていること**。
撮影対象がズレる silent 事故と違い、PRへの書き込みは外向きで取り消せない。
モード×author/reviewer×Artifact可否の全組合せを回し、`local` と
`artifact_unavailable` では PR書き込みアクションが1つも現れないことを固定する。
"""

import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'share_plan.py'
PR_WRITE_MARKERS = ('gh pr comment', 'gh pr edit', 'PR本文')


def _run(config, *args) -> tuple[int, dict, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'config.json'
        path.write_text(config if isinstance(config, str) else json.dumps(config), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--config-json', str(path), *args],
            capture_output=True, text=True,
        )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    return proc.returncode, out, proc.stderr


class TestModeResolution(unittest.TestCase):
    """決定順: 明示指定 > config > 既定"""

    def test_default_is_artifact(self):
        """新キーを書いていない既存設定は従来フローのまま"""
        code, out, _ = _run({})
        self.assertEqual(code, 0)
        self.assertEqual(out['share_mode'], 'artifact')
        self.assertEqual(out['source'], '既定値')

    def test_config_is_used(self):
        code, out, _ = _run({"share_mode": "local"})
        self.assertEqual(out['share_mode'], 'local')
        self.assertEqual(out['source'], 'config.share_mode')

    def test_override_beats_config(self):
        code, out, _ = _run({"share_mode": "artifact"}, '--share', 'local')
        self.assertEqual(out['share_mode'], 'local')
        self.assertEqual(out['source'], '実行時の明示指定')

    def test_override_can_opt_into_artifact(self):
        """local を既定にしている設定でも、明示すれば artifact に戻せる"""
        code, out, _ = _run({"share_mode": "local"}, '--share', 'artifact')
        self.assertEqual(out['share_mode'], 'artifact')
        self.assertTrue(out['allow_pr_write'])

    def test_invalid_config_mode_is_error(self):
        """既定へ黙ってフォールバックさせない（書き込ませたくないPRに書き込む）"""
        code, out, stderr = _run({"share_mode": "slack"})
        self.assertEqual(code, 1)
        self.assertIn('share_mode', out['error'])
        self.assertNotIn('Traceback', stderr)

    def test_unreadable_config_is_error(self):
        code, out, stderr = _run('{"share_mode": ')
        self.assertEqual(code, 1)
        self.assertNotIn('Traceback', stderr)


class TestPrWriteIsBlocked(unittest.TestCase):
    """本丸: 止めるべき組合せでPR書き込みが1つも現れないこと"""

    def _assert_no_pr_write(self, out):
        blob = json.dumps({k: v for k, v in out.items() if k != 'forbidden'}, ensure_ascii=False)
        for marker in PR_WRITE_MARKERS:
            self.assertNotIn(marker, blob, msg=f'{marker} が許可アクションに現れている: {blob}')
        self.assertFalse(out['allow_pr_write'])
        self.assertFalse(out['allow_artifact'])
        for marker in ('gh pr comment', 'gh pr edit'):
            self.assertIn(marker, out['forbidden'])

    def test_local_blocks_pr_write_in_all_combinations(self):
        """author/reviewer × Artifact可否 のどの組合せでも local は書き込まない"""
        for author, no_artifact in itertools.product([False, True], repeat=2):
            with self.subTest(author=author, no_artifact=no_artifact):
                args = ['--share', 'local']
                if author:
                    args.append('--author')
                if no_artifact:
                    args.append('--no-artifact')
                code, out, _ = _run({}, *args)
                self.assertEqual(code, 0)
                self.assertEqual(out['effective'], 'local')
                self._assert_no_pr_write(out)

    def test_artifact_unavailable_blocks_pr_write(self):
        """Artifact を出せないのに「比較はArtifactにあります」とPRに書かせない"""
        for author in (False, True):
            with self.subTest(author=author):
                args = ['--share', 'artifact', '--no-artifact'] + (['--author'] if author else [])
                code, out, _ = _run({}, *args)
                self.assertEqual(out['effective'], 'artifact_unavailable')
                self._assert_no_pr_write(out)


class TestArtifactMode(unittest.TestCase):
    """既定フローが従来どおり成立しているか（後方互換）"""

    def test_reviewer_requires_hitl_before_posting(self):
        code, out, _ = _run({}, '--share', 'artifact')
        self.assertTrue(out['allow_pr_write'])
        joined = ' '.join(out['actions'])
        self.assertIn('gh pr comment', joined)
        self.assertIn('承認', joined)   # 無条件投稿になっていないこと

    def test_author_edits_body_and_skips_comment(self):
        code, out, _ = _run({}, '--share', 'artifact', '--author')
        joined = ' '.join(out['actions'])
        self.assertIn('gh pr edit', joined)
        self.assertNotIn('gh pr comment', joined)
        self.assertIn('トグル', joined)  # 人間の1クリックが要ることを落とさない


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ share_plan self-test PASSED' if ok else '❌ share_plan self-test FAILED')
    sys.exit(0 if ok else 1)
