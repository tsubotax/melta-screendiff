#!/usr/bin/env python3
"""expo-router のファイルパスから「どの画面を撮るか」を機械的に決める resolver。

読み取り専用。副作用なし。`screens.resolver_command` と同じ出力契約（docs/contracts.md §2）に
範囲モード用のフィールド（`representative` / `also_direct` / `ready_selector`）と `unsupported` 配列を足す。

⚠️ **なぜ手書きの route_map でなく resolver なのか**: expo-router はファイルパスがそのまま
ルートなので、人が正規表現で写し直すと必ずズレる（設計レビューで 3 周連続、`(tabs)/search/`
配下の子画面 9 本を親の検索トップへ解決する誤りが残った）。子画面の変更を親画面の
「差分なし」で確認済みにする事故を、分類を機械に任せて塞ぐ。

分類（変更ファイルごと）:

| 変更ファイル | 分類 | 撮る画面 |
|---|---|---|
| `app_dir` 配下のルートファイルで、動的セグメント無し・`route_prefix` 一致 | **direct** | そのルート |
| `app_dir` 配下で動的セグメント（`[id]` 等）あり | **unsupported**（`fixed_params` に値があれば direct） | — |
| `app_dir` 配下の `_layout` | **representative** | その階層直下の静的ルート（無ければ `representative` 既定） |
| `app_dir` 配下の `+html` / `+not-found` / `_` 始まり / `route_prefix` 不一致 | **unsupported** | — |
| `app_dir` 配下の `.ios` / `.android` / `.native` 付き（native 専用） | **unsupported** | — |
| `app_dir` 配下の `.web` 付き | **direct**（`.web` を外したルート） | そのルート |
| `app_dir` 外で `feature_map` に当たる | **representative** | feature_map の画面 |
| `app_dir` 外で当たらない | **representative** | `representative` 既定の画面 |

同一画面に複数の変更ファイルが当たったときの集約: `representative` は **全部が代表扱いのときだけ true**、
1 件でも direct が含まれれば `also_direct: true`（入力順で注記を落とさない）。

使い方:
    python3 expo_router.py --config-json <load_config 出力> <変更ファイル...>
"""
import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath

ROUTE_EXT = (".tsx", ".ts", ".jsx", ".js")
DYNAMIC_SEG = re.compile(r"\[[^\]]+\]")
# プラットフォーム別ファイル（about.web.tsx / about.ios.tsx）。web 用はルート名から拡張子を外す。
# native 専用は web export に入らないので撮れない（unsupported）
WEB_PLATFORM_SUFFIX = ".web"
NATIVE_PLATFORM_SUFFIXES = (".ios", ".android", ".native")


def emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def route_from_file(app_dir: str, rel: str) -> tuple[str | None, str]:
    """app_dir 相対のルートファイルから URL パスを作る。(path, kind) を返す。

    kind: "route" | "layout" | "special"（+html 等）| "not_route"（拡張子外）| "native_only"（.ios/.android/.native）
    """
    inner = rel[len(app_dir):].lstrip("/")
    p = PurePosixPath(inner)
    if p.suffix not in ROUTE_EXT:
        return None, "not_route"
    name = p.stem
    stem = PurePosixPath(name)
    if stem.suffix in NATIVE_PLATFORM_SUFFIXES:
        return None, "native_only"
    if stem.suffix == WEB_PLATFORM_SUFFIX:
        name = stem.stem
    parts = [seg for seg in p.parent.parts if not (seg.startswith("(") and seg.endswith(")"))]
    if name.startswith("+") or (name.startswith("_") and name != "_layout"):
        return None, "special"
    if name == "_layout":
        return "/" + "/".join(parts), "layout"
    if name != "index":
        parts.append(name)
    return "/" + "/".join(parts), "route"


