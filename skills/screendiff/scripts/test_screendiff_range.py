#!/usr/bin/env python3
"""screendiff_range.py の回帰テスト（standalone・スキル同梱。git と Python 標準ライブラリだけで動く）。

実行:
  python3 skills/screendiff/scripts/test_screendiff_range.py

撮影エンジンは使わない。`--capture-cmd` で撮影を偽の Python スクリプトに差し替え、
build は「snapshot の HTML を export 先へコピーする」だけの bash にする。配信は
`python3 -m http.server` を実際に立てる（nonce の経路を本物で通す）。

守っているのは設計レビュー 3 周で固定した事故の形:
  1. 対象外の変更だけなら何もビルドせず no_ui_change
  2. 依存が A・B・共有元で揃わなければ撮らずに skipped_dependency_change（失敗ではない）
  3. 前回のサーバが残っていれば（固定 serve_url）起動せず失敗にする
  4. 撮った同じページで pathname / ready_selector が成立しない画面は compare_status=unresolvable
  5. worktree 比較で撮影中に入力が変わったら inputs_changed_during_capture=true
  6. ブラウザが起動できなければ skipped_runner_unavailable（通常 push を止めない exit 0）
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RUNNER = SCRIPTS / "screendiff_range.py"

FAKE_CAPTURE = textwrap.dedent('''
    #!/usr/bin/env python3
    # 偽の撮影。URL を GET して本文を PNG として保存し、本文中の data-path と ready を見る。
    import json, sys, urllib.request
    a = sys.argv[1:]
    def opt(n, d=None):
        return a[a.index(n) + 1] if n in a else d
    url, out, expect, ready = opt("--url"), opt("--out"), opt("--expect-path", ""), opt("--ready-selector", "")
    r = {"ok": False, "url": url, "final_path": None, "path_matched": False, "ready": "none" if not ready else "pending", "file": None, "error": None}
    try:
        body = urllib.request.urlopen(url, timeout=5).read().decode()
    except Exception as e:
        r["error"] = f"launch: {e}"; print(json.dumps(r)); sys.exit(1)
    import re
    m = re.search(r'data-path="([^"]+)"', body)
    r["final_path"] = m.group(1) if m else "/"
    r["path_matched"] = r["final_path"].rstrip("/") == expect.rstrip("/")
    if ready:
        r["ready"] = "ok" if ready in body else "missing"
    open(out, "wb").write(b"\\x89PNG" + body.encode())
    r["file"] = out
    r["ok"] = r["path_matched"] and r["ready"] != "missing"
    print(json.dumps(r)); sys.exit(0 if r["ok"] else 2)
''')

FAKE_CAPTURE_LAUNCH_FAIL = textwrap.dedent('''
    #!/usr/bin/env python3
    import json, sys
    print(json.dumps({"ok": False, "error": "browserType.launch: Permission denied (sandbox)", "file": None, "stage": "launch"}))
    sys.exit(1)
''')

# ページ段階の失敗（goto タイムアウト等）。runner は exit code だけでなく stage で分類する契約なので、
# わざと exit 1 で返して「stage=page なら環境問題にしない」ことを確かめる
FAKE_CAPTURE_PAGE_FAIL = textwrap.dedent('''
    #!/usr/bin/env python3
    import json, sys
    print(json.dumps({"ok": False, "error": "page.goto: Timeout 30000ms exceeded", "file": None, "stage": "page"}))
    sys.exit(1)
''')

FAKE_CAPTURE_HANG = textwrap.dedent('''
    #!/usr/bin/env python3
    import time
    time.sleep(30)
''')


def git(repo: Path, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


class Base(unittest.TestCase):
    """app/ に「ルート HTML」を置くだけの偽リポ。build は app/ を export 先へコピーする。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / ".gitignore").write_text("node_modules/\n")
        (self.repo / "node_modules").mkdir()
        (self.repo / "package.json").write_text('{"name":"x","version":"1"}\n')
        self.write("app/home.html", '<div data-path="/home.html" data-ready="home">home v1</div>')
        self.write("app/about.html", '<div data-path="/about.html">about v1</div>')
        self.write("lib/util.js", "// v1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "v1")
        self.v1 = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.capture = self.root / "fake_capture.py"
        self.capture.write_text(FAKE_CAPTURE)
        self.capture_fail = self.root / "fake_capture_fail.py"
        self.capture_fail.write_text(FAKE_CAPTURE_LAUNCH_FAIL)
        self.capture_page_fail = self.root / "fake_capture_page_fail.py"
        self.capture_page_fail.write_text(FAKE_CAPTURE_PAGE_FAIL)
        self.capture_hang = self.root / "fake_capture_hang.py"
        self.capture_hang.write_text(FAKE_CAPTURE_HANG)
        self.out_dir = self.root / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def config(self, **over):
        cfg = {
            "backend": "web",
            "target_file_patterns": ["^app/", "^lib/", "^package\\.json$"],
            "output_dir": str(self.out_dir),
            "share_mode": "local",
            "web": {"serve_command": "unused-in-range", "serve_url": "http://127.0.0.1:1", "settle_ms": 0, "ready_timeout_sec": 15},
            "range": {
                "snapshot_paths": ["app", "lib", "package.json"],
                "dependency_change_patterns": ["^package\\.json$"],
                "node_modules_link": "node_modules",
                "build_command": 'mkdir -p "$SCREENDIFF_WEB_DIR" && cp -R app/. "$SCREENDIFF_WEB_DIR"/',
                "serve_command": 'cd "$SCREENDIFF_WEB_DIR" && python3 -u -m http.server 0 --bind 127.0.0.1',
            },
            "screens": {"route_map": [
                {"file_pattern": "^app/home\\.html$", "id": "home", "path": "/home.html", "title": "ホーム"},
                {"file_pattern": "^app/about\\.html$", "id": "about", "path": "/about.html", "title": "About"},
                {"file_pattern": "^lib/", "id": "home", "path": "/home.html", "title": "ホーム"},
            ]},
        }
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        p = self.root / "screendiff.json"
        p.write_text(json.dumps(cfg))
        return p

    def run_range(self, before, after, cfg_path, capture=None, extra=()):
        cmd = [sys.executable, str(RUNNER), "--repo-root", str(self.repo), "--before", before, "--after", after,
               "--config", str(cfg_path), "--capture-cmd", f"python3 {capture or self.capture}", *extra]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"JSON でない出力: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")


