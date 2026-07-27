#!/usr/bin/env python3
"""Before として撮るコミットを撮影前に確定する（OPEN PR 専用）。

読み取り専用。副作用なし（git の読み取りコマンドしか実行しない）。

⚠️ **なぜ必要か**: OPEN PR の Before をどこから撮るかで、比較に映るものが変わる。

    branch_tip（既定） … base ブランチの先端。「今の base に対してこのPRを載せると
                          どう見えるか」を見る。ただし base が PR の分岐後に進んでいると
                          Before に他PRのマージ結果が入り、**PRが加えていない差分が混ざる**
    merge_base         … PR の分岐点（merge-base）。「このPRだけで何が変わったか」を見る。
                          base が進んでいても比較は PR 固有の差分のまま保たれる

撮影後にOIDを記録しても、撮ったもの自体は直せない（記録は監査であって訂正ではない）。
どちらのコミットを撮るかは撮影前にここで確定させる。

モード別の挙動:

    branch_tip + base が祖先        → ok（before = base）
    branch_tip + base が先行        → base_ahead / **中断**（exit 1）
    merge_base + base が祖先        → ok（merge-base は base と一致）
    merge_base + base が先行        → base_ahead / **続行**（exit 0）。before = merge-base。
                                      比較は PR 固有差分として正しいので成功偽装にならない。
                                      warning を返すので比較HTMLとユーザーへ明示すること

MERGED PR には適用しない。マージコミットの親から Before を取る経路は比較の基準が既に
固定されており、「base が動きうる」という前提が成り立たない。

`git merge-base --is-ancestor` は「祖先でない」も「オブジェクトが無い」も非0で返す
（1 と 128）。シェルの `|| echo NG` で受けると両者が同じ結論に潰れ、fetch 漏れを
「base が先行しています」と誤報告する。ここで status を分けておく。

使い方:
    python3 preflight_base.py --repo-root "$REPO_ROOT" \\
        --base-oid "$BASE_OID" --head-oid "$HEAD_OID" \\
        [--mode branch_tip|merge_base] [--base-ref main] [--head-ref feat/x]

出力（stdout, JSON）: status / mode / before_oid / base_oid / head_oid / merge_base_oid /
blocking / message。**`before_oid` が「Before として checkout すべきコミット」**。
exit 0 は blocking が false のときだけ。
"""
import argparse
import json
import subprocess
import sys

MODES = ("branch_tip", "merge_base")


def emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def git(repo_root: str, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(("git", *args), cwd=repo_root, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def resolve_commit(repo_root: str, label: str, rev: str) -> tuple[str, str]:
    """rev をフルOIDに解決する。戻り値は (oid, エラーメッセージ)。片方だけが埋まる。"""
    code, out, err = git(repo_root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if code != 0 or not out:
        return "", (f"{label} のコミットが見つかりません: {rev!r}"
                    f"{'（' + err + '）' if err else ''}"
                    "。fetch 漏れ・OIDの誤りを確認してください")
    return out, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--base-oid", required=True, help="base ブランチ先端のOID（fetch 済みであること）")
    parser.add_argument("--head-oid", required=True, help="PRブランチ先端のOID")
    parser.add_argument("--mode", default="branch_tip", choices=MODES,
                        help="Before の基準（config.before_base と同じ値）")
    parser.add_argument("--base-ref", default=None, help="案内メッセージ用のbaseブランチ名（任意）")
    parser.add_argument("--head-ref", default=None, help="案内メッセージ用のPRブランチ名（任意）")
    args = parser.parse_args()

    try:
        base_oid, base_err = resolve_commit(args.repo_root, "base", args.base_oid)
        head_oid, head_err = resolve_commit(args.repo_root, "head", args.head_oid)
    except OSError as e:
        # git が無い / repo-root が存在しない。traceback にせず構造化して返す
        return emit({"status": "error", "mode": args.mode,
                     "error": f"git を実行できません: {e}", "blocking": True}, 1)

    problems = [m for m in (base_err, head_err) if m]
    if problems:
        return emit({"status": "error", "mode": args.mode, "error": "OIDを解決できません",
                     "details": problems, "blocking": True}, 1)

    result = {"mode": args.mode, "base_oid": base_oid, "head_oid": head_oid}
    if args.base_ref:
        result["base_ref"] = args.base_ref
    if args.head_ref:
        result["head_ref"] = args.head_ref
    base_label = args.base_ref or "base"

    code, _, err = git(args.repo_root, "merge-base", "--is-ancestor", base_oid, head_oid)
    if code not in (0, 1):
        result.update({"status": "error", "blocking": True,
                       "error": f"git merge-base が異常終了しました（exit {code}）"})
        if err:
            result["details"] = [err]
        return emit(result, 1)
    base_is_ancestor = code == 0

    # merge-base は「PRの分岐点」。base が祖先ならこれは base_oid と一致する
    mb_code, merge_base_oid, mb_err = git(args.repo_root, "merge-base", base_oid, head_oid)
    if mb_code != 0 or not merge_base_oid:
        # 共通祖先が無い（履歴が繋がっていない）。どちらのモードでも比較の基準を作れない
        result.update({"status": "error", "blocking": True,
                       "error": "base と HEAD に共通祖先がありません（履歴が繋がっていません）"})
        if mb_err:
            result["details"] = [mb_err]
        return emit(result, 1)
    result["merge_base_oid"] = merge_base_oid

    if base_is_ancestor:
        result.update({
            "status": "ok", "blocking": False,
            "before_oid": base_oid,   # merge_base モードでも merge-base == base なので同じ
            "message": "base は PR の HEAD の祖先です（PR以外の差分は Before に混入しません）",
        })
        return emit(result, 0)

    # ここから base 先行。モードで扱いが分かれる
    if args.mode == "merge_base":
        result.update({
            "status": "base_ahead", "blocking": False,
            "before_oid": merge_base_oid,
            "message": (f"base（{base_label}）は PR の分岐後に進んでいますが、"
                        "Before は merge-base（PRの分岐点）から撮るため比較は PR 固有の差分のままです"),
            "warning": ("Before は base の先端ではなく merge-base 基準です。"
                        "「今の base に載せたらどう見えるか」は分かりません"),
        })
        return emit(result, 0)

    result.update({
        "status": "base_ahead", "blocking": True,
        "before_oid": base_oid,   # 撮らないが、何を撮ろうとしたかは記録に残す
        "message": (f"base（{base_label}）が PR の分岐後に進んでいます。このまま撮影すると "
                    "Before に PR と無関係な差分が混入し、比較結果が誤ります"),
        "hint": (f"PRブランチに base を取り込んでから再実行してください"
                 f"（例: PRブランチで `git merge origin/{base_label}` または "
                 f"`git rebase origin/{base_label}` して push）。"
                 "base が頻繁に進むリポジトリでは before_base: \"merge_base\" を検討してください"),
    })
    return emit(result, 1)


if __name__ == "__main__":
    sys.exit(main())