def screen_id_of(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", path.strip("/")).strip("-") or "root"


def list_static_routes(repo_root: Path, app_dir: str, route_prefix: str) -> list[str]:
    """app_dir 配下の全ルートファイルから、動的セグメント無し・prefix 一致の静的ルート一覧。"""
    base = repo_root / app_dir
    routes = set()
    if not base.is_dir():
        return []
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.suffix not in ROUTE_EXT:
            continue
        rel = f"{app_dir}/{f.relative_to(base).as_posix()}"
        path, kind = route_from_file(app_dir, rel)
        if kind == "route" and path and not DYNAMIC_SEG.search(path) and path.startswith(route_prefix):
            routes.add(path)
    return sorted(routes)


def classify(cfg: dict, repo_root: Path, changed: list[str]) -> dict:
    app_dir = cfg["app_dir"].rstrip("/")
    prefix = cfg.get("route_prefix", "/")
    feature_map = cfg.get("feature_map", [])
    default_rep = cfg.get("representative", [])
    fixed = cfg.get("fixed_params", {})
    titles = cfg.get("titles", {})
    ready = cfg.get("ready_selectors", {})
    static_routes = list_static_routes(repo_root, app_dir, prefix)

    hits = []  # (path, source_file, representative: bool)
    unsupported = []
    for rel in changed:
        if rel.startswith(app_dir + "/"):
            path, kind = route_from_file(app_dir, rel)
            if kind in ("not_route",):
                # 画像・JSON 等。ルートの横に置かれた資産は代表画面へ
                for p in default_rep:
                    hits.append((p, rel, True))
                continue
            if kind == "special":
                unsupported.append({"source_file": rel, "reason": "expo-router の特殊ファイル（+html / +not-found / _ 始まり）"})
                continue
            if kind == "native_only":
                unsupported.append({"source_file": rel, "reason": "native 専用のプラットフォーム別ファイル（web export に入らない。Simulator で確認）"})
                continue
            if kind == "layout":
                # その階層のルート自身（直下の index.tsx = path）と、1 段下の静的ルート
                base = path.rstrip("/")
                targets = [r for r in static_routes
                           if r == path or (r.startswith(base + "/") and r.count("/") == base.count("/") + 1)]
                if not targets:
                    targets = list(default_rep)
                if not targets:
                    unsupported.append({"source_file": rel, "reason": "layout の配下に静的ルートが無く、representative も未設定", "route": path})
                for t in targets:
                    hits.append((t, rel, True))
                continue
            if not path.startswith(prefix):
                unsupported.append({"source_file": rel, "reason": f"route_prefix {prefix!r} の外", "route": path})
                continue
            if DYNAMIC_SEG.search(path):
                params = fixed.get(path)
                if not params:
                    unsupported.append({"source_file": rel, "reason": "動的セグメントがあり fixed_params に固定値が無い", "route": path})
                    continue
                concrete = path
                for key, value in params.items():
                    concrete = concrete.replace(f"[{key}]", str(value))
                if DYNAMIC_SEG.search(concrete):
                    unsupported.append({"source_file": rel, "reason": "fixed_params で埋まらないセグメントが残る", "route": path})
                    continue
                hits.append((concrete, rel, False))
                continue
            hits.append((path, rel, False))
            continue
        matched = False
        for entry in feature_map:
            if re.search(entry["pattern"], rel):
                matched = True
                for p in entry["screens"]:
                    hits.append((p, rel, True))
        if not matched:
            if not default_rep:
                unsupported.append({"source_file": rel, "reason": "feature_map に当たらず representative も未設定"})
            for p in default_rep:
                hits.append((p, rel, True))

    by_path: dict[str, dict] = {}
    for path, src, rep in hits:
        item = by_path.setdefault(path, {
            "screen_id": screen_id_of(path), "title": titles.get(path, path), "path": path,
            "source_files": [], "_rep": [], })
        item["source_files"].append(src)
        item["_rep"].append(rep)
    resolved = []
    for path in sorted(by_path):
        item = by_path[path]
        reps = item.pop("_rep")
        item["source_file"] = item["source_files"][0]
        # 全部が代表扱いのときだけ representative。direct と代表が混ざれば also_direct で両方を残す
        item["representative"] = all(reps)
        item["also_direct"] = any(not r for r in reps) and any(reps)
        sel = ready.get(path)
        if sel:
            item["ready_selector"] = sel
        resolved.append(item)
    return {"resolved": resolved, "unresolved": [], "unsupported": unsupported,
            "static_routes": static_routes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-json", required=True, help="load_config.py の出力")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("changed_files", nargs="*")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return emit({"error": f"config を読めません: {e}"}, 1)
    cfg = (config.get("screens") or {}).get("expo_router")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("app_dir"), str):
        return emit({"error": "screens.expo_router.app_dir が必要です"}, 1)
    return emit(classify(cfg, Path(args.repo_root).resolve(), list(args.changed_files)), 0)


if __name__ == "__main__":
    sys.exit(main())
