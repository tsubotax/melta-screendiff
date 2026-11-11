---
name: screendiff
description: "UI変更の対象画面をBefore/Afterで実撮影し、横並びの比較HTMLを作る（Codex版）。『Before/Afterを撮って』『見た目の差分を出して』『screendiff』『push前に画面を比べて』で使う。main直pushではpush前のorigin/main..HEADを比較する。UIを含まない変更や静的レビューで足りる変更には使わない。"
---

# screendiff（Codex版・範囲モード）

目的は、ユーザーが**今回変更した画面のBefore/Afterを確認できること**。撮影の成功だけでは完了にしない。
対象の画面と状態は担当者が変更内容から決め、runnerは解決・撮影の補助に使う。
PRへの書き込みは行わず、`--share local`でローカルHTMLを渡す。

## 前提と実行経路

- 対象repoの `.claude/screendiff.json` または `~/.config/melta-screendiff/<repo名>.json` に `range` と `screens` が必要。無ければ設定不足を報告する。
- `SCRIPTS` はmelta-screendiffの `skills/screendiff/scripts`。開発中の実体は `/Users/tsubotax/My_Data/08_Melta/melta-screendiff/skills/screendiff/scripts`、plugin配置なら実際の版を確認する。
- 既存WIPを保持し、Before/Afterは隔離snapshotからビルドする。作業ツリーのcheckoutやstashで切り替えない。
- ブラウザ起動は対象repoのsandbox・復旧手順に従う。権限拒否を別Terminalで回避しない。

## 撮影前に対象を決める

会話とdiffから、変更を確認できる画面・URL（動的IDやqueryを含む）・状態を特定する。通常は短い作業メモで足り、別の計画書や承認工程は増やさない。

- 何が変わるか（例: 情報カードが消え、名前が固定ヘッダへ移る）。
- どの入口・状態で見えるか。ボトムナビの有無など変更に関わる違いだけを選ぶ。
- 同じ表示条件を両側で用意できるか。データ、機能flag、認証、viewportを揃え、対象部品が実際に使われる条件を確認する。

設定とresolverの対応を確認する。動的routeの `fixed_params` 不足、共通部品の無関係な `representative` への振り分け、API無効で別の見本部品へ入るケースに注意する。
**代表画面を撮れることは、変更した部品が写る根拠にならない。**

対応が足りなければ、既存runnerの `--config` で対象用設定を渡すか、隔離ビルドを対象URL・操作で直接撮影する。恒久設定やrunner本体の改修は別途依頼範囲で判断する。動的URLは設定を足してもexportのHTML判定で撮影不能になることがあるため、実描画まで確認する。
一時設定・撮影補助スクリプトは一時領域に置き、作成者が撮影・検証後に削除する。証跡は対象repoの保管規則に従う。

## 撮影

通常の未push範囲:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
python3 "$SCRIPTS/screendiff_range.py" --repo-root "$REPO_ROOT" --before origin/main --after HEAD --share local
# 対象用設定が必要なら --config <設定JSONの絶対パス> を追加
```

push後の撮り直しは、元のBefore/AfterのSHAを指定する。進んだorigin/mainを使って差分を消さない。
未commit比較は明示時だけ `--before HEAD --after worktree`。commit比較の代用にはしない。

runnerの出力は次のように扱う。撮影結果だけでpush可否を決めず、repoの完了条件を優先する。

| `run_status` | 対処 |
|---|---|
| `compared` | 撮影処理の成功。下の対象確認を行ってから完了を判断する |
| `no_ui_change` | diffと一致するときだけ「UI変更なし」。意図した変更があるなら範囲・対象判定を調べる |
| `skipped_dependency_change` | 依存変更のため未撮影と報告する |
| `skipped_runner_unavailable` | 起動不可として、repoが認める実行経路で復旧する。未撮影を成功扱いしない |
| `failed` | reasonとログを確認する。依頼範囲で直せる原因を修正して撮り直す |

## 比較の受け入れと提示

HTMLと実画像を開き、予定した画面・状態と照合する。

- Before/Afterが同じ画面・データ条件で、変更した部品を含むことを確認する。エラー画面や未完了の読み込みを対象画面の代わりにしない。
- 変更ごとに「画像のどこに何が変わったか」を説明できることを完了条件にする。見た目が同じなら期待どおりか、対象違い・flag・データ不足かを判断する。
- **代表画面のみ・対象欠落・別実装の見本しか撮れていない結果は『変更箇所未確認』または『一部確認』**。注記だけ付けて対象の比較完了にはしない。依頼範囲で対象を撮り直す。撮れない場合は、欠けた対象と原因を具体的に報告する。
- `unsupported` / `unresolved` / `compare_status: unresolvable` は対象単位で残す。`inputs_changed_during_capture` がtrueなら入力を固定して撮り直す。
- 比較ページの主掲載は変更対象だけにする。無関係な代表画面で埋めない。周辺画面の回帰確認が必要なら別枠にし、対象確認と区別する。
- データ変動、見本データ、画像fallback、実API経路未確認など、比較の意味を変える条件をページと最終報告に明記する。

最終報告には比較HTMLの絶対パス、確認できた変更、未確認があればその対象を含める。
既存ページの修正依頼なら元のURLから修正版を見られるようにし、旧結果は保管規則に従って残す。生のrunner結果を成功に書き換えない。
Artifact発行、PRコメント、PR本文更新は行わない。
