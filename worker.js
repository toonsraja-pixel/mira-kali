/**
 * Cloudflare Worker: Video Player & Zephyrix Stream Reverse Proxy
 * (Dashboard Web-Editor Compatible Syntax)
 *
 * Fixes for Byse / N1MWQ Video Embeds:
 * 1. Proxies outer embed (/e/:code, /assets/, /api/) to bysevepoin.com
 * 2. Proxies inner embed (/n1mwq/*) to n1mwq.org
 * 3. Rewrites n1mwq.org in JSON API responses to route through /n1mwq/
 * 4. React Ref Auto-Click: Injects a ref hook directly on the button in React JSX
 *    so as soon as React commits the button DOM node, it fires a native click!
 * 5. React Hook Auto-Trigger: Also triggers Q() with a small timeout on mount.
 * 6. Patches autostart:!1 -> autostart:!0,mute:!0 in JWPlayer bundle.
 * 7. Hidden background iframe detection with animated gradient & ripple loading screen.
 */

const ZEPHYRIX_TARGET  = "https://play.zephyrix.org";
const DEFAULT_REFERER  = "https://watchanimeworld.one/";
const CDN_REFERER      = "https://play.zephyrix.org/";
const BYSE_TARGET      = "https://bysevepoin.com";
const N1MWQ_TARGET     = "https://n1mwq.org";
const DORABASH_TARGET  = "https://dorabash.in";
const VIDEAS_TARGET    = "https://app.videas.fr";

