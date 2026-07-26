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
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

DEFAULTS = {
    "backend": "web",
    # Phase 1 の適用範囲判定に使う。1つでもマッチする変更ファイルがあれば対象PR
    "target_file_patterns": [],
    # 撮影・manifest・HTMLの出力先（repo-root からの相対）。リポジトリ外に逃がしたい
    # 場合は絶対パスも可
    "output_dir": "output/screendiff",
    # 任意: PRの変更ファイル群を引数に取る lint コマンド（変更ファイル限定で実行される）
    "lint_command": None,
    # 任意: Cleanup で元refへの復帰 + stash pop が成功した「後に」repo-root で1回実行する
    # 後片付けコマンド。gitignore されたビルド成果物は checkout では消えないため、最後に
    # Before 側でビルドした成果物が残置し、次の通常作業が stale な成果物を掴む
    # （例: "npm run clean --if-present"）。パスを限定しない広域削除は書かないこと
    # （stash pop で戻した untracked ファイルまで巻き込む）
    "cleanup_command": None,
    # 比較結果の配布方法。
    #   "artifact" … 比較HTMLを Artifact として発行し、PRへの書き込み（本文編集・コメント
    #                投稿）まで進む既定フロー
    #   "local"    … comparison.html のローカル生成で完了する。Artifact 発行も PR への
    #                書き込みも一切しない（配布は人間が手で行う）。外部への公開経路が
    #                使えない/使うべきでない案件向け
    "share_mode": "artifact",
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
        #     path は web backend では必須（"/" 始まり）。iOS では不要（id が deeplink に入る）
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


def validate_serve_url(serve_url) -> list[str]:
    """serve_url がスキーム + ホスト + 妥当なポートを持つURLか検証する。

    prefix一致だけでは "http://" が通る。一方 urlparse 自体も不正URL
    （"http://[" 等）で ValueError を投げるため、ここで整形エラーに変換する。
    ポートは urlparse では検証されず、後段の urlopen が InvalidURL で落ちるので
    ここで見る。
    """
    if not isinstance(serve_url, str):
        return [f"web.serve_url は文字列です: {type(serve_url).__name__}"]
    try:
        parsed = urlparse(serve_url)
        hostname = parsed.hostname
        port = parsed.port  # 非数値ポートはここで ValueError
    except ValueError as e:
        return [f"web.serve_url がURLとして解釈できません: {serve_url!r} ({e})"]
    if parsed.scheme not in ("http", "https"):
        return [f'web.serve_url のスキームは "http" | "https" です: {serve_url!r}']
    if not hostname:
        return [f"web.serve_url にホストがありません: {serve_url!r}"]
    if port is not None and not (1 <= port <= 65535):
        return [f"web.serve_url のポートが範囲外です: {serve_url!r}"]
    return []


def validate(config: dict) -> list[str]:
    """後段の KeyError / 不定動作を防ぐ最小限の検証。エラーメッセージのリストを返す。

    ⚠️ どんな入力でも例外を投げず、必ずエラー文字列のリストを返すこと。ここで
    traceback を出すと、設定を書き間違えただけの利用者に「プラグインが壊れている」
    と誤認させる（設定の手書きが必要な現設計では最も踏まれやすい経路）。
    """
    if not isinstance(config, dict):
        return [f"設定はオブジェクトです: {type(config).__name__}"]
    errors = []
    backend = config.get("backend")
    if backend not in ("web", "ios"):
        errors.append(f'backend は "web" | "ios" のいずれかです: {backend!r}')
    # セクションが dict でないと以降の .get() が AttributeError になる。壊れた
    # セクションの内部検証だけを飛ばし、独立した項目の検証は続ける（1回の実行で
    # 直せるエラーをまとめて出す。早期returnにすると修正→再実行の往復が増える）
    sections = {}
    for name in ("web", "ios", "screens"):
        value = config.get(name)
        if isinstance(value, dict):
            sections[name] = value
        else:
            errors.append(f"{name} はオブジェクトです: {type(value).__name__}")
            sections[name] = {}
    share_mode = config.get("share_mode")
    if share_mode not in ("artifact", "local"):
        errors.append(f'share_mode は "artifact" | "local" のいずれかです: {share_mode!r}')
    # SKILL.md 側でシェルコマンドとして実行されるため、非文字列・空文字を通さない。
    # 空文字を許すと「設定したつもりで何も走らない」状態が黙って成立する
    for key in ("lint_command", "cleanup_command"):
        value = config.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{key} は文字列です: {type(value).__name__}")
        elif not value.strip():
            errors.append(f"{key} が空文字です（実行しないなら null にしてください）")
    patterns = config.get("target_file_patterns")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        errors.append("target_file_patterns は文字列の配列です")
    elif not patterns:
        errors.append("target_file_patterns が空です（適用範囲判定ができず全PRが対象外になります）")
    else:
        # 不正な正規表現は Phase 1 の適用範囲判定まで露見しないため、ここで落とす
        for i, pattern in enumerate(patterns):
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f"target_file_patterns[{i}] の正規表現が不正です: {pattern!r} ({e})")
    screens = sections["screens"]
    route_map = screens.get("route_map", [])
    if not isinstance(route_map, list):
        errors.append("screens.route_map は配列です")
    else:
        for i, entry in enumerate(route_map):
            if not isinstance(entry, dict) or not entry.get("file_pattern") or not entry.get("id"):
                errors.append(f"screens.route_map[{i}] に file_pattern と id が必要です")
                continue
            # 非文字列は re.compile で TypeError（re.error では捕まらない）になるため先に弾く
            if not isinstance(entry["file_pattern"], str) or not isinstance(entry["id"], str):
                errors.append(f"screens.route_map[{i}] の file_pattern と id は文字列です")
                continue
            try:
                re.compile(entry["file_pattern"])
            except re.error as e:
                errors.append(
                    f"screens.route_map[{i}].file_pattern の正規表現が不正です: {entry['file_pattern']!r} ({e})")
            if backend == "web":
                # ⚠️ path 未指定を許すと resolve_screens.py が空文字を返し、撮影URLが
                # serve_url そのもの（＝トップページ）になる。エラーも出ないまま
                # 「変更された画面」としてトップを撮り、Before/Afterが一致して
                # 「差分なし」と誤結論する。silent事故の温床なので確定前に落とす
                path = entry.get("path")
                if not isinstance(path, str) or not path:
                    errors.append(
                        f'screens.route_map[{i}] (id="{entry["id"]}") に path が必要です'
                        "（web backend。省略するとトップページをその画面として撮影してしまう）")
                elif not path.startswith("/"):
                    errors.append(
                        f'screens.route_map[{i}] (id="{entry["id"]}") の path は "/" 始まりです: {path!r}')
    resolver = screens.get("resolver_command")
    if resolver is not None and not isinstance(resolver, str):
        errors.append(f"screens.resolver_command は文字列です: {type(resolver).__name__}")
    if not route_map and not resolver:
        errors.append("screens.route_map か screens.resolver_command のどちらかが必要です")
    if backend == "web":
        web = sections["web"]
        if not web.get("serve_command"):
            errors.append("web.serve_command が必要です")
        elif not isinstance(web["serve_command"], str):
            errors.append(f"web.serve_command は文字列です: {type(web['serve_command']).__name__}")
        errors.extend(validate_serve_url(web.get("serve_url")))
    if backend == "ios":
        ios = sections["ios"]
        for key in ("build_command", "app_path", "bundle_id", "deeplink", "simulator_device"):
            if not ios.get(key):
                errors.append(f"ios.{key} が必要です")
            elif not isinstance(ios[key], str):
                # SKILL.md 側でシェルコマンドに埋め込まれるため、非文字列は通さない
                errors.append(f"ios.{key} は文字列です: {type(ios[key]).__name__}")
    return errors