class TestHappyPath(Base):
    def test_compares_two_commits(self):
        self.write("app/home.html", '<div data-path="/home.html" data-ready="home">home v2</div>')
        v2 = self.commit("v2")
        code, out = self.run_range(self.v1, v2, self.config())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["run_status"], "compared")
        self.assertEqual(out["target_files"], ["app/home.html"])
        self.assertEqual([s["screen_id"] for s in out["screens"]], ["home"])
        self.assertEqual(out["screens"][0]["compare_status"], "compared")
        self.assertTrue(Path(out["comparison_html"]).exists())
        self.assertFalse(out["share_plan"]["allow_pr_write"])
        html = Path(out["comparison_html"]).read_text()
        self.assertIn("範囲比較", html)
        # data_caveat を省略（null）しても既定の注記が出る（契約 §1.5）
        self.assertIn("API データは固定していない", html)
        # nonce ファイルは撮影対象に含めない
        self.assertNotIn("screendiff-", out["screens"][0]["screen_id"])

    def test_all_new_screens_omit_before_oid(self):
        """Before を 1 枚も撮っていない回は before_oid / before_base を manifest に残さない（契約 §4）"""
        self.write("app/new.html", '<div data-path="/new.html">brand new</div>')
        v2 = self.commit("new screen")
        cfg = self.config(screens={"route_map": [
            {"file_pattern": "^app/new\\.html$", "id": "new", "path": "/new.html", "title": "New"}]})
        code, out = self.run_range(self.v1, v2, cfg)
        self.assertEqual(out["run_status"], "compared", out)
        self.assertEqual(out["screens"][0]["compare_status"], "new")
        manifest = json.loads((Path(out["out_dir"]) / "manifest.json").read_text())
        self.assertNotIn("before_oid", manifest)
        self.assertNotIn("before_base", manifest)
        self.assertEqual(manifest["head_oid"], v2)

    def test_worktree_after(self):
        self.write("app/about.html", '<div data-path="/about.html">about wip</div>')
        code, out = self.run_range("HEAD", "worktree", self.config())
        self.assertEqual(out["run_status"], "compared", out)
        self.assertEqual(out["after_source"], "worktree")
        self.assertFalse(out["inputs_changed_during_capture"])
        html = Path(out["comparison_html"]).read_text()
        self.assertIn("作業ツリー", html)

    def test_representative_note_is_rendered(self):
        self.write("lib/util.js", "// v2\n")
        v2 = self.commit("lib")
        cfg = self.config(screens={"route_map": [
            {"file_pattern": "^lib/", "id": "home", "path": "/home.html", "title": "ホーム", "representative": True}]})
        code, out = self.run_range(self.v1, v2, cfg)
        self.assertEqual(out["run_status"], "compared", out)
        self.assertTrue(out["screens"][0]["representative"])
        html = Path(out["comparison_html"]).read_text()
        self.assertIn("代表画面", html)


