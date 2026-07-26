#!/usr/bin/env python3
"""resolver の出力（resolve_screens.py / resolver_command）を撮影前に検証する。

読み取り専用。副作用なし。

⚠️ **なぜ必要か**: load_config.py の path 必須化は `screens.route_map` にしか
効かない。`resolver_command`（リポジトリ側が実装する adapter）の出力は誰も検証
しないため、自作 resolver が path を空文字や undefined（JSON.stringify でキーごと
消える）で返すと、撮影URLが serve_url そのもの＝トップページになる。200 が返り
title も正常に取れ、Before/After が一致して「差分なし」と誤結論する——route_map で
潰したのと同型の silent 事故が、adapter 経路で再開通する。

使い方:
    python3 validate_resolved.py --config-json <resolved-config.json> <resolver出力.json>
    <resolver> | python3 validate_resolved.py --config-json <config.json> -

出力: 検証を通れば入力JSONをそのまま stdout に出す（パイプで繋げる）。
      問題があれば {"error": ..., "details": [...]} を出して exit 1。
"""
import argparse
import json
import sys
from pathlib import Path


def validate(payload, backend: str) -> list[str]:
    """resolver 出力の構造を検証する。どんな入力でも例外を投げずエラー列を返す。"""
    errors = []
    if not isinstance(payload, dict):
        return [f"resolver の出力はJSONオブジェクトです: {type(payload).__name__}"]
    if "error" in payload:
        return [f"resolver がエラーを返しました: {payload['error']}"]

    for key in ("resolved", "unresolved"):
        if key in payload and not isinstance(payload[key], list):
            errors.append(f"{key} は配列です: {type(payload[key]).__name__}")
    if errors:
        return errors

    resolved = payload.get("resolved", [])
    seen_ids = {}
    for i, item in enumerate(resolved):
        if not isinstance(item, dict):
            errors.append(f"resolved[{i}] はオブジェクトです: {type(item).__name__}")
            continue
        screen_id = item.get("screen_id")
        if not isinstance(screen_id, str) or not screen_id:
            errors.append(f"resolved[{i}] に screen_id（非空の文字列）が必要です")
            continue
        title = item.get("title")
        if title is not None and not isinstance(title, str):
            errors.append(f'resolved[{i}] (screen_id="{screen_id}") の title は文字列です')
        if backend == "web":
            # ここが本丸。空 path は serve_url そのもの（トップページ）を撮ることになる
            path = item.get("path")
            if not isinstance(path, str) or not path:
                errors.append(
                    f'resolved[{i}] (screen_id="{screen_id}") に path が必要です'
                    "（web backend。省略するとトップページをその画面として撮影してしまう）")
            elif not path.startswith("/"):
                errors.append(
                    f'resolved[{i}] (screen_id="{screen_id}") の path は "/" 始まりです: {path!r}')
            elif screen_id in seen_ids and seen_ids[screen_id] != path:
                # 同じ画面IDに違うURLを与えるのは設定/実装の矛盾。入力順で黙って採用しない
                errors.append(
                    f'resolved に同じ screen_id "{screen_id}" が異なる path で複数あります: '
                    f'{seen_ids[screen_id]!r} と {path!r}')
            else:
                seen_ids[screen_id] = path

    unresolved = payload.get("unresolved", [])
    for i, item in enumerate(unresolved):
        if not isinstance(item, dict) or not item.get("source_file"):
            errors.append(f"unresolved[{i}] に source_file が必要です")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resolved_json", help="resolver 出力のJSONファイル（- で標準入力）")
    parser.add_argument("--config-json", required=True, help="load_config.py の出力を保存したファイル")
    args = parser.parse_args()

    try:
        config = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
        backend = config.get("backend", "web")
    except (OSError, ValueError) as e:
        print(json.dumps({"error": f"config を読めません: {e}"}, ensure_ascii=False))
        return 1

    try:
        raw = sys.stdin.read() if args.resolved_json == "-" else \
            Path(args.resolved_json).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(json.dumps({"error": f"resolver 出力を読めません: {e}"}, ensure_ascii=False))
        return 1

    try:
        payload = json.loads(raw)
    except ValueError as e:
        print(json.dumps({"error": f"resolver の出力がJSONとして不正です: {e}",
                          "raw_head": raw[:200]}, ensure_ascii=False))
        return 1

    errors = validate(payload, backend)
    if errors:
        print(json.dumps({"error": "resolver の出力が契約を満たしていません", "details": errors},
                         ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
