#!/usr/bin/env python3
"""run_cleanup.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_run_cleanup.py

このテストが守っているのは **後片付けの失敗が「完了しました」に化けないこと**:
Cleanup は正常終了・早期終了・エラー中断のすべての経路で走る最後の処理で、ここで
exit code を落とすと、stale なビルド成果物が残ったまま次の作業が始まる。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'run_cleanup.py'


def _run(config, repo_root=None) -> tuple[int, dict, str]:
    """config（dict または生文字列）を一時ファイルに書いて run_cleanup.py にかける。"""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / 'config.json'
        config_path.write_text(
            config if isinstance(config, str) else json.dumps(config), encoding='utf-8')
        cwd = repo_root or tmp
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--config-json', str(config_path), '--repo-root', str(cwd)],
            capture_output=True, text=True,
        )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    return proc.returncode, out, proc.stderr


class TestCleanupCommand(unittest.TestCase):

    def test_unset_is_skipped_and_succeeds(self):
        """未設定は正常系。「後片付けが無い」を失敗にしない（後方互換）"""
        code, out, stderr = _run({"backend": "web"})
        self.assertEqual(code, 0)
        self.assertEqual(out['status'], 'skipped')
        self.assertNotIn('Traceback', stderr)

    def test_null_is_skipped(self):
        """明示的な null も未設定と同じ扱い（DEFAULTS が null を入れてくる）"""
        code, out, _ = _run({"cleanup_command": None})
        self.assertEqual(code, 0)
        self.assertEqual(out['status'], 'skipped')

    def test_empty_string_is_error_not_skipped(self):
        """空文字は load_config が弾く値。ここで skipped に倒すと成功偽装が復活する"""
        for value in ('', '   '):
            with self.subTest(value=repr(value)):
                code, out, stderr = _run({"cleanup_command": value})
                self.assertEqual(code, 1)
                self.assertEqual(out['status'], 'error')
                self.assertNotIn('Traceback', stderr)

    def test_success_runs_in_repo_root(self):
        """成功時は exit 0 かつ、コマンドの cwd が repo-root であること"""
        with tempfile.TemporaryDirectory() as repo:
            code, out, _ = _run({"cleanup_command": "rm -rf build_artifact && pwd"}, repo_root=repo)
            self.assertEqual(code, 0)
            self.assertEqual(out['status'], 'succeeded')
            self.assertEqual(out['exit_code'], 0)
            # シンボリックリンク差（macOS の /var → /private/var）を吸収して比較する
            self.assertEqual(Path(out['stdout_tail'].strip()).resolve(), Path(repo).resolve())

    def test_side_effect_actually_happens(self):
        """「実行したことにして何もしない」を防ぐ。実ファイルが消えることまで見る"""
        with tempfile.TemporaryDirectory() as repo:
            stale = Path(repo) / 'dist'
            stale.mkdir()
            (stale / 'stale.js').write_text('old', encoding='utf-8')
            code, out, _ = _run({"cleanup_command": "rm -rf dist"}, repo_root=repo)
            self.assertEqual(code, 0)
            self.assertEqual(out['status'], 'succeeded')
            self.assertFalse(stale.exists())

    def test_failure_is_not_swallowed(self):
        """非0終了は exit 1 で返す。ここを握り潰すと残置に気づけない"""
        code, out, stderr = _run({"cleanup_command": "echo 何か壊れた >&2; exit 3"})
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'failed')
        self.assertEqual(out['exit_code'], 3)
        self.assertIn('何か壊れた', out['stderr_tail'])
        self.assertNotIn('Traceback', stderr)

    def test_command_not_found_is_failure(self):
        """存在しないコマンドもシェルの非0終了として失敗に倒す"""
        code, out, _ = _run({"cleanup_command": "screendiff-no-such-command-xyz"})
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'failed')
        self.assertNotEqual(out['exit_code'], 0)

    def test_unreadable_config_fails_closed(self):
        """config が壊れていたら skipped に倒さない（設定した後片付けが黙って消える）"""
        code, out, stderr = _run('{"cleanup_command": ')
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'error')
        self.assertNotIn('Traceback', stderr)

    def test_non_string_command_is_error(self):
        """load_config を通さず直接呼ばれても traceback を出さない"""
        code, out, stderr = _run({"cleanup_command": 42})
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'error')
        self.assertNotIn('Traceback', stderr)

    def test_missing_repo_root_is_error(self):
        code, out, stderr = _run({"cleanup_command": "true"}, repo_root='/no/such/dir/for/screendiff')
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'error')
        self.assertNotIn('Traceback', stderr)

    def test_non_utf8_output_does_not_break_json_contract(self):
        """非UTF-8を吐くコマンドでもJSONを返す（デコード例外でtracebackにしない）"""
        code, out, stderr = _run(
            {"cleanup_command": r"printf '\xff\xfe broken\n'; exit 4"})
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'failed')
        self.assertEqual(out['exit_code'], 4)
        self.assertNotIn('Traceback', stderr)

    def test_huge_output_is_truncated(self):
        """ビルドツールの大量出力でAIのコンテキストを潰さない"""
        code, out, _ = _run({"cleanup_command": "python3 -c \"print('x' * 50000)\""})
        self.assertEqual(code, 0)
        self.assertLess(len(out['stdout_tail']), 3000)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ run_cleanup self-test PASSED' if ok else '❌ run_cleanup self-test FAILED')
    sys.exit(0 if ok else 1)
