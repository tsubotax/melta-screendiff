---
name: screendiff
description: UI変更PRについて、PRのbaseブランチ（Before）とPRブランチ（After）をそれぞれビルド・実キャプチャしてBefore/After比較HTMLを生成、PRコメント下書きも作る（投稿はHITL）。トリガー:「PRレビューして」「PRの差分見せて」「Before/After比較」「screendiff」「このPRビルドしてチェック」。対象リポジトリに .claude/screendiff.json（またはユーザー側 ~/.config/melta-screendiff/<repo>.json）の設定が必要。UI変更を含まないPR、静的コードレビューだけで足りるPRには使わない。受け取りレビューのほか、PR作者が自分のPRに比較Artifactを添付するauthorモードあり。Artifact発行もPRへの書き込みもせず、比較HTMLをローカル生成して終わる local 共有モードもある（外部の共有面に置けない案件向け）。
user-invocable: true
---

# PR画面差分レビュー（screendiff）

UI変更PRについて、Before（PRのbase branch）/After（PRブランチ）を実際にビルド・キャプチャして視覚比較し、レビューを支援する。軽量・高速に「このPRで画面がどう変わったか」を見せることが目的。

各処理の入出力は `${CLAUDE_PLUGIN_ROOT}/docs/contracts.md` のJSON契約に従う（設定サンプルは `${CLAUDE_PLUGIN_ROOT}/examples/`）。決定論的な処理はすべて `scripts/` に任せ、このファイルは判断・分岐・エラー時の振る舞いだけを規定する。

最初にスクリプトのルートと対象PR番号を確定する。PR番号はスキル引数から取り、**正の整数であることを確認**する（無ければユーザーに聞く）:

```bash
SCRIPTS="${CLAUDE_PLUGIN_ROOT}/skills/screendiff/scripts"
PR=<引数のPR番号>   # 例: /screendiff:screendiff 42 → PR=42
```

### 共有モード（`SHARE_MODE`）

比較結果をどう配布するかを最初に確定する。**Phase 7 の分岐に直結し、PRへ書き込むかどうかが変わる**ため、撮影を始める前に決めておく:

| 値 | 挙動 |
|---|---|
| `artifact`（既定） | 比較HTMLを Artifact として発行し、PRへの書き込み（本文編集・コメント投稿）まで進む |
| `local` | comparison.html をローカルに生成して終わり。Artifact発行もPRへの書き込みも一切しない |

決め方（先に決まったものを採用）:

1. スキル引数の明示指定 — `/screendiff:screendiff 42 --share local`（`--share artifact` も同様）。ユーザーが自然文で「Artifactは出さず手元だけで」「PRには書かないで」と指定した場合も `local` として扱う
2. Phase 0 で読む `<config.share_mode>`
3. どちらも無ければ `artifact`

明示指定があれば `SHARE_ARG="--share local"` のように控えておく（**この解決とアクション確定は Phase 7 で `share_plan.py` が機械的に行う**。ここで決めるのは「ユーザーが何を指定したか」だけ）。

`local` を選ぶのは、閲覧者が Artifact の共有経路にアクセスできない場合や、比較画像を外部の共有面に置くべきでない案件の場合。生成したHTMLをどこに配るか（チャットへの添付など）は**人間が手で行う** — 配布のHITLをAI側に持ち込まない。

## authorモード（PR作者が自分のPRに比較材料を添付する場合）

PR作成直後に作者自身がレビュー材料を添付する用途。Phase 0〜6 は通常どおり実行し、**Phase 7 のうちレビューコメント下書き（レビュアーモード）はスキップ**する。代わりに Phase 7 の authorモード手順に進む。

新規画面でBefore側に対象画面が存在しない場合は、Afterキャプチャのみの片側比較でよい（比較HTMLに「新規画面のためAfterのみ」と明記される）。

⚠️ **authorモードでも `SHARE_MODE=local` ならPRへは一切書き込まない**（authorモードは「誰が使うか」、SHARE_MODEは「どこへ配るか」の直交する軸）。localの場合はHTMLのパスを提示して終わり、PR本文への追記はユーザーが手で行う。

## 前提1: PR番号は全コマンドに明示的に渡す

`$PR` を対象PR番号として、`gh pr diff "$PR"`、`gh pr view "$PR"`、`gh pr checkout "$PR"`、`gh pr comment "$PR" --body-file ...` のように必ず番号を明示する。「現在チェックアウト中のブランチに紐づくPR」への暗黙依存は、ブランチを行き来する本スキルでは必ず壊れる。

## 前提2: コマンドは失敗したら即座に止まる

`git checkout`/`git fetch`等は失敗しても後続のシェルコマンドがそのまま実行され続ける。**複数コマンドを1回のBash実行にまとめる場合は必ず`&&`で連結する**か、1コマンドずつ実行して都度終了確認する。「checkoutが失敗したのにbuildだけ実行されてしまい、Beforeのつもりで実際はAfterを撮ってしまう」事故を防ぐため、特にブランチ切り替え直後は成否を明示確認してから進める:

```bash
git checkout --detach "<ref>" && echo "CHECKOUT_OK" || { echo "CHECKOUT_FAILED — 中断してCleanupへ"; }
```

## 前提3: 全てのパスは`$REPO_ROOT`基準の絶対パスにする

