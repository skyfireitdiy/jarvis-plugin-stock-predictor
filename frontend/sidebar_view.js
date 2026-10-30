// stock-predictor 插件前端侧边栏视图。
// 约定：纯浏览器 ES module，用 window.Vue 渲染，导出 default 组件，不 import 'vue'。
// 功能：A股中短线预测——搜索/自选股、次日方向+涨跌幅区间、五维得分、K线图、触发信号。
// 认证：统一走 window.__jarvisFetch（宿主注入，自动携带 jarvis 认证信息，token 不暴露给插件）；
//       后端通过 /api/plugins/{node_id}/function-call 代理调用插件 api.py PUBLIC_FUNCTIONS。
// 图表：SVG 自绘（蜡烛图 + MA5/MA20 + 预测区间带 + 成交量），无第三方依赖。
const PLUGIN = "stock-predictor";
// 自选股列表持久化 key（按插件固定，浏览器本地存储）
const WATCHLIST_KEY = "jarvis_stock_predictor_watchlist";
// 兜底免责声明（后端 predict 正常返回时会带完整 disclaimer 覆盖）
const FALLBACK_DISCLAIMER =
  "⚠️ 本预测仅为基于公开行情数据的量化参考，不构成任何投资建议。股市有风险，入市需谨慎，据此操作盈亏自负。";

// 复刻 App.vue 的 gateway 地址解析（侧边栏组件无 props 注入，需自行解析）
function parseGatewayAddress(address) {
  address = (address || "").trim();
  if (!address) return null;
  if (address.includes("://")) {
    try {
      const url = new URL(address);
      return {
        protocol: url.protocol.replace(":", ""),
        host: url.hostname,
        port:
          url.port ||
          (url.protocol === "https:" || url.protocol === "wss:" ? "443" : "80"),
        path: url.pathname,
      };
    } catch {
      return null;
    }
  }
  if (address.includes(":")) {
    const parts = address.split(":");
    if (parts.length === 2) {
      return { protocol: null, host: parts[0], port: parts[1], path: "" };
    }
  }
  return { protocol: null, host: address, port: "8000", path: "" };
}
// 获取当前页面的 HTTP 协议（与 App.vue getHttpProtocol 一致）
function getHttpProtocol() {
  return window.location.protocol === "https:" ? "https" : "http";
}
function gatewayBase() {
  const parsed = parseGatewayAddress(
    localStorage.getItem("jarvis_gateway_url") || "",
  );
  const host = (parsed && parsed.host) || "127.0.0.1";
  const port = (parsed && parsed.port) || "8000";
  const protocol =
    parsed && parsed.protocol === "https" ? "https" : getHttpProtocol();
  return `${protocol}://${host}:${port}`;
}
// 通过 gateway 插件功能代理端点调用插件私有功能
async function callFunction(functionName, args, nodeId) {
  const authedFetch =
    (typeof window !== "undefined" && window.__jarvisFetch) || fetch;
  const targetNodeId = String(nodeId || "master").trim() || "master";
  const resp = await authedFetch(
    `${gatewayBase()}/api/plugins/${encodeURIComponent(targetNodeId)}/function-call`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plugin: PLUGIN,
        function: functionName,
        arguments: args || {},
      }),
    },
  );
  let result;
  try {
    result = await resp.json();
  } catch (e) {
    throw new Error("网关响应异常", { cause: e });
  }
  if (!result || !result.success) {
    const err = (result && result.error) || {};
    throw new Error(err.message || err.code || "调用失败");
  }
  return result.result;
}

// 方向 → 徽章配色（A股习惯：红涨绿跌）
// 注意：这些是模块顶层纯函数，仅供组件 methods 内调用；
// 模板里通过实例方法 directionColor/directionBg/scoreColor 间接访问（见 methods）。
function directionColorTop(dir) {
  if (dir === "看涨") return "#ef4444";
  if (dir === "看跌") return "#22c55e";
  return "#888888";
}
function directionBgTop(dir) {
  if (dir === "看涨") return "rgba(239,68,68,0.15)";
  if (dir === "看跌") return "rgba(34,197,94,0.15)";
  return "rgba(136,136,136,0.15)";
}
// 得分色：>0 红（A股做多情绪）<0 绿，0 灰
function scoreColorTop(s) {
  if (s > 0) return "#ef4444";
  if (s < 0) return "#22c55e";
  return "#888888";
}