def load_from(path: Path) -> int:
    # 読み込み自体の失敗も traceback にしない（パスがディレクトリ・権限なし・
    # 不正なUTF-8バイト列など。exists() はディレクトリでも True を返す）
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        print(json.dumps({"error": f"設定ファイルがUTF-8として読めません: {path} ({e})"},
                         ensure_ascii=False))
        return 1
    except OSError as e:
        print(json.dumps({"error": f"設定ファイルを読み込めません: {path} ({e})"}, ensure_ascii=False))
        return 1
    try:
        # JSONDecodeError だけでなく ValueError 全般を捕まえる（巨大な数値リテラルは
        # int 変換上限に当たり JSONDecodeError ではない ValueError になる）
        user_config = json.loads(raw)
    except ValueError as e:
        print(json.dumps({"error": f"設定ファイルのJSONが不正です: {path} ({e})"}, ensure_ascii=False))
        return 1
    if not isinstance(user_config, dict):
        print(json.dumps({"error": f"設定ファイルの最上位はJSONオブジェクトです: {path}"}, ensure_ascii=False))
        return 1
    merged = deep_merge(DEFAULTS, user_config)
    errors = validate(merged)
    if errors:
        print(json.dumps({"error": "設定が不正です", "config_source": str(path), "details": errors},
                         ensure_ascii=False, indent=2))
        return 1
    # 撮影URLは serve_url + path の連結で組み立てるため、末尾スラッシュを正規化して
    # "http://host//about" のような二重スラッシュを防ぐ（path 側は "/" 始まりを強制済み）
    # （ios backend では serve_url は未検証なので、文字列のときだけ触る）
    if isinstance(merged.get("web", {}).get("serve_url"), str):
        merged["web"]["serve_url"] = merged["web"]["serve_url"].rstrip("/")
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
