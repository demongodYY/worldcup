---
name: spdex-browser-cli-parity
description: Relates SPdex, Cursor browser MCP snapshots, DevTools JSON, Cookie, and worldcup_ah_cli.py. Use when the user says the agent must read the open webpage (not only local snapshots), use cursor-ide-browser, or match 法国-塞内加尔 to EventId and CLI predict.
---

# SPdex 浏览器与 `worldcup_ah_cli.py` 判断对齐

## 事实分层（先读再动手）

1. **会员站与入口**：[new.spdex.com](https://new.spdex.com/)、[app.spdex.com](https://app.spdex.com/) 均为 SPdex 超级指数系统入口；登录后前端通过 **XHR/Fetch** 拉取 **`/spdexapi/spdex/...`** 下的 JSON，而不是靠页面静态 HTML 承载全部指数。
2. **本仓库 CLI 的数据面**：`worldcup_ah_cli.py` 请求 **`https://app.spdex.com/spdexapi`**（与站点前端同源路径）。环境变化时可能出现：**未登录返回 HTML / 跳转登录**，或 **`app.spdex.com` 302 到 `new.spdex.com` 同源 API**；此时与浏览器一致的前提是 **携带与浏览器相同的会话 Cookie**（见下文）。
3. **推荐结论的两种档位**：
   - **与脚本完全一致（同一套加权、阈值、门控）**：必须在本地执行 `predict` / `snapshot` / `watch`（同一 `EventId`、同一 Cookie 下的数据）。
   - **仅根据用户粘贴的 Network JSON / 截图**：可做字段解读与**定性**方向讨论；**不得声称**与小数点后与 `Predictor` 完全一致的 `score`/`purchase_score`，除非已运行 CLI 或用户贴出 CLI 输出。

## `.env` 与登录（格式对齐 `.env.example`）

- 模板见仓库根目录 **`.env.example`**：`USERNAME=`、`PASSWORD=`。
- **自动登录**：`new.spdex` / `app.spdex` 登录流程常含**验证码**，本仓库脚本**不会**用 `USERNAME`/`PASSWORD` 自动换会话。
- **会话给 CLI 用**：浏览器登录成功后，把对 **`*.spdex.com`** 生效的 **`Cookie` 整串**（与 DevTools 里请求头一致，**不要**带 `Cookie:` 前缀）写入 **`.env`** 的 **`SPDEX_COOKIE=`** 单行；若有 Bearer token 则用 **`SPDEX_AUTHORIZATION=`**（脚本会按需补 `Bearer `）。
- 保存 `.env` 后在本仓库根目录执行 CLI；脚本启动时会加载 `.env`（可用 `--no-dotenv` 跳过）。

## 登录浏览器后继续：核对数据并产出与 CLI 一致的推荐

按顺序做，避免场次的 `EventId` 对错号。

### 1. 在浏览器里定位同源 JSON

1. 打开开发者工具 → **Network**（网络）。
2. 筛选 **`spdexapi`**，或按名称筛选 **`match_list`**、**`match_detail`**、**`odds`**、**`volumn`**、**`1x2`**。
3. 点开一条 **Status 为 200** 且 **Response 为 JSON**（非 HTML）的请求。
4. 在 **Response** 或 **Preview** 中确认：`EventId` / `HomeTeam` / `AwayTeam`、以及必发/亚盘相关字段与当前看的比赛一致。

### 2. 从浏览器会话接到 CLI（关键）

1. 在同一请求（或任意一条成功的 `spdexapi` 请求）打开 **Headers**。
2. 找到 **Request Headers** 里的 **`cookie:`**（或 **`Cookie:`**），复制**冒号后的整段值**（通常很长、分号分隔）。
3. 写入项目根目录 **`.env`**：

   ```env
   SPDEX_COOKIE=这里粘贴整段Cookie
   ```

4. **不要**把 Cookie 贴进聊天或提交 Git；`.env` 应已在 `.gitignore` 中。

### 3. 用 CLI 出正式推荐（与页面数据同源）

```bash
cd <WorldCup 仓库根目录>
python3 worldcup_ah_cli.py upcoming --limit 50
python3 worldcup_ah_cli.py predict --event-id <从浏览器JSON确认的EventId> --verbose
```

- 若需确认「带 Cookie 是否改变接口」：`python3 worldcup_ah_cli.py auth-probe --event-id <id>`。
- 若需时间序列：同一 `event_id` 下使用 `snapshot` 再 `trend`（见 `README.md`）。

### 4. Cursor 内置 Simple Browser 的提示

在 Cursor 里用 **Simple Browser** 打开站点时，若 **Network / Cookie 不便复制**，改用系统 **Chrome / Safari** 登录同一账号，再按上面步骤复制 Cookie；CLI 只关心 **Cookie 字符串是否与当前会话一致**，不关心浏览器壳层。

### 5. 用户要求「根据当前打开的网页」时，Agent 必须先读页（MCP）

**禁止**在未用 MCP 核对当前页的情况下，用 **`.spdex_snapshots/*.jsonl`** 或旧结论冒充「网页上读到的数据」。

1. **服务器名**：优先 **`cursor-ide-browser`**（Cursor 内置）；若用户启用 Browse 插件，描述符目录多为 **`plugin-browse-browser`**（`browser_status` 里 `running` 需为 true）。
2. **最小流程**：`browser_tabs`（确认 `viewId`）→ `browser_snapshot`（必要时提高 `maxDepth`）→ 若只有首屏、列表在下方，用 **`browser_scroll`** 再 `browser_snapshot`。
3. **找场次**：首页可能只有少量推荐链；赛事表多在 **`https://new.spdex.com/football`**。可用 **`browser_navigate`** 直达；联赛用 **`browser_select_option`** 选「世界杯」等；点 **`搜索` / `搜索 (⌘K)`** 打开 **「命令搜索」** textbox，**`browser_type`** 队名（可 `submit: true`）。若快照出现 **「无匹配项」**，如实说明，不得编造该场的页面数据。
4. **读到的内容**：无障碍快照里，单场常以 **长 `name`** 聚合一行（含必发价、必指、成交、欧赔、凯利、大单方向等）。只复述快照里**实际出现的**队名与数字。
5. **页上没有该场**：若已滚动/搜索/换筛选仍无「法国 vs 塞内加尔」等，应明确 **当前 URL 与筛选条件下页面未展示该场**（可能已开赛下架、日期非今日、或权限/会员差异），再建议用户打开单场链接或 DevTools Network；**可**在说明后附加本地快照作**复盘补充**，并标注来源非当前页。

## 输入「法国-塞内加尔」如何对齐 CLI

- CLI 的 `predict` / `snapshot` 以 **`--event-id`** 为主，没有单独的 `法国-塞内加尔` 参数。
- **推荐**：`upcoming` 输出里按 **`法国 vs 塞内加尔`** 找行首 **`event_id`**，再 `predict --event-id ...`。
- **备选**：`match_detail?keyword=...` 可用队名或 id 搜索；**以列表 + EventId 双确认最稳**。

## 与脚本「同一判断」时的检查清单

- [ ] 浏览器 JSON 里 **`EventId`** 与 CLI 使用的 **`--event-id`** 一致。
- [ ] **`.env` 已配置 `SPDEX_COOKIE`**（或 `SPDEX_AUTHORIZATION`），且 `upcoming` / `predict` 不再报「non-JSON / 登录页 HTML」类错误。
- [ ] 已跑 **`predict --verbose`**（或 `--json`），以 CLI 输出的 **推荐 / score / purchase_score / 各 Signal** 为最终「与脚本一致」的结论。
- [ ] 注意 **`IsStopUpdate`**：若接口标记停更，CLI 会提示复盘用途，不代表可实时购买（见脚本 warning 文案）。

## 仅在对话里粘贴浏览器 JSON 时（Agent 怎么做）

1. 先从 JSON 提取 **`EventId`**、队名、盘口、必发/亚盘摘要，与用户对一下是否同一场。
2. 可做**定性**：资金倾斜、水位高低、明显背离等；对照 `README.md` 里信号含义用自然语言归纳。
3. **明确告知**：未运行 `worldcup_ah_cli.py predict` 时，不提供与 CLI 完全一致的数值型 **`score` / `purchase_score` / 置信度**；若用户需要，引导其配置 **`SPDEX_COOKIE`** 后本地执行 `predict` 并把输出贴回。

## 额外参考

- 算法与信号含义、阈值：项目根目录 `README.md`。
- API 路径与参数：`worldcup_ah_cli.py` 中 **`SpdexClient`**（`match_list`、`match_detail`、`handicap_list`、`handicap_detail`、`price_volume`、`euro_trend`）。
