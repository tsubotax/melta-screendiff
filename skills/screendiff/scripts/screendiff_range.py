#!/usr/bin/env python3
"""範囲モード: PR を使わず、2 つの ref（または作業ツリー）の Before/After を 1 コマンドで撮って比較 HTML を作る。

PR モード（SKILL.md の Phase 0〜7）とは**別の入口**。PR モードの HEAD 一致ゲートや checkout の
往復は使わず、両側を `git archive` / `git ls-files` の隔離 snapshot からビルドする。作業ツリーで
checkout も stash もしない。

    python3 screendiff_range.py --repo-root <repo> --before origin/main --after HEAD
    python3 screendiff_range.py --repo-root <repo> --before HEAD --after worktree   # 未 commit を After に

結果（stdout, JSON）の `run_status` は 5 値。**どの値でも通常の push を止めない**（呼び出し元は
compared 以外のとき理由を 1 行で報告して先へ進む）:

    compared                    比較 HTML ができた
    no_ui_change                target_file_patterns に当たる変更が無い（何もビルドしない）
    skipped_dependency_change   package / lock / patches が A・B・共有元で一致せず撮らない
    skipped_runner_unavailable  ブラウザ（Playwright/Chromium）を起動できない環境
    failed                      ビルド・配信・描画の失敗

exit code: compared / no_ui_change / skipped_* は 0、failed は 1。

事故対策（設計レビュー 3 周の反映。docs/contracts.md §1.4）:
- 依存の 3 者一致: A・B・共有元（作業ツリー）の依存ファイルのハッシュが揃わなければ撮らない。
  snapshot の node_modules は共有元への symlink なので、揃っていないと両側とも別の依存でビルドする
- side ごとの出力先 + nonce: 配信先に実行固有の nonce ファイルを置き、URL 経由で同じ中身が返るまで
  ready としない（前回の残存サーバや別の配信元を撮らない）
- 撮った同じブラウザで pathname と ready_selector を検査（SPA フォールバック・JS 遷移対策）
- worktree 比較は撮影前後で入力の内容ハッシュを突き合わせ、変化したら manifest と HTML に残す
  （ハッシュは snapshot にコピーした実体から取る。コピー元を読み直すと保存のタイミングで見逃す）
- 失敗を成功偽装しない: 画面ごとの compare_status と実行全体の run_status を別に持つ。
  対象ファイルはあるのに撮れる画面が 0 のときは no_ui_change でなく failed（未確認を消さない）
- ref は最初に commit OID へ固定し、以後の diff / snapshot はその OID だけを使う（ビルド中の commit / fetch でズレない）
- 配信サーバは main の finally で必ず止める（Ctrl-C・撮影タイムアウト・node 不在でも残さない）
"""
import argparse
import base64
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath

SCRIPTS = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
RUN_STATUSES = ("compared", "no_ui_change", "skipped_dependency_change", "skipped_runner_unavailable", "failed")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DEFAULT_DATA_CAVEAT = "API データは固定していない。Before/After の間のデータ変動が差分に混ざりうる"
# 起動した配信サーバ。main の finally で必ず止める（Ctrl-C・撮影タイムアウト・例外でも残さない）
ACTIVE_SERVERS: list["Server"] = []


def emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def git(repo: Path, *args, text=True, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=text, check=check)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 入力の一覧とハッシュ
# ---------------------------------------------------------------------------

def inventory(repo: Path, ref: str | None, snapshot_paths: list[str]) -> list[str]:
    """snapshot に入れるファイル一覧。ref=None は作業ツリー（追跡 + 未追跡、ignored 除く）。"""
    if ref is None:
        out = git(repo, "ls-files", "-co", "--exclude-standard", "-z", "--", *snapshot_paths).stdout
        deleted = set(git(repo, "ls-files", "-d", "-z", "--", *snapshot_paths).stdout.split("\0"))
        files = [f for f in out.split("\0") if f and f not in deleted]
    else:
        r = git(repo, "ls-tree", "-r", "--name-only", "-z", ref, "--", *snapshot_paths)
        if r.returncode != 0:
            raise RuntimeError(f"git ls-tree {ref} に失敗: {r.stderr.strip()}")
        files = [f for f in r.stdout.split("\0") if f]
    return sorted(set(f for f in files if not re.search(r"(^|/)\.vscode/", f)))