class TestNoUiChange(Base):
    def test_outside_patterns_builds_nothing(self):
        self.write("README.md", "docs only\n")
        v2 = self.commit("docs")
        code, out = self.run_range(self.v1, v2, self.config())
        self.assertEqual(code, 0)
        self.assertEqual(out["run_status"], "no_ui_change")
        self.assertFalse((Path(out["out_dir"]) / "before").exists())

    def test_bad_ref_is_failed_not_no_change(self):
        code, out = self.run_range(self.v1, "nosuchref", self.config())
        self.assertEqual(code, 1)
        self.assertEqual(out["run_status"], "failed")
        self.assertIn("nosuchref", out["reason"])

    def test_targets_without_resolvable_screen_is_failed_not_no_change(self):
        """UI の対象ファイルはあるのに撮れる画面が 0（動的ルートだけ等）→ 未確認を「変更なし」で消さない"""
        self.write("misc/widget.js", "// ui-ish but not routable\n")
        v2 = self.commit("misc")
        cfg = self.config(target_file_patterns=["^app/", "^misc/"])
        code, out = self.run_range(self.v1, v2, cfg)
        self.assertEqual(code, 1)
        self.assertEqual(out["run_status"], "failed")
        self.assertEqual(out["target_files"], ["misc/widget.js"])
        self.assertEqual(len(out["unresolved"]), 1)
        self.assertIn("撮れる画面に解決されない", out["reason"])
        self.assertFalse((Path(out["out_dir"]) / "before").exists())


