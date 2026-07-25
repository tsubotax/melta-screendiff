#!/usr/bin/env python3
"""screendiff の設定を解決して stdout に JSON で出す。

読み取り専用。副作用なし。

探索順（先に見つかったものを採用）:
    1. --config で明示されたパス
    2. <repo-root>/.claude/screendiff.json          … チームとして導入しているリポジトリ
    3. ~/.config/melta-screendiff/<repo名>.json     … 対象リポジトリに1ファイルもコミット
       できない場合（業務委託先リポ等）のユーザー側設定。<repo名> は repo-root の basename

どこにも無ければ exit 1 でエラー JSON を出す（呼び出し元の SKILL.md がセットアップ
手順を案内する）。

出力はデフォルト値とのマージ済み。`_config_source` キーに採用したパスが入る。

使い方:
    python3 load_config.py --repo-root "$REPO_ROOT" [--config <path>]
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULTS = {
    "backend": "web",
    # Phase 1 の適用範囲判定に使う。1つでもマッチする変更ファイルがあれば対象PR
    "target_file_patterns": [],
    # 撮影・manifest・HTMLの出力先（repo-root からの相対）。リポジトリ外に逃がしたい
    # 場合は絶対パスも可
    "output_dir": "output/screendiff",
    # 任意: PRの変更ファイル群を引数に取る lint コマンド（変更ファイル限定で実行される）
    "lint_command": None,
    "web": {
        # 任意: 各サイドの checkout 直後に1回実行（依存インストール等）
        "setup_command": None,
        # dev server の起動コマンド（バックグラウンド実行される）。ビルド済み静的サイト
        # なら "npx serve dist" 等でもよい
        "serve_command": None,
        # 疎通確認と撮影のベースURL
        "serve_url": "http://localhost:3000",
        "ready_timeout_sec": 60,
        "viewport": "1280x800",
        "full_page": True,
        # 撮影前の描画待ち（ms）
        "settle_ms": 2000,
    },
    "ios": {
        "build_command": None,
        "app_path": None,
        "bundle_id": None,
        # {id} が screen_id に置換される
        "deeplink": None,
        "simulator_device": None,
        # 生成プロジェクトファイル（xcodegen等）の相対パス。無ければ diff guard をスキップ
        "generated_project_file": None,
    },
    "screens": {
        # 変更ファイル → 画面の宣言的マッピング。file_pattern は repo-root 相対パスへの正規表現
        # 例: {"file_pattern": "^src/pages/home/", "id": "home", "path": "/", "title": "Home"}
        #     path は web backend でのURLパス。iOS では不要（id が deeplink に入る）
        "route_map": [],
        # 複雑なリポジトリ向けの逃げ道。変更ファイルパスを引数に取り
        # {"resolved": [...], "unresolved": [...]} を stdout に出すコマンド（docs/contracts.md 参照）
        "resolver_command": None,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def candidate_paths(repo_root: Path) -> list[Path]:
    return [
        repo_root / ".claude" / "screendiff.json",
        Path.home() / ".config" / "melta-screendiff" / f"{repo_root.name}.json",
    ]


def validate(config: dict) -> list[str]:
    """後段の KeyError / 不定動作を防ぐ最小限の検証。エラーメッセージのリストを返す。"""
    errors = []
    backend = config.get("backend")
    if backend not in ("web", "ios"):
        errors.append(f'backend は "web" | "ios" のいずれかです: {backend!r}')
    patterns = config.get("target_file_patterns")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        errors.append("target_file_patterns は文字列の配列です")
    elif not patterns:
        errors.append("target_file_patterns が空です（適用範囲判定ができず全PRが対象外になります）")
    screens = config.get("screens", {})
    route_map = screens.get("route_map", [])
    if not isinstance(route_map, list):
        errors.append("screens.route_map は配列です")
    else:
        for i, entry in enumerate(route_map):
            if not isinstance(entry, dict) or not entry.get("file_pattern") or not entry.get("id"):
                errors.append(f"screens.route_map[{i}] に file_pattern と id が必要です")
    if not route_map and not screens.get("resolver_command"):
        errors.append("screens.route_map か screens.resolver_command のどちらかが必要です")
    if backend == "web":
        if not config.get("web", {}).get("serve_command"):
            errors.append("web.serve_command が必要です")
    if backend == "ios":
        ios = config.get("ios", {})
        for key in ("build_command", "app_path", "bundle_id", "deeplink", "simulator_device"):
            if not ios.get(key):
                errors.append(f"ios.{key} が必要です")
    return errors


def load_from(path: Path) -> int:
    try:
        user_config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"設定ファイルのJSONが不正です: {path} ({e})"}, ensure_ascii=False))
        return 1
    merged = deep_merge(DEFAULTS, user_config)
    errors = validate(merged)
    if errors:
        print(json.dumps({"error": "設定が不正です", "config_source": str(path), "details": errors},
                         ensure_ascii=False, indent=2))
        return 1
    merged["_config_source"] = str(path)
    print(json.dumps(merged, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--config", default=None,
                        help="設定ファイルの明示指定（探索をスキップ。存在しなければエラー、フォールバックしない）")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if args.config:
        explicit = Path(args.config)
        if not explicit.exists():
            # タイプミスを黙ってフォールバックで隠さない
            print(json.dumps({"error": f"--config で指定されたファイルがありません: {explicit}"}, ensure_ascii=False))
            return 1
        return load_from(explicit)

    for path in candidate_paths(repo_root):
        if path.exists():
            return load_from(path)

    print(json.dumps({
        "error": "設定ファイルが見つかりません",
        "searched": [str(p) for p in candidate_paths(repo_root)],
        "hint": "リポジトリに .claude/screendiff.json を置くか、リポジトリを汚せない場合は "
                f"~/.config/melta-screendiff/{repo_root.name}.json を作成してください（examples/ 参照）",
    }, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
