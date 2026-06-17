/**
 * SPdex 浏览器采集：在已登录、且与 /spdexapi 同源的页面里打开 DevTools → Console，粘贴整段运行。
 *
 * 注意：在 new.spdex.com 上部分环境对 /spdexapi/spdex/... 的 fetch 会返回 404 HTML/JSON；
 * 若本脚本任一请求失败，请打开 Network 找到实际返回 200 的 JSON 请求，把 Response 粘进 bundle。
 *
 * 会依次 fetch（credentials: include）：
 *   match_detail, odds/view/list, price/volumn(home+away), odds/1x2/trend
 * 并在控制台打印一段 JSON；复制保存为 my_bundle.json 后执行：
 *   python3 worldcup_web_cli.py predict --bundle my_bundle.json
 *
 * 用法：在 Console 执行后按提示输入 eventId 与亚盘 line（如 -1 或 -0.5），或先改下面默认值。
 */
(function () {
  const DEFAULT_EVENT_ID = "";
  const DEFAULT_LINE = "-1";

  function q(o) {
    return new URLSearchParams({
      app: "a",
      version: "1.01",
      dateformat: "iso8601",
      ...o,
    }).toString();
  }

  function normLine(line) {
    let s = String(line || "0").trim();
    if (s.startsWith("+")) s = s.slice(1);
    if (s === "" || s === "-") return "0";
    const v = Number(s);
    if (!Number.isFinite(v)) return s;
    if (v === 0) return "0";
    let t = v.toFixed(3).replace(/\.?0+$/, "");
    return t;
  }

  async function j(path, search) {
    const u = `${location.origin}${path}?${q(search)}`;
    const r = await fetch(u, { credentials: "include", headers: { Accept: "application/json,*/*" } });
    const text = await r.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      body = { _parseError: true, status: r.status, preview: text.slice(0, 400) };
    }
    return { url: u, status: r.status, body };
  }

  const run = async () => {
    const eventId =
      DEFAULT_EVENT_ID ||
      window.prompt("EventId（数字，如 35035313）", "")?.trim();
    if (!eventId) {
      console.error("需要 EventId");
      return;
    }
    const line = window.prompt("亚盘 line（与接口一致，如 -1）", DEFAULT_LINE) || DEFAULT_LINE;
    const nl = normLine(line);

    const md = await j("/spdexapi/spdex/match_detail", {
      keyword: String(eventId),
      product_id: 0,
      tutorial: 0,
    });
    const hl = await j("/spdexapi/spdex/odds/view/list", {
      eid: eventId,
      outcome: 3,
      line: nl,
    });
    const pvh = await j("/spdexapi/spdex/price/volumn", {
      eventid: eventId,
      hour: -1,
      selection: "home",
    });
    const pva = await j("/spdexapi/spdex/price/volumn", {
      eventid: eventId,
      hour: -1,
      selection: "away",
    });
    const et = await j("/spdexapi/spdex/odds/1x2/trend", { hour: -1, eid: eventId });

    const bundle = {
      schema: 2,
      captured_at: new Date().toISOString(),
      origin: location.origin,
      event_id: Number(eventId),
      asian_line_input: line,
      asian_line_normalized: nl,
      match_detail: md.body,
      handicap_list: hl.body,
      price_volume: { home: pvh.body, away: pva.body },
      euro_trend: et.body,
      _http: {
        match_detail: { status: md.status, url: md.url },
        handicap_list: { status: hl.status, url: hl.url },
        price_volume_home: { status: pvh.status, url: pvh.url },
        price_volume_away: { status: pva.status, url: pva.url },
        euro_trend: { status: et.status, url: et.url },
      },
    };

    const text = JSON.stringify(bundle, null, 2);
    console.log("----- 复制下面整段 JSON 到文件（如 my_bundle.json）-----");
    console.log(text);
    try {
      await navigator.clipboard.writeText(text);
      console.log("（已尝试写入剪贴板）");
    } catch (_) {
      /* ignore */
    }
    return bundle;
  };

  return run();
})();