class TestDependencyGate(Base):
    def test_dependency_change_in_range_is_skipped(self):
        self.write("package.json", '{"name":"x","version":"2"}\n')
        self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
        v2 = self.commit("deps")
        code, out = self.run_range(self.v1, v2, self.config())
        self.assertEqual(code, 0)
        self.assertEqual(out["run_status"], "skipped_dependency_change")
        self.assertFalse((Path(out["out_dir"]) / "before" / "web").exists())

    def test_shared_tree_dependency_drift_is_skipped(self):
        """A と B の依存は同じでも、作業ツリー（共有元）だけ違えば撮らない"""
        self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
        v2 = self.commit("v2")
        self.write("package.json", '{"name":"x","version":"wip"}\n')   # 未 commit
        code, out = self.run_range(self.v1, v2, self.config())
        self.assertEqual(out["run_status"], "skipped_dependency_change")

    def test_same_dependencies_proceed(self):
        self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
        v2 = self.commit("v2")
        code, out = self.run_range(self.v1, v2, self.config())
        self.assertEqual(out["run_status"], "compared")
        self.assertEqual(out["dependency_files"], {"before": 1, "after": 1, "shared": 1})

    def test_dependency_outside_snapshot_paths_is_still_checked(self):
        """package.json が snapshot_paths の外にあっても依存検査の対象（見落とすと別の依存でビルドする）"""
        self.write("package.json", '{"name":"x","version":"2"}\n')
        self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
        v2 = self.commit("deps outside snapshot")
        cfg = self.config(range={"snapshot_paths": ["app", "lib"]})
        code, out = self.run_range(self.v1, v2, cfg)
        self.assertEqual(out["run_status"], "skipped_dependency_change", out)

    def test_patterns_matching_nothing_is_a_config_error(self):
        """依存パターンが 3 者とも 0 件なら「一致」でなく設定ミス。素通りさせない"""
        self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
        v2 = self.commit("v2")
        cfg = self.config(range={"dependency_change_patterns": ["^does-not-exist\\.json$"]})
        code, out = self.run_range(self.v1, v2, cfg)
        self.assertEqual(code, 1)
        self.assertEqual(out["run_status"], "failed")
        self.assertIn("0 件", out["reason"])
        self.assertFalse((Path(out["out_dir"]) / "before").exists())


class TestServerSafety(Base):
    def test_stale_server_on_fixed_url_aborts(self):
        srv = subprocess.Popen([sys.executable, "-u", "-m", "http.server", "0", "--bind", "127.0.0.1"],
                               cwd=self.repo / "app", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            port = None
            deadline = time.time() + 10
            while time.time() < deadline and port is None:
                line = srv.stdout.readline()
                m = re.search(r"port (\d+)", line)
                if m:
                    port = int(m.group(1))
            self.assertIsNotNone(port)
            self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
            v2 = self.commit("v2")
            cfg = self.config(range={"serve_url": f"http://127.0.0.1:{port}"})
            code, out = self.run_range(self.v1, v2, cfg)
            self.assertEqual(code, 1)
            self.assertEqual(out["run_status"], "failed")
            self.assertIn("応答している", out["reason"])
        finally:
            srv.terminate()
            srv.wait(timeout=5)

    def test_server_is_stopped_after_each_side(self):
        self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
        v2 = self.commit("v2")
        code, out = self.run_range(self.v1, v2, self.config())
        self.assertTrue(out["sides"]["before"]["server_stopped"])
        self.assertTrue(out["sides"]["after"]["server_stopped"])
        for side in ("before", "after"):
            url = out["sides"][side]["serve_url"]
            with self.assertRaises(Exception):
                import urllib.request
                urllib.request.urlopen(url, timeout=1)

    def test_capture_timeout_stops_server_and_keeps_result_json(self):
        """撮影プロセスが固まっても配信サーバを残さず、out_dir 付きの result.json を書く"""
        import urllib.request
        self.write("app/home.html", '<div data-path="/home.html">v2</div>')
        v2 = self.commit("v2")
        cfg = self.config(range={"capture_timeout_sec": 1})
        code, out = self.run_range(self.v1, v2, cfg, capture=self.capture_hang)
        self.assertEqual(code, 1)
        self.assertEqual(out["run_status"], "failed")
        self.assertIn("タイムアウト", out["reason"])
        saved = json.loads((Path(out["out_dir"]) / "result.json").read_text())
        self.assertEqual(saved["run_status"], "failed")
        self.assertTrue(saved["sides"]["before"]["server_stopped"])
        # ⚠️ URL への GET 失敗では判定しない。親が死ぬと stdout のパイプが切れ、残存サーバは応答できないまま
        # 居座る（ポートを掴んだまま）。プロセスグループの生存を直接見る
        pgid = saved["sides"]["before"]["server_pgid"]
        time.sleep(0.3)
        alive = True
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            alive = False
        if alive:
            import signal
            os.killpg(pgid, signal.SIGKILL)   # 後始末してから落とす
        self.assertFalse(alive, f"配信サーバ（pgid {pgid}）が残っている")


class TestRefPinning(Base):
    def test_after_ref_is_pinned_before_the_before_build(self):
        """Before のビルド中に HEAD が進んでも、After は実行開始時の OID の中身を撮る"""
        self.write("app/home.html", '<div data-path="/home.html">home v2</div>')
        v2 = self.commit("v2")
        bump = (f'if [ "$SCREENDIFF_SIDE" = before ]; then '
                f'echo "<div data-path=\\"/home.html\\">home v3</div>" > "{self.repo}/app/home.html" && '
                f'git -C "{self.repo}" add -A && git -C "{self.repo}" commit -q -m v3; fi')
        cfg = self.config(range={"build_command": f'mkdir -p "$SCREENDIFF_WEB_DIR" && cp -R app/. "$SCREENDIFF_WEB_DIR"/ && {bump}'})
        code, out = self.run_range(self.v1, "HEAD", cfg)
        self.assertEqual(out["run_status"], "compared", out)
        self.assertEqual(out["head_oid"], v2)
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), v2)   # 実際に HEAD は進んだ
        shot = (Path(out["out_dir"]) / "after" / "shots" / "after-home-p1.png").read_bytes()
        self.assertIn(b"home v2", shot)
        self.assertNotIn(b"home v3", shot)


