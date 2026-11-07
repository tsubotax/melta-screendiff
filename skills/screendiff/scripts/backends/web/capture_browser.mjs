#!/usr/bin/env node
// 範囲モードの撮影。1 画面を Playwright で撮り、**撮った同じページで** pathname と表示完了条件を検査する。
//
// 既存の capture.py は疎通確認が urllib・撮影が別プロセスで、SPA のフォールバック（未知パスでも
// 200 + シェル HTML）や JS 遷移を検知できない（docs/contracts.md §3 の限界 1・2）。ここでは
// ブラウザ実行後の `location.pathname` と、画面固有の `ready_selector` の可視を要求する。
//
// 使い方:
//   node capture_browser.mjs --pw <playwright の node_modules パス> --url <URL> --out <png>
//        --expect-path </melta/home> [--ready-selector "[data-testid=...]"] [--settle-ms 1500]
//        [--ready-timeout-ms 10000] [--viewport 393x852] [--scale 3] [--no-full-page]
//
// 出力: stdout に JSON 1 行。exit 0 = ok（pathname 一致・ready 不成立なし）、2 = 比較不能（ページ段階の失敗を含む）、
//   1 = 起動失敗（stage が load / launch）。
//   ready: "none"（selector 未指定）| "ok" | "missing"、stage: "load" | "launch" | "page"
import { createRequire } from "node:module";

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : fallback;
};
const has = (name) => args.includes(name);

const pwPath = argOf("--pw");
const url = argOf("--url");
const out = argOf("--out");
const expectPath = argOf("--expect-path", "");
const readySelector = argOf("--ready-selector", "");
const settleMs = Number(argOf("--settle-ms", "1500"));
const readyTimeoutMs = Number(argOf("--ready-timeout-ms", "10000"));
const [vw, vh] = argOf("--viewport", "393x852").split("x").map(Number);
const scale = Number(argOf("--scale", "3"));
const fullPage = !has("--no-full-page");

// stage: どこで失敗したか。load（playwright が読めない）/ launch（ブラウザが立たない）は環境問題で exit 1、
// page（goto タイムアウト・保存失敗など画面固有）は当該画面の比較不能で exit 2。runner はこれで
// skipped_runner_unavailable（全体を打ち切って Terminal から再実行）と画面単位の unresolvable を分ける
const result = { ok: false, url, expect_path: expectPath, final_path: null, path_matched: false,
                 ready: readySelector ? "pending" : "none", ready_selector: readySelector || null,
                 file: null, error: null, stage: "load" };

if (!pwPath || !url || !out) {
  result.error = "--pw / --url / --out は必須";
  process.stdout.write(JSON.stringify(result) + "\n");
  process.exit(1);
}

const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require(pwPath));
} catch (e) {
  result.error = `playwright を読み込めない: ${pwPath} (${e.message})`;
  process.stdout.write(JSON.stringify(result) + "\n");
  process.exit(1);
}

let browser;
try {
  result.stage = "launch";
  browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: vw, height: vh }, deviceScaleFactor: scale,
                                         isMobile: true, hasTouch: true, colorScheme: "light" });
  const page = await ctx.newPage();
  result.stage = "page";
  await page.goto(url, { waitUntil: "load", timeout: 30000 });
  if (readySelector) {
    try {
      // 上限付き待機。settle 後の 1 回判定にしない（描画が遅いだけの画面を比較不能にしない）
      await page.waitForSelector(readySelector, { state: "visible", timeout: readyTimeoutMs });
      result.ready = "ok";
    } catch {
      result.ready = "missing";
    }
  }
  await page.waitForTimeout(settleMs);
  result.final_path = await page.evaluate(() => location.pathname);
  const norm = (p) => (p || "").replace(/\/+$/, "") || "/";
  result.path_matched = expectPath ? norm(result.final_path) === norm(expectPath) : true;
  await page.screenshot({ path: out, fullPage });
  result.file = out;
  result.ok = result.path_matched && result.ready !== "missing";
} catch (e) {
  result.error = String((e && e.message) || e);
} finally {
  if (browser) await browser.close().catch(() => {});
}
process.stdout.write(JSON.stringify(result) + "\n");
// 起動前の失敗だけ 1。ページ段階の失敗は ok=false の 2（画面単位の比較不能）
process.exit(result.error ? (result.stage === "page" ? 2 : 1) : result.ok ? 0 : 2);
