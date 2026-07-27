#!/usr/bin/env python3
"""run_dir.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_run_dir.py

守っているのは **実行ごとに出力先が分かれること**。同一PRの撮り直し（レビュー指摘 →
修正 push → 再撮影）は通常運用で、出力先を共有すると前イテレーションの証跡が消え、
撮影前に中断した回には前回の comparison.html が「今回の比較」として残る。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'run_dir.py'


def _run(config, repo_root, pr='42', *extra) -> tuple[int, dict, str]:
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / 'config.json'
        config_path.write_text(
            config if isinstance(config, str) else json.dumps(config), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--config-json', str(config_path),
             '--repo-root', str(repo_root), '--pr', pr, *extra],
            capture_output=True, text=True,
        )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    return proc.returncode, out, proc.stderr


class TestRunIsolation(unittest.TestCase):
    """本丸: 同じPRを撮り直しても前回の出力を踏まない"""

    def test_two_runs_get_different_dirs(self):
        with tempfile.TemporaryDirectory() as repo:
            _, first, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', '20260727-100000')
            _, second, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', '20260727-110000')
            self.assertNotEqual(first['out_dir'], second['out_dir'])
            self.assertTrue(Path(first['out_dir']).is_dir())
            self.assertTrue(Path(second['out_dir']).is_dir())

    def test_previous_run_artifacts_survive(self):
        """前イテレーションの比較HTMLが消えないこと（証跡が残る）"""
        with tempfile.TemporaryDirectory() as repo:
            _, first, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', 'run-1')
            stale = Path(first['out_dir']) / 'comparison.html'
            stale.write_text('前回の比較', encoding='utf-8')
            _, second, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', 'run-2')
            self.assertTrue(stale.exists())
            # 今回のディレクトリには前回の成果物が居ない（＝古いHTMLを掴めない）
            self.assertFalse((Path(second['out_dir']) / 'comparison.html').exists())

    def test_different_prs_are_separated(self):
        with tempfile.TemporaryDirectory() as repo:
            _, a, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', 'r')
            _, b, _ = _run({"output_dir": "out"}, repo, '43', '--run-id', 'r')
            self.assertNotEqual(a['out_dir'], b['out_dir'])

    def test_default_run_id_is_timestamp(self):
        with tempfile.TemporaryDirectory() as repo:
            code, out, _ = _run({"output_dir": "out"}, repo)
            self.assertEqual(code, 0)
            self.assertRegex(out['run_id'], r'^\d{8}-\d{6}$')


class TestCollisionSafety(unittest.TestCase):
    """既定run-idは秒精度。同じ秒に2回起動しても前回を上書きしない"""

    def test_same_second_runs_do_not_share_a_directory(self):
        with tempfile.TemporaryDirectory() as repo:
            # run-id を指定せず連続実行する（同一秒に入る）
            dirs = set()
            for _ in range(3):
                code, out, _ = _run({"output_dir": "out"}, repo)
                self.assertEqual(code, 0)
                dirs.add(out['out_dir'])
            self.assertEqual(len(dirs), 3, msg=f"ディレクトリが共有された: {dirs}")

    def test_existing_run_id_is_rejected_not_reused(self):
        """明示run-idの再利用は、固定名ファイルの上書きになるので落とす"""
        with tempfile.TemporaryDirectory() as repo:
            code, first, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', 'same')
            self.assertEqual(code, 0)
            (Path(first['out_dir']) / 'comparison.html').write_text('前回', encoding='utf-8')
            code, out, stderr = _run({"output_dir": "out"}, repo, '42', '--run-id', 'same')
            self.assertEqual(code, 1)
            self.assertIn('既に', out['error'])
            self.assertNotIn('Traceback', stderr)
            # 前回の成果物は無傷
            self.assertEqual(
                (Path(first['out_dir']) / 'comparison.html').read_text(encoding='utf-8'), '前回')


class TestLatestPointer(unittest.TestCase):
    """添付・共有時の安定参照"""

    def test_latest_points_to_newest_run(self):
        with tempfile.TemporaryDirectory() as repo:
            _run({"output_dir": "out"}, repo, '42', '--run-id', 'run-1')
            _, second, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', 'run-2')
            latest = Path(second['pr_dir']) / 'latest'
            self.assertTrue(latest.is_symlink())
            self.assertEqual(latest.resolve(), Path(second['out_dir']).resolve())

    def test_existing_directory_named_latest_is_not_destroyed(self):
        """利用者の成果物を消さない（symlinkでない latest には触らない）"""
        with tempfile.TemporaryDirectory() as repo:
            _, first, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', 'run-1')
            latest_dir = Path(first['pr_dir']) / 'latest'
            latest_dir.unlink()   # 1回目が張った symlink を、実体ディレクトリに置き換える
            latest_dir.mkdir()
            (latest_dir / 'keep.txt').write_text('大事なファイル', encoding='utf-8')
            code, out, _ = _run({"output_dir": "out"}, repo, '42', '--run-id', 'run-2')
            self.assertEqual(code, 0)          # 作成自体は成功させる
            self.assertIsNone(out['latest'])   # ただし latest は張らない
            self.assertTrue((latest_dir / 'keep.txt').exists())


class TestPathHandling(unittest.TestCase):

    def test_absolute_output_dir_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as outside:
            code, out, _ = _run({"output_dir": outside}, repo, '42', '--run-id', 'r')
            self.assertEqual(code, 0)
            self.assertTrue(out['out_dir'].startswith(str(Path(outside).resolve())))

    def test_unsafe_run_ids_are_rejected(self):
        """出力先をPRディレクトリ外へ逃がさない。パス区切りだけ見ると "." ".." が通る"""
        # `--run-id=VALUE` 形式で渡す（`-leading` を argparse がオプションと誤解しないように）
        for run_id in ('../escape', '..', '.', '', 'a/b', '-leading'):
            with self.subTest(run_id=repr(run_id)):
                with tempfile.TemporaryDirectory() as repo:
                    code, out, stderr = _run({"output_dir": "out"}, repo, '42', f'--run-id={run_id}')
                    self.assertEqual(code, 1, msg=json.dumps(out, ensure_ascii=False))
                    self.assertNotIn('Traceback', stderr)
                    self.assertFalse((Path(repo) / 'out' / '42').exists())  # 作られていない

    def test_unsafe_pr_is_rejected(self):
        """PR番号もパス要素になる"""
        for pr in ('../42', '.', 'abc'):
            with self.subTest(pr=pr):
                with tempfile.TemporaryDirectory() as repo:
                    code, out, stderr = _run({"output_dir": "out"}, repo, pr, '--run-id', 'r')
                    self.assertEqual(code, 1)
                    self.assertNotIn('Traceback', stderr)

    def test_broken_config_is_error(self):
        with tempfile.TemporaryDirectory() as repo:
            code, out, stderr = _run('{"output_dir": ', repo)
            self.assertEqual(code, 1)
            self.assertNotIn('Traceback', stderr)

    def test_missing_output_dir_is_error(self):
        with tempfile.TemporaryDirectory() as repo:
            code, out, stderr = _run({}, repo)
            self.assertEqual(code, 1)
            self.assertNotIn('Traceback', stderr)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ run_dir self-test PASSED' if ok else '❌ run_dir self-test FAILED')
    sys.exit(0 if ok else 1)
