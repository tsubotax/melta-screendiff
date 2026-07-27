#!/usr/bin/env python3
"""この実行専用の出力ディレクトリを作る（Phase 0）。

⚠️ **なぜ実行ごとに分けるのか**: 出力先を `output_dir/<PR>` に固定すると、同じPRの
再実行が同じ場所を使う。同一PRの撮り直し（レビュー指摘 → 修正 push → 再撮影）は
例外ではなく通常運用なので、これは次の2つを同時に起こす:

1. **前イテレーションの証跡が消える** — 修正前後の比較HTMLを並べられない
2. **撮影前に中断した回に、前回の comparison.html が「今回の比較」として残る**
   （SKILL.md 側の COMPARISON_READY と合わせた多層防御。片方だけに頼らない）

`output_dir/<PR>/<実行時刻>/` を作り、`output_dir/<PR>/latest` を最新実行へ向ける。
latest は添付・共有時の安定参照用（symlink が作れない環境では省略され、`latest` は
null で返る。ディレクトリ作成自体は成功させる）。

読み取り専用ではない（ディレクトリと symlink を作る）。既存ファイルは一切消さない。

使い方:
    python3 run_dir.py --config-json "$CONFIG_JSON" --repo-root "$REPO_ROOT" --pr 42

出力（stdout, JSON）: out_dir（絶対パス）/ run_id / pr_dir / latest。exit 0。
config が読めない・作成できない場合は exit 1。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

# パス区切りだけを弾くと "." と ".." が通り、出力先がPRディレクトリの外へ出る。
# 先頭は英数字に限定し、以降も安全な文字だけ許す（許可リスト方式）
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MAX_RUN_ID_SUFFIX = 100


def emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def link_latest(pr_dir: Path, run_id: str) -> str | None:
    """`latest` を今回の実行へ向ける。作れない環境では None を返す（失敗にしない）。"""
    latest = pr_dir / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            # ディレクトリ実体が居座っている場合は触らない（利用者の成果物を消さない）
            if not latest.is_symlink():
                return None
            latest.unlink()
        # 相対リンクにしておくと output_dir ごと移動しても壊れない
        latest.symlink_to(run_id, target_is_directory=True)
        return str(latest)
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-json", required=True, help="load_config.py の出力を保存したファイル")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--pr", required=True, help="PR番号")
    parser.add_argument("--run-id", default=None,
                        help="実行ID（既定はローカル時刻 YYYYmmdd-HHMMSS）")
    args = parser.parse_args()

    try:
        config = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return emit({"error": f"config を読めません: {e}"}, 1)
    if not isinstance(config, dict):
        return emit({"error": f"config はJSONオブジェクトです: {type(config).__name__}"}, 1)

    output_dir = config.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        return emit({"error": f"output_dir が不正です: {output_dir!r}"}, 1)

    # output_dir は repo-root 相対 or 絶対パス（contracts §1）
    base = Path(output_dir)
    if not base.is_absolute():
        base = Path(args.repo_root) / base

    if not re.fullmatch(r"\d+", str(args.pr)):
        # PR番号はパス要素になる。"../x" のような値を通さない
        return emit({"error": f"--pr は正の整数です: {args.pr!r}"}, 1)
    if args.run_id is not None and not SAFE_NAME.fullmatch(args.run_id):
        return emit({"error": f"run-id に使えない文字が含まれています: {args.run_id!r}",
                     "hint": "英数字で始まり、英数字と - _ . のみ使えます"}, 1)

    pr_dir = base / str(args.pr)
    # ⚠️ exist_ok=True で作らない。既定の run-id は秒精度なので、同じ秒に2回起動すると
    # 同じディレクトリを共有し、固定名ファイル（comparison.html 等）が前回を上書きする
    # ＝「実行ごとに分ける」目的が静かに崩れる
    if args.run_id is not None:
        candidates = [args.run_id]
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        candidates = [stamp] + [f"{stamp}-{i}" for i in range(2, MAX_RUN_ID_SUFFIX + 1)]

    for run_id in candidates:
        out_dir = pr_dir / run_id
        try:
            out_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue
        except OSError as e:
            return emit({"error": f"出力ディレクトリを作成できません: {out_dir} ({e})"}, 1)
    else:
        if args.run_id is not None:
            return emit({"error": f"指定された run-id のディレクトリが既にあります: {pr_dir / args.run_id}",
                         "hint": "前回の実行結果を上書きしないため、別の run-id を指定してください"}, 1)
        return emit({"error": f"空きの run-id を見つけられません: {pr_dir}"}, 1)

    return emit({
        "out_dir": str(out_dir.resolve()),
        "run_id": run_id,
        "pr_dir": str(pr_dir.resolve()),
        "latest": link_latest(pr_dir, run_id),
    }, 0)


if __name__ == "__main__":
    sys.exit(main())