class TestSnapshotIntegrity(Base):
    def test_worktree_snapshot_hash_is_of_the_copied_bytes(self):
        """ハッシュはコピーした実体から。コピー後に元を書き換えても snapshot のハッシュは旧内容を指す"""
        sys.path.insert(0, str(SCRIPTS))
        import screendiff_range as sr
        rng = {"snapshot_paths": ["app"], "fixtures": []}
        dest = self.root / "snap"
        files = sr.inventory(self.repo, None, ["app"])
        original = sr.shutil.copyfile

        def copy_then_edit_source(src, dst, *a, **kw):
            # コピー完了直後にエディタが保存した状況を作る（コピー〜ハッシュ取得の隙間）
            r = original(src, dst, *a, **kw)
            Path(src).write_text(Path(src).read_text() + "<!-- saved right after copy -->")
            return r

        sr.shutil.copyfile = copy_then_edit_source
        try:
            info = sr.make_snapshot(self.repo, None, dest, rng)
        finally:
            sr.shutil.copyfile = original
        self.assertEqual(info["inputs_sha256"], sr.inputs_hash(dest, None, files))
        self.assertNotEqual(info["inputs_sha256"], sr.inputs_hash(self.repo, None, files))

    def test_fixture_cannot_escape_snapshot(self):
        """fixture の書き込み先が共有 node_modules（symlink）や .. を通って作業ツリーへ出ない"""
        self.write("app/home.html", '<div data-path="/home.html">v2</div>')
        v2 = self.commit("v2")
        for bad, msg in (("node_modules/evil.txt", "symlink"), ("../evil.txt", ".."), ("/tmp/evil.txt", "..")):
            cfg = self.config(range={"fixtures": [{"path": bad, "base64": "aGk="}]})
            code, out = self.run_range(self.v1, v2, cfg)
            self.assertEqual(out["run_status"], "failed", out)
            self.assertIn("fixture", out["reason"])
            self.assertIn(msg, out["reason"], bad)
        self.assertFalse((self.repo / "node_modules" / "evil.txt").exists())
        self.assertFalse((self.root / "evil.txt").exists())


