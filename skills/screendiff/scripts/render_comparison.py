#!/usr/bin/env python3
"""Before/After画面キャプチャ画像とPRメタデータからBefore/After比較HTMLを生成する。

画像はディスク上のPNGを直接base64エンコードしてHTMLに埋め込む（AIの会話コンテキストを
一切通さない）。プレースホルダー方式（str.replace）。テンプレートのCSSに{}を含むため
str.format()は使わない。

使い方:
    python3 render_comparison.py --manifest manifest.json --output comparison.html

manifest.jsonの形式は本ファイル末尾の EXAMPLE_MANIFEST と docs/contracts.md を参照。
"""
import argparse
import base64
import html
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = SCRIPT_DIR / "template.html"

# 任意の画面ステータスバッジ。manifest の status がこの辞書に無い場合はバッジを出さない
STATUS_BADGE_LABEL = {
    "official": "✅ Official",
    "exploring": "🔍 Exploring",
    "archived": "🗄 Archived",
}


def encode_image(path_str: str) -> str:
    path = Path(path_str)
    data = path.read_bytes()
    return base64.b64encode(data).decode("ascii")


def render_shot_frame(path_str: str, empty_label: str) -> str:
    if not path_str:
        return f'<div class="shot-frame empty">{empty_label}</div>'
    if not Path(path_str).exists():
        # capture失敗・manifest誤り等で画像が無くてもHTML全体の生成は続ける
        return f'<div class="shot-frame empty">画像ファイルが見つからない: {html.escape(Path(path_str).name)}</div>'
    b64 = encode_image(path_str)
    return f'<div class="shot-frame"><img src="data:image/png;base64,{b64}" alt=""></div>'


def capture_paths(screen: dict, side: str) -> list:
    """{side}_paths（複数ページ）を返す。旧schemaの {side}_path 単数キーも1要素配列として扱う。"""
    paths = screen.get(f"{side}_paths")
    if paths is not None:
        return [p for p in paths if p]
    single = screen.get(f"{side}_path")
    return [single] if single else []


def render_paged_column(paths: list, side: str, total_pages: int, missing_label: str) -> str:
    """1カラム分（Before or After）のページ積み。sideは 'before'|'after'。"""
    tag_label = {"before": "Before", "after": "After"}[side]
    blocks = []
    for i in range(total_pages):
        page_note = f' <span class="page-num">ページ {i + 1}/{total_pages}</span>' if total_pages > 1 else ""
        frame = (
            render_shot_frame(paths[i], "画像なし")
            if i < len(paths)
            else f'<div class="shot-frame empty">{missing_label}</div>'
        )
        blocks.append(f'''
          <div class="page-block">
            <div class="col-label"><span class="col-tag {side}">{tag_label}</span>{page_note}</div>
            {frame}
          </div>''')
    return "\n".join(blocks)


def lint_error_count(screen: dict) -> int:
    # 旧schema（ds_error_count）後方互換
    return screen.get("lint_error_count", screen.get("ds_error_count", 0)) or 0


def render_screen_card(screen: dict) -> str:
    # screen_idはid属性/アンカーに使うため、kebab-case想定外の文字は落としてサニタイズする
    # （エスケープでは属性が壊れるため）。title/descriptionはPR本文やAIの自由記述に
    # 由来しうるためHTMLエスケープする。
    screen_id = re.sub(r"[^a-zA-Z0-9_-]", "", screen["screen_id"])
    title = html.escape(screen.get("title", screen_id))
    status = screen.get("status", "")
    kind = screen.get("kind", "changed")  # "changed" | "new" | "removed"
    description = html.escape(screen.get("description", ""))
    lint_ng = lint_error_count(screen) > 0

    badges = []
    if status in STATUS_BADGE_LABEL:  # class属性に入るため既知の値以外はバッジを出さない
        badges.append(f'<span class="badge {status}">{STATUS_BADGE_LABEL[status]}</span>')
    if kind == "new":
        badges.append('<span class="badge new">🆕 新規画面</span>')
    elif kind == "removed":
        badges.append('<span class="badge removed">🗑 削除された画面</span>')
    if lint_ng:
        badges.append(f'<span class="badge lint-ng">LINT ERROR {lint_error_count(screen)}</span>')

    before_paths = capture_paths(screen, "before")
    after_paths = capture_paths(screen, "after")

    if kind == "new":
        compare_class = "compare single-column"
        total = max(len(after_paths), 1)
        body = f'''
          <div>{render_paged_column(after_paths or [""], "after", total, "画像なし")}
          </div>'''
    elif kind == "removed":
        compare_class = "compare single-column"
        total = max(len(before_paths), 1)
        body = f'''
          <div>{render_paged_column(before_paths or [""], "before", total, "画像なし")}
          </div>'''
    else:
        compare_class = "compare"
        total = max(len(before_paths), len(after_paths), 1)
        body = f'''
          <div>{render_paged_column(before_paths or [""], "before", total, "Beforeはこのスクロール位置に届かない（コンテンツが短い）")}
          </div>
          <div>{render_paged_column(after_paths or [""], "after", total, "Afterはこのスクロール位置に届かない（コンテンツが短い）")}
          </div>'''

    notes = []
    if kind == "changed" and before_paths and after_paths and len(before_paths) != len(after_paths):
        notes.append(
            f"ℹ️ ページ数が Before {len(before_paths)} / After {len(after_paths)} で異なる"
            "（スクロール可能なコンテンツ高さが変化）"
        )
    for side, label in (("before", "Before"), ("after", "After")):
        if screen.get(f"{side}_truncated"):
            notes.append(f"⚠️ {label} は撮影上限に達したため末尾まで写っていない可能性あり（終端未検出）")
        if screen.get(f"{side}_fallback_single"):
            notes.append(f"⚠️ {label} は全域撮影が使えずファーストビュー1枚のみ（スクロール領域は未撮影）")
        if screen.get(f"{side}_paging_failed"):
            notes.append(f"⚠️ {label} はページ送りが途中で失敗（撮影済みページ以降のスクロール領域は未撮影の可能性）")
    notes_html = "".join(f'<div class="page-note">{n}</div>' for n in notes)

    desc_html = f'<div class="screen-desc">{description}</div>' if description else ""

    return f'''
<div class="screen-card" id="screen-{screen_id}">
  <div class="screen-card-head">
    <h2>{title}</h2>
    {" ".join(badges)}
  </div>
  <div class="screen-id">{screen_id}</div>
  <div class="{compare_class}">{body}
  </div>
  {notes_html}
  {desc_html}
</div>'''


