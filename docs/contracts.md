# melta-screendiff — JSON契約

スキルの各層はJSON契約で接続される。**「揺れてよい判断」はSKILL.md、「揺れてはいけない処理」はスクリプト**が原則。スクリプトは読み取り専用 or 冪等で、実行判断（checkout・破棄・投稿）は必ずSKILL.md側に残す。

```
SKILL.md（オーケストレーション層 / AIが読む手順書）
  └── scripts/（決定論的処理層 / Python標準ライブラリのみ）
        ├── load_config.py       設定解決（2段フォールバック）
        ├── resolve_screens.py   変更ファイル → 画面解決（route_map）
        ├── render_comparison.py manifest.json → base64埋め込み比較HTML
        └── backends/
            ├── web/capture.py   Playwright/Chrome フルページ撮影
            └── ios/…            simctl + sim-use ページング撮影・AXチェック
```

画像はAIのコンテキストを通さない。render_comparison.py がディスク上のPNGを直接base64エンコードしてHTMLに埋める（何十枚撮ってもトークンを消費しない）。

## 1. config（load_config.py の出力）

探索順: `--config` 明示 → `<repo>/.claude/screendiff.json` → `~/.config/melta-screendiff/<repo名>.json`。
後者は**対象リポジトリに1ファイルもコミットできない場合**（業務委託先リポ等）のユーザー側設定。

```jsonc
{
  "backend": "web",                    // "web" | "ios"
  "target_file_patterns": ["^src/(pages|components)/"],  // Phase 1 の適用範囲判定（re.search）
  "output_dir": "output/screendiff",   // repo-root相対 or 絶対パス
  "lint_command": null,                // 任意。変更ファイル群を引数に取るlint
  "web": {
    "setup_command": null,             // 任意。checkout直後に1回（npm ci 等）
    "serve_command": "npm run dev",    // バックグラウンド起動される
    "serve_url": "http://localhost:3000",
    "ready_timeout_sec": 60,
    "viewport": "1280x800",
    "full_page": true,
    "settle_ms": 2000
  },
  "ios": {
    "build_command": "cd app && make build",
    "app_path": "app/build/Debug-iphonesimulator/App.app",  // repo-root相対
    "bundle_id": "com.example.app",
    "deeplink": "myapp://screen/{id}",
    "simulator_device": "iPhone 17 Pro",
    "generated_project_file": null     // xcodegen等の生成物パス。無ければguardスキップ
  },
  "screens": {
    "route_map": [
      // web backend では path が必須（"/" 始まり）。iOSでは不要（id が deeplink の {id} に入る）
      {"file_pattern": "^src/pages/home/", "id": "home", "path": "/", "title": "ホーム"}
    ],
    "resolver_command": null           // 下記2の契約を満たす外部コマンド（route_mapより優先）
  }
}
```

load_config.py は後段の silent 事故を防ぐため、**確定前に**以下を検証して落とす:

- `target_file_patterns` / `route_map[].file_pattern` が正規表現としてコンパイルできること
- web backend の `route_map[]` に**非空の `path`（"/" 始まり）**があること
  — ⚠️ 省略を許すと resolve_screens.py が空文字を返し、撮影URLが `serve_url` そのもの（＝トップページ）になる。エラーも出ないまま「変更された画面」としてトップを撮り、Before/Afterが一致して「差分なし」と誤結論する
- `web.serve_url` が `http://` / `https://` で始まること（末尾スラッシュは出力時に除去され、`serve_url + path` の連結が二重スラッシュにならない）
- 設定ファイルの最上位がJSONオブジェクトであること

## 2. resolver 契約（resolve_screens.py / resolver_command）

入力: 変更ファイルの repo-root 相対パス群（引数）。出力（stdout, JSON）:

