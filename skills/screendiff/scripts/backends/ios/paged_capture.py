#!/usr/bin/env python3
"""iOSシミュレーター上の画面をスクロールしながらページ単位でキャプチャする。

deep link（--deeplink の {id} を screen_id で置換）で対象画面を開き、sim-useの
低速ドラッグで1ページずつスクロールしながら simctl screenshot を撮る。スクロール
終端は「連続2ショットの中央領域（ステータスバー・ホームインジケータを除く）が
一致したか」で判定する（ステータスバーの時計が毎ショット変わるため全体比較は
使えない）。

前提: sim-use CLI（なければ1ページ目のみ撮影して pages=1 で正常終了）。

使い方:
    python3 paged_capture.py --udid <UDID> --screen-id <id> \
        --deeplink "myapp://screen/{id}" \
        --out-dir <dir> --prefix before|after [--max-pages 6]

出力: 撮影結果のJSONをstdoutに1行で出す。
    {"screen_id": ..., "pages": N, "files": [...], "truncated": bool,
     "fallback_single": bool, "paging_failed": bool}
- truncated: max-pagesに達しても終端を検出できなかった（アニメーション等で
  一致判定が成立しない画面の暴走ガード）
- fallback_single: sim-use不在でページングせず1枚のみ撮影した
- paging_failed: sim-useはあるがswipe/撮影が途中で失敗し、全域をカバーできて
  いない可能性がある（撮れたページまでは files に含む）
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# iPhone 17 Pro（402x874pt）基準のドラッグ座標のデフォルト。中央X、コンテンツ領域内で
# ボトムタブ（下端~83pt）とステータスバーを避ける。他機種はCLIフラグで上書きする。
DEFAULT_SWIPE_X = 201
DEFAULT_SWIPE_START_Y = 700
DEFAULT_SWIPE_END_Y = 180
# 低速ドラッグで慣性スクロールを抑える（リリース時速度を落とす）
SWIPE_DURATION = "1.2"
SWIPE_POST_DELAY = "1.0"


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def screenshot(udid: str, path: Path) -> bool:
    r = run(["xcrun", "simctl", "io", udid, "screenshot", str(path)])
    return r.returncode == 0 and path.exists()


def png_size(path: Path) -> tuple[int, int]:
    """PNGヘッダからwidth,heightを読む（IHDR固定オフセット）。"""
    data = path.read_bytes()[:24]
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return w, h


def content_hash(path: Path, work_dir: Path) -> str:
    """ステータスバー等を除いた中央領域のハッシュ。sipsで中央クロップして比較する。"""
    w, h = png_size(path)
    margin = max(180, int(h * 0.09))  # 時計/Dynamic Island と ホームインジケータを除外
    crop_h = h - margin * 2
    cropped = work_dir / f"crop-{path.name}"
    r = run(["sips", "--cropToHeightWidth", str(crop_h), str(w), str(path), "--out", str(cropped)])
    if r.returncode != 0 or not cropped.exists():
        # クロップ失敗時は全体ハッシュにフォールバック（時計差で一致しにくくなるだけで安全側）
        return hashlib.md5(path.read_bytes()).hexdigest()
    digest = hashlib.md5(cropped.read_bytes()).hexdigest()
    cropped.unlink(missing_ok=True)
    return digest


def swipe_up(udid: str, x: int, start_y: int, end_y: int) -> bool:
    r = run([
        "sim-use", "swipe",
        "--start-x", str(x), "--start-y", str(start_y),
        "--end-x", str(x), "--end-y", str(end_y),
        "--duration", SWIPE_DURATION, "--post-delay", SWIPE_POST_DELAY,
        "--device", udid,
    ], timeout=30)
    return r.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--screen-id", required=True)
    parser.add_argument("--deeplink", required=True, help='deep linkテンプレート（例 "myapp://screen/{id}"）')
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--prefix", required=True, help="before / after")
    parser.add_argument("--max-pages", type=int, default=6)
    parser.add_argument("--settle-sec", type=float, default=2.0, help="deep link後の描画待ち")
    parser.add_argument("--swipe-x", type=int, default=DEFAULT_SWIPE_X, help="ドラッグX座標(pt)")
    parser.add_argument("--swipe-start-y", type=int, default=DEFAULT_SWIPE_START_Y, help="ドラッグ開始Y(pt)")
    parser.add_argument("--swipe-end-y", type=int, default=DEFAULT_SWIPE_END_Y, help="ドラッグ終了Y(pt)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    url = args.deeplink.replace("{id}", args.screen_id)
    r = run(["xcrun", "simctl", "openurl", args.udid, url])
    if r.returncode != 0:
        print(f"ERROR: openurl failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    subprocess.run(["sleep", str(args.settle_sec)])

    def page_path(n: int) -> Path:
        return out_dir / f"{args.prefix}-{args.screen_id}-p{n}.png"

    first = page_path(1)
    if not screenshot(args.udid, first):
        print("ERROR: screenshot failed (page 1)", file=sys.stderr)
        return 1

    files = [first]
    truncated = False
    paging_failed = False
    fallback_single = shutil.which("sim-use") is None

    if not fallback_single:
        with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
            work_dir = Path(tmp)
            prev_hash = content_hash(first, work_dir)
            for n in range(2, args.max_pages + 1):
                if not swipe_up(args.udid, args.swipe_x, args.swipe_start_y, args.swipe_end_y):
                    print("WARN: swipe failed — 以降のページ撮影を中止", file=sys.stderr)
                    paging_failed = True
                    break
                candidate = work_dir / f"candidate-p{n}.png"
                if not screenshot(args.udid, candidate):
                    print(f"WARN: screenshot failed (page {n})", file=sys.stderr)
                    paging_failed = True
                    break
                cur_hash = content_hash(candidate, work_dir)
                if cur_hash == prev_hash:
                    break  # スクロール終端（前ページと同一）
                dest = page_path(n)
                shutil.move(str(candidate), str(dest))
                files.append(dest)
                prev_hash = cur_hash
            else:
                # max-pages=1（ループ未実行）は「1枚だけ撮る」指定なのでtruncated扱いにしない
                truncated = args.max_pages >= 2

    print(json.dumps({
        "screen_id": args.screen_id,
        "pages": len(files),
        "files": [str(f) for f in files],
        "truncated": truncated,
        "fallback_single": fallback_single,
        "paging_failed": paging_failed,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