サブディレクトリにcdして作業する場面があり、cwdによって相対パスの意味が変わる（誤ったネスト`app/output/...`を`app/`配下に作る事故の実例あり）。スキル開始時に一度だけ取得する:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
```

以降、出力先・アプリパスは全て`$REPO_ROOT`基準の絶対パスで組み立てる。

## Phase 0: 設定解決

```bash
CONFIG_JSON=$(mktemp -t screendiff-config)   # 固定パスは並行セッションで上書きされるため使わない
python3 "$SCRIPTS/load_config.py" --repo-root "$REPO_ROOT" > "$CONFIG_JSON" \
  && echo "CONFIG_OK" || echo "CONFIG_NG"
cat "$CONFIG_JSON"
```

CONFIG_NGなら、出力の `error` / `searched` / `details` / `hint` を提示して終了する（対象リポジトリにコミットできない場合はユーザー側 `~/.config/melta-screendiff/<repo名>.json` を案内。`${CLAUDE_PLUGIN_ROOT}/examples/` にサンプルあり）。以降、config値は `<config.xxx>` と表記する。

続けて**この実行専用の出力ディレクトリ**を作る。出力先を `output_dir/<PR>` に固定すると、同一PRの撮り直し（レビュー指摘 → 修正 push → 再撮影。**例外ではなく通常運用**）が同じ場所を使い、前イテレーションの証跡が消える／中断した回に前回の `comparison.html` が残る:

```bash
python3 "$SCRIPTS/run_dir.py" --config-json "$CONFIG_JSON" --repo-root "$REPO_ROOT" --pr "$PR" \
  > "$RUN_DIR_JSON" && echo "RUN_DIR_OK" || echo "RUN_DIR_NG"   # RUN_DIR_JSON=$(mktemp -t screendiff-rundir)
cat "$RUN_DIR_JSON"
```

`RUN_DIR_NG` なら**ここで終了**する（出力先が無いまま撮影に進まない）。`RUN_DIR_OK` なら出力の `out_dir` を `OUT_DIR` として控える（絶対パス。以降の出力は全てこの下に置く）。

`latest`（`output_dir/<PR>/latest`）は最新実行を指すが、**`latest` が `null` で返ることがある**（利用者が同名のディレクトリを置いている、symlink を作れない環境）。その場合は latest に触れず、共有・報告には常に実体の `$OUT_DIR` を使う。**過去の実行ディレクトリは消さない。**

## Phase 1: 適用範囲判定 + 事前安全性チェック + 退避

```bash
gh pr diff "$PR" --name-only
```

`<config.target_file_patterns>` のいずれかにマッチするファイルが0件なら「このPRは対象UIファイルの変更を含まないため対象外です」と報告してここで終了する（ビルド等は一切走らせない）。

続けて安全性チェック:

```bash
ORIGINAL_REF=$(git branch --show-current)
[ -n "$ORIGINAL_REF" ] || ORIGINAL_REF=$(git rev-parse HEAD)   # detached HEAD開始でもCleanupで必ず戻れるようにSHAを入れる
echo "ORIGINAL_REF=$ORIGINAL_REF"
git status --porcelain
```

- 作業ツリーに変更（untracked含む）がある場合、内容を提示しユーザーに確認する。「PRと無関係だから無視してよい」と判断しても、後続の`git checkout`（別コミットへのdetached checkout）はそのファイルに競合があると失敗する。無関係と判断した場合でも、ユーザー確認の上で一時退避する:

```bash
git stash push -u -m "screendiff-$PR-autostash"
```

- `<config.ios.generated_project_file>` が設定されている場合、その事前dirty状態も控える（`git diff --quiet -- <path>; echo "gen_pre_dirty=$?"`）。Phase 3/4で自動破棄してよいかの判定に使う。

stashしたかどうか（`STASHED=true/false`）を必ず控えておく。**Cleanup（Phase 6）で必ず`git stash pop`する。**

## Phase 2: PRメタデータ取得・画面解決

```bash
gh pr view "$PR" --json number,title,url,baseRefName,headRefName,body,state,mergedAt,mergeCommit,headRefOid
```

### マージ済みPRの分岐（重要）

`state` が `MERGED` の場合、**`gh pr checkout`は使わない**（マージ後にリモートのPRブランチが削除され失敗することがある）。**Afterは`headRefOid`ではなく、マージコミット自体（`mergeCommit.oid`）を使う**（`headRefOid`はPRブランチ分岐後にbase側で加わった変更を含まない/巻き戻った状態のことがあり、base側差分の混入を実測済み。マージコミットこそが実際にbaseへ適用された正しい状態）:

```bash
MERGE_OID=$(gh pr view "$PR" --json mergeCommit --jq '.mergeCommit.oid')
git cat-file -e "$MERGE_OID^{commit}" 2>/dev/null || git fetch origin "$MERGE_OID"   # ローカル未取得・shallow cloneでも動かす
PARENT_COUNT=$(git log -1 --format=%P "$MERGE_OID" | wc -w | tr -d ' ') && echo "PARENT_COUNT=$PARENT_COUNT"
```

- `PARENT_COUNT` が **1でも2でもない**（0＝`git log`失敗含む）場合は中断してCleanupへ進み、状態を報告する（fetch失敗・OID不正の可能性）。
- `PARENT_COUNT` が **2**（通常のmerge commit）の場合のみ自動処理する。親1がbase側（Beforeの基準）であることを`git log --oneline --graph -3 "$MERGE_OID"`で目視確認してから、Afterは`$MERGE_OID`、Beforeは`${MERGE_OID}^1`を使う。
- `PARENT_COUNT` が **1**（squash mergeやrebase merge）の場合、Beforeの自動特定は信頼できない（PRが複数コミットのrebase mergeだと`^1`は「1つ前のPRコミット」でありBeforeではない）。**推測せず中断し、ユーザーにbase branch当時のコミットSHAを確認する。**

`state` が `OPEN` の場合は通常どおり:

```bash
gh pr checkout "$PR" && echo "CHECKOUT_OK" || { echo "CHECKOUT_FAILED — ORIGINAL_REFのままです"; }
git branch --show-current   # 実際にチェックアウトされたブランチ名を控える
# 撮るのが本当にPR先端かを検証する。同名のローカルブランチが既にあり未pushコミットを
# 持っている場合、gh pr checkout は --force なしではPR先端へリセットしない。その状態で
# 撮ると「PRに存在しないコミット」をAfterとして撮り、manifestにもそのOIDを記録してしまう
HEAD_OID=$(git rev-parse HEAD) && echo "HEAD_OID=$HEAD_OID"
[ "$HEAD_OID" = "<headRefOid>" ] && echo "HEAD_MATCHES_PR" || echo "HEAD_MISMATCH"
```

CHECKOUT_FAILEDの場合はCleanupへ進み終了する。HEAD_MISMATCHの場合も**先へ進まず**、ローカルの差分（`git log --oneline "<headRefOid>..HEAD"`）を提示してユーザーに確認する（未pushコミットを撮るのか、PR先端に合わせるのか。勝手に `--force` で捨てない）。実行中にPR側へpushされた場合も不一致になるので、その場合は再実行を案内する。

### base先行チェック（OPEN PRのみ・撮影前に必ず）

OPEN PR の Before をどのコミットから撮るかを、**撮影前に**確定させる。既定（`branch_tip`）では base ブランチ先端から撮るため、base が PR の分岐後に進んでいると**Beforeに他PRのマージ結果が入り、PRが加えていない差分が比較に混ざる**。撮影後にOIDを記録しても撮ったもの自体は直せない:

```bash
git fetch origin "<baseRefName>" && BASE_OID=$(git rev-parse FETCH_HEAD) && echo "BASE_OID=$BASE_OID"
python3 "$SCRIPTS/preflight_base.py" --repo-root "$REPO_ROOT" \
  --base-oid "$BASE_OID" --head-oid "$HEAD_OID" --mode "<実効モード>" \
  --base-ref "<baseRefName>" --head-ref "<headRefName>" \
  > "$PREFLIGHT_JSON" && echo "PREFLIGHT_OK" || echo "PREFLIGHT_NG"   # PREFLIGHT_JSON="$OUT_DIR/preflight-base.json"
