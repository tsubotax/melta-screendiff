# melta-screendiff — JSON契約

スキルの各層はJSON契約で接続される。**「揺れてよい判断」はSKILL.md、「揺れてはいけない処理」はスクリプト**が原則。スクリプトは読み取り専用 or 冪等で、実行判断（checkout・破棄・投稿）は必ずSKILL.md側に残す。

```
SKILL.md（オーケストレーション層 / AIが読む手順書）
  └── scripts/（決定論的処理層 / Python標準ライブラリのみ）
        ├── load_config.py       設定解決（2段フォールバック）
        ├── run_dir.py           実行ごとの出力ディレクトリ作成
        ├── resolve_screens.py   変更ファイル → 画面解決（route_map）
        ├── validate_resolved.py resolver出力の契約検証（撮影前ゲート）
        ├── preflight_base.py    base先行チェック（撮影前ゲート・OPEN PRのみ）
        ├── share_plan.py        共有モード解決 → 許可/禁止アクション確定（--mode range で PR 書き込み禁止）
        ├── render_comparison.py manifest.json → base64埋め込み比較HTML
        ├── run_cleanup.py       cleanup_command の実行（Cleanupフェーズ）
        ├── screendiff_range.py  範囲モード（PR 不要）。2 点 / 作業ツリーを隔離 snapshot から比較する 1 コマンド runner
        ├── resolvers/
        │   └── expo_router.py   expo-router のパスから 直接 / 代表 / 未対応 を機械判定する resolver
        └── backends/
            ├── web/capture.py   Playwright/Chrome フルページ撮影（PR モード）
            ├── web/capture_browser.mjs  範囲モードの撮影。同じページで pathname / ready_selector を検査
            └── ios/…            simctl + sim-use ページング撮影・AXチェック
```

画像はAIのコンテキストを通さない。render_comparison.py がディスク上のPNGを直接base64エンコードしてHTMLに埋める（何十枚撮ってもトークンを消費しない）。

同梱テストは `bash skills/screendiff/scripts/run_tests.sh` で全部走る（Python標準ライブラリのみ）。撮影まで進む3ケースだけは Playwright / Chrome が要るため、無い環境では自動的に skip される（「撮影エンジン不要」と謳って実は必要、という状態を避ける）。**契約を変える変更を入れるときは、対応するテストを先に足すこと** — このスキルは「SKILL.md（AIが読む手順書）」と「scripts（決定論的処理）」の二層で、scripts が静かに壊れてもAIは手順どおり呼び続けて異常に気づかない。テストが唯一の防波堤になる。

## 1. config（load_config.py の出力）

探索順: `--config` 明示 → `<repo>/.claude/screendiff.json` → `~/.config/melta-screendiff/<repo名>.json`。
後者は**対象リポジトリに1ファイルもコミットできない場合**（業務委託先リポ等）のユーザー側設定。

