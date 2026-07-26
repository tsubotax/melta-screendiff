#!/usr/bin/env python3
"""撮影前に「base が PR の HEAD の祖先か」を確認する（OPEN PR 専用）。

読み取り専用。副作用なし（git の読み取りコマンドしか実行しない）。

⚠️ **なぜ必要か**: OPEN PR の Before は「撮影時点の base ブランチ先端」から撮る。
base が PR の分岐後に進んでいると、Before には **PR と無関係な他PRのマージ結果** が
入る。比較HTMLには PR が加えていない差分が混ざり、レビュアーは「このPRの変更」として
それを読む。撮影後にOIDを記録しても比較そのものの正しさは回復しない（記録は監査であって
訂正ではない）。よって撮影前に落とす。

MERGED PR は対象外。マージコミットの親から Before を取る経路は比較の基準が既に固定
されており、この検査の前提（base が動きうる）が成り立たない。

3値を厳密に区別するのがこのスクリプトの仕事:
    ok         … base は HEAD の祖先（＝ PR は最新の base の上にある）
    base_ahead … base が先行している。撮影に進んではいけない
    error      … OIDが解決できない・git が異常終了した等（"祖先ではない" と混同しない）

`git merge-base --is-ancestor` は「祖先でない」も「オブジェクトが無い」も非0で返す
（1 と 128）。シェルの `|| echo NG` で受けると両者が同じ結論に潰れ、fetch 漏れを
「base が先行しています」と誤報告する。ここで分けておく。

使い方:
    python3 preflight_base.py --repo-root "$REPO_ROOT" \\
        --base-oid "$BASE_OID" --head-oid "$HEAD_OID" [--base-ref main] [--head-ref feat/x]

出力（stdout, JSON）: status / base_oid / head_oid（解決後のフルOID）/ message。
exit 0 は status == "ok" のときだけ。
"""
import argparse
import json
import subprocess
import sys


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
    parser.add_argument("--base-ref", default=None, help="案内メッセージ用のbaseブランチ名（任意）")
    parser.add_argument("--head-ref", default=None, help="案内メッセージ用のPRブランチ名（任意）")
    args = parser.parse_args()

    try:
        base_oid, base_err = resolve_commit(args.repo_root, "base", args.base_oid)
        head_oid, head_err = resolve_commit(args.repo_root, "head", args.head_oid)
    except OSError as e:
        # git が無い / repo-root が存在しない。traceback にせず構造化して返す
        return emit({"status": "error", "error": f"git を実行できません: {e}"}, 1)

    problems = [m for m in (base_err, head_err) if m]
    if problems:
        return emit({"status": "error", "error": "OIDを解決できません", "details": problems}, 1)

    code, _, err = git(args.repo_root, "merge-base", "--is-ancestor", base_oid, head_oid)
    result = {"base_oid": base_oid, "head_oid": head_oid}
    if args.base_ref:
        result["base_ref"] = args.base_ref
    if args.head_ref:
        result["head_ref"] = args.head_ref

    if code == 0:
        result["status"] = "ok"
        result["message"] = "base は PR の HEAD の祖先です（PR以外の差分は Before に混入しません）"
        return emit(result, 0)
    if code == 1:
        base_label = args.base_ref or "base"
        result["status"] = "base_ahead"
        result["message"] = (
            f"base（{base_label}）が PR の分岐後に進んでいます。このまま撮影すると "
            "Before に PR と無関係な差分が混入し、比較結果が誤ります")
        result["hint"] = (
            f"PRブランチに base を取り込んでから再実行してください"
            f"（例: PRブランチで `git merge origin/{base_label}` または "
            f"`git rebase origin/{base_label}` して push）")
        return emit(result, 1)

    result["status"] = "error"
    result["error"] = f"git merge-base が異常終了しました（exit {code}）"
    if err:
        result["details"] = [err]
    return emit(result, 1)


if __name__ == "__main__":
    sys.exit(main())