cat "$PREFLIGHT_JSON"
```

`PREFLIGHT_OK` なら、後続で使う値を**目で拾わず機械的に取り出す**（モードとOIDの食い違いを作らないため。ここを手作業にすると「merge-base で撮ったのに manifest は branch_tip」が起こる）:

```bash
python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
for k in ("before_oid", "base_oid", "head_oid", "merge_base_oid", "mode"):
    print(f"{k}={d.get(k, \"\")}")' "$PREFLIGHT_JSON"
```

出力の `before_oid` / `mode` が空なら**中断**する（preflight の出力が契約どおりでない）。空でなければ `BEFORE_OID` / `BASE_OID` / `HEAD_OID` / `MERGE_BASE_OID` / `BEFORE_BASE`（= `mode`）として控える。**`BEFORE_BASE` は config の値ではなく preflight が実際に使ったモード**（実行時 override を反映済み）。

**Before をどこから撮るかは `<実効モード>` が決める** — 実行時の明示指定（`--before-base merge_base`）があればそれ、無ければ `<config.before_base>`（既定 `branch_tip`）。この値を `--mode` に渡す:

| モード | Before | base が先行していたら |
|---|---|---|
| `branch_tip`（既定） | base ブランチ先端 | **中断**（PRと無関係な差分が混入するため） |
| `merge_base` | PRの分岐点 | **続行**（比較はPR固有の差分のまま保たれる）。警告を明示する |

- `PREFLIGHT_NG` かつ `status: base_ahead`（`branch_tip` モード）なら**撮影に進まず中断**し、`message` / `hint` をそのまま提示してCleanupへ進む。「とりあえず撮って差分を見る」で継続しない。baseが日常的に進むリポジトリなら `before_base: "merge_base"` を案内する
- `PREFLIGHT_NG` かつ `status: error` は fetch漏れ・OID不正・共通祖先なしであって「base先行」ではない。原因を提示して中断する（両者を同じ結論に潰さない）
- `PREFLIGHT_OK` で `warning` が付いている場合（`merge_base` モードで base が先行）は、**その warning をユーザーへの報告に必ず含める**。「今の base に載せたらどう見えるか」は分からない比較であることを黙って伏せない
- `PREFLIGHT_OK` なら出力の `before_oid` / `base_oid` / `head_oid` / `merge_base_oid` を控える。**Phase 4 の Before checkout は `before_oid`**（撮るべきコミットとして確定済み。自分で組み立て直さない）、Phase 5 で manifest に記録する

MERGED PR ではこのチェックは**実行しない**。マージコミットの親から Before を取る経路は比較の基準が既に固定されており、base が動きうるという前提が成り立たない。代わりに manifest 用のOIDだけ控える:

- `PARENT_COUNT=2`: `HEAD_OID=$MERGE_OID`、`BASE_OID=$(git rev-parse "${MERGE_OID}^1")`、`BEFORE_OID=$BASE_OID`、`BEFORE_BASE=merge_parent`（`MERGE_BASE_OID` はこの経路では算出しない）
- `PARENT_COUNT=1`（squash/rebase merge）: **`^1` を BASE_OID にしない。** 上の分岐で「`^1` はBeforeとして信頼できない」と判断した対象そのもので、記録すると誤った監査記録が残る。ユーザーに確認したbase側コミットのSHAを `BASE_OID` にする（確認が取れていなければ `base_oid` は記録しない）

### 画面解決

対象ファイル（Phase 1でマッチしたもの）を渡して解決する。**`<config.screens.resolver_command>` が設定されていればそのコマンドを直接実行し**（引数に変更ファイル群を渡す。出力は同じJSON契約）、無ければ同梱のroute_map版を使う:

```bash
# resolver_command がある場合（リポジトリ側実装のadapter）
(cd "$REPO_ROOT" && <config.screens.resolver_command> <変更ファイル...>) > "$OUT_DIR/resolved-raw.json"
# 無い場合（同梱のroute_map版）
python3 "$SCRIPTS/resolve_screens.py" --config-json "$CONFIG_JSON" <変更ファイル...> > "$OUT_DIR/resolved-raw.json"

