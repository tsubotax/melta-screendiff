#!/usr/bin/env bash
# 同梱テストを全部走らせる。Python標準ライブラリのみで動く（撮影エンジンは不要）。
#
# 実行: bash skills/screendiff/scripts/run_tests.sh
set -u

cd "$(dirname "$0")" || exit 1

TESTS=(
  test_load_config.py
  test_resolve_screens.py
  backends/web/test_capture.py
  backends/ios/test_ax_dup_check.py
)

failed=0
for t in "${TESTS[@]}"; do
  echo "── $t"
  if ! python3 "$t" > /tmp/screendiff-test-$$.log 2>&1; then
    failed=1
    echo "❌ FAILED: $t"
    cat /tmp/screendiff-test-$$.log
  else
    tail -1 /tmp/screendiff-test-$$.log
  fi
done
rm -f /tmp/screendiff-test-$$.log

if [ "$failed" -eq 0 ]; then
  echo "✅ 全テスト PASSED"
else
  echo "❌ 失敗したテストがあります"
fi
exit "$failed"
