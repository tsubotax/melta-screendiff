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
    # OPEN PR で Before をどのコミットから撮るか。
    #   "branch_tip" … base ブランチの先端（既定・従来動作）。「今の base にこのPRを載せると
    #                  どう見えるか」を見る。base が PR の分岐後に進んでいると撮影前に中断する
    #   "merge_base" … PR の分岐点（merge-base）。「このPRだけで何が変わったか」を見る。
    #                  base が日常的に進むリポジトリ向け（中断せず警告して続行する）
    # MERGED PR には効かない（マージコミットの親が Before で、基準が既に固定されているため）
    "before_base": "branch_tip",
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
        #     範囲モードでは任意で ready_selector（表示完了条件の CSS selector）と
        #     representative（代表画面。変更箇所が写らない可能性を HTML に注記する）を持てる
        "route_map": [],
        # 複雑なリポジトリ向けの逃げ道。変更ファイルパスを引数に取り
        # {"resolved": [...], "unresolved": [...]} を stdout に出すコマンド（docs/contracts.md 参照）
        "resolver_command": None,
        # 同梱 resolver の選択。"expo_router" は expo-router のファイルパスから
        # 直接 / 代表 / 未対応を機械判定する（resolvers/expo_router.py、docs/contracts.md §2.1）
        "resolver": None,
        "expo_router": None,
    },
    # 範囲モード（screendiff_range.py）専用。PR を使わず 2 点（または作業ツリー）を隔離 snapshot から
    # ビルドして比較する。docs/contracts.md §1.4
    "range": {
        # snapshot に入れるパス（git ls-files / git archive の pathspec）
        "snapshot_paths": [],
        # 依存ファイル（package.json / lock / patches 等）。Before・After・共有元の 3 者で内容が
        # 一致しないと撮らない（node_modules を共有元へ symlink するため）
        "dependency_change_patterns": [],
        # 共有元の node_modules（repo-root 相対）。snapshot からここへ symlink する。null なら作らない
        "node_modules_link": None,
        # capture_browser.mjs が require する playwright（repo-root 相対）
        "playwright_module": "node_modules/playwright",
        # ignored だがビルドに要るファイルを固定 bytes から両 snapshot に生成する
        "fixtures": [],
        # build / serve に渡す環境変数（.env を読まない・API を固定するなど）
        "env": {},
        # snapshot ルートを cwd に実行。$SCREENDIFF_WEB_DIR（side 別の export 先）と $SCREENDIFF_SIDE が入る
        "build_command": None,
        "serve_command": None,
        # serve_command が起動時に URL を出力しない（固定ポート）場合に指定。起動前に応答があれば中断する
        "serve_url": None,
        "device_scale_factor": 1,
        "ready_timeout_ms": 10000,
        "build_timeout_sec": 600,
        # 1 画面の撮影プロセスの上限。超えたら run_status=failed（サーバは止める）
        "capture_timeout_sec": 180,
        # null なら runner が既定の「API データは固定していない」注記を出す
        "data_caveat": None,
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
    for name in ("web", "ios", "screens", "range"):
        value = config.get(name)
        if isinstance(value, dict):
            sections[name] = value
        else:
            errors.append(f"{name} はオブジェクトです: {type(value).__name__}")
            sections[name] = {}
    share_mode = config.get("share_mode")
    if share_mode not in ("artifact", "local"):
        errors.append(f'share_mode は "artifact" | "local" のいずれかです: {share_mode!r}')
    # タイポを既定へ黙って倒すと「PR固有差分を見ているつもりで base の進行分も見ている」
    # 状態になる。撮影対象が変わる設定なので確定前に落とす
    before_base = config.get("before_base")
    if before_base not in ("branch_tip", "merge_base"):
        errors.append(
            f'before_base は "branch_tip" | "merge_base" のいずれかです: {before_base!r}')
    # SKILL.md 側でシェルコマンドとして実行されるため、非文字列は通さない
    for key in ("lint_command", "cleanup_command"):
        value = config.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{key} は文字列です: {type(value).__name__}")
    # 空文字を許すと「設定したつもりで何も走らない」状態が黙って成立する。ただし
    # lint_command は本キー導入以前から空文字が素通りしていたため、ここで落とすと
    # 既存設定が Phase 0 で動かなくなる → 空文字は None へ正規化して扱いを明示する
    # （新規キーの cleanup_command には既存利用者がいないので厳格に落とす）
    cleanup_command = config.get("cleanup_command")
    if isinstance(cleanup_command, str) and not cleanup_command.strip():
        errors.append("cleanup_command が空文字です（実行しないなら null にしてください）")
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
            sel = entry.get("ready_selector")
            if sel is not None and (not isinstance(sel, str) or not sel.strip()):
                errors.append(f'screens.route_map[{i}] (id="{entry["id"]}") の ready_selector は非空の文字列です')
            if "representative" in entry and not isinstance(entry["representative"], bool):
                errors.append(f'screens.route_map[{i}] (id="{entry["id"]}") の representative は true | false です')
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
    builtin = screens.get("resolver")
    if builtin is not None and builtin != "expo_router":
        errors.append(f'screens.resolver は "expo_router" | null です: {builtin!r}')
    if builtin == "expo_router":
        er = screens.get("expo_router")
        if not isinstance(er, dict):
            errors.append("screens.expo_router はオブジェクトです（resolver が expo_router のとき必須）")
        else:
            if not isinstance(er.get("app_dir"), str) or not er["app_dir"].strip():
                errors.append("screens.expo_router.app_dir は非空の文字列です")
            if "route_prefix" in er and not isinstance(er["route_prefix"], str):
                errors.append("screens.expo_router.route_prefix は文字列です")
            fm = er.get("feature_map", [])
            if not isinstance(fm, list):
                errors.append("screens.expo_router.feature_map は配列です")
            else:
                for i, entry in enumerate(fm):
                    if (not isinstance(entry, dict) or not isinstance(entry.get("pattern"), str)
                            or not isinstance(entry.get("screens"), list) or not entry["screens"]
                            or not all(isinstance(s, str) and s.startswith("/") for s in entry["screens"])):
                        errors.append(f"screens.expo_router.feature_map[{i}] は {{pattern, screens[\"/...\"]}} です")
                        continue
                    try:
                        re.compile(entry["pattern"])
                    except re.error as e:
                        errors.append(f"screens.expo_router.feature_map[{i}].pattern の正規表現が不正です ({e})")
            rep = er.get("representative", [])
            if not isinstance(rep, list) or not all(isinstance(s, str) and s.startswith("/") for s in rep):
                errors.append('screens.expo_router.representative は "/" 始まりの文字列の配列です')
            for key in ("titles", "ready_selectors", "fixed_params"):
                if key in er and not isinstance(er[key], dict):
                    errors.append(f"screens.expo_router.{key} はオブジェクトです")
    if not route_map and not resolver and not builtin:
        errors.append("screens.route_map か screens.resolver_command のどちらかが必要です"
                      "（同梱 resolver を使うなら screens.resolver）")
    rng = sections["range"]
    range_configured = bool(rng.get("build_command") or rng.get("serve_command"))
    if range_configured:
        for key in ("build_command", "serve_command"):
            if not isinstance(rng.get(key), str) or not rng[key].strip():
                errors.append(f"range.{key} は非空の文字列です（範囲モードでは両方必要）")
        sp = rng.get("snapshot_paths")
        if not isinstance(sp, list) or not sp or not all(isinstance(p, str) and p for p in sp):
            errors.append("range.snapshot_paths は非空の文字列の配列です")
        dp = rng.get("dependency_change_patterns", [])
        if not isinstance(dp, list) or not all(isinstance(p, str) for p in dp):
            errors.append("range.dependency_change_patterns は文字列の配列です")
        else:
            for i, p in enumerate(dp):
                try:
                    re.compile(p)
                except re.error as e:
                    errors.append(f"range.dependency_change_patterns[{i}] の正規表現が不正です ({e})")
        if rng.get("node_modules_link") is not None and not isinstance(rng["node_modules_link"], str):
            errors.append("range.node_modules_link は文字列か null です")
        if rng.get("node_modules_link") and not dp:
            # symlink で依存を共有するのに一致条件が無いと、両側が別の依存でビルドされても気づけない
            errors.append("range.node_modules_link を使うなら range.dependency_change_patterns が必要です")
        fx = rng.get("fixtures", [])
        if not isinstance(fx, list) or not all(isinstance(f, dict) and isinstance(f.get("path"), str)
                                                and isinstance(f.get("base64"), str) for f in fx):
            errors.append("range.fixtures は {path, base64} の配列です")
        env = rng.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            errors.append("range.env は文字列→文字列のオブジェクトです")
        if rng.get("serve_url") is not None:
            errors.extend(f"range.{e[4:]}" if e.startswith("web.") else e for e in validate_serve_url(rng["serve_url"]))
        for key in ("ready_timeout_ms", "build_timeout_sec"):
            if not isinstance(rng.get(key), int) or rng[key] <= 0:
                errors.append(f"range.{key} は正の整数です")
        if not isinstance(rng.get("device_scale_factor"), (int, float)) or rng["device_scale_factor"] <= 0:
            errors.append("range.device_scale_factor は正の数です")
    if backend == "web":
        web = sections["web"]
        if not web.get("serve_command"):
            if not range_configured:
                errors.append("web.serve_command が必要です（範囲モードだけ使うなら range.serve_command）")
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
    # 空文字の lint_command は「未設定」として扱う（後方互換。SKILL.md 側の
    # 「設定されていれば実行する」判定に空文字を渡すと解釈が揺れる）
    if isinstance(merged.get("lint_command"), str) and not merged["lint_command"].strip():
        merged["lint_command"] = None
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