export default {
  name: "StockPredictorSidebarView",
  data() {
    return {
      searchInput: "",
      // 自选股列表（localStorage 持久化）
      watchlist: [],
      // 当前选中代码
      current: "",
      loading: false,
      error: "",
      // predict 结果
      pred: null,
      // K线数组 [{date,open,close,high,low,volume,amount,turnover}]
      kline: [],
      // 展开的维度名（默认展开权重最大的技术面）
      expandedDim: "",
      // 自选行情缓存（watchlist 接口结果，key=symbol）
      quoteCache: {},
    };
  },
  mounted() {
    this.loadWatchlist();
    if (this.watchlist.length > 0) {
      this.switchStock(this.watchlist[0]);
    }
  },
  computed: {
    // 兜底免责声明（模板可访问）
    fallbackDisclaimer() {
      return FALLBACK_DISCLAIMER;
    },
    // 按权重降序排列的维度列表
    dimsList() {
      if (!this.pred || !this.pred.dims) return [];
      const list = Object.values(this.pred.dims || {}).filter(
        (d) => d && d.name,
      );
      list.sort((a, b) => (b.weight || 0) - (a.weight || 0));
      return list;
    },
  },
  methods: {
    // 模板中使用的工具函数（模板只能访问组件实例上的方法/属性，
    // 模块顶层函数不会暴露到实例，否则渲染时会 TypeError 导致组件空白）
    directionColor(dir) {
      return directionColorTop(dir);
    },
    directionBg(dir) {
      return directionBgTop(dir);
    },
    scoreColor(s) {
      return scoreColorTop(s);
    },
    // ---- 自选股管理 ----
    loadWatchlist() {
      try {
        const raw = localStorage.getItem(WATCHLIST_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        this.watchlist = Array.isArray(arr)
          ? arr.filter((s) => /^\d{6}$/.test(String(s).trim()))
          : [];
      } catch {
        this.watchlist = [];
      }
    },
    saveWatchlist() {
      try {
        localStorage.setItem(WATCHLIST_KEY, JSON.stringify(this.watchlist));
      } catch {
        /* 忽略 */
      }
    },
    addToWatchlist(code) {
      code = String(code || "").trim();
      if (!/^\d{6}$/.test(code)) return;
      if (!this.watchlist.includes(code)) {
        this.watchlist.push(code);
        this.saveWatchlist();
      }
    },
    removeFromWatchlist(code) {
      this.watchlist = this.watchlist.filter((c) => c !== code);
      this.saveWatchlist();
      if (this.current === code) {
        if (this.watchlist.length > 0) {
          this.switchStock(this.watchlist[0]);
        } else {
          this.current = "";
          this.pred = null;
          this.kline = [];
          this.error = "";
        }
      }
    },
    switchStock(code) {
      code = String(code || "").trim();
      if (!/^\d{6}$/.test(code)) return;
      this.current = code;
      this.searchInput = code;
      this.load(code);
    },
    // ---- 搜索 ----
    doSearch() {
      const code = String(this.searchInput || "").trim();
      if (!/^\d{6}$/.test(code)) {
        this.toast("请输入 6 位 A股股票代码", "warning");
        return;
      }
      this.addToWatchlist(code);
      this.switchStock(code);
    },
    // ---- 数据加载 ----
    async load(symbol) {
      const code = String(symbol || "").trim();
      if (!/^\d{6}$/.test(code)) return;
      this.loading = true;
      this.error = "";
      this.pred = null;
      this.kline = [];
      // 并行拉取预测 + K线
      const results = await Promise.allSettled([
        callFunction("predict", { symbol: code, days: 120 }),
        callFunction("get_kline_json", { symbol: code, days: 60 }),
      ]);
      this.loading = false;
      const p = results[0];
      if (p.status === "fulfilled" && p.value && p.value.success) {
        this.pred = p.value;
        if (p.value.dims && !this.expandedDim) {
          const top = Object.values(p.value.dims).sort(
            (a, b) => (b.weight || 0) - (a.weight || 0),
          )[0];
          this.expandedDim = top ? top.name : "";
        }
        this.addToWatchlist(code);
      } else {
        const reason = p.status === "rejected" ? p.reason : p.value;
        const msg = (reason && reason.message) || "预测获取失败";
        this.error = msg;
        this.toast(msg, "error");
      }
      const k = results[1];
      if (k.status === "fulfilled" && k.value && k.value.success) {
        this.kline = Array.isArray(k.value.kline) ? k.value.kline : [];
      }
      // 尝试刷新自选行情（限流友好）
      this.refreshQuotes(true);
    },
    refresh() {
      if (this.current) this.load(this.current);
    },
    // 批量拉自选行情（throttle：同一只 30s 内不重复拉，防后端限流）
    async refreshQuotes(silent) {
      if (this.watchlist.length === 0) return;
      const now = Date.now();
      const need = this.watchlist.filter((c) => {
        const q = this.quoteCache[c];
        return !q || !q._ts || now - q._ts > 30000;
      });
      if (need.length === 0) return;
      try {
        const res = await callFunction("watchlist", { symbols: need });
        if (res && Array.isArray(res.quotes)) {
          for (const q of res.quotes) {
            if (q && q.symbol) {
              this.quoteCache[q.symbol] = Object.assign({}, q, { _ts: now });
            }
          }
        }
      } catch (e) {
        if (!silent) this.toast((e && e.message) || "自选行情获取失败", "error");
      }
    },
    // ---- 交互 ----
    askAgent() {
      if (!this.pred || !this.pred.success) {
        this.toast("请先加载一只股票的预测", "warning");
        return;
      }
      const p = this.pred;
      const signals =
        Array.isArray(p.signals) && p.signals.length > 0
          ? "\n触发信号：" + p.signals.slice(0, 5).join("；")
          : "";
      const text =
        `【A股预测】${p.symbol} ${p.trade_date} 收盘 ${p.last_close}\n` +
        `方向：${p.direction}；预期区间：${p.range_low}% ~ ${p.range_high}%；` +
        `综合得分 ${p.score}；置信度 ${p.confidence}%。` +
        signals +
        `\n请基于以上预测帮我做更深入的分析（参考技术面/资金面/消息面证据）。`;
      const send =
        typeof window !== "undefined" && window.__jarvisSendToActiveAgent;
      if (typeof send === "function") {
        try {
          send(text, { silent: true });
          this.toast("已发送给当前 Agent，请在对话中查看", "success");
        } catch (e) {
          this.toast("发送失败：" + ((e && e.message) || "未知错误"), "error");
        }
      } else {
        this.toast("当前环境不支持发送给 Agent", "warning");
      }
    },
    toast(message, type) {
      const t = typeof window !== "undefined" && window.__jarvisShowToast;
      if (typeof t === "function") {
        try {
          t(message, type || "info");
        } catch {
          /* 忽略 */
        }
        return;
      }
      if (type === "error") this.error = message;
    },
    // ---- SVG K线图 ----
    klineSvg() {
      const data = this.kline;
      if (!Array.isArray(data) || data.length === 0) return "";
      const n = data.length;
      // 尺寸
      const W = 560;
      const H = 300;
      const plotLeft = 10;
      const plotRight = W - 10;
      const plotTop = 14;
      const plotBottom = 212;
      const volTop = 228;
      const volBottom = 288;
      const plotW = plotRight - plotLeft;
      const plotH = plotBottom - plotTop;
      const slotW = plotW / n;
      const candleW = Math.max(1, slotW * 0.6);
      // 收盘价数组（用于 MA）
      const closes = data.map((d) => Number(d.close));
      // MA 计算
      const ma = (arr, period) => {
        const out = [];
        for (let i = 0; i < arr.length; i++) {
          if (i < period - 1) {
            out.push(null);
            continue;
          }
          let s = 0;
          let c = 0;
          for (let j = i - period + 1; j <= i; j++) {
            const v = arr[j];
            if (v !== null && v !== undefined && !Number.isNaN(v)) {
              s += v;
              c++;
            }
          }
          out.push(c > 0 ? s / c : null);
        }
        return out;
      };
      const ma5 = ma(closes, 5);
      const ma20 = ma(closes, 20);
      // 价格范围（纳入预测区间）
      let minP = Infinity;
      let maxP = -Infinity;
      for (const d of data) {
        const lo = Number(d.low);
        const hi = Number(d.high);
        if (!Number.isNaN(lo)) minP = Math.min(minP, lo);
        if (!Number.isNaN(hi)) maxP = Math.max(maxP, hi);
      }
      let predLowP = null;
      let predHighP = null;
      const lastClose = closes[n - 1];
      if (
        this.pred &&
        this.pred.success &&
        lastClose !== null &&
        lastClose !== undefined &&
        !Number.isNaN(lastClose)
      ) {
        predLowP = lastClose * (1 + Number(this.pred.range_low || 0) / 100);
        predHighP = lastClose * (1 + Number(this.pred.range_high || 0) / 100);
        minP = Math.min(minP, predLowP);
        maxP = Math.max(maxP, predHighP);
      }
      if (!isFinite(minP) || !isFinite(maxP) || minP === maxP) {
        if (!isFinite(minP) || !isFinite(maxP)) {
          minP = 0;
          maxP = 1;
        } else if (minP === maxP) {
          maxP = minP + 1;
        }
      }
      const pad = (maxP - minP) * 0.06;
      minP -= pad;
      maxP += pad;
      const y = (p) => plotTop + ((maxP - p) / (maxP - minP)) * plotH;
      const x = (i) => plotLeft + i * slotW + slotW / 2;
      // 成交量范围
      let maxVol = 0;
      for (const d of data) {
        const v = Number(d.volume);
        if (!Number.isNaN(v)) maxVol = Math.max(maxVol, v);
      }
      maxVol = maxVol || 1;
      const volH = (v) => ((v || 0) / maxVol) * (volBottom - volTop);

      let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" xmlns="http://www.w3.org/2000/svg" style="display:block;font-family:system-ui,sans-serif;">`;
      svg += `<rect x="0" y="0" width="${W}" height="${H}" fill="#1a1a1a" rx="6"/>`;

      // 网格线
      for (let g = 0; g <= 4; g++) {
        const gy = plotTop + (plotH / 4) * g;
        svg += `<line x1="${plotLeft}" y1="${gy}" x2="${plotRight}" y2="${gy}" stroke="#2a2a2a" stroke-width="1"/>`;
        const price = maxP - ((maxP - minP) / 4) * g;
        svg += `<text x="${plotLeft + 2}" y="${gy - 3}" fill="#666" font-size="9">${price.toFixed(2)}</text>`;
      }

      // 预测区间带（最后一根K线之后）
      if (predLowP !== null && predHighP !== null) {
        const fx0 = x(n - 1) + candleW / 2;
        const fx1 = plotRight;
        const fy0 = y(predHighP);
        const fy1 = y(predLowP);
        svg += `<rect x="${fx0}" y="${Math.min(fy0, fy1)}" width="${fx1 - fx0}" height="${Math.abs(fy1 - fy0)}" fill="rgba(245,158,11,0.12)" stroke="#f59e0b" stroke-width="1" stroke-dasharray="4,3"/>`;
        svg += `<text x="${fx0 + 4}" y="${Math.min(fy0, fy1) - 4}" fill="#f59e0b" font-size="9">预测 ${this.pred.range_low}% ~ ${this.pred.range_high}%</text>`;
      }

      // MA 折线
      const linePath = (arr, color) => {
        let d = "";
        for (let i = 0; i < arr.length; i++) {
          if (arr[i] === null || arr[i] === undefined || Number.isNaN(arr[i]))
            continue;
          const px = x(i);
          const py = y(arr[i]);
          d += d
            ? ` L${px.toFixed(2)},${py.toFixed(2)}`
            : `M${px.toFixed(2)},${py.toFixed(2)}`;
        }
        return d
          ? `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.4" opacity="0.9"/>`
          : "";
      };
      svg += linePath(ma20, "#3b82f6");
      svg += linePath(ma5, "#f59e0b");

      // 蜡烛 + 成交量
      for (let i = 0; i < n; i++) {
        const d = data[i];
        const o = Number(d.open);
        const c = Number(d.close);
        const hi = Number(d.high);
        const lo = Number(d.low);
        if ([o, c, hi, lo].some((v) => Number.isNaN(v))) continue;
        const cx = x(i);
        const up = c >= o;
        const col = up ? "#ef4444" : "#22c55e";
        const yc = y(c);
        const yo = y(o);
        const yhi = y(hi);
        const ylo = y(lo);
        // 影线
        svg += `<line x1="${cx}" y1="${yhi}" x2="${cx}" y2="${ylo}" stroke="${col}" stroke-width="1"/>`;
        // 实体
        const top = Math.min(yc, yo);
        const bodyH = Math.max(1, Math.abs(yc - yo));
        svg += `<rect x="${(cx - candleW / 2).toFixed(2)}" y="${top.toFixed(2)}" width="${candleW.toFixed(2)}" height="${bodyH.toFixed(2)}" fill="${up ? col : "none"}" stroke="${col}" stroke-width="1"/>`;
        // 成交量
        const v = Number(d.volume);
        if (!Number.isNaN(v)) {
          const vh = volH(v);
          svg += `<rect x="${(cx - candleW / 2).toFixed(2)}" y="${(volBottom - vh).toFixed(2)}" width="${candleW.toFixed(2)}" height="${vh.toFixed(2)}" fill="${col}" opacity="0.55"/>`;
        }
      }

      // 日期刻度（首/中/尾）
      const idxs = [0, Math.floor(n / 2), n - 1];
      for (const i of idxs) {
        const label = String(data[i].date || "").slice(5, 10) || String(i);
        svg += `<text x="${x(i)}" y="${volBottom + 12}" fill="#666" font-size="9" text-anchor="middle">${label}</text>`;
      }
      // 图例
      svg += `<text x="${plotLeft + 2}" y="${plotTop + 10}" fill="#f59e0b" font-size="9">MA5</text>`;
      svg += `<text x="${plotLeft + 32}" y="${plotTop + 10}" fill="#3b82f6" font-size="9">MA20</text>`;
      svg += `</svg>`;
      return svg;
    },
    // 维度条宽度（-100~100 → 半条百分比）
    dimBarWidth(score) {
      const s = Number(score);
      if (Number.isNaN(s)) return 0;
      return Math.min(50, (Math.abs(s) / 100) * 50);
    },
  },
  template: `
  <div style="display:flex;flex-direction:column;gap:10px;padding:10px;font-size:13px;color:#ddd;background:transparent;height:100%;overflow:auto;box-sizing:border-box;">
    <!-- 搜索框 -->
    <div style="display:flex;gap:6px;">
      <input v-model="searchInput" placeholder="输入 6 位股票代码" @keydown.enter.prevent="doSearch"
        style="flex:1;min-width:0;padding:6px 8px;background:#1e1e1e;color:#ddd;border:1px solid #444;border-radius:4px;font-size:13px;outline:none;"
        maxlength="6"/>
      <button @click="doSearch" :disabled="loading"
        style="padding:6px 12px;background:#1f6feb;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">
        {{ loading ? '加载中' : '查询' }}
      </button>
    </div>

    <!-- 自选股 -->
    <div v-if="watchlist.length > 0"
      style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
      <span style="font-size:11px;color:#888;">自选:</span>
      <span v-for="c in watchlist" :key="c" @click="switchStock(c)"
        style="display:inline-flex;align-items:center;gap:4px;padding:2px 6px;background:#252526;border:1px solid #3c3c3c;border-radius:10px;cursor:pointer;font-size:11px;color:#ddd;"
        :style="c === current ? 'border-color:#1f6feb;color:#58a6ff;' : ''">
        {{ c }}
        <span @click.stop="removeFromWatchlist(c)" style="color:#888;font-size:10px;cursor:pointer;"
          title="删除">✕</span>
      </span>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" style="padding:20px;text-align:center;color:#888;font-size:12px;">
      正在获取行情与预测数据…
    </div>

    <!-- 错误 -->
    <div v-else-if="error && !pred"
      style="padding:14px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.4);border-radius:6px;color:#f87171;font-size:12px;">
      {{ error }}
    </div>

    <!-- 空态 -->
    <div v-else-if="!current"
      style="padding:24px;text-align:center;color:#666;font-size:12px;border:1px dashed #333;border-radius:6px;">
      输入股票代码查询，或从上方自选股选择<br/>
      <span style="font-size:11px;color:#555;">支持沪深 A股（6 位代码，如 600519 / 000001）</span>
    </div>

    <!-- 预测主卡片 -->
    <div v-else-if="pred && pred.success"
      style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:10px;">
      <!-- 头部：代码 + 日期 + 刷新 -->
      <div style="display:flex;align-items:center;gap:8px;">
        <span style="font-weight:600;font-size:15px;color:#eee;">{{ pred.symbol }}</span>
        <span style="font-size:11px;color:#888;">{{ pred.trade_date }} 收盘 {{ pred.last_close }}</span>
        <span style="flex:1;"></span>
        <button @click="refresh" title="刷新"
          style="padding:3px 8px;background:#252526;color:#aaa;border:1px solid #3c3c3c;border-radius:4px;cursor:pointer;font-size:11px;">⟳</button>
      </div>
      <!-- 方向徽章 + 关键指标 -->
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <span
          style="padding:6px 14px;border-radius:16px;font-size:15px;font-weight:600;"
          :style="{ background: directionBg(pred.direction), color: directionColor(pred.direction), border: '1px solid ' + directionColor(pred.direction) }">
          {{ pred.direction }}
        </span>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:11px;color:#888;">预期区间</span>
          <span style="font-size:15px;font-weight:600;color:#eee;">{{ pred.range_low }}% ~ {{ pred.range_high }}%</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:11px;color:#888;">综合得分</span>
          <span style="font-size:15px;font-weight:600;" :style="{ color: scoreColor(pred.score) }">{{ pred.score }}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:11px;color:#888;">置信度</span>
          <span style="font-size:15px;font-weight:600;color:#eee;">{{ pred.confidence }}%</span>
        </div>
      </div>

      <!-- K线图 -->
      <div v-if="kline.length > 0" style="border:1px solid #2a2a2a;border-radius:6px;overflow:hidden;"
        v-html="klineSvg()"></div>
      <div v-else style="padding:8px;text-align:center;color:#666;font-size:11px;">K线数据不可用</div>

      <!-- 操作按钮 -->
      <div style="display:flex;gap:8px;">
        <button @click="askAgent"
          style="flex:1;padding:7px 0;background:#1f6feb;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">
          问 Agent 深度分析
        </button>
      </div>
    </div>

    <!-- 五维得分 -->
    <div v-if="pred && pred.success"
      style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:8px;">
      <div style="font-size:12px;color:#aaa;font-weight:600;">五维信号（点击展开证据）</div>
      <div v-for="(d, i) in dimsList" :key="i" style="display:flex;flex-direction:column;gap:3px;">
        <div style="display:flex;align-items:center;gap:6px;cursor:pointer;"
          @click="expandedDim = expandedDim === d.name ? '' : d.name">
          <span style="width:68px;font-size:12px;color:#ddd;">{{ d.name }}</span>
          <span style="font-size:10px;color:#888;">{{ Math.round((d.weight || 0) * 100) }}%</span>
          <div style="flex:1;position:relative;height:14px;background:#2a2a2a;border-radius:3px;overflow:hidden;">
            <div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:#444;"></div>
            <div v-if="Number(d.score) !== 0"
              :style="{
                position: 'absolute',
                top: '2px', bottom: '2px', borderRadius: '2px',
                background: scoreColor(d.score),
                left: Number(d.score) > 0 ? '50%' : (50 - dimBarWidth(d.score)) + '%',
                width: dimBarWidth(d.score) + '%',
              }"></div>
          </div>
          <span style="font-size:12px;font-weight:600;width:44px;text-align:right;"
            :style="{ color: scoreColor(d.score) }">{{ d.score }}</span>
        </div>
        <div v-if="expandedDim === d.name && d.evidence && d.evidence.length > 0"
          style="margin-left:2px;padding-left:8px;border-left:2px solid #333;">
          <div v-for="(ev, j) in d.evidence" :key="j"
            style="font-size:11px;color:#aaa;line-height:1.5;padding:1px 0;">
            · {{ ev }}
          </div>
        </div>
      </div>
    </div>

    <!-- 触发信号 -->
    <div v-if="pred && pred.success && pred.signals && pred.signals.length > 0"
      style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:6px;">
      <div style="font-size:12px;color:#aaa;font-weight:600;">触发信号</div>
      <div v-for="(s, i) in pred.signals" :key="i"
        style="font-size:12px;color:#fbbf24;line-height:1.5;">
        {{ s }}
      </div>
    </div>

    <!-- 免责声明 -->
    <div v-if="pred && pred.success && pred.disclaimer"
      style="padding:8px 10px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);border-radius:6px;color:#f59e0b;font-size:11px;line-height:1.5;">
      {{ pred.disclaimer }}
    </div>
    <div v-else
      style="padding:8px 10px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);border-radius:6px;color:#f59e0b;font-size:11px;line-height:1.5;">
      {{ fallbackDisclaimer }}
    </div>
  </div>
  `,
};