```jsonc
{
  "backend": "web",                    // "web" | "ios"
  "target_file_patterns": ["^src/(pages|components)/"],  // Phase 1 の適用範囲判定（re.search）
  "output_dir": "output/screendiff",   // repo-root相対 or 絶対パス
  "lint_command": null,                // 任意。変更ファイル群を引数に取るlint
  "cleanup_command": null,             // 任意。Cleanup成功後にrepo-rootで1回（下記§1.1）
  "before_base": "branch_tip",         // "branch_tip"（既定） | "merge_base"（下記§1.3）
  "share_mode": "artifact",            // "artifact"（既定） | "local"（下記§1.2）
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
- `web` / `ios` / `screens` がオブジェクトであること、`file_pattern` / `id` / `resolver_command` /
  `serve_command` / `ios.*` が文字列であること（**どんな入力でも traceback を出さない**のがこの
  関数の契約。設定を書き間違えただけの利用者に「プラグインが壊れている」と誤認させない）
- `before_base` が `"branch_tip"` | `"merge_base"` のいずれかであること
  — ⚠️ タイポを既定へ倒すと「PR固有差分を見ているつもりで base の進行分も見ている」状態になる
- `share_mode` が `"artifact"` | `"local"` のいずれかであること
  — ⚠️ タイポを既定値へ黙ってフォールバックさせない。「PRに書き込むかどうか」の分岐であり、
  `"slack"` のような値を無視して `artifact` として実行すると、書き込ませたくないPRに書き込む
- `lint_command` / `cleanup_command` が文字列または `null` であること
  — `cleanup_command` は空文字も不可（「設定したつもりで何も走らない」状態が黙って成立する）。
  `lint_command` の空文字は本キー導入以前から素通りしていたため、落とさず `null` へ正規化する
  （既存設定が Phase 0 で動かなくなるのを避ける。扱いは「未設定」で確定させる）

### 1.1 cleanup_command

Cleanup で **元refへの復帰と `git stash pop` が成功した後にのみ**、repo-root を cwd として1回実行する
（実行は `run_cleanup.py`。順序の担保はSKILL.md側の責務）。gitignore されたビルド成果物は
`git checkout` では消えないため、最後に Before 側でビルドした成果物が作業ツリーに残る。
「成果物が無ければビルドする」型のセットアップスクリプトを持つリポジトリでは、次の通常作業が
その stale な成果物を掴む。

`run_cleanup.py` の出力（stdout, JSON）と終了コード:

```jsonc
{"status": "skipped"}                        // 未設定(null)。exit 0。空文字は error（load_config と揃える）
{"status": "succeeded", "exit_code": 0, ...} // exit 0
{"status": "failed", "exit_code": 3, ...}    // exit 1。stdout_tail / stderr_tail に末尾2000字
{"status": "error", "error": "..."}          // exit 1。config不正・repo-root不在など実行前の失敗
```

**失敗を握り潰さない**のがここでも核。非0終了は exit 1 で返し、SKILL.md は共有フェーズへ進まずに
`git status` / `git stash list` を提示する（残置に気づかないまま「完了しました」と報告しない）。

⚠️ **広域削除を書かないこと。** `git clean -fdx` のようなコマンドは、直前の `git stash pop` で
復元した untracked ファイルや、比較HTMLを置いた `output_dir` まで消す。削除するならパスを
限定する（`npm run clean --if-present` のようなリポジトリ側のスクリプトを呼ぶのが安全）。

### 1.2 share_mode

比較結果の配布方法。**PRへ書き込むかどうかを決める設定**であり、撮影内容には影響しない。

| 値 | 挙動 |
|---|---|
| `artifact`（既定） | 比較HTMLを Artifact として発行し、PR本文編集・コメント投稿まで進む（従来フロー） |
| `local` | comparison.html をローカルに生成して終わり。Artifact発行もPRへの書き込みも一切しない |

`local` は「配布先中立」の概念として定義してある。生成されたHTMLをどこへ配るか（チャットへの
添付・社内共有ドライブ・そのままブラウザで開く）は**人間が手で決める**。特定の配布先を前提にした
名前を付けない（配布先が変わるたびにモードが増えるのを避ける）。

実行時に `--share local` / `--share artifact` で config を上書きできる。解決と
アクション確定は `share_plan.py` が行う（Phase 7 の入口）:

```jsonc
{
  "share_mode": "local", "source": "実行時の明示指定", "author_mode": false,
  "effective": "local",           // "artifact" | "local" | "artifact_unavailable"
  "allow_artifact": false, "allow_pr_write": false,
  "actions": ["comparison.html の絶対パスを提示する", "..."],
  "forbidden": ["Artifact の発行", "gh pr comment", "gh pr edit"]
}
```

**分岐を散文に置かないのが要点**。`local` は「PRに書き込まないこと」自体が目的の機能で、
読み違い1回で取り消せない書き込みが起きる（撮影対象がズレる silent 事故と違い、外向きで
不可逆）。SKILL.md は `actions` を実行し、`forbidden` は理由を問わず実行しない。

`--no-artifact`（Artifact機能が使えない環境）は `local` に落とさず `artifact_unavailable`
として**PR書き込みも止める**。Artifact URL を作れないのに「比較はArtifactにあります」と
PR本文へ書くと、リンク先の無い案内が残るため。

### 1.3 before_base

OPEN PR で **Before をどのコミットから撮るか**。撮影対象そのものが変わる設定で、
MERGED PR には効かない（マージコミットの親が Before で、基準が既に固定されているため）。

| 値 | Before | 何が見えるか | base 先行時 |
|---|---|---|---|
| `branch_tip`（既定） | base ブランチ先端 | 今の base にこのPRを載せた姿 | **中断**（PRと無関係な差分が混入する） |
| `merge_base` | PR の分岐点（merge-base） | このPRだけで何が変わったか | **続行**（比較はPR固有の差分のまま） |

**`merge_base` は成功偽装ではない。** base 先行時に `branch_tip` で撮ると「PRが加えていない
差分」が混ざるのに対し、`merge_base` の比較はPR固有の差分として正しい。ただし
「今の base に載せたらどう見えるか」は分からないので、**基準が変わったことを必ず伝える**:

- **主経路は比較HTMLヘッダの `Before基準: merge-base（PRの分岐点）` チップ**（HTMLだけ
  受け取った第三者に届く唯一の経路なので必須）
- チャットへの報告は preflight が返す `info` の1行（先行コミット数つき）。このモードでは
  base 先行が常態なので大警告にはしない — 毎回出る警告は読み飛ばされ、
  「config で明示的に選んだモードの再通知」にしかならない。数字を出すことで
  「いつもより離れている」ことには気づける状態を保つ

base が日常的に進むリポジトリ（他チームの日次リリースやリリース自動コミットがある等）では
`branch_tip` の中断ゲートがほぼ毎回発火して運用が回らないため、`merge_base` を選ぶ。
実行時に `--before-base merge_base` で上書きできる。

### 1.4 出力ディレクトリ（run_dir.py）

`output_dir/<PR>/<実行時刻>/` を実行ごとに作り、`output_dir/<PR>/latest` を最新実行へ向ける。

同一PRの撮り直し（レビュー指摘 → 修正 push → 再撮影）は例外ではなく通常運用で、
出力先を `output_dir/<PR>` に固定すると2つの問題が同時に起きる:

1. **前イテレーションの証跡が消える** — 修正前後の比較HTMLを並べられない
2. **撮影前に中断した回に、前回の comparison.html が「今回の比較」として残る**
   （SKILL.md 側の `COMPARISON_READY` と合わせた多層防御。片方だけに頼らない）

既存ファイルは一切消さない。`latest` が symlink 以外（利用者が作ったディレクトリ等）なら
触らず `latest: null` を返す（呼び出し元は latest に触れず実体パスを使う）。

既定の run-id は秒精度のため、**`exist_ok=False` で作り、衝突したら連番を足して再採番する**
（同じ秒に2回起動して同一ディレクトリを共有すると、固定名ファイルが前回を上書きし
「実行ごとに分ける」目的が静かに崩れる）。`--run-id` を明示して既存と衝突した場合は
再利用せずエラーにする。run-id / PR番号はパス要素になるため許可文字を限定する
（`.` `..` はパス区切りを含まないので、区切り文字チェックだけでは通ってしまう）。

⚠️ **検証で防げないもの**: `serve_command` にSPAフォールバックを持つサーバー（Vite の
dev / preview は `appType` 既定 `"spa"`、Next.js 等も同様）を指定すると、存在しないパスでも
200 とルートの `index.html` が返り、撮影対象がズレても検知できない。ビルド成果物を素の静的
サーバーで配信すると 404 になり capture が中断する（README の「serve_command の罠」参照）。

### 1.5 range（範囲モード・screendiff_range.py）

PR を使わず 2 点（または作業ツリー）を比較するときの設定。PR モードの `web.serve_command` とは独立で、
**snapshot ルートを cwd** に実行される。`$SCREENDIFF_WEB_DIR`（side 別の export 先）と `$SCREENDIFF_SIDE`
（`before` / `after`）が環境変数で入る。

```jsonc
"range": {
  "snapshot_paths": ["mobile", "design/serve.mjs"],           // git ls-files / git archive の pathspec
  "dependency_change_patterns": ["^mobile/(package\\.json|package-lock\\.json|patches/)"],
  "node_modules_link": "mobile/node_modules",                  // snapshot からここへ symlink。使うなら上の patterns が必須
  "playwright_module": "mobile/node_modules/playwright",       // capture_browser.mjs が require する
  "fixtures": [{"path": "mobile/assets/mock-photos/done-ui.png", "base64": "..."}],  // ignored だがビルドに要るもの
  "env": {"EXPO_NO_DOTENV": "1", "CI": "1", "EXPO_PUBLIC_API_URL": ""},
  "build_command": "cd mobile && node node_modules/expo/bin/cli export --platform web --clear --output-dir \"$SCREENDIFF_WEB_DIR\"",
  "serve_command": "node design/serve.mjs --dir \"$SCREENDIFF_WEB_DIR\" --port 0",   // 起動時に URL を出力する、か serve_url を指定
  "serve_url": null,                 // 固定ポートのとき。起動前に応答があれば中断（残存サーバを撮らない）
  "device_scale_factor": 3, "ready_timeout_ms": 10000, "build_timeout_sec": 600,
  "capture_timeout_sec": 180,        // 1 画面の撮影プロセスの上限。超えたら failed（配信サーバは止める）
  "data_caveat": null                // null なら既定の「API データは固定していない」注記を HTML に出す
}
```

runner の不変条件（screendiff_range.py）:

- `--before` / `--after` は最初に commit OID へ固定し、以後の diff・依存検査・snapshot はその OID だけを使う。
  ビルド中に別ターミナルで commit / fetch されても After の中身は `head_oid` と一致する
- 依存ファイル（`dependency_change_patterns`）は **`snapshot_paths` と独立にリポ全体から**探す。`node_modules_link` を
  使う設定でパターンが 3 者とも 0 件なら設定ミスとして `failed`（一致扱いで素通りさせない）
- `target_file_patterns` に当たる変更があるのに撮れる画面が 0（動的ルートだけ・route_map 不一致だけ）のときは
  `no_ui_change` でなく **`failed`**。`unsupported` / `unresolved` を result に残す（未確認を「変更なし」で消さない）
- 作業ツリー snapshot の `inputs_sha256` は**コピーした実体**から取る（コピー元を読み直すと保存のタイミングで取り違える）
- fixture の書き込み先は snapshot の内側に限る（絶対パス・`..`・共有 `node_modules` の symlink 経由は `failed`）
- 配信サーバは例外・Ctrl-C・撮影タイムアウトでも止める。途中で落ちた回も `out_dir/result.json` に `sides[].serve_url` と
  理由が残る
- `capture_browser.mjs` の exit 1（`stage` が `load` / `launch`）だけを `skipped_runner_unavailable` に分類する。
  `stage: "page"`（goto タイムアウト・保存失敗）は当該画面の `compare_status: unresolvable`
- Before を 1 枚も撮れていない回（全画面が新規）は manifest に `before_oid` / `before_base` を書かない（§4）

⚠️ **隔離の保証範囲**: snapshot の `node_modules` は作業ツリーへの**書き込み可能な symlink**なので、`build_command`
が `node_modules/.cache` 等へ書くことまでは止めない。runner が保証するのは「作業ツリーの追跡ファイルと `.git` に
触らない」ことで、共有 `node_modules` の無変更ではない。

load_config.py は `build_command` / `serve_command` のどちらかがあれば範囲モードの設定として検証する
（両方必須、`snapshot_paths` 非空、`node_modules_link` を使うなら `dependency_change_patterns` 必須、
`fixtures` は `{path, base64}`）。範囲モードだけ使うリポジトリでは `web.serve_command` を省略できる。

runner の出力（stdout, JSON）は `run_status`（`compared` / `no_ui_change` / `skipped_dependency_change` /
`skipped_runner_unavailable` / `failed`）を持ち、`failed` だけ exit 1。**どの値でも通常の push を止めない**
のが呼び出し元（SKILL.md / codex/screendiff/SKILL.md）の契約。出力先は `output_dir/range/<時刻>/`
（side ごとに `input/` `web/` `shots/`、`manifest.json`、`comparison.html`、`result.json`）。

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

**web backend では `resolved[].path` が必須（非空・"/" 始まり）。** `screen_id` も非空の文字列。
同じ `screen_id` に異なる `path` を返してはいけない。解決不能なら `resolved` に載せず
`unresolved` に倒すか、`{"error": ...}` を出して exit 1 にする（0件成功に偽装しない）。

⚠️ load_config.py の path 必須化は **`route_map` にしか効かない**。resolver_command の出力は
別経路なので、**`validate_resolved.py` を必ず通す**（SKILL.md の Phase 2 で規定）。空 path を
通すと撮影URLが `serve_url` そのもの＝トップページになり、200が返り title も取れ、Before/After が
一致して「差分なし」と誤結論する——route_map で潰したのと同型の事故が adapter 経路で再開通する。

### 2.1 同梱 resolver: expo_router（resolvers/expo_router.py）

`screens.resolver: "expo_router"` と `screens.expo_router` で、expo-router の**ファイルパスから撮る画面を機械的に決める**。
手書きの正規表現は持たない（設計レビューで 3 周連続、`(tabs)/search/` 配下の子画面 9 本を親の検索トップへ
直接解決する誤りが残った。子画面の変更を親画面の「差分なし」で確認済みにする事故を、分類を機械に任せて塞ぐ）。

```jsonc
"screens": {
  "resolver": "expo_router",
  "expo_router": {
    "app_dir": "mobile/src/app",          // expo-router の app ディレクトリ（repo-root 相対）
    "route_prefix": "/melta",             // ここに当たらないルートは unsupported（旧ルート・lab 等）
    "feature_map": [{"pattern": "^mobile/src/melta/(workshop|completion)/", "screens": ["/melta/workshop"]}],
    "representative": ["/melta/home"],    // feature_map に当たらない変更の代表画面
    "titles": {"/melta/home": "ホーム"},
    "ready_selectors": {"/melta/home": "[data-testid=screen-home-ready]"},   // 任意
    "fixed_params": {"/melta/search/project/[id]": {"id": "demo-1"}}         // 任意。動的ルートを direct に昇格
  }
}
```

| 変更ファイル | 分類 | 撮る画面 |
|---|---|---|
| `app_dir` 配下の静的ルート（`route_prefix` 一致） | direct | そのルート |
| 動的セグメント（`[id]`）あり | unsupported（`fixed_params` に値があれば direct） | — |
| `_layout` | representative | その階層直下の静的ルート（無ければ `representative` 既定） |
| `+html` / `+not-found` / `route_prefix` の外 | unsupported | — |
| `app_dir` 外で `feature_map` に当たる | representative | 指定の画面 |
| それ以外 | representative | `representative` 既定 |

出力は §2 の契約に次を足す（`validate_resolved.py` は追加キーを素通しする）:

```jsonc
{"resolved": [{"screen_id": "melta-home", "title": "ホーム", "path": "/melta/home",
               "source_file": "...", "source_files": ["..."],
               "representative": true,      // 全部が代表扱い（変更箇所が写らない可能性）
               "also_direct": false,        // 直接と代表が混ざった
               "ready_selector": "[data-testid=...]"}],
 "unresolved": [],
 "unsupported": [{"source_file": "...", "route": "/melta/search/project/[id]", "reason": "動的セグメント..."}],
 "static_routes": ["/melta/home", "..."]}