# ⚠️ どちらの経路でも必ず検証を通す（load_config の path 必須化は route_map にしか
# 効かない。resolver が空pathを返すと serve_url そのもの＝トップページを「その画面」
# として撮り、Before/Afterが一致して「差分なし」と誤結論する）
python3 "$SCRIPTS/validate_resolved.py" --config-json "$CONFIG_JSON" "$OUT_DIR/resolved-raw.json" \
  > "$OUT_DIR/resolved.json" && echo "RESOLVED_OK" || echo "RESOLVED_INVALID"
cat "$OUT_DIR/resolved.json"
```

RESOLVED_INVALID なら**先へ進まず**、出力の `details` を提示してユーザーに確認する（resolver の実装バグを撮影で表面化させない）。

- 出力の `resolved` から `screen_id`/`title`/`path` を取得し、全画面を一覧提示する。
- `unresolved` が出た場合や0件の場合は、**推測せずユーザーに直接確認する。**
- 新規/削除画面の判定: base側に画面（ルート/レジストリエントリ）が存在しなければ `kind: new`、After側に無ければ `kind: removed`。判定できる情報が無ければユーザーに確認する。全画面が新規ならPhase 4の本体処理はスキップするが、**Cleanupは必ず実行する**。

## Phase 3: After取得

### web backend

```bash
# 失敗を握り潰さない（setup が落ちたまま進むと、前のrefのビルド成果物を配信して
# 別refを撮る事故になる）。ビルドは setup_command に置かず serve_command 側に
# `build && serve` の形で内包する方が構造的に安全（README「serve_command の罠」参照）
if [ -n "<config.web.setup_command>" ]; then
  (cd "$REPO_ROOT" && <config.web.setup_command>) && echo "SETUP_OK" || echo "SETUP_FAILED"
fi
```

SETUP_FAILED なら**先へ進まず**Cleanupへ進み、状態を報告する。

**serve前の事前チェック（stale server対策・重要）**: `<config.web.serve_url>` が**起動前から既に応答する場合は中断**し、ユーザーに確認する。ユーザーが別ターミナルで立てている dev server は今checkoutしているrefのコードを配信している保証がなく、「Afterのつもりで別バージョンを撮る」事故になる（iOS backendのstale binary対策と同型）。

```bash
curl -s -o /dev/null --max-time 2 "<config.web.serve_url>" && echo "PORT_BUSY — 中断してユーザー確認" || echo "PORT_FREE"
mkdir -p "$OUT_DIR"
python3 "$SCRIPTS/backends/web/serve_ctl.py" start \
  --cmd "<config.web.serve_command>" --cwd "$REPO_ROOT" \
  --log "$OUT_DIR/serve-after.log" --pid-file "$OUT_DIR/serve.pid" && echo "SERVE_STARTED"
```

serverの起動・停止は必ず `serve_ctl.py` を使う（`( cmd ) &` + PGID kill の自前管理は、非対話シェルでは呼び出し元シェルを道連れにする事故が実測済み）。

各画面を撮影する（capture.py内で疎通待ち＋対象URLのHTTPステータス検証＋リダイレクト検知をする）。configの値を**全て**引数に渡し、**必ず `$REPO_ROOT` を cwd にして**実行する（npxが対象リポのdevDependencyのPlaywrightをcwdから解決するため。別のcwdだと無言でChromeフォールバックに落ちる）。stdout JSONはファイルに保存してから読む（成否echoと混ざるのを防ぐ）:

```bash
(cd "$REPO_ROOT" && python3 "$SCRIPTS/backends/web/capture.py" \
  --url "<config.web.serve_url><screen.path>" --screen-id "<screen_id>" \
  --out-dir "$OUT_DIR" --prefix after \
  --viewport "<config.web.viewport>" --settle-ms <config.web.settle_ms> \
  --wait-ready-sec <config.web.ready_timeout_sec> \
  <config.web.full_page が false なら --no-full-page>) \
  > "$OUT_DIR/capture-after-<screen_id>.json" \
  && echo "CAPTURE_OK" || echo "CAPTURE_FAILED"
