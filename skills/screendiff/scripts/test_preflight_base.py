#!/usr/bin/env python3
"""preflight_base.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_preflight_base.py

実際に一時gitリポジトリを作って履歴の形を変える（`git merge-base --is-ancestor` の
挙動をモックせず本物で確かめる）。git が無い環境ではテストごと skip する。

守っているのは **base 先行を撮影前に止めること**、および
**「祖先でない」と「オブジェクトが無い」を混同しないこと**。両者は git の終了コードが
1 と 128 で異なるのに、シェルの `|| echo NG` で受けると同じ結論に潰れる。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'preflight_base.py'
GIT_ENV = {
    'GIT_AUTHOR_NAME': 'screendiff-test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
    'GIT_COMMITTER_NAME': 'screendiff-test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
    'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
    'HOME': '/nonexistent-for-screendiff-test', 'PATH': '/usr/bin:/bin:/usr/local/bin',
}


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(('git', *args), cwd=repo, capture_output=True, text=True, env=GIT_ENV)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def _commit(repo: str, message: str) -> str:
    _git(repo, 'commit', '--allow-empty', '-q', '-m', message)
    return _git(repo, 'rev-parse', 'HEAD')


def _run(repo: str, base: str, head: str, *extra: str) -> tuple[int, dict, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), '--repo-root', repo, '--base-oid', base, '--head-oid', head,
         *extra],
        capture_output=True, text=True,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    return proc.returncode, out, proc.stderr


@unittest.skipIf(shutil.which('git') is None, 'git が無い環境')
class TestPreflightBase(unittest.TestCase):
    """base が先行した履歴を実際に作って検査する"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        _git(self.repo, 'init', '-q')
        # 既定ブランチ名は git のバージョン/設定で揺れるため明示的に作る（未出生ブランチでOK）
        _git(self.repo, 'checkout', '-q', '-b', 'basebranch')
        self.root = _commit(self.repo, 'root')
        self.branch_point = _commit(self.repo, 'base commit 1')
        # PRブランチ（branch_point から分岐して2コミット）
        _git(self.repo, 'checkout', '-q', '-b', 'feature')
        _commit(self.repo, 'pr commit 1')
        self.head = _commit(self.repo, 'pr commit 2')

    def tearDown(self):
        self._tmp.cleanup()

    def _advance_base(self) -> str:
        """base ブランチを PR 分岐後に進める（他PRがマージされた状況）"""
        _git(self.repo, 'checkout', '-q', 'basebranch')
        ahead = _commit(self.repo, 'base commit 2 (別PRのマージ相当)')
        _git(self.repo, 'checkout', '-q', 'feature')
        return ahead

    def test_base_is_ancestor_passes(self):
        """PR が最新の base の上にある通常ケース"""
        code, out, stderr = _run(self.repo, self.branch_point, self.head)
        self.assertEqual(code, 0, msg=json.dumps(out, ensure_ascii=False))
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(out['base_oid'], self.branch_point)
        self.assertEqual(out['head_oid'], self.head)
        self.assertNotIn('Traceback', stderr)

    def test_same_commit_passes(self):
        """自分自身は自分の祖先。ここを落とすと誤検知になる"""
        code, out, _ = _run(self.repo, self.head, self.head)
        self.assertEqual(code, 0)
        self.assertEqual(out['status'], 'ok')

    def test_base_ahead_is_blocked(self):
        """本丸: base が先行していたら撮影前に止める"""
        ahead = self._advance_base()
        code, out, stderr = _run(self.repo, ahead, self.head, '--base-ref', 'main')
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'base_ahead')
        self.assertIn('main', out['hint'])
        # OIDは中断時にも記録できるよう返す（監査用）
        self.assertEqual(out['base_oid'], ahead)
        self.assertEqual(out['head_oid'], self.head)
        self.assertNotIn('Traceback', stderr)

    def test_unknown_oid_is_error_not_base_ahead(self):
        """fetch 漏れを「base が先行しています」と誤報告しない"""
        missing = '0' * 40
        code, out, stderr = _run(self.repo, missing, self.head)
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'error')
        self.assertNotEqual(out.get('status'), 'base_ahead')
        self.assertIn('base', json.dumps(out, ensure_ascii=False))
        self.assertNotIn('Traceback', stderr)

    def test_unknown_head_oid_is_error(self):
        code, out, stderr = _run(self.repo, self.branch_point, 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef')
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'error')
        self.assertNotIn('Traceback', stderr)

    def test_short_oid_and_ref_name_are_resolved(self):
        """短縮OID/ブランチ名で呼ばれてもフルOIDに解決して返す"""
        code, out, _ = _run(self.repo, self.branch_point[:8], 'feature')
        self.assertEqual(code, 0)
        self.assertEqual(out['base_oid'], self.branch_point)
        self.assertEqual(out['head_oid'], self.head)

    def test_non_git_directory_is_error(self):
        """gitリポジトリでない場所でも traceback を出さない"""
        with tempfile.TemporaryDirectory() as plain:
            code, out, stderr = _run(plain, self.branch_point, self.head)
            self.assertEqual(code, 1)
            self.assertEqual(out['status'], 'error')
            self.assertNotIn('Traceback', stderr)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ preflight_base self-test PASSED' if ok else '❌ preflight_base self-test FAILED')
    sys.exit(0 if ok else 1)