class TestSameScreenTwiceIsCaught(Base):
    """同じ画面を 2 回撮って「差分なし」にしない"""

    def test_path_mismatch_is_unresolvable(self):
        # about の HTML が home へ「遷移」した状態（SPA フォールバック相当）
        self.write("app/about.html", '<div data-path="/home.html">fallback</div>')
        v2 = self.commit("v2")
        code, out = self.run_range(self.v1, v2, self.config())
        s = {x["screen_id"]: x for x in out["screens"]}["about"]
        self.assertEqual(s["compare_status"], "unresolvable")
        self.assertIn("遷移先", s["compare_reason"])

    def test_ready_selector_missing_is_unresolvable(self):
        self.write("app/home.html", '<div data-path="/home.html">no ready marker</div>')
        v2 = self.commit("v2")
        cfg = self.config(screens={"route_map": [
            {"file_pattern": "^app/home\\.html$", "id": "home", "path": "/home.html", "title": "ホーム",
             "ready_selector": "data-ready=\"home\""}]})
        code, out = self.run_range(self.v1, v2, cfg)
        s = out["screens"][0]
        self.assertEqual(s["compare_status"], "unresolvable")
        self.assertIn("表示完了条件", s["compare_reason"])
        self.assertEqual(out["run_status"], "failed")   # 全画面が比較不能

    def test_missing_route_html_is_unresolvable(self):
        self.write("app/home.html", '<div data-path="/home.html">v2</div>')
        v2 = self.commit("v2")
        cfg = self.config(screens={"route_map": [
            {"file_pattern": "^app/home\\.html$", "id": "home", "path": "/nope.html", "title": "x"}]})
        code, out = self.run_range(self.v1, v2, cfg)
        self.assertEqual(out["screens"][0]["compare_status"], "unresolvable")
        self.assertIn("route html", out["screens"][0]["compare_reason"])


class TestWorktreeInputsChange(Base):
    def test_change_during_capture_is_flagged(self):
        # build_command の中で作業ツリーのファイルを書き換える = 撮影中の編集を模す
        self.write("app/home.html", '<div data-path="/home.html">wip</div>')
        cfg = self.config(range={
            # After の snapshot を取った後（= After の build 中）に作業ツリーを書き換える
            "build_command": 'mkdir -p "$SCREENDIFF_WEB_DIR" && cp -R app/. "$SCREENDIFF_WEB_DIR"/ && '
                             f'if [ "$SCREENDIFF_SIDE" = after ]; then echo "<div data-path=\\"/home.html\\">edited</div>" > "{self.repo}/app/home.html"; fi'})
        code, out = self.run_range("HEAD", "worktree", cfg)
        self.assertEqual(out["run_status"], "compared", out)
        self.assertTrue(out["inputs_changed_during_capture"])
        html = Path(out["comparison_html"]).read_text()
        self.assertIn("撮影中に入力が変わった", html)


class TestRunnerUnavailable(Base):
    def test_browser_launch_failure_is_skipped_not_failed(self):
        self.write("app/home.html", '<div data-path="/home.html">v2</div>')
        v2 = self.commit("v2")
        code, out = self.run_range(self.v1, v2, self.config(), capture=self.capture_fail)
        self.assertEqual(code, 0)
        self.assertEqual(out["run_status"], "skipped_runner_unavailable")
        self.assertTrue(out["sides"]["before"]["server_stopped"])

    def test_page_stage_failure_is_unresolvable_not_runner_unavailable(self):
        """goto タイムアウト等の画面固有の失敗は、環境問題（再実行案内）に分類しない"""
        self.write("app/home.html", '<div data-path="/home.html">v2</div>')
        v2 = self.commit("v2")
        code, out = self.run_range(self.v1, v2, self.config(), capture=self.capture_page_fail)
        self.assertNotEqual(out["run_status"], "skipped_runner_unavailable")
        self.assertEqual(out["run_status"], "failed")   # 全画面が比較不能
        self.assertEqual(out["screens"][0]["compare_status"], "unresolvable")
        self.assertIn("page.goto", out["screens"][0]["compare_reason"])
        self.assertTrue(out["sides"]["after"]["server_stopped"])


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print("✅ screendiff_range self-test PASSED" if ok else "❌ screendiff_range self-test FAILED")
    sys.exit(0 if ok else 1)
