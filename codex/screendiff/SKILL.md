---
name: screendiff
description: "UI 変更の Before/After を実撮影して横並びの比較 HTML を作る（Codex 版）。PR を出さない main 直 push の個人開発では、push 前に `--range origin/main..HEAD` で未 push の commit 群を比較する。『Before/After を撮って』『見た目の差分を出して』『screendiff』『push 前に画面を比べて』で使う。UI を含まない変更、静的レビューで足りる変更には使わない。"
---

# screendiff（Codex 版・範囲モード）

Claude Code plugin `melta-screendiff` の範囲モードを Codex から使う手順書。判断は最小限で、
決定論的な処理は全部 `screendiff_range.py` が行う。**PR は使わない。PR への書き込みは常に禁止。**

## 前提

- 対象リポジトリに `.claude/screendiff.json`（または `~/.config/melta-screendiff/<repo名>.json`）があり、
  `range` 節と `screens` が設定されていること。無ければ `examples/screendiff.range-expo.json` を案内して止まる
- スクリプトの場所: `SCRIPTS=<melta-screendiff のルート>/skills/screendiff/scripts`
  （plugin のキャッシュなら `~/.claude-tsubotax/plugins/cache/melta/screendiff/<version>/skills/screendiff/scripts`、
  開発中なら `/Users/tsubotax/My_Data/08_Melta/melta-screendiff/skills/screendiff/scripts`）
- Codex には Artifact が無い。**`--share local` 固定**。HTML の絶対パスを提示して終わる
- **ブラウザ（Chromium）は sandbox の中では起動できない**。`run_status` が `skipped_runner_unavailable` なら、
  通常の Terminal か cmux の自分のペインから同じコマンドを実行する。撮れなかった回は「未確認」として残し、
  通常の push は止めない

## 手順

1. 対象判定は runner に任せる。叩くだけ:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
python3 "$SCRIPTS/screendiff_range.py" --repo-root "$REPO_ROOT" --before origin/main --after HEAD --share local
# 未 commit を見たいときだけ（明示実行のみ。commit 比較の代用にしない）
python3 "$SCRIPTS/screendiff_range.py" --repo-root "$REPO_ROOT" --before HEAD --after worktree --share local
```

2. stdout の JSON の `run_status` で分岐する。**どの値でも push を止めない**:

| run_status | 報告 | 次 |
|---|---|---|
| `compared` | `comparison_html` の絶対パスと、`screens[]` の要約 | 3 へ |
| `no_ui_change` | 「UI 変更なし（対象ファイル 0 件）」1 行 | 終了 |
| `skipped_dependency_change` | 「依存変更のため比較対象外」1 行 | 終了（撮影成功に数えない） |
| `skipped_runner_unavailable` | 「ブラウザ起動不可。Terminal から再実行」1 行 | 本人へ再実行の 1 行を渡す |
| `failed` | `reason` と `out_dir` のログの場所 | 直せるなら直して再実行 |

3. `compared` のときは comparison.html を**自分で開いて目視**し、画面ごとに 1 行で差分を書く。
   次の注記は**必ず要約に残す**（落とすと成功偽装になる）:
   - `compare_status: unresolvable` の画面は「比較不能（理由）」。差分なしと書かない
   - `representative: true` の画面は「代表画面のみ。変更箇所が写っていない可能性」
   - `unsupported[]` があれば「未対応: <ファイル>（Simulator で確認）」
   - `inputs_changed_during_capture: true` なら「撮影中に作業ツリーが変わった。撮り直し」
   - データ変動の注記（API を固定していない）

4. 撮り直しは同じコマンド。出力は実行ごとに `output_dir/range/<時刻>/` に分かれ、過去は消さない。

## やらないこと

- `gh pr comment` / `gh pr edit` / PR 本文の更新（範囲モードには PR が無い。share_plan が forbidden にする）
- 作業ツリーでの `git checkout` / `git stash`（両側とも隔離 snapshot からビルドする）
- 比較不能・未対応・代表画面の注記を省いた「差分なし」の報告
