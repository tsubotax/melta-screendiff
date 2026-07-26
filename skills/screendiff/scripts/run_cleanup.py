#!/usr/bin/env python3
"""config の `cleanup_command` を repo-root で1回実行する（Cleanup フェーズ専用）。

⚠️ **なぜスクリプトなのか**（`setup_command` のようにSKILL.md内のシェル直書きにしない理由）:

1. **未設定の判定を人間/AIの目視に委ねない** — 設定値をシェルに埋め込む方式だと、
   未設定時に `if [ -n "null" ]` という常に真の条件が出来上がり、`null` という名の
   コマンドを実行して失敗する（あるいは条件ブロックごと書き落として黙ってスキップする）。
   このスクリプトは config を自分で読むので、埋め込みの判断が発生しない
2. **失敗を握り潰さない** — Cleanup は正常終了・早期終了・エラー中断のすべての経路で
   走る「最後の処理」で、AIが最も雑になりやすい。ここで exit code を落とすと、
   stale なビルド成果物が残ったまま「完了しました」と報告される。exit code と
   構造化JSONの両方で失敗を返す

読み取り専用ではない（設定されたコマンドを実行する）。実行判断はSKILL.md側にある
＝「復帰と stash pop が成功した後にのみ呼ぶ」という順序契約は呼び出し元が守る。

使い方:
    python3 run_cleanup.py --config-json "$CONFIG_JSON" --repo-root "$REPO_ROOT"

出力（stdout, JSON）:
    {"status": "skipped"}                                  … cleanup_command 未設定(null)。exit 0
    {"status": "succeeded", "exit_code": 0, ...}           … 実行して成功。exit 0
    {"status": "failed", "exit_code": 3, ...}              … 実行して失敗。exit 1
    {"status": "error", "error": "..."}                    … 実行前に失敗（config不正等）。exit 1

「未設定」は **null（またはキーごと無し）だけ**。空文字は load_config が不正として弾く値なので、
ここでも error にする（片方が黙って受け入れると、直接呼ばれた経路で成功偽装が復活する）。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# 出力が巨大なビルドツールでもAIのコンテキストを潰さないよう末尾だけ返す
TAIL_CHARS = 2000


def tail(text: str) -> str:
    if len(text) <= TAIL_CHARS:
        return text
    return "…（省略）…\n" + text[-TAIL_CHARS:]


def emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-json", required=True, help="load_config.py の出力を保存したファイル")
    parser.add_argument("--repo-root", required=True, help="コマンドの cwd（リポジトリルート）")
    args = parser.parse_args()

    # config が読めないときに「未設定」へ倒さない（fail closed）。設定した後片付けが
    # 黙って実行されないまま skipped と報告されるのが最悪のケース
    try:
        config = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return emit({"status": "error", "error": f"config を読めません: {e}"}, 1)
    if not isinstance(config, dict):
        return emit({"status": "error",
                     "error": f"config はJSONオブジェクトです: {type(config).__name__}"}, 1)

    if "cleanup_command" not in config or config["cleanup_command"] is None:
        return emit({"status": "skipped", "message": "cleanup_command は未設定です"}, 0)
    command = config["cleanup_command"]
    if not isinstance(command, str):
        return emit({"status": "error",
                     "error": f"cleanup_command は文字列です: {type(command).__name__}"}, 1)
    if not command.strip():
        # 空文字を skipped に倒すと、load_config が不正として弾く値をこちらは正常終了で
        # 受け入れることになる（＝「設定したつもりで何も走らない」を成功偽装する）。
        # 未設定は null で表現する
        return emit({"status": "error",
                     "error": "cleanup_command が空文字です（実行しないなら null にしてください）"}, 1)

    repo_root = Path(args.repo_root)
    if not repo_root.is_dir():
        return emit({"status": "error", "error": f"--repo-root がディレクトリではありません: {repo_root}"}, 1)

    try:
        # errors="replace" が無いと、非UTF-8を吐くコマンド（ロケール依存のツール等）で
        # デコードが UnicodeDecodeError になり、JSONを返さず traceback で落ちる
        # ＝「必ず構造化して返す」契約が破れる
        proc = subprocess.run(command, shell=True, cwd=str(repo_root),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as e:
        # シェル自体が起動できない等。traceback にせず構造化して返す
        return emit({"status": "error", "command": command,
                     "error": f"コマンドを起動できません: {e}"}, 1)

    payload = {
        "status": "succeeded" if proc.returncode == 0 else "failed",
        "command": command,
        "exit_code": proc.returncode,
        "stdout_tail": tail(proc.stdout),
        "stderr_tail": tail(proc.stderr),
    }
    if proc.returncode != 0:
        payload["hint"] = ("後片付けが失敗しています。ビルド成果物が残置したまま次の作業に入ると "
                           "stale な成果物を掴むため、共有フェーズへ進まずユーザーに状態を提示してください")
    return emit(payload, 0 if proc.returncode == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
