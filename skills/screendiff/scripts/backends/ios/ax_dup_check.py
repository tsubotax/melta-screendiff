#!/usr/bin/env python3
"""AXラベル重複チェック — sim-use describe-ui の出力から「同一ラベルの StaticText が近接して複数個」を検出する。

背景:
    SwiftUI にテキスト stroke が無いため、縁取りを「同一 Text を8方向に offset した
    コピーの重ね置き」で作る実装が生まれる。見た目は1つの文字だが、AXツリーには
    同一ラベルの StaticText が多重に乗り、VoiceOver が「2 2 2 2…」と重複読み上げする
    （実例で9重を実測）。静的 lint では offset 計算（三角関数等の実行時値）を追えない
    ため、実行時の describe-ui 出力を機械チェックする。

使い方:
    sim-use describe-ui --udid "$SIM_UDID" | python3 ax_dup_check.py
    オプション: --radius <pt>（近接判定の中心間距離。default 16）
                --min-count <n>（クラスタ成立の最小個数。default 3）

判定:
    同一ラベルの StaticText 群を貪欲クラスタリング（中心間距離 ≤ radius で連結）し、
    要素数 ≥ min-count のクラスタを重複として報告する。
    - 縁取りスタック（オフセット2pt × 8方向 + 本体）→ 確実に1クラスタになる
    - リスト/グリッドの正当な同一ラベル（例: 金額表示の横並び）→
      セル間隔が radius を超えるため拾わない（実測: 隣接セル中心間 66pt）

出力: JSON（stdout）
    {"checked": <StaticText総数>, "duplicates": [{"label", "count", "center": [x,y]}], "ok": bool}
    exit code は常に 0（advisory。パース不能時のみ 2）
"""

import argparse
import json
import re
import sys

# describe-ui の行形式: `  @25  StaticText  "14"  (263,438 19x19)`
# ラベル内の `\"` エスケープは考慮しない（sim-use 出力に現状存在しないため）
LINE_PATTERN = re.compile(
    r'@\d+\s+StaticText\s+"(?P<label>[^"]*)"\s+'
    r'\((?P<x>-?\d+),(?P<y>-?\d+)\s+(?P<w>\d+)x(?P<h>\d+)\)'
)


def cluster(points: list[tuple[float, float]], radius: float) -> list[list[int]]:
    """中心間距離 ≤ radius で連結する貪欲クラスタリング（要素数は高々数百なので O(n^2) で十分）"""
    n = len(points)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            dx = points[i][0] - points[j][0]
            dy = points[i][1] - points[j][1]
            if dx * dx + dy * dy <= radius * radius:
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--radius', type=float, default=16.0)
    ap.add_argument('--min-count', type=int, default=3)
    args = ap.parse_args()

    text = sys.stdin.read()
    if not text.strip():
        print(json.dumps({'error': 'describe-ui 出力が空です'}, ensure_ascii=False))
        return 2

    # パース可能性の検証: 非空だが describe-ui の形式でない入力
    # — 例: `Error: describe-ui failed` — を「重複なし・正常」と誤判定しない。
    # 要素行 `@N ...` が1行も無ければ describe-ui 出力ではないとみなしエラー終了する。
    # （StaticText が0個の画面は @N の Image/Button 行が存在するため誤爆しない）
    if not re.search(r'^\s*@\d+\s', text, re.MULTILINE):
        print(json.dumps(
            {'error': 'describe-ui の要素行（@N）が見つかりません。入力がエラー出力でないか確認してください',
             'head': text.strip().splitlines()[0][:120]},
            ensure_ascii=False,
        ))
        return 2

    by_label: dict[str, list[tuple[float, float]]] = {}
    checked = 0
    parsed = 0
    for m in LINE_PATTERN.finditer(text):
        parsed += 1
        label = m.group('label')
        if not label:
            continue
        cx = int(m.group('x')) + int(m.group('w')) / 2
        cy = int(m.group('y')) + int(m.group('h')) / 2
        by_label.setdefault(label, []).append((cx, cy))
        checked += 1

    # StaticText らしき行があるのに1件もパースできない場合は出力形式の変化を疑い
    # エラー終了する（形式変化時の空振り正常判定を防ぐ。空ラベルのみの画面は
    # parsed > 0 になるため誤爆しない）
    statictext_lines = len(re.findall(r'@\d+\s+StaticText\b', text))
    if statictext_lines > 0 and parsed == 0:
        print(json.dumps(
            {'error': f'StaticText 行が {statictext_lines} 件あるのに1件もパースできません。'
                      'sim-use describe-ui の出力形式が変わった可能性があります（LINE_PATTERN 要更新）'},
            ensure_ascii=False,
        ))
        return 2

    duplicates = []
    for label, points in by_label.items():
        if len(points) < args.min_count:
            continue
        for group in cluster(points, args.radius):
            if len(group) >= args.min_count:
                gx = sum(points[i][0] for i in group) / len(group)
                gy = sum(points[i][1] for i in group) / len(group)
                duplicates.append({
                    'label': label,
                    'count': len(group),
                    'center': [round(gx), round(gy)],
                })

    duplicates.sort(key=lambda d: -d['count'])
    print(json.dumps(
        {'checked': checked, 'duplicates': duplicates, 'ok': not duplicates},
        ensure_ascii=False,
    ))
    return 0


if __name__ == '__main__':
    sys.exit(main())