cat "$OUT_DIR/capture-after-<screen_id>.json"
```

CAPTURE_FAILEDの画面は「取得失敗」としてmanifestに載せず、ユーザーに報告する（他の画面の処理は継続してよい）。

**リダイレクトで中断した場合**（stderrに「〜にリダイレクトされました」）は、認証が必要な画面である可能性が高い。**勝手に `--allow-redirect` を付けて撮り直さない**（ログイン画面をその画面として撮る事故になる）。ユーザーに提示し、リダイレクト先を撮るのが意図どおりか確認を取ってから付ける。

**撮影結果の `requested_url` / `final_url` / `title` はBefore/Afterで突き合わせる**。titleが食い違う、あるいは対象画面すべてのtitleが同一なら、SPAの404フォールバックやログイン画面を撮っている疑いがある（これらはHTTP 200を返すためcapture.py側では検知できない）。推測で進めず、比較HTMLを目視したうえでユーザーに確認する。

全画面の撮影が終わったら**必ずserverを止める**（次のcheckoutの前に。動いたままだと Before 側で別refのコードを配信し続ける）。serve_ctl が子プロセスごとSIGTERM→SIGKILLし、**ポートが空いたことまで確認**する:

```bash
python3 "$SCRIPTS/backends/web/serve_ctl.py" stop --pid-file "$OUT_DIR/serve.pid" && echo "SERVER_STOPPED" || echo "STOP_FAILED"
curl -s -o /dev/null --max-time 2 "<config.web.serve_url>" && echo "STILL_ALIVE" || echo "PORT_FREE"
```

STOP_FAILED または STILL_ALIVE の場合は**先へ進まず**状態をユーザーに提示する（別refのserverを撮る事故になるため）。

### ios backend

```bash
(cd "$REPO_ROOT" && <config.ios.build_command>)
```

失敗したら中断し、ビルドエラー内容を報告した上でCleanupへ進む（Afterのビルド失敗はPRのバグの可能性が高い）。

`<config.ios.generated_project_file>` が設定されていれば:

```bash
git diff -- <config.ios.generated_project_file> | python3 "$SCRIPTS/backends/ios/generated_project_diff_guard.py"
```

`verdict` が `SAFE` かつ Phase 1で事前clean（`gen_pre_dirty=0`）だった場合のみ `git checkout -- <path>` で破棄する。事前dirty、または `ATTENTION` の場合は**絶対に自動破棄しない**（生成ツールのバージョン差ノイズとユーザーの意図的変更を区別できないため）。提示のみに留め、Phase 7のコメント下書き候補にメモする。

各screen idについて、**stale binary対策として必ず `uninstall → install` を明示的に挟んでから**撮影する（「アプリが入っているか」しか見ない条件付きインストールは、ブランチ切替後に前のバイナリのまま撮影する事故を起こす）:

```bash
SIM_UDID=$(xcrun simctl list devices available -j | python3 -c "import sys,json; ds=json.load(sys.stdin)['devices']; found=[d['udid'] for r in sorted(ds.keys(),reverse=True) for d in ds[r] if d['name']=='<config.ios.simulator_device>' and d['isAvailable']]; print(found[0] if found else '')")
[ -n "$SIM_UDID" ] || { echo "ERROR: simulator not found — 中断してCleanupへ"; }
xcrun simctl boot "$SIM_UDID" 2>&1 | grep -v "Unable to boot device in current state: Booted" || true
xcrun simctl bootstatus "$SIM_UDID" -b && echo "BOOT_OK" || echo "BOOT_FAILED — 中断してCleanupへ"
xcrun simctl uninstall "$SIM_UDID" "<config.ios.bundle_id>" || true
xcrun simctl install "$SIM_UDID" "$REPO_ROOT/<config.ios.app_path>" && echo "INSTALL_OK" || echo "INSTALL_FAILED"
```

simulator未検出・BOOT_FAILEDは中断してCleanupへ。INSTALL_FAILEDの場合はその側の撮影を**行わず**「取得失敗」として報告する（stale binaryのまま撮る事故防止。前提2のfail-fastをここでも適用する）。INSTALL_OKの場合のみ:

```bash
mkdir -p "$OUT_DIR"
python3 "$SCRIPTS/backends/ios/paged_capture.py" \
  --udid "$SIM_UDID" --screen-id "<screen_id>" --deeplink "<config.ios.deeplink>" \
  --out-dir "$OUT_DIR" --prefix after \
  > "$OUT_DIR/capture-after-<screen_id>.json" \
  && echo "CAPTURE_OK" || echo "CAPTURE_FAILED"
cat "$OUT_DIR/capture-after-<screen_id>.json"
```

CAPTURE_FAILEDの画面は「取得失敗」としてmanifestに載せず、ユーザーに報告する（他の画面の処理は継続してよい）。

さらに、sim-use があればAXラベル重複チェックを画面ごとに実行する（`--prefix` に合わせた別ファイル名。**同名だとAfter結果を上書きする**）:

```bash
AX_OUT="$OUT_DIR/ax-dup-after-<screen_id>.json"
if ! command -v sim-use >/dev/null; then
  echo "AX_CHECK_SKIPPED"   # ← skipped 確定。以降のコマンドは実行しない
else
  { xcrun simctl openurl "$SIM_UDID" "<deeplinkの{id}を置換したURL>" && sleep 2 \
    && sim-use describe-ui --udid "$SIM_UDID" \
       | python3 "$SCRIPTS/backends/ios/ax_dup_check.py" > "$AX_OUT"; } \
    && echo "AX_CHECK_OK" || echo "AX_CHECK_FAILED"
