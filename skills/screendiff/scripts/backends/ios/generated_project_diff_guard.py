#!/usr/bin/env python3
"""生成プロジェクトファイル（xcodegen等が再生成する project.pbxproj）の差分が、
自動破棄して安全なノイズ（バージョン文字列・グループ参照順序等）かどうかを判定する。

読み取り専用。判定のみ行い、実際の`git checkout --`実行はしない
（呼び出し元のSKILL.mdが判定結果を見て実行するかどうかを決める）。

使い方:
    git diff -- <生成プロジェクトファイル> | python3 generated_project_diff_guard.py
    オプション: --safe-keys <regex>（SAFE判定するキー名の正規表現を上書き）

出力: {"verdict": "CLEAN"|"SAFE"|"ATTENTION", "changed_lines": [...]}
"""
import argparse
import json
import re
import sys

# xcodegenの実行環境・バージョン差で揺れることが確認されている、実害のないキー。
# 実例: compatibilityVersion 追加 / productRefGroup 削除の1行ずつの差分。
DEFAULT_SAFE_KEYS = (
    r"compatibilityVersion|productRefGroup|LastUpgradeCheck|"
    r"LastSwiftUpdateCheck|BuildIndependentTargetsInParallel|"
    r"preferredProjectObjectVersion"
)
MAX_SAFE_LINES = 6


def classify(diff_text: str, safe_keys: re.Pattern):
    changed = [
        line
        for line in diff_text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    if not changed:
        return "CLEAN", changed
    if len(changed) <= MAX_SAFE_LINES and all(safe_keys.match(line) for line in changed):
        return "SAFE", changed
    return "ATTENTION", changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--safe-keys", default=DEFAULT_SAFE_KEYS,
                        help="SAFE判定するキー名の正規表現（| 区切り）")
    args = parser.parse_args()

    safe_keys = re.compile(rf"^[+-]\s*(?:{args.safe_keys})\b")
    diff_text = sys.stdin.read()
    verdict, changed_lines = classify(diff_text, safe_keys)
    print(json.dumps({"verdict": verdict, "changed_lines": changed_lines}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