addEventListener("fetch", (event) => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  const url = new URL(request.url);

  // ── 0a. N1MWQ INNER IFRAME PROXY (/n1mwq/*) ─────────────────────────────────
  if (url.pathname.startsWith("/n1mwq/") || url.pathname === "/n1mwq") {
    return handleN1mwqProxy(request, url);
  }

  // ── 0a-NEW. DORABASH PROXY (/dorabash/*) ─────────────────────────────────────
  // Routes dorabash.in requests through the Worker, bypassing Cloudflare protection.
  // Endpoints:
  //   /dorabash/search?q=SHOW_NAME           — search for a show, returns JSON [{id, title, url}]
  //   /dorabash/episodes?anime_id=ID          — get episode embed codes for a show
  //   /dorabash/page?url=PATH                 — fetch any dorabash page, returns HTML
  if (url.pathname.startsWith("/dorabash/")) {
    return handleDoraBashProxy(request, url);
  }

  // ── 0b-VIDEAS. VIDEAS EMBED AUTO-PLAY & EXTRACTOR (/embed/:uuid or /videas/:uuid) ──
  // Handles Videas embeds (e.g. https://app.videas.fr/embed/ee4be8a9-a06c-4920-bd02-77bd92e66ea6/)
  // Auto-extracts the official master HLS stream and serves an ad-free, auto-playing video player!

  // 1. Check for Videas embed URL in query params (?url=... or ?v=... or ?src=...)
  const queryEmbedParam = url.searchParams.get("url") || url.searchParams.get("v") || url.searchParams.get("src");
  if (queryEmbedParam) {
    const qvMatch = queryEmbedParam.match(/videas\.fr\/embed\/(?:media\/)?([0-9a-fA-F-]{36})/i);
    if (qvMatch) {
      const vId = qvMatch[1];
      try {
        const result = await videasExtract(vId);
        return new Response(buildHlsPlayerHtml(result.hls_url, result.title || vId, result.poster_url || ""), {
          headers: { "Content-Type": "text/html; charset=utf-8", "Access-Control-Allow-Origin": "*" }
        });
      } catch (e) {
        return new Response(`Videas extraction failed: ${e.message}`, { status: 500 });
      }
    }
  }

  // 2. JSON API route: /videas-extract/:uuid
  if (url.pathname.startsWith("/videas-extract/")) {
    const vId = url.pathname.slice("/videas-extract/".length).split("/")[0].replace(/[^0-9a-fA-F-]/g, "");
    if (!vId) return new Response(JSON.stringify({ error: "Missing Videas embed ID" }), {
      status: 400,
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
    });
    try {
      const result = await videasExtract(vId);
      return new Response(JSON.stringify(result), {
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    }
  }

  // 3. Master M3U8 route: /videas-master/:uuid.m3u8
  const videasMasterMatch = url.pathname.match(/^\/videas-master\/([0-9a-fA-F-]{36})\.m3u8$/i);
  if (videasMasterMatch) {
    const vId = videasMasterMatch[1];
    try {
      const result = await videasExtract(vId);
      const m3u8Res = await fetch(result.hls_url, {
        headers: { "User-Agent": "Mozilla/5.0", "Referer": `${VIDEAS_TARGET}/embed/${vId}/` }
      });
      const m3u8Body = await m3u8Res.text();
      return new Response(m3u8Body, {
        status: m3u8Res.status,
        headers: {
          "Content-Type": "application/vnd.apple.mpegurl",
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "public, max-age=300"
        }
      });
    } catch (e) {
      return new Response(`# ERROR: ${e.message}\n`, {
        status: 500,
        headers: { "Content-Type": "application/vnd.apple.mpegurl", "Access-Control-Allow-Origin": "*" }
      });
    }
  }

  // 4. Auto-playing Player page: /embed/:uuid or /embed/media/:uuid or /videas/:uuid
  const videasEmbedMatch = url.pathname.match(/^\/(?:embed(?:\/media)?|videas)\/([0-9a-fA-F-]{36})\/?$/i);
  if (videasEmbedMatch) {
    const vId = videasEmbedMatch[1];
    try {
      const result = await videasExtract(vId);
      return new Response(buildHlsPlayerHtml(result.hls_url, result.title || vId, result.poster_url || ""), {
        headers: { "Content-Type": "text/html; charset=utf-8", "Access-Control-Allow-Origin": "*" }
      });
    } catch (e) {
      return new Response(`Videas extraction failed: ${e.message}`, { status: 500 });
    }
  }

  // ── 0c. ABYSS/PLAYHYDRAX SERVER-SIDE EXTRACTOR (/abyss-extract/:id) ──────────
  if (url.pathname.startsWith("/abyss-extract/")) {
    const abyssId = url.pathname.slice("/abyss-extract/".length).split("/")[0];
    if (!abyssId) return new Response("Missing video id", { status: 400 });
    try {
      const result = await abyssExtract(abyssId);
      return new Response(JSON.stringify(result), {
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    }
  }

  // ── 0d. ABYSS MASTER M3U8 (/abyss-master/:id.m3u8) ───────────────────────────
  // Returns an HLS master playlist with all quality variants.
  // Each variant points to /abyss-media/:b64url.m3u8 (single-file media playlist).
  if (url.pathname.match(/^\/abyss-master\/[A-Za-z0-9_-]+\.m3u8$/)) {
    const abyssId = url.pathname.slice("/abyss-master/".length).replace(/\.m3u8$/, "");
    try {
      const result = await abyssExtract(abyssId);
      const sources = (result.sources || []).filter(s => s.status);
      if (sources.length === 0) return new Response("No sources found", { status: 404 });
      const masterM3u8 = buildAbyssMasterM3u8(sources, url.origin);
      return new Response(masterM3u8, {
        headers: {
          "Content-Type": "application/vnd.apple.mpegurl",
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "no-cache"
        }
      });
    } catch (e) {
      return new Response(`# ERROR: ${e.message}\n`, { status: 500, headers: { "Content-Type": "application/vnd.apple.mpegurl" } });
    }
  }

  // ── 0e. ABYSS MEDIA PLAYLIST (/abyss-media/:b64.m3u8) ────────────────────────
  // Wraps a proxied MP4 URL as an HLS VOD media playlist (uses /abyss-stream/ proxy).
  if (url.pathname.match(/^\/abyss-media\/[A-Za-z0-9_=-]+\.m3u8$/)) {
    const b64 = url.pathname.slice("/abyss-media/".length).replace(/\.m3u8$/, "");
    try {
      const videoUrl = atob(b64.replace(/-/g, "+").replace(/_/g, "/"));
      // Point segment at our proxy so external HLS players get correct Referer too
      const proxyUrl = `${url.origin}/abyss-stream/${b64}`;
      const mediaM3u8 = buildAbyssMediaM3u8(proxyUrl);
      return new Response(mediaM3u8, {
        headers: {
          "Content-Type": "application/vnd.apple.mpegurl",
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "no-cache"
        }
      });
    } catch (e) {
      return new Response(`# ERROR: ${e.message}\n`, { status: 400, headers: { "Content-Type": "application/vnd.apple.mpegurl" } });
    }
  }

  // ── 0e2. ABYSS STREAM PROXY (/abyss-stream/:b64) ─────────────────────────────
  // Proxies a sssrr.org MP4 with Referer: https://playhydrax.com/ so the CDN
  // accepts the request. Forwards Range headers for seeking support.
  if (url.pathname.match(/^\/abyss-stream\/[A-Za-z0-9_=-]+/)) {
    const b64 = url.pathname.slice("/abyss-stream/".length).split("/")[0];
    let videoUrl;
    try {
      videoUrl = atob(b64.replace(/-/g, "+").replace(/_/g, "/"));
    } catch (e) {
      return new Response("Invalid stream id", { status: 400 });
    }
    const proxyHeaders = new Headers();
    proxyHeaders.set("Referer",    "https://playhydrax.com/");
    proxyHeaders.set("Origin",     "https://playhydrax.com");
    proxyHeaders.set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36");
    // Forward Range header for video seeking
    const rangeHeader = request.headers.get("Range");
    if (rangeHeader) proxyHeaders.set("Range", rangeHeader);

    try {
      const upstream = await fetch(videoUrl, {
        method: "GET",
        headers: proxyHeaders,
        redirect: "follow"
      });
      const respHeaders = new Headers();
      // Pass through important headers
      for (const h of ["Content-Type","Content-Length","Content-Range","Accept-Ranges","Last-Modified","ETag"]) {
        const v = upstream.headers.get(h);
        if (v) respHeaders.set(h, v);
      }
      respHeaders.set("Access-Control-Allow-Origin", "*");
      respHeaders.set("Cache-Control", "public, max-age=3600");
      return new Response(upstream.body, {
        status: upstream.status,
        headers: respHeaders
      });
    } catch (e) {
      return new Response(`Stream proxy error: ${e.message}`, { status: 502 });
    }
  }

  // ── 0f. ABYSS PLAYER PAGE (/abyss/:id) ───────────────────────────────────────
  // Serves a quality-picker player using direct <video> with proxied MP4 URLs.
  // Proxy handles Referer injection so CDN accepts the request from the browser.
  if (url.pathname.match(/^\/abyss\/[A-Za-z0-9_-]+$/)) {
    const abyssId = url.pathname.slice("/abyss/".length);
    try {
      const result  = await abyssExtract(abyssId);
      const sources = (result.sources || []).filter(s => s.status);
      if (sources.length === 0) return new Response("No sources found", { status: 404 });
      return new Response(buildAbyssPlayerHtml(sources, abyssId, url.origin), {
        headers: { "Content-Type": "text/html; charset=utf-8", "Access-Control-Allow-Origin": "*" }
      });
    } catch (e) {
      return new Response(`Abyss extraction failed: ${e.message}`, { status: 500 });
    }
  }

  // ── 0b-NEW. BYSE SERVER-SIDE EXTRACTOR (/byse-extract/:code) ────────────────
  // Full challenge→attest→captcha/verify→playback→AES-GCM decrypt in one Worker.
  if (url.pathname.startsWith("/byse-extract/")) {
    const code = url.pathname.slice("/byse-extract/".length).split("/")[0];
    if (!code) return new Response("Missing code", { status: 400 });
    try {
      const result = await byseExtract(code);
      return new Response(JSON.stringify(result), {
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    }
  }

  // ── 0b-NEW. BYSE EMBED OVERRIDE (/e/:code) ───────────────────────────────────
  // Instead of proxying the React SPA (which requires captcha UI), do server-side
  // extraction and serve a clean HLS player page.
  if (url.pathname.match(/^\/e\/[a-z0-9]+$/i)) {
    const code = url.pathname.slice(3);
    try {
      const result = await byseExtract(code);
      const sources = result.sources || [];
      if (sources.length > 0) {
        const m3u8 = sources[0].url;
        const title = result.title || code;
        const poster = result.poster_url || "";
        return new Response(buildHlsPlayerHtml(m3u8, title, poster), {
          headers: { "Content-Type": "text/html; charset=utf-8", "Access-Control-Allow-Origin": "*" }
        });
      }
    } catch (e) {
      // Fall through to proxy if extraction fails
    }
    return handleByseProxy(request, url);
  }

  // ── 0b. BYSE OUTER EMBED PROXY ──────────────────────────────────────────────
  if (
    url.pathname.startsWith("/e/") ||
    url.pathname.startsWith("/assets/") ||
    (url.pathname.startsWith("/api/") && !url.pathname.startsWith("/api/videos/access"))
  ) {
    return handleByseProxy(request, url);
  }

  // ── 1. GRID CDN VIDEO SEGMENTS PROXY ────────────────────────────────────────
  if (url.pathname.startsWith("/grid-proxy")) {
    const targetCdnUrl = url.searchParams.get("url");
    if (!targetCdnUrl) return new Response("Missing URL", { status: 400 });

    const cdnHeaders = new Headers(request.headers);
    cdnHeaders.set("Referer", CDN_REFERER);
    cdnHeaders.set("Origin", "https://play.zephyrix.org");
    cdnHeaders.set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36");

    const res = await fetch(targetCdnUrl, { method: request.method, headers: cdnHeaders, redirect: "follow" });
    const resHeaders = new Headers(res.headers);
    resHeaders.set("Access-Control-Allow-Origin", "*");
    return new Response(res.body, { status: res.status, headers: resHeaders });
  }

  // ── 2. URL LIVENESS CHECK ────────────────────────────────────────────────────
  if (url.pathname === "/url-check") {
    const checkUrl = url.searchParams.get("url");
    if (!checkUrl) {
      return new Response(JSON.stringify({ isDead: true }), {
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    }
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);
      const res = await fetch(checkUrl, {
        method: "GET",
        headers: {
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
          "Referer": DEFAULT_REFERER,
          "Range": "bytes=0-4096"
        },
        signal: controller.signal,
        redirect: "follow"
      });
      clearTimeout(timeoutId);

      if (res.status >= 400) {
        return new Response(JSON.stringify({ isDead: true, status: res.status }), {
          headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
        });
      }
      const chunk = (await res.text()).slice(0, 4000).toLowerCase();
      const deadMarkers = [
        "something went wrong on the video process","something went wrong",
        "<title>warning</title>","file not found","video not found","deleted",
        "does not match","player domain","no longer available","video was removed",
        "file was removed","video is deleted","has been removed","has been deleted"
      ];
      const isDead = deadMarkers.some(m => chunk.includes(m));
      return new Response(JSON.stringify({ isDead, status: res.status }), {
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    } catch (e) {
      return new Response(JSON.stringify({ isDead: true, error: e.message }), {
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
      });
    }
  }

  // ── 3. ZEPHYRIX REVERSE PROXY ────────────────────────────────────────────────
  if (
    url.pathname.startsWith("/video/") ||
    url.pathname.startsWith("/zephyrix/") ||
    url.pathname.startsWith("/hls/") ||
    url.pathname.startsWith("/cdn/") ||
    url.searchParams.has("vid")
  ) {
    return handleZephyrixProxy(request, url);
  }

  // ── 4. LANDING PAGE ──────────────────────────────────────────────────────────
  return new Response(buildLandingPageHtml(url), {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=0, must-revalidate" }
  });
}

// ═══════════════════════════════════════════════════════════════════════════════
// DoraBash Proxy — Fetches dorabash.in through the Worker (bypasses CF challenge)
// ═══════════════════════════════════════════════════════════════════════════════
const DB_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Linux; Android 10; TX6s) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
  "Referer": "https://dorabash.in/",
  "X-Requested-With": "com.DoraBash",
  "sec-ch-ua-mobile": "?1",
  "sec-ch-ua-platform": "\"Android\"",
};

async function dbFetch(path, opts = {}) {
  const fullUrl = DORABASH_TARGET + path;
  return fetch(fullUrl, { headers: DB_HEADERS, redirect: "follow", ...opts });
}

async function handleDoraBashProxy(request, url) {
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, User-Agent",
    "Content-Type": "application/json"
  };
  if (request.method === "OPTIONS") {
    return new Response(null, { headers: cors });
  }

  const sub = url.pathname.slice("/dorabash/".length);

  // ── /dorabash/extract?url=PATH&ep=EPISODE ─────────────────────────────────
  // Extracts the episode iframe / Byse embed code directly from dorabash.in.
  // Supports series page URL (auto-discovers episode) or direct watch page URL.
  if (sub === "extract") {
    const inputUrl = url.searchParams.get("url") || "";
    const ep = parseInt(url.searchParams.get("ep") || "1", 10);
    if (!inputUrl) {
      return new Response(JSON.stringify({ error: "Missing url parameter" }), { status: 400, headers: cors });
    }

    try {
      let watchUrl = null;
      let cleanInput = inputUrl.trim();

      if (cleanInput.includes("/watch/")) {
        watchUrl = cleanInput;
      } else {
        // It's a series page — fetch it to get episode watch links
        const path = cleanInput.startsWith("http") ? "/" + cleanInput.split("/").slice(3).join("/") : cleanInput;
        const rSeries = await dbFetch(path);
        const seriesHtml = await rSeries.text();

        // Match all watch links
        const linkRe = /href=["'](https?:\/\/(?:www\.)?dorabash\.in\/watch\/[^"']+)["']/gi;
        const epMap = {};
        let lm;
        while ((lm = linkRe.exec(seriesHtml)) !== null) {
          const u = lm[1];
          const numM = /episode-(\d+)/i.exec(u);
          if (numM) {
            const n = parseInt(numM[1], 10);
            if (!epMap[n]) epMap[n] = u;
          }
        }

        watchUrl = epMap[ep];
        if (!watchUrl) {
          const slug = cleanInput.replace(/\/+$/, "").split("/").pop();
          watchUrl = `https://dorabash.in/watch/${slug}-episode-${ep}/`;
        }
      }

      // Fetch the episode watch page
      const watchPath = watchUrl.startsWith("http") ? "/" + watchUrl.split("/").slice(3).join("/") : watchUrl;
      const rWatch = await dbFetch(watchPath);
      const watchHtml = await rWatch.text();

      let byseCode = null;
      let byseUrl = null;
      let abyssUrl = null;
      const servers = {};

      // 1. Check iframes
      const ifrRe = /<iframe[^>]+(?:src|data-src)=["']([^"']+)["']/gi;
      let im;
      while ((im = ifrRe.exec(watchHtml)) !== null) {
        const src = im[1];
        const bm = /(?:bysevepoin|bysetayico)\.com\/e\/([a-zA-Z0-9_-]{6,})/i.exec(src);
        if (bm) {
          byseCode = bm[1];
          byseUrl = src;
          servers["byse"] = src;
        }
        if (src.includes("abyssplayer.com")) {
          abyssUrl = src;
          servers["abyss"] = src;
        }
      }

      // 2. Check data-embed-id base64 payloads
      const embedSpanRe = /data-embed-id=["']([^"']+)["']/gi;
      let sm;
      while ((sm = embedSpanRe.exec(watchHtml)) !== null) {
        const val = sm[1];
        const parts = val.split(":");
        for (const p of parts) {
          try {
            const dec = atob(p);
            const bm = /(?:bysevepoin|bysetayico)\.com\/e\/([a-zA-Z0-9_-]{6,})/i.exec(dec);
            if (bm) {
              if (!byseCode) byseCode = bm[1];
              if (!byseUrl) byseUrl = dec;
              servers["byse"] = dec;
            }
            if (dec.includes("abyssplayer.com")) {
              abyssUrl = dec;
              servers["abyss"] = dec;
            }
          } catch (e) {}
        }
      }

      // 3. Fallback raw regex
      if (!byseCode) {
        const rm = /(?:bysevepoin|bysetayico)\.com\/e\/([a-zA-Z0-9_-]{6,})/i.exec(watchHtml);
        if (rm) {
          byseCode = rm[1];
          byseUrl = `https://bysevepoin.com/e/${byseCode}`;
          servers["byse"] = byseUrl;
        }
      }

      const primaryIframe = byseUrl || abyssUrl || (Object.values(servers)[0] || null);

      return new Response(JSON.stringify({
        status: byseCode || primaryIframe ? "success" : "not_found",
        episode: ep,
        watch_url: watchUrl,
        byse_code: byseCode,
        byse_url: byseUrl,
        iframe_url: primaryIframe,
        servers: servers
      }), { headers: cors });

    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), { status: 500, headers: cors });
    }
  }

  // ── /dorabash/episodes?url=PATH ───────────────────────────────────────────
  if (sub === "episodes") {
    const pageUrl = url.searchParams.get("url") || "";
    if (pageUrl) {
      try {
        const path = pageUrl.startsWith("http") ? "/" + pageUrl.split("/").slice(3).join("/") : pageUrl;
        const r = await dbFetch(path);
        const html = await r.text();

        const linkRe = /href=["'](https?:\/\/(?:www\.)?dorabash\.in\/watch\/[^"']+)["']/gi;
        const epMap = {};
        let lm;
        while ((lm = linkRe.exec(html)) !== null) {
          const u = lm[1];
          const numM = /episode-(\d+)/i.exec(u);
          if (numM) {
            const n = parseInt(numM[1], 10);
            if (!epMap[n]) epMap[n] = u;
          }
        }

        const episodes = Object.keys(epMap).map(k => ({
          episode: parseInt(k, 10),
          watch_url: epMap[k]
        })).sort((a, b) => a.episode - b.episode);

        return new Response(JSON.stringify({ status: "success", count: episodes.length, episodes }), { headers: cors });
      } catch (e) {
        return new Response(JSON.stringify({ error: e.message }), { status: 500, headers: cors });
      }
    }
    return new Response(JSON.stringify({ error: "Missing url parameter" }), { status: 400, headers: cors });
  }

  // ── /dorabash/page?url=PATH ───────────────────────────────────────────────
  if (sub === "page") {
    const pageUrl = url.searchParams.get("url") || "";
    const path = pageUrl.startsWith("http") ? "/" + pageUrl.split("/").slice(3).join("/") : pageUrl;
    try {
      const r = await dbFetch(path || "/");
      const html = await r.text();
      return new Response(html, {
        headers: { "Access-Control-Allow-Origin": "*", "Content-Type": "text/html; charset=utf-8" }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), { status: 500, headers: cors });
    }
  }

  return new Response(JSON.stringify({ error: "Unknown dorabash endpoint: " + sub }), { status: 404, headers: cors });
}

// ═══════════════════════════════════════════════════════════════════════════════
// Helper: Patch Video JavaScript Bundle
// ═══════════════════════════════════════════════════════════════════════════════
function patchVideoBundle(text, workerOrigin) {
  // Method A: React ref auto-click directly on the button JSX
  // When React renders <button className="captcha-gate__play" onClick={Y} ref={...} />,
  // the ref callback fires the moment the node is attached to the DOM with React's listeners attached!
  text = text.replace(
    'onClick:Y',
    'onClick:Y,ref:e=>{if(e&&!e._done){e._done=!0;setTimeout(()=>{try{e.click()}catch{}},80)}}'
  );

  // Method B: Direct Hook auto-trigger with setTimeout delay so React Fiber finishes mounting
  text = text.replace(
    'Y=d.useCallback(()=>{R.current=0,Q()},[Q])',
    'Y=d.useCallback(()=>{R.current=0,Q()},[Q]),d.useEffect(()=>{setTimeout(()=>{try{Q()}catch{}},120)},[])'
  );

  // Method C: Autostart JWPlayer
  text = text.replace(/autostart\s*:\s*!1/g, 'autostart:!0,mute:!0');

  // Method D: Domain rewrites
  text = text.replaceAll('https://n1mwq.org', workerOrigin + '/n1mwq');

  return text;
}

// ═══════════════════════════════════════════════════════════════════════════════
// N1MWQ INNER IFRAME PROXY
// Route: /n1mwq/<path> -> https://n1mwq.org/<path>
// ═══════════════════════════════════════════════════════════════════════════════
async function handleN1mwqProxy(request, url) {
  const realPath = url.pathname.slice("/n1mwq".length) || "/";
  const targetUrl = `${N1MWQ_TARGET}${realPath}${url.search}`;

  const proxyHeaders = new Headers(request.headers);
  proxyHeaders.set("Host",    "n1mwq.org");
  proxyHeaders.set("Origin",  "https://bysevepoin.com");
  proxyHeaders.set("Referer", "https://bysevepoin.com/");
  proxyHeaders.set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36");

  let requestBody = null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    requestBody = await request.arrayBuffer();
  }

  const response = await fetch(targetUrl, {
    method: request.method,
    headers: proxyHeaders,
    body: requestBody,
    redirect: "follow"
  });

  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete("Content-Security-Policy");
  responseHeaders.delete("Content-Security-Policy-Report-Only");
  responseHeaders.delete("X-Frame-Options");
  responseHeaders.set("Access-Control-Allow-Origin", "*");

  const contentType = response.headers.get("Content-Type") || "";

  // HTML: route rewriting & proxying
  if (contentType.includes("text/html")) {
    let text = await response.text();

    const bootstrapScript = `<script>
(function(){
  try {
    if(location.pathname.startsWith('/n1mwq/')){
      history.replaceState(null,'',location.pathname.slice('/n1mwq'.length)+location.search+location.hash);
    }
  } catch(e){}

  (function(){
    var _f = window.fetch;
    window.fetch = function(input, init){
      var u = typeof input==='string' ? input
            : input instanceof Request ? input.url
            : String(input);
      if(/^\\/api\\//.test(u)){
        var n = '/n1mwq'+u;
        input = typeof input==='string' ? n : new Request(n, input);
      }
      return _f.call(this, input, init);
    };
    var _xo = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(){
      var a = [].slice.call(arguments);
      if(typeof a[1]==='string' && /^\\/api\\//.test(a[1])) a[1]='/n1mwq'+a[1];
      return _xo.apply(this,a);
    };
  })();
})();
<\/script>`;

    text = text.replace("<head>", "<head>" + bootstrapScript);
    text = text.replaceAll("https://n1mwq.org", url.origin + "/n1mwq");

    responseHeaders.set("Content-Type", "text/html; charset=utf-8");
    responseHeaders.delete("Content-Encoding");
    return new Response(text, { status: response.status, headers: responseHeaders });
  }

  // JS Bundle: patch code
  if (contentType.includes("javascript")) {
    let text = await response.text();
    text = patchVideoBundle(text, url.origin);
    responseHeaders.delete("Content-Encoding");
    return new Response(text, { status: response.status, headers: responseHeaders });
  }

  return new Response(response.body, { status: response.status, headers: responseHeaders });
}

// ═══════════════════════════════════════════════════════════════════════════════
// BYSE OUTER PAGE PROXY (bysevepoin.com)
// ═══════════════════════════════════════════════════════════════════════════════
async function handleByseProxy(request, url) {
  const targetUrl = `${BYSE_TARGET}${url.pathname}${url.search}`;

  const proxyHeaders = new Headers(request.headers);
  proxyHeaders.set("Host",    "bysevepoin.com");
  proxyHeaders.set("Origin",  "https://bysevepoin.com");
  proxyHeaders.set("Referer", "https://bysevepoin.com/");
  proxyHeaders.set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36");

  let requestBody = null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    requestBody = await request.arrayBuffer();
  }

  const response = await fetch(targetUrl, {
    method: request.method,
    headers: proxyHeaders,
    body: requestBody,
    redirect: "follow"
  });

  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete("Content-Security-Policy");
  responseHeaders.delete("Content-Security-Policy-Report-Only");
  responseHeaders.delete("X-Frame-Options");
  responseHeaders.set("Access-Control-Allow-Origin", "*");

  const contentType = response.headers.get("Content-Type") || "";
  const isText = contentType.includes("text/html")
               || contentType.includes("javascript")
               || contentType.includes("application/json")
               || contentType.includes("text/plain");

  if (isText) {
    let text = await response.text();
    // Route inner iframe through /n1mwq/
    text = text.replaceAll("https://n1mwq.org", url.origin + "/n1mwq");

    if (contentType.includes("javascript")) {
      text = patchVideoBundle(text, url.origin);
    }
    responseHeaders.delete("Content-Encoding");
    if (contentType.includes("text/html")) {
      responseHeaders.set("Content-Type", "text/html; charset=utf-8");
    }
    return new Response(text, { status: response.status, headers: responseHeaders });
  }

  return new Response(response.body, { status: response.status, headers: responseHeaders });
}

// ═══════════════════════════════════════════════════════════════════════════════
// ZEPHYRIX PROXY
// ═══════════════════════════════════════════════════════════════════════════════
async function handleZephyrixProxy(request, url) {
  let targetPath = url.pathname;
  if (targetPath.startsWith("/zephyrix/")) targetPath = targetPath.replace("/zephyrix/", "/");
  if (url.searchParams.has("vid")) targetPath = "/video/" + url.searchParams.get("vid");

  const targetUrl = `${ZEPHYRIX_TARGET}${targetPath}${url.search}`;

  const proxyHeaders = new Headers(request.headers);
  proxyHeaders.set("Referer", DEFAULT_REFERER);
  proxyHeaders.set("Origin", "https://watchanimeworld.one");
  proxyHeaders.set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36");

  let requestBody = null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    requestBody = await request.arrayBuffer();
  }

  const response = await fetch(targetUrl, {
    method: request.method,
    headers: proxyHeaders,
    body: requestBody,
    redirect: "follow"
  });

  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete("Content-Security-Policy");
  responseHeaders.delete("X-Frame-Options");
  responseHeaders.set("Access-Control-Allow-Origin", "*");

  const contentType = response.headers.get("Content-Type") || "";

  if (
    contentType.includes("json") || contentType.includes("javascript") ||
    contentType.includes("text/html") || contentType.includes("mpegurl") ||
    url.pathname.includes(".m3u8") || url.pathname.startsWith("/hls/")
  ) {
    let text = await response.text();
    const escapedOrigin = url.origin.replace(/\//g, "\\/");
    text = text.replaceAll("https:\\/\\/play.zephyrix.org", escapedOrigin);
    text = text.replaceAll("https://play.zephyrix.org", url.origin);
    text = text.replace(/https:\/\/(s\d+\.zn-grid\d+\.top\/[^\s"'\\]+)/g, `${url.origin}/grid-proxy?url=https://$1`);
    return new Response(text, { status: response.status, headers: responseHeaders });
  }

  return new Response(response.body, { status: response.status, headers: responseHeaders });
}

// ═══════════════════════════════════════════════════════════════════════════════
// BYSE SERVER-SIDE EXTRACTOR
// Ports DoraBash ByseSX.kt logic to Cloudflare Workers.
// Full flow: details → settings → challenge → attest → captcha → verify → playback → AES-GCM decrypt
// ═══════════════════════════════════════════════════════════════════════════════

function urlSafeBase64(bytes) {
  // bytes is Uint8Array
  let b64 = btoa(String.fromCharCode(...bytes));
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

function b64UrlDecode(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

// Custom "gr" PoW hash from ByseSX.kt (SipHash-like PRNG)
function _re(val, shift) {
  val = val >>> 0;
  return (((val << shift) | (val >>> (32 - shift))) >>> 0);
}
function _ye(p) {
  p[0] = ((p[0] + p[1]) >>> 0);
  p[3] = (_re(((p[3] ^ p[0]) >>> 0), 16));
  p[2] = ((p[2] + p[3]) >>> 0);
  p[1] = (_re(((p[1] ^ p[2]) >>> 0), 12));
  p[0] = ((p[0] + p[1]) >>> 0);
  p[3] = (_re(((p[3] ^ p[0]) >>> 0), 8));
  p[2] = ((p[2] + p[3]) >>> 0);
  p[1] = (_re(((p[1] ^ p[2]) >>> 0), 7));
}
function grHash(data) {
  // data: Uint8Array
  const p = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a]);
  for (const byte of data) {
    p[0] = ((p[0] + (byte & 0xFF)) >>> 0);
    p[0] = _re(p[0], 7);
    _ye(p);
  }
  for (let i = 0; i < 8; i++) _ye(p);
  const v = new Uint32Array(512);
  for (let i = 0; i < 512; i++) {
    _ye(p);
    v[i] = ((p[0] ^ p[2]) >>> 0);
  }
  for (let pass = 0; pass < 2; pass++) {
    for (let i = 0; i < 512; i++) {
      const idx2 = v[i] & 511;
      const t = (_re(((v[i] + v[idx2]) >>> 0), 13) ^ (Math.imul(v[(i+1)&511], 2654435761) >>> 0)) >>> 0;
      v[i] = t;
      p[0] = ((p[0] ^ t) >>> 0);
      _ye(p);
    }
  }
  const out = new Uint32Array(8);
  const chunkSize = 64; // 512/8
  for (let i = 0; i < 8; i++) {
    _ye(p);
    let acc = p[0];
    for (let j = 0; j < chunkSize; j++) {
      const val = v[i * chunkSize + j];
      acc = (_re(((acc + val) >>> 0), 5) ^ (Math.imul(val, 2246822519) >>> 0)) >>> 0;
    }
    out[i] = ((p[2] ^ acc) >>> 0);
  }
  return out;
}

function wrLeadingZeros(hashU32) {
  let bits = 0;
  for (const v of hashU32) {
    if (v === 0) { bits += 32; continue; }
    return bits + (32 - Math.floor(Math.log2(v)) - 1);
  }
  return bits;
}

function solvePoW(nonce, difficulty) {
  const enc = new TextEncoder();
  const prefix = enc.encode(nonce + ':');
  let counter = 0;
  const maxIter = 500000;
  while (counter < maxIter) {
    const num = enc.encode(String(counter));
    const candidate = new Uint8Array(prefix.length + num.length);
    candidate.set(prefix);
    candidate.set(num, prefix.length);
    if (wrLeadingZeros(grHash(candidate)) >= difficulty) {
      return String(counter);
    }
    counter++;
  }
  return null; // give up
}

async function generateFingerprint() {
  // Generate random viewer_id / device_id (16 random bytes hex each)
  const vid = Array.from(crypto.getRandomValues(new Uint8Array(16))).map(b=>b.toString(16).padStart(2,'0')).join('');
  const did = Array.from(crypto.getRandomValues(new Uint8Array(16))).map(b=>b.toString(16).padStart(2,'0')).join('');
  const iat = Math.floor(Date.now() / 1000);
  const confidence = 0.95;
  const enc = new TextEncoder();
  const payload = JSON.stringify({viewer_id:vid,device_id:did,confidence,iat,exp:iat+600});
  const tB64 = urlSafeBase64(enc.encode(payload));
  const sigHash = await crypto.subtle.digest('SHA-256', enc.encode(tB64));
  const tSig = urlSafeBase64(new Uint8Array(sigHash));
  const token = tB64 + '.' + tSig;
  return { viewer_id: vid, device_id: did, confidence, token };
}

async function byseAttest(innerBase, challengeResp, cookies) {
  const enc = new TextEncoder();
  // Generate ECDSA P-256 keypair
  const kp = await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'}, true, ['sign']);
  const rawPub = await crypto.subtle.exportKey('raw', kp.publicKey);
  const pubBytes = new Uint8Array(rawPub); // 65 bytes: 04 || x (32) || y (32)
  const xB64 = urlSafeBase64(pubBytes.slice(1, 33));
  const yB64 = urlSafeBase64(pubBytes.slice(33, 65));

  const nonce = challengeResp.nonce || '';
  const sigDer = await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'}, kp.privateKey, enc.encode(nonce));
  // DER -> raw r||s (64 bytes)
  const der = new Uint8Array(sigDer);
  let pos = 2; // skip 0x30, total-len
  if (der[1] === 0x81) pos++; // long form
  pos++; // 0x02
  let rLen = der[pos++];
  let rStart = pos;
  pos += rLen;
  pos++; // 0x02
  let sLen = der[pos++];
  let sStart = pos;
  // Pad r and s to 32 bytes
  function pad32(arr, offset, len) {
    const out = new Uint8Array(32);
    const src = arr.slice(offset, offset+len);
    if (src.length === 33 && src[0] === 0) {
      out.set(src.slice(1));
    } else if (src.length < 32) {
      out.set(src, 32 - src.length);
    } else {
      out.set(src.slice(src.length - 32));
    }
    return out;
  }
  const r32 = pad32(der, rStart, rLen);
  const s32 = pad32(der, sStart, sLen);
  const sigConcat = new Uint8Array(64);
  sigConcat.set(r32); sigConcat.set(s32, 32);
  const sigB64 = urlSafeBase64(sigConcat);

  const rnd = Math.random();
  const body = {
    viewer_id: '', device_id: '',
    challenge_id: challengeResp.challenge_id || '',
    nonce,
    signature: sigB64,
    public_key: { crv:'P-256', ext:true, key_ops:['verify'], kty:'EC', x:xB64, y:yB64 },
    client: {
      user_agent: 'Mozilla/5.0 (Linux; Android 10; TX6s) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
      architecture:'arm', bitness:'32', platform:'Android', platform_version:'10.0.0', model:'TX6s',
      ua_full_version:'137.0.7337.0',
      brand_full_versions:[{brand:'Chromium',version:'137.0.7337.0'}],
      pixel_ratio:1, screen_width:1280, screen_height:720, color_depth:24,
      languages:['en-US'], timezone:'America/New_York', hardware_concurrency:4,
      device_memory:2, touch_points:1,
      webgl_vendor:'Google Inc. (ARM)',
      webgl_renderer:'ANGLE (ARM, Mali-G31 MP2, OpenGL ES 3.2)',
      canvas_hash: await fhWorker(rnd),
      audio_hash: await fhWorker(rnd + 1),
      webgl_params_hash: await fhWorker(rnd + 2),
      fonts_hash: await fhWorker(rnd + 3),
      codecs_hash: await fhWorker(rnd + 4),
      media_devices:'ai1ao1vi4', pointer_type:'coarse',
      extra:{ vendor:'Google Inc.', appVersion:'5.0 (Linux; Android 10; TX6s) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36' }
    },
    storage: {}, attributes: { entropy:'high' }
  };
  const resp = await fetch(`${innerBase}/api/videos/access/attest`, {
    method:'POST', headers:{ 'Content-Type':'application/json', Cookie:cookies },
    body: JSON.stringify(body)
  });
  return resp.json();
}

async function fhWorker(val) {
  const enc = new TextEncoder();
  const h = await crypto.subtle.digest('SHA-256', enc.encode(String(val)));
  return urlSafeBase64(new Uint8Array(h));
}

function buildAesKey(keyParts, version) {
  const v = parseInt(version, 10) || 0;
  if (v > 0) {
    const k1 = b64UrlDecode(keyParts[v - 1]);
    const k2 = b64UrlDecode(keyParts[keyParts.length - v]);
    const key = new Uint8Array(k1.length + k2.length);
    key.set(k1); key.set(k2, k1.length);
    return key;
  }
  let total = 0;
  const parts = keyParts.map(k => { const b = b64UrlDecode(k); total += b.length; return b; });
  const key = new Uint8Array(total);
  let off = 0;
  for (const p of parts) { key.set(p, off); off += p.length; }
  return key;
}

async function decryptPlayback(playbackObj) {
  const keyBytes = buildAesKey(playbackObj.key_parts, playbackObj.version);
  const iv = b64UrlDecode(playbackObj.iv);
  const payload = b64UrlDecode(playbackObj.payload);
  const aesKey = await crypto.subtle.importKey('raw', keyBytes, {name:'AES-GCM'}, false, ['decrypt']);
  const plain = await crypto.subtle.decrypt({name:'AES-GCM', iv, tagLength:128}, aesKey, payload);
  let text = new TextDecoder().decode(plain);
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  return JSON.parse(text);
}

async function byseExtract(code) {
  const baseUrl = 'https://bysevepoin.com';
  const ua = 'Mozilla/5.0 (Linux; Android 10; TX6s) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36';

  // 1. Details
  const detRes = await fetch(`${baseUrl}/api/videos/${code}/details`, {
    headers:{ 'User-Agent':ua, 'Referer':`${baseUrl}/`, 'Origin':baseUrl }
  });
  const details = await detRes.json();

  const embedFrameUrl = details.embed_frame_url || `${baseUrl}/e/${code}`;
  const parsedEmbed = new URL(embedFrameUrl);
  const innerBase = `${parsedEmbed.protocol}//${parsedEmbed.hostname}`;
  const innerCode = parsedEmbed.pathname.split('/').filter(Boolean).pop();

  const headers = {
    'User-Agent': ua,
    'Referer': `${innerBase}/`,
    'Origin': innerBase,
    'X-Embed-Parent': `${baseUrl}/e/${code}`,
    'X-Embed-Origin': 'dorabash.in',
    'X-Embed-Referer': 'https://dorabash.in/'
  };

  // 2. Settings
  const setRes = await fetch(`${innerBase}/api/videos/${innerCode}/settings`, { headers });
  const settings = await setRes.json();

  // 3. Generate fingerprint
  const fp = await generateFingerprint();
  let cookies = `byse_viewer_id=${fp.viewer_id}; byse_device_id=${fp.device_id}`;
  headers['Cookie'] = cookies;

  let captchaToken = null;

  if (settings.captcha_required) {
    // 3a. Challenge
    const chalRes = await fetch(`${innerBase}/api/videos/access/challenge`, {
      method:'POST', headers:{ ...headers, 'Content-Type':'application/json' }, body:'{}'
    });
    const challenge = await chalRes.json();

    // 3b. Attest
    const attest = await byseAttest(innerBase, challenge, cookies);
    const attestFp = {
      token: attest.token || '',
      viewer_id: attest.viewer_id || '',
      device_id: attest.device_id || '',
      confidence: attest.confidence || 1.0
    };
    fp.token = attestFp.token;
    fp.viewer_id = attestFp.viewer_id;
    fp.device_id = attestFp.device_id;
    fp.confidence = attestFp.confidence;

    // 3c. Captcha POST
    const capRes = await fetch(`${innerBase}/api/videos/${innerCode}/captcha`, {
      method:'POST', headers:{ ...headers, 'Content-Type':'application/json' },
      body: JSON.stringify({ fingerprint: attestFp })
    });
    const cap = await capRes.json();

    // 3d. Solve PoW
    const solution = solvePoW(cap.pow_nonce, cap.pow_difficulty);
    if (!solution) throw new Error('PoW solver failed');

    // 3e. Verify
    const verRes = await fetch(`${innerBase}/api/videos/${innerCode}/captcha/verify`, {
      method:'POST', headers:{ ...headers, 'Content-Type':'application/json' },
      body: JSON.stringify({ pow_token: cap.pow_token, solution, fingerprint: attestFp })
    });
    const ver = await verRes.json();
    if (ver.token) {
      captchaToken = ver.token;
      headers['X-Captcha-Token'] = captchaToken;
    }
  }

  // 4. Playback
  const pbRes = await fetch(`${innerBase}/api/videos/${innerCode}/playback`, {
    method:'POST', headers:{ ...headers, 'Content-Type':'application/json' },
    body: JSON.stringify({ fingerprint: fp })
  });
  if (!pbRes.ok) {
    const errBody = await pbRes.text();
    throw new Error(`Playback failed ${pbRes.status}: ${errBody}`);
  }
  const pbJson = await pbRes.json();
  const playbackData = pbJson.playback || pbJson;

  // 5. Decrypt
  const decrypted = await decryptPlayback(playbackData);
  return {
    ...decrypted,
    title: details.title,
    poster_url: details.poster_url || decrypted.poster_url
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Abyss / Playhydrax Extractor
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Extract video sources from an Abyssplayer embed.
 *
 * Flow (mirrors DoraBash CloudStream extension – Abyass.kt):
 *  1. GET https://playhydrax.com/?v={id}
 *  2. Regex-extract `const datas = "..."` from all inline <script> text
 *  3. POST the encrypted string to https://enc-dec.app/api/dec-abyss
 *  4. Return sources filtered to status == true
 *
 * @param {string} videoId  – e.g. "K8R6OOjS7"
 * @returns {{ sources: Array<{url,type,codec,size,status}> }}
 */
async function abyssExtract(videoId) {
  const ABYSS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36";
  const ABYSS_REFERER = "https://playhydrax.com/";
  const ABYSS_ORIGIN  = "https://playhydrax.com";

  const embedUrl = `https://playhydrax.com/?v=${encodeURIComponent(videoId)}`;

  // 1. Fetch the embed page
  const pageRes = await fetch(embedUrl, {
    headers: {
      "User-Agent": ABYSS_UA,
      "Referer":    ABYSS_REFERER,
      "Origin":     ABYSS_ORIGIN,
      "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.5"
    },
    redirect: "follow"
  });
  if (!pageRes.ok) {
    throw new Error(`Playhydrax page fetch failed: ${pageRes.status}`);
  }
  const html = await pageRes.text();

  // 2. Extract `const datas = "..."` from inline scripts
  const datasMatch = /const\s+datas\s*=\s*"([^"]+)"/.exec(html);
  if (!datasMatch || !datasMatch[1]) {
    throw new Error("Could not find `datas` variable in playhydrax embed page");
  }
  const encryptedData = datasMatch[1];

  // 3. POST to enc-dec.app decryption API
  const decRes = await fetch("https://enc-dec.app/api/dec-abyss", {
    method: "POST",
    headers: {
      "User-Agent":   ABYSS_UA,
      "Referer":      ABYSS_REFERER,
      "Origin":       ABYSS_ORIGIN,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ text: encryptedData })
  });
  if (!decRes.ok) {
    const errText = await decRes.text().catch(() => "");
    throw new Error(`enc-dec.app returned ${decRes.status}: ${errText.slice(0, 200)}`);
  }
  const decJson = await decRes.json();

  // Expected shape: { status: 200, result: { sources: [ { url, size, type, codec, status } ] } }
  const rawSources = (decJson.result && decJson.result.sources) ? decJson.result.sources : [];
  const sources = rawSources.filter(s => s.status === true).map(s => ({
    url:   s.url,
    type:  s.type   || "unknown",
    codec: s.codec  || "h264",
    size:  s.size   || 0,
    status: true,
    // Streams require this Referer to play
    referer: ABYSS_REFERER
  }));

  return { sources, videoId };
}

// ─────────────────────────────────────────────────────────────────────────────
// Abyss HLS Playlist Builders
// ─────────────────────────────────────────────────────────────────────────────

/** Quality metadata table for HLS STREAM-INF tags */
const ABYSS_QUALITY_META = {
  "240p":  { bandwidth: 300000,   resolution: "426x240"   },
  "360p":  { bandwidth: 600000,   resolution: "640x360"   },
  "480p":  { bandwidth: 1200000,  resolution: "854x480"   },
  "720p":  { bandwidth: 2800000,  resolution: "1280x720"  },
  "1080p": { bandwidth: 5500000,  resolution: "1920x1080" },
  "1440p": { bandwidth: 10000000, resolution: "2560x1440" },
  "2160p": { bandwidth: 18000000, resolution: "3840x2160" },
};

/** Codec string for HLS CODECS attribute */
function abyssCodecString(codec) {
  if (!codec) return "avc1.42E01E,mp4a.40.2";
  const c = codec.toLowerCase();
  if (c === "av1")           return "av01.0.08M.08,mp4a.40.2";
  if (c === "hevc" || c === "h265") return "hvc1.1.6.L153.B0,mp4a.40.2";
  return "avc1.42E01E,mp4a.40.2"; // h264 default
}

/** URL-safe base64 encode a string */
function b64EncodeUrl(str) {
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Build a master HLS playlist for all Abyss sources.
 * Each variant stream URI points to /abyss-media/{b64url}.m3u8
 * which wraps the MP4 as a single-segment VOD media playlist.
 *
 * Sorted best-quality-first (highest bandwidth).
 */
function buildAbyssMasterM3u8(sources, workerOrigin) {
  const qualityOrder = ["2160p","1440p","1080p","720p","480p","360p","240p"];

  const sorted = [...sources].sort((a, b) => {
    const ai = qualityOrder.indexOf(a.type);
    const bi = qualityOrder.indexOf(b.type);
    // Unknown labels go last; within same label, sort by size desc
    if (ai !== bi) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    return b.size - a.size;
  });

  const lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""];

  for (const s of sorted) {
    const meta = ABYSS_QUALITY_META[s.type] || {
      bandwidth: Math.round((s.size || 5000000) / 600), // rough bps from file size / 10min
      resolution: "1280x720"
    };
    const codecs   = abyssCodecString(s.codec);
    const label    = `${s.type}${s.codec ? " ("+s.codec+")" : ""}`;
    const mediaUri = `${workerOrigin}/abyss-media/${b64EncodeUrl(s.url)}.m3u8`;

    lines.push(`#EXT-X-STREAM-INF:BANDWIDTH=${meta.bandwidth},RESOLUTION=${meta.resolution},CODECS="${codecs}",NAME="${label}"`);
    lines.push(mediaUri);
  }

  return lines.join("\n") + "\n";
}

/**
 * Build a single-segment HLS VOD media playlist wrapping one direct video URL.
 * hls.js and most native HLS players support this format.
 */
function buildAbyssMediaM3u8(videoUrl) {
  return [
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-PLAYLIST-TYPE:VOD",
    "#EXT-X-TARGETDURATION:86400",
    "#EXT-X-MEDIA-SEQUENCE:0",
    "#EXTINF:86400.0,",
    videoUrl,
    "#EXT-X-ENDLIST",
    ""
  ].join("\n");
}

/**
 * Build a quality-picker player page for Abyss sources.
 * Uses direct <video> tag with /abyss-stream/ proxy so CORS and Referer
 * are handled by the Worker — no hls.js needed for direct MP4s.
 * Also outputs master M3U8 URL for external player use.
 */
function buildAbyssPlayerHtml(sources, videoId, workerOrigin) {
  const qualityOrder = ["2160p","1440p","1080p","720p","480p","360p","240p"];
  sources = [...sources].sort((a, b) => {
    const ai = qualityOrder.indexOf(a.type);
    const bi = qualityOrder.indexOf(b.type);
    if (ai !== bi) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    return b.size - a.size;
  });

  // Build proxied URLs: /abyss-stream/<b64encodedOriginalUrl>
  const proxiedSources = sources.map(s => ({
    label:    s.type + (s.codec ? " (" + s.codec + ")" : ""),
    url:      workerOrigin + "/abyss-stream/" + btoa(s.url).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,""),
    size:     s.size,
    type:     s.type,
    codec:    s.codec
  }));

  const masterUrl  = `${workerOrigin}/abyss-master/${videoId}.m3u8`;
  const sourcesJson = JSON.stringify(proxiedSources);
  const title       = `Abyss – ${videoId}`;

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>${title}</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;background:#000;overflow:hidden;font-family:sans-serif;color:#fff}
  #wrap{display:flex;flex-direction:column;width:100%;height:100%}
  #v{flex:1;width:100%;background:#000;display:block;min-height:0}
  #bar{display:flex;align-items:center;gap:8px;padding:5px 10px;background:#111;flex-shrink:0;flex-wrap:wrap}
  #bar label{color:#aaa;font-size:12px}
  #qsel{background:#222;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 8px;font-size:13px;cursor:pointer}
  #qsel:focus{outline:none;border-color:#6cf}
  #info{color:#666;font-size:11px}
  #ext{margin-left:auto;color:#555;font-size:11px;word-break:break-all}
</style>
</head>
<body>
<div id="wrap">
  <video id="v" controls autoplay playsinline style="flex:1;width:100%;height:100%;background:#000;min-height:0"></video>
  <div id="bar">
    <label for="qsel">Quality:</label>
    <select id="qsel"></select>
    <span id="info"></span>
    <span id="ext" title="Copy for external HLS players (VLC, Infuse, mpv)">M3U8: ${masterUrl}</span>
  </div>
</div>
<script>
(function(){
  var sources = ${sourcesJson};
  var video   = document.getElementById('v');
  var qsel    = document.getElementById('qsel');
  var info    = document.getElementById('info');

  sources.forEach(function(s, i){
    var opt = document.createElement('option');
    opt.value = i;
    opt.textContent = s.label;
    qsel.appendChild(opt);
  });

  function loadSource(idx, keepTime){
    var s = sources[idx];
    if(!s) return;
    var t = keepTime ? (video.currentTime || 0) : 0;
    var wasPlaying = keepTime ? !video.paused : true;
    video.src = s.url;
    video.load();
    if(t > 0) video.currentTime = t;
    if(wasPlaying) video.play().catch(function(){});
    var mb = s.size ? ' \u00b7 ' + (s.size / 1048576).toFixed(0) + ' MB' : '';
    info.textContent = s.type + (s.codec ? ' \u00b7 ' + s.codec : '') + mb;
  }

  qsel.addEventListener('change', function(){ loadSource(parseInt(this.value, 10), true); });
  loadSource(0, false);
})();
<\/script>
</body>
</html>`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Videas Extractor
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Extract master HLS stream and media details from a Videas embed.
 *
 * Flow:
 *  1. GET https://app.videas.fr/embed/{embedId}/ -> extract `opening` from videas-embed-config
 *  2. GET https://app.videas.fr/api/visitor/embed/{embedId}/ -> get playback_token, session_id, asset_id
 *  3. POST https://app.videas.fr/api/visitor/playback/info/ -> get official hls_url (stream.m3u8)
 *  4. Fetch master m3u8 to extract all available quality streams (360p, 480p, etc.)
 *
 * @param {string} embedId - Videas UUID (e.g. "ee4be8a9-a06c-4920-bd02-77bd92e66ea6")
 * @returns {Promise<{uid: string, title: string, poster_url: string, duration: number, hls_url: string, sources: Array}>}
 */
async function videasExtract(embedId) {
  const ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36";
  const embedPageUrl = `${VIDEAS_TARGET}/embed/${embedId}/`;

  // Step 1: Fetch embed page HTML to get opening token
  const pageRes = await fetch(embedPageUrl, {
    headers: {
      "User-Agent": ua,
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
  });
  if (!pageRes.ok) {
    throw new Error(`Videas page fetch failed: ${pageRes.status}`);
  }
  const html = await pageRes.text();

  const cfgMatch = html.match(/<script id="videas-embed-config"[^>]*>([\s\S]*?)<\/script>/);
  if (!cfgMatch || !cfgMatch[1]) {
    throw new Error("Could not find videas-embed-config in embed page");
  }
  const config = JSON.parse(cfgMatch[1]);
  const opening = config.opening || "";

  // Step 2: Query the visitor embed API
  const embApiUrl = `${VIDEAS_TARGET}/api/visitor/embed/${embedId}/`;
  const embRes = await fetch(embApiUrl, {
    headers: {
      "User-Agent": ua,
      "Referer": embedPageUrl,
      "Origin": VIDEAS_TARGET,
      "X-Embed-Opening": opening,
      "Accept": "application/json"
    }
  });
  if (!embRes.ok) {
    throw new Error(`Videas visitor embed API failed: ${embRes.status}`);
  }
  const embData = await embRes.json();

  const assetId = (embData.items && embData.items[0] && embData.items[0].asset)
    ? embData.items[0].asset.uid
    : embedId;
  const playbackToken = embData.playback_token;
  const sessionId = embData.session_id;
  const title = embData.name || (embData.items && embData.items[0] && embData.items[0].asset && embData.items[0].asset.name) || embedId;
  let poster = embData.thumbnail_url || "";
  if (poster && poster.startsWith("/")) {
    poster = VIDEAS_TARGET + poster;
  }
  const duration = (embData.items && embData.items[0] && embData.items[0].asset)
    ? embData.items[0].asset.duration
    : null;

  // Step 3: POST to playback info API to retrieve the HLS stream
  const infoApiUrl = `${VIDEAS_TARGET}/api/visitor/playback/info/`;
  const infoRes = await fetch(infoApiUrl, {
    method: "POST",
    headers: {
      "User-Agent": ua,
      "Referer": embedPageUrl,
      "Origin": VIDEAS_TARGET,
      "Content-Type": "application/json",
      "Accept": "application/json"
    },
    body: JSON.stringify({
      asset_id: assetId,
      playback_token: playbackToken,
      session_id: sessionId
    })
  });
  if (!infoRes.ok) {
    throw new Error(`Videas playback info API failed: ${infoRes.status}`);
  }
  const infoData = await infoRes.json();

  let hlsPath = (infoData.hls_url && infoData.hls_url.url) ? infoData.hls_url.url : "";
  if (!hlsPath) {
    throw new Error("No HLS URL in playback info");
  }
  const hlsUrl = hlsPath.startsWith("/") ? (VIDEAS_TARGET + hlsPath) : hlsPath;

  // Step 4: Parse quality sources from master m3u8
  const sources = [];
  try {
    const m3u8Res = await fetch(hlsUrl, {
      headers: { "User-Agent": ua, "Referer": embedPageUrl }
    });
    if (m3u8Res.ok) {
      const m3u8Text = await m3u8Res.text();
      const lines = m3u8Text.split("\n");
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (line.startsWith("#EXT-X-STREAM-INF:")) {
          const resMatch = line.match(/RESOLUTION=(\d+x\d+)/);
          const nameMatch = line.match(/NAME="([^"]+)"/);
          const bwMatch = line.match(/BANDWIDTH=(\d+)/);
          const nextUrl = (lines[i + 1] || "").trim();
          if (nextUrl && !nextUrl.startsWith("#")) {
            sources.push({
              resolution: resMatch ? resMatch[1] : "",
              quality: nameMatch ? nameMatch[1] : (resMatch ? resMatch[1].split("x")[1] + "p" : "auto"),
              bandwidth: bwMatch ? parseInt(bwMatch[1], 10) : 0,
              url: nextUrl
            });
          }
        }
      }
    }
  } catch (e) {
    // Non-fatal if parsing variant streams fails
  }

  return {
    uid: embedId,
    title,
    poster_url: poster,
    duration,
    hls_url: hlsUrl,
    sources
  };
}

function buildHlsPlayerHtml(m3u8Url, title, posterUrl) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>${title.replace(/</g,'&lt;')}</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;background:#000;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
  #playerContainer{position:relative;width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:#000;}
  video{width:100%;height:100%;display:block;background:#000;object-fit:contain;}
  #unmuteBadge{
    position:absolute;
    top:16px;
    right:16px;
    z-index:99;
    display:none;
    align-items:center;
    gap:8px;
    background:rgba(0,0,0,0.8);
    color:#fff;
    padding:8px 16px;
    border-radius:24px;
    font-size:13px;
    font-weight:600;
    cursor:pointer;
    border:1px solid rgba(255,255,255,0.25);
    backdrop-filter:blur(10px);
    box-shadow:0 4px 16px rgba(0,0,0,0.6);
    user-select:none;
    transition:transform .15s, background .15s;
    animation:pulseBadge 2s infinite;
  }
  #unmuteBadge:hover{transform:scale(1.05);background:rgba(20,20,20,0.95);}
  @keyframes pulseBadge{
    0%,100%{box-shadow:0 0 0 0 rgba(255,255,255,0.4)}
    50%{box-shadow:0 0 0 8px rgba(255,255,255,0)}
  }
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"><\/script>
</head>
<body>
<div id="playerContainer">
  <video id="v" controls autoplay playsinline webkit-playsinline preload="auto" poster="${posterUrl.replace(/"/g,'&quot;')}"></video>
  <div id="unmuteBadge">🔊 Tap to Unmute</div>
</div>
<script>
(function(){
  var video = document.getElementById('v');
  var badge = document.getElementById('unmuteBadge');
  var src = "${m3u8Url.replace(/"/g,'\\"')}";

  function unmute(){
    video.muted = false;
    if(badge) badge.style.display = 'none';
  }

  if(badge){
    badge.addEventListener('click', function(e){
      e.stopPropagation();
      unmute();
    });
  }

  // First click/tap anywhere unmutes if video was started muted
  document.addEventListener('click', function(){
    if(video.muted) unmute();
  }, { once: true });

  function attemptAutoplay(){
    var p = video.play();
    if(p && p.catch){
      p.catch(function(){
        // Browser blocked audio autoplay, switch to muted autoplay
        video.muted = true;
        var p2 = video.play();
        if(p2 && p2.then){
          p2.then(function(){
            if(badge) badge.style.display = 'inline-flex';
          }).catch(function(){});
        }
      });
    }
  }

  if(Hls.isSupported()){
    var hls = new Hls({maxBufferLength: 30, maxMaxBufferLength: 60});
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, function(){
      attemptAutoplay();
    });
    hls.on(Hls.Events.ERROR, function(e, data){
      if(data.fatal){
        switch(data.type){
          case Hls.ErrorTypes.NETWORK_ERROR:
            hls.startLoad();
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hls.recoverMediaError();
            break;
          default:
            hls.destroy();
            break;
        }
      }
    });
  } else if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.src = src;
    video.addEventListener('loadedmetadata', function(){
      attemptAutoplay();
    });
  }
})();
<\/script>
</body>
</html>`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Landing Page Builder & HTML
// ─────────────────────────────────────────────────────────────────────────────
function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function buildLandingPageHtml(url) {
  let title = "Video Player";
  let videoUrl = "";
  let delaySec = 30;

  if (url) {
    const encodedData = url.searchParams.get("data");
    if (encodedData) {
      try {
        const raw = atob(encodedData.replace(/-/g, "+").replace(/_/g, "/"));
        const d = JSON.parse(raw);
        if (d.t) title = d.t.replace(/_/g, " ");
        if (d.u) videoUrl = d.u;
        if (d.d) delaySec = parseInt(d.d, 10) || 30;
      } catch (e) {}
    }
    if (!videoUrl) {
      if (url.searchParams.has("url")) videoUrl = url.searchParams.get("url");
      else if (url.searchParams.has("u")) videoUrl = url.searchParams.get("u");
    }
    if (title === "Video Player" && url.searchParams.has("t")) {
      title = url.searchParams.get("t").replace(/_/g, " ");
    }
    if (url.searchParams.has("d")) {
      delaySec = parseInt(url.searchParams.get("d"), 10) || delaySec;
    }
  }

  // If videoUrl is a Videas embed, route to /videas/:uuid for autoplay
  let iframeSrc = videoUrl;
  if (videoUrl) {
    const vMatch = videoUrl.match(/videas\.fr\/embed\/([0-9a-fA-F-]{36})/i);
    if (vMatch) {
      iframeSrc = "/videas/" + vMatch[1];
    }
  }

  const safeTitle = escapeHtml(title);
  const safeIframeSrc = escapeHtml(iframeSrc);
  const safeVideoUrl = escapeHtml(videoUrl);

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>${safeTitle}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background-color:#050505;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}
.bg-gradient{position:absolute;inset:0;z-index:1;background:linear-gradient(-45deg,#0f0c29,#302b63,#24243e,#1a0b2e);background-size:400% 400%;animation:gradientMove 15s ease infinite;}
@keyframes gradientMove{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
.bg-dots{position:absolute;inset:-100% 0;z-index:2;background-image:radial-gradient(rgba(255,255,255,.3) 1px,transparent 1.1px);background-size:30px 30px;animation:dotMove 20s linear infinite;pointer-events:none;}
@keyframes dotMove{from{transform:translateY(0)}to{transform:translateY(300px)}}
.app-header{position:relative;z-index:10;height:50px;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6);backdrop-filter:blur(12px);border-bottom:1px solid rgba(255,255,255,.1);color:#fff;font-weight:600;}
.app-content{position:relative;z-index:5;width:100%;height:calc(100% - 50px);}
.detect-iframe{position:absolute;width:0;height:0;border:0;z-index:1;}
.overlay{position:absolute;inset:0;z-index:6;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:20px;}
.ripple-container{position:relative;width:80px;height:80px;margin:0 auto 30px;}
.ripple-container div{position:absolute;border:4px solid #fff;border-radius:50%;animation:ripple 1.2s cubic-bezier(0,0.2,0.8,1) infinite;}
.ripple-container div:nth-child(2){animation-delay:-0.6s}
@keyframes ripple{0%{top:36px;left:36px;width:0;height:0;opacity:1}100%{top:0;left:0;width:72px;height:72px;opacity:0}}
.loading-title{font-size:22px;font-weight:700;background:linear-gradient(to right,#fff 20%,#888 40%,#888 60%,#fff 80%);background-size:200% auto;-webkit-background-clip:text;color:transparent;animation:shine 3s linear infinite;}
@keyframes shine{to{background-position:200% center}}
.error-msg-box{background:rgba(10,5,20,.85);border:1px solid rgba(255,80,80,.4);padding:40px;border-radius:20px;color:white;pointer-events:auto;}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="bg-gradient"></div>
<div class="bg-dots"></div>
<div class="app-header" id="headerTitle">${safeTitle}</div>
<div class="app-content">
  <!-- Hidden input for apps scanning input values -->
  <input type="hidden" id="app_detected_url" value="${safeVideoUrl}">
  <!-- Iframe for apps scanning iframes -->
  <iframe id="videoFrame" class="detect-iframe" src="${safeIframeSrc}" allow="autoplay; fullscreen; picture-in-picture"></iframe>

  <div class="overlay" id="overlay">
    <div id="loadingUI">
      <div class="ripple-container"><div></div><div></div></div>
      <div class="loading-title">Loading Video...</div>
      <p style="color:rgba(255,255,255,.6);font-size:14px">Please wait while we connect</p>
    </div>
    <div id="messageUI" class="hidden">
      <div class="error-msg-box">
        <span style="font-size:50px">⚠️</span>
        <div style="color:#ff6b6b;font-size:20px;font-weight:bold">Video Not Loading?</div>
        <div style="margin-top:10px;font-size:15px">If the current video is not loading,<br>please try another server.</div>
      </div>
    </div>
  </div>
</div>
<script>
(function(){
  try {
    const params = new URLSearchParams(location.search);
    const encodedData = params.get('data');
    let title = params.get('t') || '';
    let url = params.get('url') || params.get('u') || '';
    let delaySec = parseInt(params.get('d') || '0', 10);

    if (encodedData) {
      try {
        const raw = atob(encodedData.replace(/-/g, '+').replace(/_/g, '/'));
        const d = JSON.parse(raw);
        if (d.t) title = d.t.replace(/_/g, ' ');
        if (d.u) url = d.u;
        if (d.d) delaySec = parseInt(d.d, 10) || 30;
      } catch (e) {}
    }

    if (title) {
      document.title = title;
      const h = document.getElementById('headerTitle');
      if (h) h.innerText = title;
    }

    if (url) {
      const vMatch = url.match(/videas\\.fr\\/embed\\/([0-9a-fA-F-]{36})/i);
      const effectiveSrc = vMatch ? ('/videas/' + vMatch[1]) : url;
      const f = document.getElementById('videoFrame');
      if (f && (!f.src || f.src === 'about:blank' || !f.getAttribute('src'))) {
        f.src = effectiveSrc;
      }
      const inp = document.getElementById('app_detected_url');
      if (inp) inp.value = url;
    }

    const delayTime = (delaySec > 0 ? delaySec : ${delaySec}) * 1000;
    setTimeout(() => {
      const l = document.getElementById('loadingUI');
      const m = document.getElementById('messageUI');
      if (l) l.remove();
      if (m) m.classList.remove('hidden');
    }, delayTime);
  } catch (err) {
    console.error(err);
  }
})();
<\/script>
</body>
</html>`;
}

const ORIGINAL_PLAYER_HTML = buildLandingPageHtml(null);