fi
cat "$AX_OUT" 2>/dev/null
```

- `openurl` の失敗も `&&` 連鎖で `AX_CHECK_FAILED` に落とす（前の画面を検査して `passed` 扱いになる事故を防ぐ）。deep linkで開き直してから実行する（paged_capture後はスクロール末尾の状態のため）。
- 画面ごとに `ax_check_status` を必ず4値のどれかで記録する: `skipped` / `failed` / `duplicates` / `passed`。**failed/skippedをpassed扱いで省略しない**。manifestの該当画面 `description` 末尾に含め、Phase 7の備考にも転記する。
- 既存画面（kind: changed）はBefore側でも同じチェックを実行し（`ax-dup-before-<screen_id>.json`）、Beforeにも同じ重複がある場合は「既存の重複（PR起因ではない）」と明記する。

### lint（backend共通・任意）

`<config.lint_command>` が設定されていれば、**PRの変更ファイル限定**で実行する（リポジトリ全体の監査は既存警告が多くノイズになる。「このPRが持ち込む違反」だけを浮かせる）:

```bash
gh pr diff "$PR" --name-only | <target_file_patternsでフィルタ> | xargs <config.lint_command> || true
```

検出があればPhase 7のコメント下書きに件数と代表例を記載する。

## Phase 4: Before取得

全画面が新規（`kind: new`）ならこのフェーズの本体処理をスキップする（`BEFORE_CAPTURED=false`。Cleanupには必ず進む）。

**`BEFORE_CAPTURED` を必ず控える。** Before 側を実際に checkout して1画面以上撮影できたときだけ `true`。Phase 5 の manifest（OIDを書くか）と Phase 7 の定型文（「Before/After比較」と書いてよいか）がこの値で変わる。

```bash
cd "$REPO_ROOT"
```

- **OPENなPR**: `git checkout --detach "$BEFORE_OID" && echo "CHECKOUT_OK" || echo "CHECKOUT_FAILED"`（Phase 2 の preflight が返した `before_oid`。ここで再fetchして`FETCH_HEAD`を取り直すと、検証したコミットと撮るコミットがズレて preflight が意味を失う。`git checkout origin/<baseRefName>`もstaleなremote-trackingを踏むため使わない）
- **MERGED済みPR（PARENT_COUNT=2）**: `git checkout --detach "${MERGE_OID}^1" && echo "CHECKOUT_OK" || echo "CHECKOUT_FAILED"`（fetch不要。マージコミット自体がローカル履歴に含まれている）

CHECKOUT_FAILEDの場合は `BEFORE_CAPTURED=false` としてCleanupへ進み、「Beforeブランチ取得に失敗したためAfterのみの比較になります」と報告する。

CHECKOUT_OKなら、Phase 3と同じbackend手順を `--prefix before` で実行する。ただし:

- 撮影は「既存」判定された画面のみ（新規画面はBeforeが存在しない）。
- **Beforeのビルド/起動失敗は「PR起因ではない可能性が高い」旨を明記し、Afterのみの比較として継続する**（スキル全体は失敗させない。Cleanupには進む）。この場合も `BEFORE_CAPTURED=false`。
- webは**serverを必ず立て直す**（Phase 3で止めた状態から。事前のPORT_BUSYチェックも再度行う）。撮影後また止める。
- iosのgenerated_project_fileガードもPhase 3と全く同様に実行する（破棄せず持ち越すとCleanupでの復帰後にdirtyとして残る）。

## Phase 5: 比較HTML生成

各画面のbefore/after PNG（**全ページ**。スクロール下部の変更を見落とさない）をReadツールで実際に確認し、1〜2行の変更点コメントを`description`として書く（プレーンテキスト。render側でHTMLエスケープされる）。

manifest.json（契約は `docs/contracts.md` §4）を`$OUT_DIR`に組み立てる。`*_paths` にはcapture JSONの `files` をページ順のまま渡し、`truncated`/`fallback_single`/`paging_failed` フラグも同JSONから転記する（**失敗を成功偽装しない**。フラグは比較HTMLに警告表示される）。

OIDの記録規則（`before_oid` と `before_base` は比較HTMLのヘッダにも出るため、**撮っていないものを書かない**）:

| 経路 | `before_oid` | `before_base` | その他 |
|---|---|---|---|
| OPEN・Before撮影済み | Phase 2 の `BEFORE_OID` | Phase 2 の `BEFORE_BASE` | `base_oid` / `merge_base_oid` / `head_oid` |
| MERGED（`PARENT_COUNT=2`） | `${MERGE_OID}^1` | `"merge_parent"` | `head_oid`。**`merge_base_oid` は記録しない**（この経路では算出していない） |
| **Before を撮っていない**（全画面が新規／Before checkout失敗／Beforeビルド失敗） | **記録しない** | 記録しない | `head_oid` のみ |

⚠️ 最後の行が重要。`before_base` を無条件に書くと、**Afterしか撮っていない回に「Beforeは○○基準」と表示され、片側撮影が両側成功に見える**。Before 側を実際に checkout して撮影できたか（`BEFORE_CAPTURED=true/false`）を控えておき、false なら OID 系は `head_oid` だけにする。

```bash
python3 "$SCRIPTS/render_comparison.py" \
  --manifest "$OUT_DIR/manifest.json" \
  --output "$OUT_DIR/comparison.html" \
  && echo "COMPARISON_READY=true" || echo "COMPARISON_READY=false"
```

⚠️ **`COMPARISON_READY` を必ず控える。** 出力先は Phase 0 で実行ごとに分けてあるが、それとは独立した共有ゲートとして必要。「ファイルが存在するか」ではなく「**今回の実行でrenderが成功したか**」で共有の可否を決める（`latest` 経由の参照や、同じ `$OUT_DIR` を指したまま再試行する経路でも判断が揺れないようにするため）。renderが成功した場合のみ `true`。**このフェーズに到達せず中断した場合は `false`。**

画像は生成時にbase64埋め込み済みなので追加変換は不要。**生成した時点ではまだ配布しない** — 先に Phase 6（Cleanup）を実行する。作業ツリーが壊れたままPRへ書き込むと、外向きの取り消せない操作だけが進んで手元の破損が放置される。

## Phase 6: Cleanup（作業ツリー復帰）— どこで打ち切っても必ず実行する

Phase 1〜5のどのタイミングで処理を打ち切っても（対象UIファイル変更を含まないPRでの早期終了を除く — その場合はまだ何も動かしていない）、**共有（Phase 7）とユーザーへの最終報告の前に**必ず次を実行する:

```bash
# webでserverが生きていれば止める。pid-fileが無い場合は stopped:true / exit 0 が返る
# （＝未起動・停止済み）ので、失敗を `|| true` で潰さない。撮影途中の異常終了で
# serverが残ったまま「Cleanup成功」と扱うと、次の作業が別refのserverを掴む
python3 "$SCRIPTS/backends/web/serve_ctl.py" stop --pid-file "$OUT_DIR/serve.pid" \
  && echo "SERVER_STOP_OK" || echo "SERVER_STOP_FAILED"
