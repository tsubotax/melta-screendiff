#!/usr/bin/env python3
"""PRの変更ファイルから対象画面を解決する（config の screens.route_map ベース）。

読み取り専用。副作用なし。

route_map の file_pattern（repo-root 相対パスへの正規表現、re.search）に最初に
マッチしたエントリの画面として解決する。1ファイルが複数画面に対応する場合は
route_map に同じ file_pattern で複数エントリを書く（全マッチを採用する）。

target_file_patterns にはマッチするが route_map で解決できないファイルは
unresolved として報告する（呼び出し元は推測せずユーザーに確認する）。

より複雑な逆引き（コード上のレジストリのパース等）が必要なリポジトリは、
config の screens.resolver_command に同じ出力契約のコマンドを指定して
本スクリプトを置き換える（docs/contracts.md 参照）。

使い方:
    python3 resolve_screens.py --config-json <resolved-config.json> <変更ファイル...>

出力: JSON（stdout）
    {"resolved": [{"screen_id", "title", "path", "source_file"}], "unresolved": [{"source_file", "reason"}]}
"""
import argparse
import json
import re
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("changed_files", nargs="+", help="repo-root からの相対パス")
    parser.add_argument("--config-json", required=True, help="load_config.py の出力を保存したファイル")
    args = parser.parse_args()

    config = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    route_map = config.get("screens", {}).get("route_map", [])
    if not route_map:
        print(json.dumps({
            "resolved": [],
            "unresolved": [{"source_file": f, "reason": "screens.route_map が設定されていません"}
                           for f in args.changed_files],
        }, ensure_ascii=False, indent=2))
        return 0

    compiled = []
    for entry in route_map:
        try:
            compiled.append((re.compile(entry["file_pattern"]), entry))
        except re.error as e:
            print(json.dumps({"error": f"route_map の正規表現が不正です: {entry.get('file_pattern')} ({e})"},
                             ensure_ascii=False))
            return 1

    # 同一idに異なるpath/titleを割り当てた設定矛盾は入力順で黙って採用せずエラーにする
    by_id = {}
    for entry in route_map:
        prev = by_id.setdefault(entry["id"], entry)
        if (prev.get("path", ""), prev.get("title", "")) != (entry.get("path", ""), entry.get("title", "")):
            print(json.dumps({"error": f'route_map の id "{entry["id"]}" に異なる path/title が定義されています'},
                             ensure_ascii=False))
            return 1

    resolved = []
    unresolved = []
    seen = set()
    for rel_path in args.changed_files:
        hits = [entry for pattern, entry in compiled if pattern.search(rel_path)]
        if not hits:
            unresolved.append({"source_file": rel_path, "reason": "route_map にマッチするエントリがありません"})
            continue
        for entry in hits:
            key = entry["id"]
            item = {
                "screen_id": entry["id"],
                "title": entry.get("title", entry["id"]),
                "path": entry.get("path", ""),
                "source_file": rel_path,
            }
            if key in seen:
                continue  # 同一画面が複数ファイルから解決されても1回だけ
            seen.add(key)
            resolved.append(item)

    print(json.dumps({"resolved": resolved, "unresolved": unresolved}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