```jsonc
{
  "resolved": [
    {"screen_id": "home", "title": "ホーム", "path": "/", "source_file": "src/pages/home/index.tsx"}
    // iOS backend では "path" の代わりに screen_id が deeplink の {id} に入る。
    // レジストリ逆引き型の resolver は "status" / "is_deleted" を追加してよい
  ],
  "unresolved": [
    {"source_file": "src/pages/misc.tsx", "reason": "route_map にマッチするエントリがありません"}
  ]
}
```

同梱の resolve_screens.py は config の `screens.route_map`（宣言的マッピング）で解決する。
コード上のレジストリをパースする等の複雑な逆引きが必要なリポジトリは、この契約を満たす
コマンドを `screens.resolver_command` に指定する（リポジトリ側が実装を持つ = adapter方式）。
**unresolved が出たら呼び出し元は推測せずユーザーに確認する。**

## 3. capture 契約（backends/*/capture系）

入力は backend ごとのCLI引数。出力（stdout, JSON）は全backend共通:

```jsonc
{
  "screen_id": "home",
  "pages": 3,                 // webフルページは常に1
  "files": ["<abs>/before-home-p1.png", "..."],  // ページ順
  "truncated": false,         // 上限到達で終端未検出（iOSページングのみ）
  "fallback_single": false,   // 全域撮影が使えず1枚のみ（iOS: sim-use不在 / web: Playwright不在）
  "paging_failed": false,     // ページ送りが途中失敗。撮れたページまでは files に含む

  // 以下はweb backendのみ。「何を撮ったか」の記録
  "requested_url": "http://localhost:5173/about",
  "final_url": "http://localhost:5173/about",  // リダイレクト追跡後
  "title": "About"                             // レスポンスHTMLの <title>
}
```

**失敗を成功偽装しない**のがこの契約の核。3つのboolフラグは manifest に転記され、比較HTMLに
警告表示される（「全域撮れたつもりで実は1ページ目だけ」が差分見落としを生むため）。
exit 0 以外は「取得失敗」であり、その画面をmanifestに載せてはいけない。

web backend は撮影前に対象URLを1回GETし、**`final_url` が要求URLと異なれば中断**する
（末尾スラッシュのみの差は同一とみなす）。認証が要る画面でログイン画面を「その画面」として
撮る事故を防ぐため。意図的なリダイレクトを撮りたい場合のみ `--allow-redirect`。

⚠️ **この検知はHTTPレベルのリダイレクトに限る**。JSによるクライアントサイド遷移や、SPAの
404フォールバック（存在しないパスでも200 + シェルHTMLを返す）は素通りする。`title` が
Before/Afterで食い違う、または対象画面すべてで同一の場合はこれらを疑い、比較HTMLの目視
確認に回す。

## 4. manifest 契約（render_comparison.py の入力）

```jsonc
{
  "pr_number": 42,
  "pr_title": "ホーム画面にお知らせカルーセルを追加",
  "pr_url": "https://github.com/example/app/pull/42",
  "base_ref": "main",
  "head_ref": "feature/home-carousel",
  "build_status": "SUCCEEDED",         // "SUCCEEDED"を含めばok表示
  "validation_status": "",             // 任意（lint/DS検証等の要約。"PASSED"/"ERROR 0"でok表示）
  "generated_at": "2026-07-24T16:00:00+09:00",
  "screens": [
    {
      "screen_id": "home",
      "title": "ホーム",
      "status": "stable",              // 任意（stable/experimental/deprecated のみバッジ表示）
      "kind": "changed",               // "changed" | "new"（Before無し） | "removed"（After無し）
      "before_paths": ["..."],         // capture契約の files をページ順のまま
      "after_paths": ["..."],
      "before_truncated": false,       // capture契約のフラグを side別に転記
      "after_fallback_single": false,
      "description": "1〜2行の変更点コメント（AIが全ページ目視して書く）",
      "lint_error_count": 0            // 任意
    }
  ]
}
```

旧schema（`before_path`/`after_path` 単数キー、`ds_validation_status`、`ds_error_count`）も
後方互換で読める。
