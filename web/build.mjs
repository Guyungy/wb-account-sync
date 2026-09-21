/**
 * 把静态资源复制进 `dist/`，让 `web/dist` 成为**自包含**的静态站点。
 *
 * 为什么这一步不能省：Tauri 的 `frontendDist` 和 Rust 侧的资产嵌入都要求
 * 一个目录里既有 HTML/CSS 又有编译好的 JS。少一样，部署时就会变成
 * "页面出来了但什么都没加载" —— 而且看起来像业务 bug。
 */

import { copyFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "dist");

const STATIC = ["index.html", "style.css"];

await mkdir(dist, { recursive: true });
for (const name of STATIC) {
  await copyFile(join(here, name), join(dist, name));
  process.stdout.write(`  复制 ${name} → dist/\n`);
}
process.stdout.write(`静态站点已就绪：${dist}\n`);
