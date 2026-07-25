#!/usr/bin/env python3
"""ax_dup_check.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/backends/ios/test_ax_dup_check.py

fixture は describe-ui の実出力形式に合わせた合成データ:
  - 縁取りスタック（同一番号 "7" ×9・オフセット±2pt）→ 1クラスタとして検出する
  - 金額 "300" の横並び（セル中心間 72pt）→ 検出しない（正当な繰り返しラベル）
  - describe-ui 形式でない入力（エラー文字列）→ exit 2（正常判定しない）
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'ax_dup_check.py'

FIXTURE_DUP = """App: Sample  390x844

[Content  y=100..760]
  @14  StaticText  "7"  (150,398 10x19)
  @15  StaticText  "7"  (152,398 10x19)
  @16  StaticText  "7"  (153,398 10x19)
  @27  StaticText  "7"  (150,400 10x19)
  @28  StaticText  "7"  (152,400 10x19)
  @29  StaticText  "7"  (154,400 10x19)
  @40  StaticText  "7"  (150,401 10x19)
  @41  StaticText  "7"  (153,401 10x19)
  @51  StaticText  "7"  (152,402 10x19)
  @57  StaticText  "300"  (40,440 23x14)
  @58  StaticText  "300"  (112,440 23x14)
  @59  StaticText  "300"  (184,440 23x14)
  @60  StaticText  "詳細を見る"  (48,520 90x19)
"""

FIXTURE_CLEAN = """App: Sample  390x844

[Content  y=100..760]
  @1  StaticText  "ホーム"  (50,780 34x40)
  @2  Image  "sample_banner"  (12,150 366x100)
  @3  Button  "追加"  (379,68 24x46)
"""


def _run(stdin_text: str):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text, capture_output=True, text=True,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload


class TestAxDupCheck(unittest.TestCase):
    def test_outline_stack_detected(self):
        code, out = _run(FIXTURE_DUP)
        self.assertEqual(code, 0)
        self.assertFalse(out['ok'])
        self.assertEqual(len(out['duplicates']), 1, 'メタガード: 縁取りスタックが検出されない＝空振り')
        self.assertEqual(out['duplicates'][0]['label'], '7')
        self.assertEqual(out['duplicates'][0]['count'], 9)

    def test_spaced_repeats_not_flagged(self):
        code, out = _run(FIXTURE_DUP)
        self.assertEqual(code, 0)
        labels = [d['label'] for d in out['duplicates']]
        self.assertNotIn('300', labels, '正当な横並び同一ラベル（72pt間隔）を誤検出している')

    def test_clean_screen_ok(self):
        code, out = _run(FIXTURE_CLEAN)
        self.assertEqual(code, 0)
        self.assertTrue(out['ok'])
        self.assertEqual(out['duplicates'], [])

    def test_error_input_exits_2(self):
        code, out = _run('Error: describe-ui failed (device not booted)\n')
        self.assertEqual(code, 2, '非describe-ui入力を正常判定してはいけない')
        self.assertIn('error', out)

    def test_empty_input_exits_2(self):
        code, out = _run('')
        self.assertEqual(code, 2)
        self.assertIn('error', out)

    def test_unparseable_statictext_exits_2(self):
        """StaticText 行があるのに1件もパースできない＝出力形式変化を空振り正常判定しない"""
        code, out = _run(
            'App: Sample  390x844\n'
            '  @1  StaticText  ラベルにクォート無し形式  frame=(10,10,20,20)\n'
        )
        self.assertEqual(code, 2)
        self.assertIn('error', out)

    def test_no_statictext_screen_ok(self):
        """StaticText が1つも無い画面（Image/Buttonのみ）は正常（ok: true）"""
        code, out = _run(
            'App: Sample  390x844\n'
            '  @1  Image  "sample_banner"  (12,150 366x100)\n'
            '  @2  Button  "追加"  (379,68 24x46)\n'
        )
        self.assertEqual(code, 0)
        self.assertTrue(out['ok'])


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print('✅ ax_dup_check self-test PASSED' if ok else '❌ ax_dup_check self-test FAILED')
    sys.exit(0 if ok else 1)
