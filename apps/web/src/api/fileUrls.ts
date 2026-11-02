/** 媒体签名取址（方案 A 前端侧）。
 *
 * **背景**：产物文件不允许用登录令牌保护（浏览器给 `<img src>`/`<video src>`
 * 发请求时**不会带 Authorization 头**），所以后端改成签发**短时签名链接**。
 * 本模块是前端取址的签名层：组件继续调 `fileUrl(id)` / `signedRawFileUrl(rel)`，
 * 签名有没有、什么时候过期都由这里管——46 个调用点不用各改一遍。
 *
 * **降级是有意的**：没有签名时返回未签名的旧地址。这样在
 * `AVS_MEDIA_REQUIRE_SIGNATURE` 关闭（默认）时行为与改造前完全一致；
 * 打开后由本模块申请签名，组件下次渲染即为签名地址。
 */

import { api } from "./client";

/** artifact_id → 带签名的相对地址 */
const cache = new Map<string, string>();
/** /files 下的相对路径 → 带签名的相对地址 */
const fileCache = new Map<string, string>();
/** 同刻多个组件取址时合并成一次请求 */
let inflight: Promise<void> | null = null;
let pendingIds = new Set<string>();
let pendingFiles = new Set<string>();
/** 签名过期时间（秒级时间戳）；0 = 未知 */
let expiresAt = 0;
/** 提前续签窗口：过期前 10 分钟换新签名，避免播放到一半 403 */
const RENEW_BEFORE_SEC = 600;

/** 缓存就绪通知：签名到货后广播，组件据此重渲染拿到带签名的地址 */
export const SIGNED_URLS_READY = "avs:signed-urls-ready";

/** 当前是否已有可用签名（调试/降级判断用） */
export function hasSignedUrls(): boolean {
  return cache.size > 0 || fileCache.size > 0;
}

function normalizeRel(relPath: string): string {
  return (relPath || "").replace(/\\/g, "/").replace(/^\/+/, "");
}

/** 是否该续签（无签名 / 临近过期） */
function needsRenew(): boolean {
  if (expiresAt === 0) return true;
  return Math.floor(Date.now() / 1000) >= expiresAt - RENEW_BEFORE_SEC;
}

/**
 * 批量申请/续签签名。
 *
 * - 只签**传进来的 id**（不猜"当前页面有什么"，避免签一堆用不到的东西）；
 * - 同刻多次调用合并成一次请求；
 * - 失败静默：拿不到签名时组件回退未签名地址（严格模式未开时完全无感）。
 */
export function primeSignedUrls(artifactIds: string[] = [],
                                files: string[] = []): Promise<void> {
  for (const id of artifactIds) {
    if (id) pendingIds.add(id);
  }
  for (const f of files) {
    const rel = normalizeRel(f);
    if (rel) pendingFiles.add(rel);
  }
  if (typeof window === "undefined") return Promise.resolve();
  if (inflight) return inflight;
  if (pendingIds.size === 0 && pendingFiles.size === 0 && !needsRenew()) {
    return Promise.resolve();
  }
  const ids = [...pendingIds];
  const rels = [...pendingFiles];
  pendingIds = new Set();
  pendingFiles = new Set();
  inflight = (async () => {
    try {
      const r = await api.signMedia({ artifact_ids: ids, files: rels });
      for (const [id, url] of Object.entries(r.artifacts ?? {})) {
        if (url) cache.set(id, url);
      }
      for (const [rel, url] of Object.entries(r.files ?? {})) {
        if (url) fileCache.set(normalizeRel(rel), url);
      }
      expiresAt = r.expires_at || 0;
      window.dispatchEvent(new CustomEvent(SIGNED_URLS_READY));
    } catch {
      // 静默：严格模式未开时不需要签名，失败不影响显示
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

/** 取产物带签名的地址（无签名返回空串，调用方自行回退） */
export function cachedSignedUrl(artifactId: string): string {
  return cache.get(artifactId) ?? "";
}

/** 取 /files 相对路径的签名地址（无则空串） */
export function cachedSignedFileUrl(relPath: string): string {
  return fileCache.get(normalizeRel(relPath)) ?? "";
}

/** 清空缓存（登出/切项目时调，避免把旧项目签名带过去） */
export function clearSignedUrls(): void {
  cache.clear();
  fileCache.clear();
  pendingIds = new Set();
  pendingFiles = new Set();
  expiresAt = 0;
  inflight = null;
}