cd "$REPO_ROOT"
git checkout "$ORIGINAL_REF" && echo "RETURN_OK" || echo "RETURN_FAILED — 状態を確認して手動対応が必要"
git branch --show-current   # ORIGINAL_REFと一致するか確認
```

Phase 1で`STASHED=true`だった場合、続けて:

```bash
git stash pop && echo "STASH_POP_OK" || echo "STASH_POP_FAILED"
```

### 後片付けコマンド（`cleanup_command`）

**RETURN_OK かつ（stashしていれば）STASH_POP_OK のときにのみ**実行する。gitignoreされたビルド成果物は`git checkout`では消えないため、最後にBefore側でビルドした成果物が作業ツリーに残る。「成果物が無ければビルドする」型のセットアップスクリプトを持つリポジトリでは、次の通常作業がその stale な成果物を掴む:

```bash
python3 "$SCRIPTS/run_cleanup.py" --config-json "$CONFIG_JSON" --repo-root "$REPO_ROOT" \
  && echo "CLEANUP_OK" || echo "CLEANUP_FAILED"
```

- 未設定なら `status: skipped` で正常終了する（設定していないリポジトリでは何も起きない）。設定値の有無の判定はスクリプト側が行うので、シェルに値を埋め込んで `if` を書かない
- **復帰・stash popが失敗している状態では実行しない。** 壊れた作業ツリーに後片付けを重ねると被害が広がる（stash popで戻ったばかりのファイルを消しうる）

### 失敗契約

`SERVER_STOP_FAILED` / `RETURN_FAILED` / `STASH_POP_FAILED`（コンフリクト含む）/ `CLEANUP_FAILED` のいずれかが出たら、**黙って進まず Phase 7 へ進まない**。現在の状態をそのまま提示してユーザーの判断を仰ぐ:

```bash
git status
git stash list
curl -s -o /dev/null --max-time 2 "<config.web.serve_url>" && echo "SERVER_STILL_ALIVE" || echo "PORT_FREE"   # webのみ
```

`COMPARISON_READY=true` なら比較HTMLのパスは伝えてよい（撮影自体は終わっている）。ただし**Artifact発行・PR本文編集・`gh pr comment` 投稿は行わない**（手元が壊れている状態で外向きの操作だけ先に進めない）。

## Phase 7: 共有（`share_plan.py` が確定したアクションを実行）

**`COMPARISON_READY=true` かつ Phase 6 が全て成功した場合のみ**進む。

`COMPARISON_READY=false`（Phase 2〜4 で中断した、renderが失敗した）の場合は**このフェーズに入らない**。Cleanupの結果と中断理由を報告して終了する。過去の実行ディレクトリや `latest` に `comparison.html` があっても**提示しない**（今回撮っていないものを「このPRの比較」として渡すことになる）。

まず許可アクションを確定する。**分岐を目で追わず、この出力に従う**（PRへの書き込みは外向きで取り消せないため、判断を散文に置かない）:

```bash
python3 "$SCRIPTS/share_plan.py" --config-json "$CONFIG_JSON" \
  $SHARE_ARG <authorモードなら --author> <Artifact機能が使えない環境なら --no-artifact> \
  && echo "PLAN_OK" || echo "PLAN_NG"
```

`PLAN_NG` なら共有せず、出力の `error` を提示して終了する。`PLAN_OK` なら出力の `actions` を上から実行し、`forbidden` に載っているものは**理由を問わず実行しない**。`effective` が取りうる値は3つ:

### `effective: "local"`

1. `comparison.html` の**絶対パス**（`$OUT_DIR/comparison.html`）を提示する。ブラウザで開く手順も添える
2. Artifactは発行しない。**PRへの書き込み（`gh pr comment` / `gh pr edit`）は一切しない**
3. 変更画面の要約（Phase 5 で書いた `description`）はチャットに提示してよい。PRやチャットツールへ貼るかどうかはユーザーが手で決める
4. ユーザーがPRへ手で貼れるよう、1行の定型文も**提示だけ**する（**AIは投稿しない**）。`BEFORE_CAPTURED` で文面を変える — Afterしか撮っていない回に「Before/After比較」と書くのは成功偽装:

```
# BEFORE_CAPTURED=true
【Before/After比較（実キャプチャ）】別途共有（比較コミット: <before_oid短縮>...<head_oid短縮>）
# BEFORE_CAPTURED=false（全画面が新規 / Before取得失敗）
【Afterのみのキャプチャ】別途共有（<理由: 全画面が新規 / Before取得失敗> / After: <head_oid短縮>）
```

ここで終了する。下記のPRコメント下書きには進まない。

### `effective: "artifact_unavailable"`

Artifact 機能が使えない環境で `artifact` が指定された場合。`comparison.html` の絶対パスを提示して終了する。**PRへの書き込みもしない** — Artifact URL を作れないのに「比較はArtifactにあります」とPR本文へ書くと、リンク先の無い案内が残るため。

### `effective: "artifact"`

生成された `comparison.html` を Artifact として提示する。続けてモード別に:

#### authorモード（PR作者が自分のPRに添付する場合）

1. **Artifact 共有トグルを ON** にするようユーザーに促す（共有OFFのままではレビュアーが開けない。トグルはAI側から操作できないため人間の1クリックが必須）
2. 共有ONの確認後、Artifact URL を `gh pr edit "$PR" --body-file ...` でPR本文の「概要」直下に追記する（例: `【Before/After比較（実キャプチャ）】{URL}`）
3. 下記のレビューコメント下書き・投稿はスキップして終了する

#### レビュアーモード（既定）: PRコメント下書き → HITL → 投稿

マージ済みPRの場合、コメント投稿の要否をユーザーに確認する（マージ後のコメントは実務的意味が薄いことが多い）。

以下の下書きを組み立て、**全文をそのままチャットに提示**する:

```markdown
## 🎨 Before/After 画面レビュー（screendiff）

