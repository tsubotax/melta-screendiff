#!/usr/bin/env python3
"""dev server の起動・停止を管理する（web backend用）。

serve_command を**独立したプロセスグループ**（start_new_session）で起動する。
シェルから `( cmd ) &` + `kill -- -PGID` で管理すると、非対話シェルでは
バックグラウンドジョブが呼び出し元と同じプロセスグループになるため、
グループkillが呼び出し元シェル自身を道連れにする（実地で確認済み）。
本スクリプトはそれを構造的に防ぐ。

使い方:
    python3 serve_ctl.py start --cmd "npm run dev" --cwd <repo-root> \
        --log <logfile> --pid-file <pidfile>
    python3 serve_ctl.py stop --pid-file <pidfile>

出力: JSON（stdout）
    start → {"pid": N}（起動のみ。疎通確認は capture.py の --wait-ready-sec が行う）
    stop  → {"stopped": bool, "forced": bool}
    stop は npm 等の子プロセスも含めてプロセスグループごと SIGTERM → 5秒待って
    残っていれば SIGKILL。stopped: false（それでも残存）は exit 1。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def pid_lstart(pid: int) -> str:
    """psのプロセス起動時刻（lstart）。プロセスがいなければ空文字。"""
    r = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def start(args) -> int:
    log = open(args.log, "w")
    proc = subprocess.Popen(
        args.cmd, shell=True, cwd=args.cwd,
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # 自前のセッション/プロセスグループ（pgid = pid）
    )
    # 起動時刻も記録し、stop 時に「pid が別プロセスに再利用されていないか」を検証する
    # （pid + lstart はほぼ一意。コマンド文字列マッチはpsが解決済みバイナリパスを出すため使えない）
    try:
        Path(args.pid_file).write_text(json.dumps(
            {"pid": proc.pid, "cmd": args.cmd, "lstart": pid_lstart(proc.pid)}))
    except OSError as e:
        # pid-fileを書けないと起動済みserverが追跡不能（Cleanupから止められない）になるため、
        # server側を即座に片付けてからエラーにする
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        print(json.dumps({"error": f"pid-fileを書き込めません（serverは停止しました）: {e}"}, ensure_ascii=False))
        return 1

    # 即死検知: コマンドのタイポ・ポート衝突は起動直後に死ぬ。ready待ちのタイムアウト
    # （数十秒）まで気づけないより、ここで即エラーにしてログ末尾を見せる
    time.sleep(0.7)
    if proc.poll() is not None:
        log.flush()
        tail = Path(args.log).read_text(errors="ignore")[-500:]
        Path(args.pid_file).unlink(missing_ok=True)
        print(json.dumps({"error": f"serve_command が起動直後に終了しました（exit {proc.returncode}）",
                          "log_tail": tail}, ensure_ascii=False))
        return 1
    print(json.dumps({"pid": proc.pid}))
    return 0


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop(args) -> int:
    pid_file = Path(args.pid_file)
    if not pid_file.exists():
        print(json.dumps({"stopped": True, "forced": False, "note": "pid-fileなし（未起動または停止済み）"}))
        return 0
    raw = pid_file.read_text().strip()
    try:
        info = json.loads(raw)
        pgid, lstart = int(info["pid"]), info.get("lstart", "")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # serve_ctl start が書いた形式以外は扱わない（数値のみ等の不明な形式で
        # 検証なしのkillpgをすると、pid再利用時に無関係プロセスを殺しうる）
        print(json.dumps({"stopped": False, "forced": False,
                          "note": f"pid-fileがserve_ctlの形式ではありません。手動確認が必要: {raw[:80]}"},
                         ensure_ascii=False))
        return 1
    if pgid <= 1:
        # pid 0 は「呼び出し元自身のプロセスグループ」を意味し killpg(0) で自爆する
        print(json.dumps({"stopped": False, "forced": False, "note": f"pid-fileのpidが不正です: {pgid}"}))
        return 1

    current_lstart = pid_lstart(pgid)
    if group_alive(pgid) and current_lstart and lstart and current_lstart != lstart:
        # リーダーpidが生きているが起動時刻が違う = server自然死後のpid再利用。無関係プロセスをkillしない。
        # （current_lstart が空 = リーダーだけ死んで子が同一PGIDで残存しているケース。これは自分たちの
        #   残骸なので通常どおりkillpgに進む。PGIDはグループ生存中は別プロセスに再割当てされない）
        pid_file.unlink(missing_ok=True)
        print(json.dumps({"stopped": True, "forced": False,
                          "note": "pidが別プロセスに再利用されていたためkillせず終了（serverは既に停止済み）"},
                         ensure_ascii=False))
        return 0
    forced = False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and group_alive(pgid):
        time.sleep(0.2)
    if group_alive(pgid):
        forced = True
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)
    stopped = not group_alive(pgid)
    if stopped:
        pid_file.unlink(missing_ok=True)
    print(json.dumps({"stopped": stopped, "forced": forced}))
    return 0 if stopped else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("--cmd", required=True, help="serve_command（shell経由で実行）")
    p_start.add_argument("--cwd", required=True)
    p_start.add_argument("--log", required=True)
    p_start.add_argument("--pid-file", required=True)
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--pid-file", required=True)
    args = parser.parse_args()
    return start(args) if args.action == "start" else stop(args)


if __name__ == "__main__":
    sys.exit(main())