def render(manifest: dict, template_text: str) -> str:
    screens = manifest["screens"]
    screen_cards = "\n".join(render_screen_card(s) for s in screens)
    toc_links = "\n".join(
        f'<a href="#screen-{re.sub(r"[^a-zA-Z0-9_-]", "", s["screen_id"])}">{html.escape(s.get("title", s["screen_id"]))}</a>'
        for s in screens
    )

    build_status = manifest.get("build_status", "unknown")
    build_status_class = "ok" if "SUCCEEDED" in build_status.upper() else "bad"
    # 旧schema（ds_validation_status）後方互換
    validation_status = manifest.get("validation_status", manifest.get("ds_validation_status", ""))
    validation_class = "ok" if "PASSED" in validation_status.upper() or "ERROR 0" in validation_status.upper() else "bad"

    replacements = {
        "{{PR_NUMBER}}": str(manifest.get("pr_number", "")),
        "{{PR_TITLE}}": html.escape(manifest.get("pr_title", "")),
        "{{PR_URL}}": html.escape(manifest.get("pr_url", "")),
        "{{BASE_REF}}": html.escape(manifest.get("base_ref", "")),
        "{{HEAD_REF}}": html.escape(manifest.get("head_ref", "")),
        "{{BUILD_STATUS}}": html.escape(build_status),
        "{{BUILD_STATUS_CLASS}}": build_status_class,
        "{{VALIDATION_STATUS}}": html.escape(validation_status or "—"),
        "{{VALIDATION_STATUS_CLASS}}": validation_class if validation_status else "",
        "{{SCREEN_COUNT}}": str(len(screens)),
        "{{GENERATED_AT}}": manifest.get("generated_at", ""),
        "{{TOC_LINKS}}": toc_links,
        "{{SCREEN_CARDS}}": screen_cards,
    }
    rendered = template_text
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="manifest.jsonのパス")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="テンプレートHTMLのパス")
    parser.add_argument("--output", required=True, help="出力HTMLのパス")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    template_text = Path(args.template).read_text(encoding="utf-8")
    rendered_html = render(manifest, template_text)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered_html, encoding="utf-8")
    print(f"生成しました: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


EXAMPLE_MANIFEST = {
    "pr_number": 42,
    "pr_title": "ホーム画面にお知らせカルーセルを追加",
    "pr_url": "https://github.com/example/app/pull/42",
    "base_ref": "main",
    "head_ref": "feature/home-carousel",
    "build_status": "SUCCEEDED",
    "validation_status": "PASSED (ERROR 0)",  # 任意。無ければ「—」表示
    "generated_at": "2026-07-24T16:00:00+09:00",
    "screens": [
        {
            "screen_id": "home",
            "title": "ホーム",
            "status": "official",  # 任意（official/exploring/archived 以外はバッジ非表示）
            "kind": "changed",  # "changed" | "new" | "removed"
            # ページング撮影の複数ページ。旧schemaの before_path/after_path（単数）も引き続き有効
            "before_paths": ["/tmp/before-home-p1.png", "/tmp/before-home-p2.png"],
            "after_paths": ["/tmp/after-home-p1.png", "/tmp/after-home-p2.png", "/tmp/after-home-p3.png"],
            # capture backend の truncated / fallback_single / paging_failed をそのまま渡す（任意）
            "before_truncated": False,
            "after_truncated": False,
            "description": "上タブ直下にお知らせカルーセル（3枚）を新規追加。",
            "lint_error_count": 0,  # 任意
        }
    ],
}
