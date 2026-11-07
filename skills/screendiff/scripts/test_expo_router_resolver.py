#!/usr/bin/env python3
"""resolvers/expo_router.py の回帰テスト（standalone・スキル同梱）。

実行:
  python3 skills/screendiff/scripts/test_expo_router_resolver.py

守っているのは **子画面の変更を親画面の比較で「確認済み」にしないこと**。
設計レビューで 3 周連続残った事故の形（`(tabs)/search/project/[id].tsx` を `/melta/search` に
直接解決する）を、動的ルートは unsupported、layout と共通部品は representative、
静的ルートだけ direct、という分類で固定する。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "resolvers" / "expo_router.py"

APP = "mobile/src/app"
ROUTE_FILES = [
    f"{APP}/melta/_layout.tsx",
    f"{APP}/melta/(tabs)/_layout.tsx",
    f"{APP}/melta/(tabs)/home.tsx",
    f"{APP}/melta/(tabs)/search/index.tsx",
    f"{APP}/melta/(tabs)/search/project/[id].tsx",
    f"{APP}/melta/(tabs)/search/kit-search/detail.tsx",
    f"{APP}/melta/workshop.tsx",
    f"{APP}/melta/settings.tsx",
    f"{APP}/melta/+html.tsx",
    f"{APP}/(tabs)/home.tsx",      # 旧ルート（prefix の外）
    f"{APP}/lab/home-melta.tsx",   # prefix の外
]

CONFIG = {
    "screens": {"expo_router": {
        "app_dir": APP,
        "route_prefix": "/melta",
        "feature_map": [
            {"pattern": "^mobile/src/melta/(workshop|completion|inventory)/", "screens": ["/melta/workshop"]},
        ],
        "representative": ["/melta/home"],
        "titles": {"/melta/home": "ホーム", "/melta/workshop": "工房"},
        "ready_selectors": {"/melta/home": "[data-testid=screen-home-ready]"},
        "fixed_params": {},
    }},
}


def _run(repo: Path, config: dict, *changed) -> tuple[int, dict]:
    cfg = repo / "config.json"
    cfg.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--config-json", str(cfg), "--repo-root", str(repo), *changed],
                          capture_output=True, text=True)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"_stdout": proc.stdout, "_stderr": proc.stderr}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        for f in ROUTE_FILES:
            p = self.repo / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("export default function S(){}\n")

    def tearDown(self):
        self._tmp.cleanup()

    def paths(self, out):
        return {r["path"]: r for r in out["resolved"]}


class TestDirect(Base):
    def test_static_route_is_direct(self):
        code, out = _run(self.repo, CONFIG, f"{APP}/melta/workshop.tsx")
        self.assertEqual(code, 0, out)
        p = self.paths(out)
        self.assertEqual(list(p), ["/melta/workshop"])
        self.assertFalse(p["/melta/workshop"]["representative"])
        self.assertFalse(p["/melta/workshop"]["also_direct"])
        self.assertEqual(p["/melta/workshop"]["title"], "工房")
        self.assertEqual(p["/melta/workshop"]["screen_id"], "melta-workshop")
        self.assertEqual(out["unsupported"], [])

    def test_group_and_index_are_stripped(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/(tabs)/home.tsx", f"{APP}/melta/(tabs)/search/index.tsx")
        self.assertEqual(sorted(self.paths(out)), ["/melta/home", "/melta/search"])

    def test_ready_selector_is_passed_through(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/(tabs)/home.tsx")
        self.assertEqual(self.paths(out)["/melta/home"]["ready_selector"], "[data-testid=screen-home-ready]")


class TestChildScreensAreNotParents(Base):
    """本丸。子画面の変更が親画面に direct 解決されない。"""

    def test_dynamic_child_is_unsupported_not_search(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/(tabs)/search/project/[id].tsx")
        self.assertEqual(out["resolved"], [])
        self.assertEqual(len(out["unsupported"]), 1)
        self.assertEqual(out["unsupported"][0]["route"], "/melta/search/project/[id]")
        self.assertIn("動的セグメント", out["unsupported"][0]["reason"])

    def test_static_child_is_its_own_route(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/(tabs)/search/kit-search/detail.tsx")
        self.assertEqual(list(self.paths(out)), ["/melta/search/kit-search/detail"])

    def test_fixed_params_promote_dynamic_to_direct(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["screens"]["expo_router"]["fixed_params"] = {"/melta/search/project/[id]": {"id": "demo-1"}}
        _, out = _run(self.repo, cfg, f"{APP}/melta/(tabs)/search/project/[id].tsx")
        self.assertEqual(list(self.paths(out)), ["/melta/search/project/demo-1"])
        self.assertEqual(out["unsupported"], [])


class TestRepresentative(Base):
    def test_feature_dir_maps_to_screen_as_representative(self):
        _, out = _run(self.repo, CONFIG, "mobile/src/melta/completion/form.tsx")
        p = self.paths(out)
        self.assertEqual(list(p), ["/melta/workshop"])
        self.assertTrue(p["/melta/workshop"]["representative"])

    def test_unmapped_component_falls_back_to_default_representative(self):
        _, out = _run(self.repo, CONFIG, "mobile/src/melta/components/project-start.tsx")
        p = self.paths(out)
        self.assertEqual(list(p), ["/melta/home"])
        self.assertTrue(p["/melta/home"]["representative"])

    def test_mixed_direct_and_representative_keeps_both_flags(self):
        """home 本体と共通部品を同時に変更しても、代表注記が入力順で落ちない"""
        for order in ((f"{APP}/melta/(tabs)/home.tsx", "mobile/src/melta/components/x.tsx"),
                      ("mobile/src/melta/components/x.tsx", f"{APP}/melta/(tabs)/home.tsx")):
            _, out = _run(self.repo, CONFIG, *order)
            p = self.paths(out)["/melta/home"]
            self.assertFalse(p["representative"], order)
            self.assertTrue(p["also_direct"], order)
            self.assertEqual(len(p["source_files"]), 2)

    def test_layout_targets_static_routes_directly_under_it(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/(tabs)/_layout.tsx")
        p = self.paths(out)
        # (tabs) はグループなので /melta 直下の静的ルート = home / search / workshop / settings
        self.assertEqual(sorted(p), ["/melta/home", "/melta/search", "/melta/settings", "/melta/workshop"])
        self.assertTrue(all(v["representative"] for v in p.values()))

    def test_layout_includes_its_own_index_route(self):
        """post/_layout.tsx の変更は post/index.tsx（= /melta/post）も代表に含める。既定代表へ逃げない"""
        for f in ("melta/post/_layout.tsx", "melta/post/index.tsx", "melta/post/drafts.tsx", "melta/post/[id].tsx"):
            p = self.repo / APP / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("export default function S(){}\n")
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/post/_layout.tsx")
        self.assertEqual(sorted(self.paths(out)), ["/melta/post", "/melta/post/drafts"])

    def test_no_representative_and_no_match_is_unsupported(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["screens"]["expo_router"]["representative"] = []
        _, out = _run(self.repo, cfg, "mobile/src/melta/components/x.tsx")
        self.assertEqual(out["resolved"], [])
        self.assertEqual(len(out["unsupported"]), 1)


class TestUnsupported(Base):
    def test_special_files(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/+html.tsx")
        self.assertEqual(out["resolved"], [])
        self.assertEqual(len(out["unsupported"]), 1)

    def test_outside_prefix(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/(tabs)/home.tsx", f"{APP}/lab/home-melta.tsx")
        self.assertEqual(out["resolved"], [])
        self.assertEqual({u["route"] for u in out["unsupported"]}, {"/home", "/lab/home-melta"})

    def test_native_only_platform_file_is_unsupported(self):
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/about.ios.tsx", f"{APP}/melta/about.android.tsx")
        self.assertEqual(out["resolved"], [])
        self.assertEqual(len(out["unsupported"]), 2)
        self.assertIn("native", out["unsupported"][0]["reason"])


class TestPlatformSuffix(Base):
    def test_web_suffix_is_stripped_from_route(self):
        """about.web.tsx は /melta/about（/melta/about.web ではない）"""
        _, out = _run(self.repo, CONFIG, f"{APP}/melta/about.web.tsx")
        p = self.paths(out)
        self.assertEqual(list(p), ["/melta/about"])
        self.assertFalse(p["/melta/about"]["representative"])


class TestConfigErrors(unittest.TestCase):
    def test_missing_app_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = _run(Path(tmp), {"screens": {}}, "x.tsx")
        self.assertEqual(code, 1)
        self.assertIn("app_dir", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