このPRで変更された画面を Before / After でビルド・比較しました。

### 変更画面
| 画面ID | 画面名 | 変更内容 |
|---|---|---|
| `<screen_id>` | <title> | <1-2行の差分要約> |

### ビルド
- After: <✅ SUCCEEDED / ❌ FAILED>
- Before: <✅ / ❌ / スキップ（新規画面のため）>

### 検証
- <lint等の結果。実行していなければ「—」>

### 備考
- <AXチェック結果・生成ファイル差分ATTENTION等、あれば>

---
🤖 このコメントは melta-screendiff が生成しました。
```

「この内容で `gh pr comment "$PR"` を投稿してよいですか？」と明示確認し、承認が出るまで絶対に投稿しない。修正要望があれば直して再提示する。承認後のみ実行:

```bash
gh pr comment "$PR" --body-file "$OUT_DIR/comment-draft.md"
```

**このHITLゲートは省略不可。**

## エッジケース

| ケース | 扱い |
|---|---|
| 対象UIファイル変更を含まないPR | Phase 1で早期終了（Cleanup不要、まだ何も動かしていない） |
| OPEN PRでbaseが分岐後に進んでいる（`branch_tip`） | Phase 2のpreflightで**撮影前に**中断。baseの取り込みか`merge_base`モードを案内 |
| 同上（`merge_base`） | 中断せずPRの分岐点からBeforeを撮る。warningをユーザー報告と比較HTMLの両方に明示 |
| baseとHEADに共通祖先が無い | どちらのモードでも比較の基準を作れない。中断（0件成功に倒さない） |
| 同じPRの撮り直し | Phase 0で実行ごとの出力ディレクトリを作る。過去の実行は消さない |
| preflightがOIDを解決できない | fetch漏れ/OID不正。「base先行」とは別物として原因を提示し中断 |
| 設定ファイルが無い | Phase 0で終了、セットアップ手順を案内 |
| Afterビルド/起動失敗 | 中断、エラー内容を報告してCleanupへ |
| Beforeビルド/起動失敗 | PR起因でない旨を明記しAfterのみで継続、最後にCleanupへ |
| serve_urlが起動前から応答する（web） | 中断してユーザー確認（stale server事故防止） |
| 対象URLが別URLへリダイレクトされる（web） | capture.pyが中断。認証必須画面の疑いを提示し、確認後のみ `--allow-redirect` |
| Before/Afterでtitleが食い違う・全画面のtitleが同一（web） | SPAフォールバック/ログイン画面の疑い。目視確認のうえユーザーに確認 |
| lint検出あり | 比較は継続、HTML/コメント下書き双方で明記 |
| 新規/削除画面 | 該当側をスキップし単カラム表示 |
| 全画面が新規 | Phase 4の本体処理はスキップ、Cleanupは実行 |
| 画面解決0件/unresolved | 推測せずユーザーに確認 |
| 作業ツリーが汚れている | 内容提示しユーザー確認、必要ならstash |
| 生成プロジェクトファイルが事前dirty | 自動破棄せず提示のみ |
| マージ済み・親2つのmerge commit | headRefOidではなくmergeCommit.oidをAfter、`^1`をBefore |
| マージ済み・親1つ（squash/rebase） | Before自動特定を諦め、ユーザーにbase側コミットを確認 |
| `git checkout`/`git fetch`失敗 | 後続を実行せず即中断・Cleanupへ進み状態を報告 |
| Cleanupでのserver停止・復帰・stash pop・cleanup_command失敗 | 黙って進まず`git status`/`git stash list`を提示し判断を仰ぐ。**Phase 7の共有には進まない** |
| 撮影前・撮影中に中断した（`COMPARISON_READY=false`） | Cleanupは実行、Phase 7には入らない。過去の実行の`comparison.html`が残っていても提示しない |
| Beforeを撮っていない（全画面新規/Before取得失敗） | `BEFORE_CAPTURED=false`。manifestに`before_oid`/`before_base`を書かず、local定型文も「Afterのみ」に切り替える |
| `latest`が張れない（同名ディレクトリ/symlink不可） | `latest: null`。latestに触れず実体の`$OUT_DIR`で報告・共有する |
| `gh pr checkout`後のHEADが`headRefOid`と不一致 | 未pushのローカルコミットを撮る事故。中断してユーザー確認（勝手に`--force`しない） |
| `SHARE_MODE=local` | comparison.htmlの絶対パス提示で完了。Artifact発行もPRへの書き込みもしない |
| Artifact機能が使えない環境 | `artifact`指定でも絶対パス提示に倒す（PRへの書き込みはしない） |