```

同一画面に複数の変更が当たったときの集約: `representative` は**全部が代表のときだけ true**、混ざれば
`also_direct: true`（入力順で注記を落とさない）。`route_map` 版（resolve_screens.py）も同じ集約をし、
エントリの `representative` / `ready_selector` を引き継ぐ。

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

⚠️ **この検知は「撮れた＝要求した画面」を保証しない。** 限界が3つある:

1. **HTTPレベルのリダイレクトしか見えない** — JSによるクライアントサイド遷移や、SPAの
   404フォールバック（存在しないパスでも200 + シェルHTMLを返す）は素通りする
2. **疎通確認と撮影が別プロセス・別User-Agent** — 疎通確認は urllib、撮影は Playwright /
   Chrome。UAで応答を変えるサーバーなら、urllib には200を返しつつブラウザだけログイン画面へ
   飛ばす食い違いが起こりうる。`final_url` は「urllib から見た最終URL」であって撮影実体ではない
3. **fragment（hash route）は検証できない** — サーバーに送られずHTTP応答にも現れない

`title` がBefore/Afterで食い違う、または対象画面すべてで同一の場合はこれらを疑い、比較HTMLの
目視確認に回す。機械的に潰すには撮影自体をブラウザAPIに移し、実行後の `location.href` と DOM を
見る必要がある（別タスク）。

## 4. manifest 契約（render_comparison.py の入力）

```jsonc
{
  "pr_number": 42,
  "pr_title": "ホーム画面にお知らせカルーセルを追加",
  "pr_url": "https://github.com/example/app/pull/42",
  "base_ref": "main",
  "head_ref": "feature/home-carousel",
  // 実際に撮った2コミット（任意だが記録することを強く推奨）。OPEN PRでは preflight_base.py が
  // 解決したフルOIDをそのまま入れる。MERGED PRでは head=mergeCommit.oid / before=その^1。
  // before_oid と head_oid が揃っているときだけ比較HTMLのヘッダに短縮OIDが出る
  // （manifest.json はローカルにしか残らないため、HTMLだけを受け取った第三者が
  //   「何と何を比べたか」を確認できるようにする）
  "before_oid": "9f1c2b4e5a6d7c8b9a0f1e2d3c4b5a6978890123",  // ★実際にBeforeとして撮ったコミット
  "head_oid": "1a2b3c4d5e6f7890abcdef1234567890abcdef12",
  "base_oid": "9f1c2b4e5a6d7c8b9a0f1e2d3c4b5a6978890123",    // 撮影時点のbaseブランチ先端（OPENのみ）
  "merge_base_oid": "9f1c2b4e5a6d7c8b9a0f1e2d3c4b5a6978890123",  // OPENのみ
  "before_base": "branch_tip",         // "branch_tip" | "merge_base" | "merge_parent"（MERGED PR）
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
後方互換で読める。OIDが無い manifest もそのまま描画できる（チップが出ないだけ）。`before_oid` が
無ければ `base_oid` を Before として扱う（`before_oid` 導入前のスキーマ）。

⚠️ **`before_base` が `"merge_base"` のとき、`base_oid` へはフォールバックしない**（`before_oid` →
`merge_base_oid` の順で解決する）。撮ったのは base 先端ではなく分岐点であり、`base_oid` を出すと
「撮っていないコミット」を提示することになる。

⚠️ **`Before基準` のチップはOIDの有無と独立に描画する。** OIDが欠けたときに一緒に消えると、
「今の base に載せた姿ではない」という警告だけが黙って落ちる。

⚠️ **Before を撮っていない回（全画面が新規・Before取得失敗）は `before_oid` も `before_base` も
記録しない。** 書くと片側撮影が両側成功に見える（`head_oid` だけを記録する）。

### 4.1 範囲モードの追加キー

```jsonc
{
  "mode": "range", "before_base": "range",     // ヘッダに「範囲比較」と出す。PR の文言は出ない
  "base_ref": "origin/main", "head_ref": "HEAD" | "worktree",
  "after_source": "ref" | "worktree",          // worktree なら「push 対象の commit と対応しない」注記
  "before_oid": "...", "head_oid": "...",      // After は従来どおり head_oid（after_oid は作らない）
  "inputs_changed_during_capture": false,      // true なら赤い注記（撮り直し）
  "inputs_sha256": {"before": "...", "after": "..."},
  "unsupported": [...],                        // resolver の unsupported をそのまま。HTML に一覧で出す
  "data_caveat": "...",
  "screens": [{...,
    "compare_status": "compared" | "new" | "removed" | "unresolvable",   // 既存の status（stable 等）と別キー
    "compare_reason": "After: 遷移先が /login（期待 /about）",
    "representative": true, "also_direct": false, "source_files": [...]}]
}
```

`kind` は従来どおり描画用（`unresolvable` は `changed` のまま、バッジと注記で「比較不能」を出す）。
`compare_status` / `representative` / `inputs_changed_during_capture` / `unsupported` の注記は
**成功偽装を防ぐためのもの**で、render は黙って落とさない（test_render_comparison.py で固定）。

## 5. preflight 契約（preflight_base.py）

OPEN PR の Before は「撮影時点の base ブランチ先端」から撮る。base が PR の分岐後に進んでいると、
**Before に他PRのマージ結果が入り、PRが加えていない差分が比較に混ざる**。撮影後にOIDを記録しても
比較そのものの正しさは戻らない（記録は監査であって訂正ではない）ため、**撮影前**に確定させる。

```jsonc
{
  "status": "ok",              // base は HEAD の祖先
  "mode": "branch_tip",        // "branch_tip" | "merge_base"
  "blocking": false,           // exit 0 は blocking が false のときだけ
  "before_oid": "9f1c…",       // ★Before として checkout すべきコミット（呼び出し元は組み立て直さない）
  "base_oid": "9f1c…", "head_oid": "1a2b…", "merge_base_oid": "9f1c…"
}
// base 先行 + branch_tip → 撮影に進んではいけない（exit 1）
{"status": "base_ahead", "blocking": true, "message": "...", "hint": "..."}
// base 先行 + merge_base → 分岐点から撮って続行（exit 0）
// info は1行で報告する（このモードでは常態。大警告にすると読み飛ばされる）
{"status": "base_ahead", "blocking": false, "before_oid": "<merge-base>",
 "info": "base（main）は分岐後 12 コミット進行（Before は分岐点基準）", "base_ahead_count": 12}
// OID解決不能・共通祖先なし・git異常終了（exit 1）
{"status": "error", "blocking": true, "error": "...", "details": [...]}
```

`base_ahead` と `error` を**分けているのが要点**。`git merge-base --is-ancestor` は「祖先でない」も
「オブジェクトが無い」も非0で返す（1 と 128）。シェルの `|| echo NG` で受けると両者が同じ結論に潰れ、
fetch 漏れを「base が先行しています」と誤報告する。

MERGED PR には適用しない。マージコミットの親から Before を取る経路は比較の基準が既に固定されており、
「base が動きうる」という前提が成り立たない。
