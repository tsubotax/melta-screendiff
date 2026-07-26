#!/usr/bin/env python3
"""共有モードを解決し、許可/禁止アクションを確定する（Phase 7 の入口）。

読み取り専用。副作用なし。

⚠️ **なぜスクリプトなのか**: `local` は「PRに書き込まないこと」そのものが目的の機能で、
外部に公開できない案件のために存在する。判断を SKILL.md の散文だけに置くと、
分岐の読み違い1回で `gh pr comment` が走り、**取り消せない書き込みが起きる**。
撮影対象がズレる silent 事故と違い、こちらは外向きで不可逆。

決定順（先に決まったものを採用）:
    1. --share の明示指定（スキル引数 / ユーザーの明示指示）
    2. config の share_mode
    3. "artifact"

`--no-artifact`（Artifact 機能が使えない環境）は artifact 指定を local に落とすのではなく
**"artifact_unavailable" として PR書き込みも止める**。Artifact URL を作れないのに PR 本文へ
追記する経路（＝リンク先の無い案内をPRに残す）を塞ぐため。

使い方:
    python3 share_plan.py --config-json "$CONFIG_JSON" [--share local|artifact]
                          [--author] [--no-artifact]

出力（stdout, JSON）: share_mode / source / allow_* / actions / forbidden。exit 0。
config が読めない等は exit 1（そのときは共有フェーズに進まない）。
"""
import argparse
import json
import sys
from pathlib import Path

VALID_MODES = ("artifact", "local")

# PRへの書き込み系。local / artifact_unavailable では必ず空になること（テストで固定）
PR_WRITE_ACTIONS = ("gh pr comment", "gh pr edit")


def emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def build_plan(config_mode, override, author: bool, artifact_available: bool) -> dict:
    if override is not None:
        mode, source = override, "実行時の明示指定"
    elif config_mode is not None:
        mode, source = config_mode, "config.share_mode"
    else:
        mode, source = "artifact", "既定値"

    plan = {"share_mode": mode, "source": source, "author_mode": author}

    if mode == "artifact" and not artifact_available:
        # Artifact を出せないのに PR へ「比較はArtifactにあります」と書く経路を塞ぐ
        plan["effective"] = "artifact_unavailable"
        plan["allow_artifact"] = False
        plan["allow_pr_write"] = False
        plan["actions"] = ["comparison.html の絶対パスを提示する"]
        plan["note"] = ("Artifact を発行できない環境のため、PRへの書き込みも行わない"
                        "（リンク先の無い案内をPRに残さない）")
    elif mode == "local":
        plan["effective"] = "local"
        plan["allow_artifact"] = False
        plan["allow_pr_write"] = False
        plan["actions"] = ["comparison.html の絶対パスを提示する",
                           "変更画面の要約をチャットに提示する"]
        plan["note"] = "配布は人間が手で行う（PRへの書き込みはユーザーの判断）"
    else:
        plan["effective"] = "artifact"
        plan["allow_artifact"] = True
        plan["allow_pr_write"] = True
        if author:
            plan["actions"] = ["comparison.html を Artifact として提示する",
                               "Artifact 共有トグルの ON をユーザーに促す（AI側から操作できない）",
                               "共有ON確認後、Artifact URL を gh pr edit でPR本文に追記する"]
            plan["note"] = "レビューコメント下書き・投稿はスキップする（authorモード）"
        else:
            plan["actions"] = ["comparison.html を Artifact として提示する",
                               "PRコメント下書きを全文提示する",
                               "ユーザーの明示承認後のみ gh pr comment で投稿する"]
            plan["note"] = "投稿前のHITLゲートは省略不可"

    plan["forbidden"] = [] if plan["allow_pr_write"] else list(PR_WRITE_ACTIONS)
    if not plan["allow_artifact"]:
        plan["forbidden"] = ["Artifact の発行", *plan["forbidden"]]
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-json", required=True, help="load_config.py の出力を保存したファイル")
    parser.add_argument("--share", default=None, choices=VALID_MODES,
                        help="config より優先する明示指定")
    parser.add_argument("--author", action="store_true", help="authorモード（PR作者が自分のPRに添付）")
    parser.add_argument("--no-artifact", action="store_true",
                        help="Artifact 機能が使えない環境")
    args = parser.parse_args()

    try:
        config = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return emit({"error": f"config を読めません: {e}"}, 1)
    if not isinstance(config, dict):
        return emit({"error": f"config はJSONオブジェクトです: {type(config).__name__}"}, 1)

    config_mode = config.get("share_mode")
    if config_mode is not None and config_mode not in VALID_MODES:
        # load_config を通っていれば起きないが、直接呼ばれた経路で既定へ黙って
        # フォールバックさせない（書き込ませたくないPRに書き込む事故になる）
        return emit({"error": f'config.share_mode が不正です: {config_mode!r}',
                     "hint": f"許容値: {' | '.join(VALID_MODES)}"}, 1)

    return emit(build_plan(config_mode, args.share, args.author, not args.no_artifact), 0)


if __name__ == "__main__":
    sys.exit(main())