def read_input(repo: Path, ref: str | None, rel: str) -> bytes:
    if ref is None:
        return (repo / rel).read_bytes()
    r = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=repo, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git show {ref}:{rel} に失敗")
    return r.stdout


def inputs_hash(repo: Path, ref: str | None, files: list[str]) -> str:
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode()); h.update(b"\0"); h.update(sha256_bytes(read_input(repo, ref, rel)).encode())
    return h.hexdigest()


def dependency_hash(repo: Path, ref: str | None, patterns: list[str]) -> tuple[str, int]:
    """依存ファイルのハッシュと件数。snapshot_paths とは独立にリポ全体から探す
    （snapshot の外にある package.json / lock を見落として、別の依存でビルドしない）。"""
    compiled = [re.compile(p) for p in patterns]
    files = [f for f in inventory(repo, ref, []) if any(c.search(f) for c in compiled)]
    return inputs_hash(repo, ref, files), len(files)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def make_snapshot(repo: Path, ref: str | None, dest: Path, rng: dict) -> dict:
    """隔離 snapshot を作る。返り値は inputs_sha256 / inputs_count。"""
    dest.mkdir(parents=True, exist_ok=True)
    files = inventory(repo, ref, rng["snapshot_paths"])
    if ref is None:
        for rel in files:
            src = repo / rel
            if not src.is_file():
                raise RuntimeError(f"nonregular input: {rel}")
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / rel)
        # ハッシュは**コピーした実体**から取る。コピー元から読み直すと、コピー完了〜ハッシュ取得の間の
        # 保存で「snapshot は旧内容・ハッシュは新内容」になり、撮影後の突き合わせが変化を見逃す
        digest = inputs_hash(dest, None, files)
    else:
        ar = subprocess.Popen(["git", "archive", "--format=tar", ref, "--", *rng["snapshot_paths"]],
                              cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=ar.stdout, capture_output=True)
        _, err = ar.communicate()
        if ar.returncode or tar.returncode:
            raise RuntimeError(f"git archive {ref} に失敗: {err.decode(errors='replace').strip()}")
        digest = inputs_hash(repo, ref, files)
    link = rng.get("node_modules_link")
    if link:
        target = repo / link
        nm = dest / link
        if nm.exists() or nm.is_symlink():
            raise RuntimeError(f"snapshot に {link} が既にある（追跡されている？）")
        if not target.is_dir():
            raise RuntimeError(f"node_modules_link の実体が無い: {target}")
        nm.parent.mkdir(parents=True, exist_ok=True)
        nm.symlink_to(target, target_is_directory=True)
    dest_real = dest.resolve()
    for fx in rng.get("fixtures", []):
        rel = PurePosixPath(fx["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(f"fixture のパスは snapshot 相対で .. を含まないこと: {fx['path']}")
        p = dest / fx["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        # symlink（共有 node_modules 等）を経由して snapshot の外へ書かない
        if not p.parent.resolve().is_relative_to(dest_real) or p.is_symlink():
            raise RuntimeError(f"fixture の書き込み先が snapshot の外に出る（symlink 経由）: {fx['path']}")
        p.write_bytes(base64.b64decode(fx["base64"]))
    return {"inputs_sha256": digest, "inputs_count": len(files)}


# ---------------------------------------------------------------------------
# 配信
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: float = 3.0) -> tuple[int | None, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:  # noqa: BLE001  接続拒否・タイムアウト
        return None, b""


class Server:
    """serve_command を独立プロセスグループで起動し、nonce で配信元を確認し、必ず止める。"""

    def __init__(self, cmd: str, cwd: Path, env: dict, log: Path):
        self.cmd, self.cwd, self.env, self.log = cmd, cwd, env, log
        self.proc = None
        self.url = None

    def start(self, fixed_url: str | None, web_dir: Path, nonce: str, timeout: float) -> str:
        if fixed_url:
            status, _ = http_get(fixed_url)
            if status is not None:
                raise RuntimeError(f"起動前から {fixed_url} が応答している（前回の残存サーバか別の dev server）。止めてから再実行")
        self.proc = subprocess.Popen(["bash", "-c", self.cmd], cwd=self.cwd, env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     start_new_session=True)
        ACTIVE_SERVERS.append(self)
        deadline = time.time() + timeout
        url = fixed_url
        lines = []
        # readline() はパイプ越しだと（子が stdout をバッファしていると）永久に待つ。期限付きの select で読む
        while time.time() < deadline and url is None:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.5)
            if ready:
                line = self.proc.stdout.readline()
                if not line:
                    break
                lines.append(line)
                m = re.search(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+", line)
                if m:
                    url = m.group(0).replace("0.0.0.0", "127.0.0.1")
            elif self.proc.poll() is not None:
                break
        self.log.write_text("".join(lines))
        if url is None:
            self.stop()
            raise RuntimeError("serve_command の出力から URL を取れない（range.serve_url を指定するか、起動時に URL を出力させる）")
        # nonce: この export を配信していることを確認するまで ready としない
        want = nonce.encode()
        while time.time() < deadline:
            status, body = http_get(f"{url}/{nonce}")
            if status == 200 and body.strip() == want:
                self.url = url
                return url
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"{url} が今回の export（nonce）を返さない。別の配信元を見ているか起動失敗")

    def stop(self) -> bool:
        if self.proc is None:
            return True
        import signal
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return False
        if self in ACTIVE_SERVERS:
            ACTIVE_SERVERS.remove(self)
        return True


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------

def make_run_dir(base: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for cand in [stamp] + [f"{stamp}-{i}" for i in range(2, 101)]:
        d = base / cand
        try:
            d.mkdir(parents=True, exist_ok=False)
            return d
        except FileExistsError:
            continue
    raise RuntimeError(f"空きの run-id を見つけられません: {base}")


def run_script(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def resolve_screens(config: dict, config_json: Path, repo: Path, changed: list[str]) -> dict:
    screens = config.get("screens", {})
    if screens.get("resolver") == "expo_router":
        r = run_script([sys.executable, str(SCRIPTS / "resolvers" / "expo_router.py"),
                        "--config-json", str(config_json), "--repo-root", str(repo), *changed])
    elif screens.get("resolver_command"):
        r = subprocess.run(["bash", "-c", screens["resolver_command"] + ' "$@"', "_", *changed],
                           cwd=repo, capture_output=True, text=True)
    else:
        r = run_script([sys.executable, str(SCRIPTS / "resolve_screens.py"), "--config-json", str(config_json), *changed])
    if r.returncode != 0:
        raise RuntimeError(f"resolver が失敗: {(r.stdout + r.stderr)[-800:]}")
    raw = json.loads(r.stdout)
    v = run_script([sys.executable, str(SCRIPTS / "validate_resolved.py"), "--config-json", str(config_json), "-"],
                   input=json.dumps(raw))
    if v.returncode != 0:
        raise RuntimeError(f"resolver の出力が契約を満たさない: {v.stdout[-800:]}")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--before", required=True, help="Before の ref（例: origin/main）")
    parser.add_argument("--after", required=True, help="After の ref、または worktree（未 commit を含む作業ツリー）")
    parser.add_argument("--config", default=None, help="設定ファイルの明示指定（省略時は load_config.py の探索順）")
    parser.add_argument("--share", default=None, choices=("artifact", "local"))
    parser.add_argument("--capture-cmd", default=None,
                        help="撮影コマンドの差し替え（テスト・代替エンジン用）。既定は node capture_browser.mjs")
    parser.add_argument("--label", default=None, help="run-id の接頭辞（英数字・-_.）")
    a = parser.parse_args()

    repo = Path(a.repo_root).resolve()
    result = {"mode": "range", "run_status": None, "before": a.before, "after": a.after}

    # Phase 0: 設定
    cfg_args = [sys.executable, str(SCRIPTS / "load_config.py"), "--repo-root", str(repo)]
    if a.config:
        cfg_args += ["--config", a.config]
    r = run_script(cfg_args)
    if r.returncode != 0:
        result.update(run_status="failed", reason="設定を読めない", details=r.stdout[-1500:])
        return emit(result, 1)
    config = json.loads(r.stdout)
    rng = config.get("range")
    if not isinstance(rng, dict) or not rng.get("build_command") or not rng.get("serve_command"):
        result.update(run_status="failed", reason="range.build_command と range.serve_command が必要（docs/contracts.md §1.4）")
        return emit(result, 1)
    web = config.get("web", {})

    # share plan（範囲モードでは PR 書き込みは常に禁止。share_plan.py が確定する）
    sp_args = [sys.executable, str(SCRIPTS / "share_plan.py"), "--config-json", "-", "--mode", "range"]
    if a.share:
        sp_args += ["--share", a.share]
    sp = run_script(sp_args, input=json.dumps(config))
    if sp.returncode != 0:
        result.update(run_status="failed", reason="share_plan が失敗", details=sp.stdout[-800:])
        return emit(result, 1)
    result["share_plan"] = json.loads(sp.stdout)

    # 出力先
    out_base = Path(config["output_dir"])
    if not out_base.is_absolute():
        out_base = repo / out_base
    if a.label and not SAFE_NAME.fullmatch(a.label):
        result.update(run_status="failed", reason=f"--label に使えない文字: {a.label!r}")
        return emit(result, 1)
    try:
        out = make_run_dir(out_base / "range" / a.label if a.label else out_base / "range")
    except OSError as e:
        result.update(run_status="failed", reason=f"出力ディレクトリを作れない: {e}")
        return emit(result, 1)
    result["out_dir"] = str(out)
    config_json = out / "config.json"
    config_json.write_text(json.dumps(config, ensure_ascii=False, indent=2))


    try:
        return run(a, repo, config, rng, web, out, config_json, result)
    except subprocess.TimeoutExpired as e:
        cmd = e.cmd if isinstance(e.cmd, (list, tuple)) else [str(e.cmd)]
        result.update(run_status="failed", reason=f"タイムアウト: {' '.join(str(c) for c in cmd[:3])}")
    except KeyboardInterrupt:
        result.update(run_status="failed", reason="中断（Ctrl-C）")
    except Exception as e:  # noqa: BLE001  node 不在（FileNotFoundError）等。黙って抜けずに記録する
        result.update(run_status="failed", reason=f"{type(e).__name__}: {e}")
    finally:
        # 配信サーバは独立セッションなので、ここで止めないと Ctrl-C でも生き残る
        for srv in list(ACTIVE_SERVERS):
            srv.stop()
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return emit(result, 1)


def run(a, repo: Path, config: dict, rng: dict, web: dict, out: Path, config_json: Path, result: dict) -> int:
    def save() -> None:
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))

    before_name = a.before
    after_name = None if a.after == "worktree" else a.after
    # ref の存在確認（typo を「変更なし」に倒さない）
    for ref in (before_name, after_name):
        if ref and git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode != 0:
            result.update(run_status="failed", reason=f"ref を解決できない: {ref}")
            return emit(result, 1)
    # ⚠️ 以後の git 操作は**ここで固定した OID だけ**を使う。`HEAD` / `origin/main` の名前のまま進めると、
    # Before のビルド中に別ターミナルで commit / fetch されたとき After の中身が記録した OID とズレる
    oids = {"before": git(repo, "rev-parse", f"{before_name}^{{commit}}").stdout.strip(),
            "after": git(repo, "rev-parse", f"{after_name or 'HEAD'}^{{commit}}").stdout.strip()}
    before_ref = oids["before"]
    after_ref = None if after_name is None else oids["after"]
    result["before_oid"], result["head_oid"] = oids["before"], oids["after"]
    result["after_source"] = "worktree" if after_ref is None else "ref"

    # Phase 1: 対象判定
    if after_ref is None:
        tracked = git(repo, "diff", "--name-only", "-z", before_ref, "--").stdout
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout
        changed = sorted(set(f for f in (tracked + untracked).split("\0") if f))
    else:
        changed = sorted(set(f for f in git(repo, "diff", "--name-only", "-z", before_ref, after_ref, "--").stdout.split("\0") if f))
    patterns = [re.compile(p) for p in config["target_file_patterns"]]
    targets = [f for f in changed if any(p.search(f) for p in patterns)]
    result["changed_files"] = len(changed)
    result["target_files"] = targets
    if not targets:
        result.update(run_status="no_ui_change", reason="target_file_patterns に当たる変更が無い")
        save()
        return emit(result, 0)

    # 依存の 3 者一致（リポ全体から探す。snapshot_paths の外の package.json も対象）
    dep_patterns = rng.get("dependency_change_patterns", [])
    try:
        dep = {side: dependency_hash(repo, ref, dep_patterns)
               for side, ref in (("before", before_ref), ("after", after_ref), ("shared", None))}
    except RuntimeError as e:
        result.update(run_status="failed", reason=str(e))
        save()
        return emit(result, 1)
    result["dependency_hashes"] = {side: h for side, (h, _) in dep.items()}
    result["dependency_files"] = {side: n for side, (_, n) in dep.items()}
    if rng.get("node_modules_link"):
        if all(n == 0 for _, n in dep.values()):
            result.update(run_status="failed",
                          reason="dependency_change_patterns に当たるファイルが 3 者とも 0 件。パターンが実際の package.json / lock に"
                                 "当たっていない（設定ミス）ので、依存一致を確認できない")
            save()
            return emit(result, 1)
        if len({h for h, _ in dep.values()}) != 1:
            result.update(run_status="skipped_dependency_change",
                          reason="package / lock / patches が Before・After・共有元（作業ツリー）で一致しない。隔離インストールは初版未対応")
            save()
            return emit(result, 0)

    # Phase 2: 画面解決
    try:
        resolved = resolve_screens(config, config_json, repo, targets)
    except (RuntimeError, ValueError) as e:
        result.update(run_status="failed", reason=str(e))
        save()
        return emit(result, 1)
    (out / "resolved.json").write_text(json.dumps(resolved, ensure_ascii=False, indent=2))
    screens = resolved.get("resolved", [])
    result["unsupported"] = resolved.get("unsupported", [])
    result["unresolved"] = resolved.get("unresolved", [])
    if not screens:
        # UI の変更はあるのに撮れる画面が 1 つも無い。「変更なし」と報告すると未確認が消えるので failed
        result.update(run_status="failed",
                      reason="対象ファイルはあるが撮れる画面に解決されない。unsupported / unresolved の画面を Simulator 等で別途確認する")
        save()
        return emit(result, 1)

    # Phase 3/4: 両側
    env = dict(os.environ)
    env.update({k: str(v) for k, v in rng.get("env", {}).items()})
    capture_cmd = a.capture_cmd
    capture_timeout = int(rng.get("capture_timeout_sec", 180))
    pw = repo / rng.get("playwright_module", "node_modules/playwright")
    sides = result["sides"] = {}
    runner_unavailable = False
    for side, ref in (("before", before_ref), ("after", after_ref)):
        side_dir = out / side
        snap, web_dir, shots = side_dir / "input", side_dir / "web", side_dir / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        web_dir.mkdir(parents=True, exist_ok=True)
        info = sides[side] = {"ref": ref or "worktree"}
        try:
            info.update(make_snapshot(repo, ref, snap, rng))
        except RuntimeError as e:
            result.update(run_status="failed", reason=f"{side} の snapshot 作成に失敗: {e}")
            save()
            return emit(result, 1)
        side_env = dict(env, SCREENDIFF_WEB_DIR=str(web_dir), SCREENDIFF_SIDE=side)
        t0 = time.time()
        b = subprocess.run(["bash", "-c", rng["build_command"]], cwd=snap, env=side_env, capture_output=True, text=True,
                           timeout=int(rng.get("build_timeout_sec", 600)))
        (side_dir / "build.log").write_text(b.stdout + "\n--- stderr ---\n" + b.stderr)
        info["build_sec"] = round(time.time() - t0, 1)
        info["build_exit"] = b.returncode
        if b.returncode != 0:
            info["build_tail"] = (b.stdout + b.stderr)[-1500:]
            result.update(run_status="failed", reason=f"{side} の build_command が失敗（build.log 参照）")
            save()
            return emit(result, 1)
        for s in screens:
            # 静的検査: export 成果物にそのルートの HTML があるか（SPA フォールバックで 200 が返る前に塞ぐ）
            rel = s["path"].strip("/")
            s[f"{side}_html"] = (rel == "" or (web_dir / rel).is_file() or (web_dir / f"{rel}.html").is_file()
                                 or (web_dir / rel / "index.html").is_file())
        nonce = f"screendiff-{out.name}-{side}.txt"
        (web_dir / nonce).write_text(nonce)
        srv = Server(rng["serve_command"], snap, side_env, side_dir / "serve.log")
        try:
            try:
                url = srv.start(rng.get("serve_url"), web_dir, nonce, float(web.get("ready_timeout_sec", 60)))
            except RuntimeError as e:
                result.update(run_status="failed", reason=f"{side} の配信に失敗: {e}")
                save()
                return emit(result, 1)
            info["serve_url"] = url
            info["server_pgid"] = srv.proc.pid   # start_new_session なので pid = プロセスグループ id
            save()   # 撮影中に落ちても serve_url / server_pgid と out_dir が result.json に残る
            for s in screens:
                if not s[f"{side}_html"]:
                    s[f"{side}_capture"] = {"ok": False, "error": "route html が export に無い"}
                    continue
                png = shots / f"{side}-{s['screen_id']}-p1.png"
                if capture_cmd:
                    cmd = ["bash", "-c", capture_cmd + ' "$@"', "_"]
                else:
                    cmd = ["node", str(SCRIPTS / "backends" / "web" / "capture_browser.mjs"), "--pw", str(pw)]
                cmd += ["--url", f"{url}{s['path']}", "--out", str(png), "--expect-path", s["path"],
                        "--settle-ms", str(web.get("settle_ms", 1500)), "--viewport", str(web.get("viewport", "393x852")),
                        "--scale", str(rng.get("device_scale_factor", 3)), "--ready-timeout-ms", str(rng.get("ready_timeout_ms", 10000))]
                if not web.get("full_page", True):
                    cmd.append("--no-full-page")
                if s.get("ready_selector"):
                    cmd += ["--ready-selector", s["ready_selector"]]
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=capture_timeout, cwd=repo)
                try:
                    cap = json.loads(cp.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    cap = {"ok": False, "error": (cp.stdout + cp.stderr)[-800:]}
                # exit 1 = ブラウザ側の起動失敗（playwright が読めない / chromium が立たない）。
                # 画面ごとの goto タイムアウトや保存失敗（stage=page, exit 2）は当該画面の比較不能であって環境問題ではない
                if cp.returncode == 1 and cap.get("error") and cap.get("stage", "launch") in ("load", "launch"):
                    runner_unavailable = True
                s[f"{side}_capture"] = cap
        finally:
            info["server_stopped"] = srv.stop()
        if not info["server_stopped"]:
            result.update(run_status="failed", reason=f"{side} のサーバを止められない（pid {srv.proc.pid}）")
            save()
            return emit(result, 1)
        if runner_unavailable:
            result.update(run_status="skipped_runner_unavailable",
                          reason="ブラウザを起動できない環境（sandbox 内など）。通常の Terminal か cmux のペインから同じコマンドを実行",
                          details=[s.get(f"{side}_capture", {}).get("error") for s in screens][:1])
            save()
            return emit(result, 0)

    # worktree: 撮影中に入力が変わっていないか（snapshot のコピー実体のハッシュと、今の作業ツリーを突き合わせる）
    if after_ref is None:
        now = inputs_hash(repo, None, inventory(repo, None, rng["snapshot_paths"]))
        result["inputs_changed_during_capture"] = now != sides["after"]["inputs_sha256"]

    # Phase 5: manifest → HTML
    cards = []
    for s in screens:
        b, af = s.get("before_capture", {}), s.get("after_capture", {})
        if b.get("ok") and af.get("ok"):
            status = "compared"
        elif af.get("ok") and not s.get("before_html"):
            status = "new"
        elif b.get("ok") and not s.get("after_html"):
            status = "removed"
        else:
            status = "unresolvable"
        reasons = []
        for side, cap in (("Before", b), ("After", af)):
            if cap.get("error"):
                reasons.append(f"{side}: {cap['error']}")
            elif cap.get("path_matched") is False:
                reasons.append(f"{side}: 遷移先が {cap.get('final_path')}（期待 {s['path']}）")
            elif cap.get("ready") == "missing":
                reasons.append(f"{side}: 表示完了条件 {s.get('ready_selector')} が成立しない")
        card = {
            "screen_id": s["screen_id"], "title": s.get("title", s["screen_id"]),
            "kind": status if status in ("new", "removed") else "changed",
            "compare_status": status, "compare_reason": " / ".join(reasons),
            "representative": bool(s.get("representative")), "also_direct": bool(s.get("also_direct")),
            "source_files": s.get("source_files", [s.get("source_file")]),
            "before_paths": [b["file"]] if b.get("file") and status != "new" else [],
            "after_paths": [af["file"]] if af.get("file") and status != "removed" else [],
            "before_ready": b.get("ready"), "after_ready": af.get("ready"),
            "description": "",
        }
        cards.append(card)
    manifest = {
        "mode": "range", "pr_number": "", "pr_title": f"{a.before} → {a.after}", "pr_url": "",
        "base_ref": a.before, "head_ref": a.after,
        "head_oid": oids["after"],
        "after_source": result["after_source"],
        "inputs_changed_during_capture": result.get("inputs_changed_during_capture", False),
        "inputs_sha256": {"before": sides["before"]["inputs_sha256"], "after": sides["after"]["inputs_sha256"]},
        "unsupported": result["unsupported"],
        "data_caveat": rng.get("data_caveat") or DEFAULT_DATA_CAVEAT,   # null でも既定の注記を出す（契約 §1.5）
        "build_status": "SUCCEEDED", "validation_status": "",
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"), "screens": cards,
    }
    # Before を 1 枚も撮れていない回（全画面が新規など）は before_oid / before_base を記録しない（契約 §4）
    if any(s.get("before_capture", {}).get("ok") for s in screens):
        manifest["before_oid"], manifest["before_base"] = oids["before"], "range"
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    rp = run_script([sys.executable, str(SCRIPTS / "render_comparison.py"), "--manifest", str(out / "manifest.json"),
                     "--output", str(out / "comparison.html")])
    (out / "render.log").write_text(rp.stdout + rp.stderr)
    result["screens"] = [{k: c[k] for k in ("screen_id", "title", "compare_status", "compare_reason", "representative", "also_direct")} for c in cards]
    if rp.returncode != 0:
        result.update(run_status="failed", reason="比較 HTML の描画に失敗（render.log 参照）")
    elif any(c["compare_status"] in ("compared", "new", "removed") for c in cards):
        result.update(run_status="compared", comparison_html=str(out / "comparison.html"))
    else:
        result.update(run_status="failed", reason="全画面が比較不能（compare_reason 参照）", comparison_html=str(out / "comparison.html"))
    save()
    return emit(result, 0 if result["run_status"] != "failed" else 1)


if __name__ == "__main__":
    sys.exit(main())
