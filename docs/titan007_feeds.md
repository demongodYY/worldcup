# Titan007 / 球探 live 数据接口备忘（非官方）

以下 URL 在 **带 Referer** 的浏览器或脚本请求下曾返回 200（2026-06 验证）；站点可能随时调整路径或校验规则。

## HTTP 拉取约定

| 说明 | 值 |
|------|-----|
| User-Agent | 常见桌面浏览器 UA |
| Referer | 按域分别携带，否则部分 `livestatic` 资源返回 **404** |
| 编码 | `bf.titan007.com/vbsxml/bfdata.js` 为 **GB18030/GBK** 中文，需 `iconv` 或 Python `gb18030` 解码 |

## CLI 行为备忘

- `titan007_ah_cli.py upcoming` 默认赛程源为 **live**（`bfdata_ut.js`，与 oldIndexall 同源）；未传 ``--schedule-source`` 时读 ``TITAN007_SCHEDULE_SOURCE``，再未设置亦为 live。筛世界杯可加 ``--world-cup``、``-w`` 或 ``--wc``。``upcoming --with-okooo`` 会在列表中多输出 **澳客 okooo_id** 与 **match_source**（并拉 betfa，等同全局 ``--okooo-bifa``）。默认**不**逐场请求 `1x2d`（避免卡死），需要欧赔时传 ``--fetch-euro``。``predict`` 场景下可选 ``--okooo-bifa``（或 ``TITAN007_OKOOO_BIFA=1``）在每次 ``refresh_feeds`` 时拉取澳客「必发盈亏」页并合并 ``Bf*``，见下文「澳客」节。

- `TITAN007_SCHEDULE_TZ`：解析 bfdata 第 12 段、竞彩 `bf_jc.txt` 计划开赛（第 1 列）所用 IANA 时区，默认 `Asia/Shanghai`。**月份逗号字段为 JavaScript 0–11**（与 `Date.getMonth()` 一致），见 `titan007_client._parse_bf_match_time`。

## 赛程与队名、联赛、开赛时间

| 用途 | URL 模板 | Referer |
|------|------------|---------|
| bf 域赛程大块 | `https://bf.titan007.com/vbsxml/bfdata.js` | `https://bf.titan007.com/` |
| **CLI 默认** / oldIndexall 同款 | `https://livestatic.titan007.com/vbsxml/bfdata_ut.js?r=007{ts}` | `https://live.titan007.com/oldIndexall.aspx` |
| 竞彩页 [jc/index](https://jc.titan007.com/index.aspx)（`football.js` → `loadData`） | `https://jc.titan007.com/xml/bf_jc.txt` | `https://jc.titan007.com/index.aspx` |

**说明**：`bfdata.js` 与 `bfdata_ut.js` 在实测中常为 **同一批 `A[]` 场次**；页面能看到的「未来赛程」若仍不在索引里，则是服务端切片未更新，换 URL 也未必多出行。`bf_jc.txt` 为 **竞彩开售子集**，行数通常远少于 bf 全表；正文以 `$` 分场次、段首以 `!` 接联赛块，本仓库 `parse_bf_jc_rows` 将其转成与 `bfdata` 相同的 row 字典供 `build_match` 使用。

正文为 `var A=Array(N);` 与 `A[i]="...".split('^');`。`^` 分隔字段，**ScheduleID** 在 **下标 0**；**主队** 简体 **5**、繁体 **6**、英文 **7**；**客队** 简体 **8**、繁体 **9**、英文 **10**（**不要**把 8、9 当成主、客各一列，二者均为客队简/繁）。`Match.home/away` 取 **5 / 8** 简中优先，缺省再用 **7 / 10**；开赛时间串 **12**（形如 ``2026,5,17,22,00,00``：**月份为 JS 的 0–11**，5=六月；按 **`TITAN007_SCHEDULE_TZ` 默认 Asia/Shanghai** 理解后转 UTC）；联赛简体名 **2**；亚盘均值盘口 **29**；联赛类 ID **45**（可作 `league_id`）。

**赛程范围**：公开接口目前以 `bfdata.js` 这一份索引为主；曾探测 `bfdata2.js`、`bfdata_wc.js` 等路径，多为 **404 HTML**（非 A[] 数据）。因此「未来 N 场世界杯」完全取决于 **`bfdata.js` 当前是否包含未开球的世界杯行**。CLI 支持 `upcoming --all-future --league-contains 世界杯 --limit 5`：在索引已有未来场次时，按联赛名列子串筛选并取最近若干场，无需把 `--hours` 拉到极大。赛程索引来源可通过 **`--schedule-source bf|live|jc`** 或环境变量 **`TITAN007_SCHEDULE_SOURCE`** 切换（见 README）。

## 即时比分 / 亚赔水位（列表级）

| 用途 | URL 模板 | Referer |
|------|------------|---------|
| 比分与多段亚赔快照 | `https://livestatic.titan007.com/vbsxml/ch_goalbf3.xml?r=007{ts}` | `https://live.titan007.com/oldIndexall.aspx` |
| 时间戳校验 | `https://livestatic.titan007.com/vbsxml/time.txt?r=007{ts}` | 同上 |

`ch_goalbf3.xml` 内 `<m>` 为逗号分隔一行多赛；首字段为 **ScheduleID**，后续为多家公司亚赔、大小球等（以 `;` 分段）。本仓库解析器取 **首段** 内主盘口上下盘水位填充 `AsianAvrHome` / `AsianAvrAway`（若可解析）。

## 多公司亚盘矩阵（`handicap_list`）

| 用途 | URL 模板 | Referer |
|------|------------|---------|
| `sData[sid]=[[...],...]` | `https://livestatic.titan007.com/vbsxml/sbOddsData.js?r=007{ts}` | `https://live.titan007.com/oldIndexall.aspx` |

每个 `sid` 对应 **6 行**（常见为多家/多类盘口打包）；每行 9 个数字：`home水, 盘口, away水, 初home水, 初盘, 初away水, ...`。映射为 `HandicapRow`（公司名暂用 `公司1`…`公司6`，`bookmaker_id` 用 `1000+行号`）。

## 百家欧赔与 Kelly（`euro_trend`）

| 用途 | URL 模板 | Referer |
|------|------------|---------|
| `game=Array("...")` | `https://1x2d.titan007.com/{ScheduleID}.js` | `https://live.titan007.com/` 或 `https://bf.titan007.com/` |

每行 `|` 分隔；**3–5** 为即时胜平负赔率，**10–12** 为初盘；**17–19** 作 Kelly 近似填入；时间字段见脚本内解析（格式与 SPDEX ISO 不同，已做容错）。

## 列表增量（可选）

| 用途 | URL 模板 | Referer |
|------|------------|---------|
| 部分场次状态增量 | `https://livestatic.titan007.com/vbsxml/change2_ut.xml?r=007{ts}` | `https://live.titan007.com/oldIndexall.aspx` |

`changeN_ut.xml` 中 **N 随页面类型变化**；`oldIndexall` 当前环境仅 `change2` 为 200，其它 N 可能 404。

## WebSocket（未在 CLI 中实现）

`wss://live.titan007.com/stream?channels=...&token=...` 见页面 `SocketHelper.js`；高实时场景可自行扩展，本 CLI 以 **HTTP 轮询** 为主。

## SPDEX 字段矩阵（实现状态）

| SPDEX / `Predictor` | Titan007 来源 | 状态 |
|---------------------|---------------|------|
| `Match.event_id` | ScheduleID | 已用 |
| `home` / `away` | bfdata A[] **5 / 8**（简中主客），**7 / 10** 英名作回退 | 已用 |
| `match_time` | bfdata A[] 12（**JS 月 0–11**）；jc `bf_jc` 计划开赛 | 已用 |
| `asian_line` | bfdata A[] 29，可与 ch_goalbf3 首段核对 | 已用 |
| `AsianAvrHome/Away` | ch_goalbf3 首段主盘口 | 已用（缺省回退 sData 首行） |
| `handicap_list` | sbOddsData `sData[sid]` | 已用 |
| `euro_trend` | 1x2d `{sid}.js` game 行 | 已用（Kelly 为站方字段近似） |
| `BfIndex* / BfAmount* / BfPayout* / BfOdds*` | 可选：澳客竞彩 [必发盈亏](https://www.okooo.cn/jingcai/shuju/betfa/) HTML（GB18030）；`--okooo-bifa` 或 `TITAN007_OKOOO_BIFA=1` 时在 `refresh_feeds` 拉取；**优先** `OKOOO_TITAN_MAP_PATH` / `TITAN007_OKOOO_IDS` 与澳客 `okooo_id` 对齐，否则队名+时间启发式；明细页见 `/soccer/match/{id}/exchanges/detail/` | **启发式或映射**（`BfIndex*` 暂用页内「必发比例」列数值） |
| `price_volume` | 无原生分时成交 | **优先**：若已解析出澳客 ``okooo_id``（``_okooo_match_id`` 或 ``TITAN007_OKOOO_IDS`` / 映射）且环境变量 **`OKOOO_COOKIE`** 非空，则 GET [exchanges/detail](https://www.okooo.cn/soccer/match/{id}/exchanges/detail/) HTML（与 ``okooo_ah_cli`` 同源解析）；**否则**回退 **``.titan007_snapshots`` 快照合成** |

## 澳客竞彩「必发盈亏」（可选 Bf*）

| 用途 | URL | 说明 |
|------|-----|------|
| 列表页 HTML | `https://www.okooo.cn/jingcai/shuju/betfa/`（或 `OKOOO_BETFA_URL`） | 服务端渲染表；编码 **GB18030**；SSL 在部分环境需跳过校验（脚本已重试） |

- **CLI**：`titan007_ah_cli.py --okooo-bifa …` 或环境变量 **`TITAN007_OKOOO_BIFA=1`**；`--no-okooo-bifa` 显式关闭。  
- **Cookie**：可选 **`OKOOO_COOKIE`**（未设时回退 `TITAN007_COOKIE`）。写入浏览器登录 www.okooo.cn 后的 Cookie 后，**Titan007 `predict` / `price_volume`** 可拉 [必发成交明细](https://www.okooo.cn/soccer/match/1317856/exchanges/detail/) 页（HTML），供「必发成交走势」信号使用（实现为 ``okooo_bifa.fetch_okooo_exchanges_detail_series``，复用 ``okooo_ah_cli`` 表格解析）。
- **匹配**：与球探 **ScheduleID 无官方对应**；`okooo_bifa.best_okooo_bifa_match` 用 **队名模糊相似度 + 开赛墙钟（与 `TITAN007_SCHEDULE_TZ` 一致）约 90 分钟内** 选最佳一场；主客列可能与球探左右相反时 **`_okooo_bifa_swapped`**。  
- **显式 ID 映射（推荐）**：在 `refresh_feeds` 时读取 **`OKOOO_TITAN_MAP_PATH`**（JSON，如 `{"2906745": 1316319}`）和/或 **`TITAN007_OKOOO_IDS`**（逗号分隔 `球探ID=澳客ID` 或 `球探ID:澳客ID`）；合并时 **优先按澳客 `okooo_id` 命中**，再回退启发式。映射命中后 `raw["_okooo_exchanges_detail"]` 为 [成交明细页](https://www.okooo.cn/soccer/match/1316319/exchanges/detail/) 模板 URL（替换其中的比赛 ID）。若映射中的澳客 ID 不在当前必发列表页解析结果中，则 **`_okooo_id_map_miss`**。示例见仓库 `fixtures/okooo_titan_map.example.json`。
- **字段**：`BfAmount*`≈总成交额列，`BfPayout*`≈模拟盈亏，`BfOdds*`≈必发赔率，`BfIndex*` 当前写入页内 **必发比例（%）** 数值；冷热/市场指数在 **`_okooo_cold_*` / `_okooo_market_*`**。

## 环境变量

| 变量 | 含义 |
|------|------|
| `TITAN007_COOKIE` | 可选 Cookie，部分域若启用登录校验时使用 |
| `TITAN007_POLL_SEC` | `watch` 默认轮询间隔（秒） |
| `TITAN007_SNAPSHOT_DIR` | 快照目录，默认 `.titan007_snapshots` |
| `TITAN007_SCHEDULE_SOURCE` | 赛程索引：`bf`、`live`、`jc`；**未设时 CLI 默认 live**；传了 ``--schedule-source`` 则以 CLI 为准 |
| `TITAN007_SCHEDULE_TZ` | 解析开赛时间的 IANA 时区（bf 列 12 / jc 列 1），默认 `Asia/Shanghai` |
| `TITAN007_OKOOO_IDS` | 逗号分隔 `ScheduleID=澳客比赛ID`（或 `:`），与 JSON 映射合并时 **覆盖同键**；例 `2906745=1316319` |
| `OKOOO_TITAN_MAP_PATH` | JSON 文件路径，对象键为球探 ID、值为澳客 `/soccer/match/{id}/` 的 `id`（见 `fixtures/okooo_titan_map.example.json`） |
| `OKOOO_BETFA_URL` | 覆盖默认澳客列表页 URL |
| `TITAN007_OKOOO_BIFA` | 设为 `1`/`true`/`on` 时，`refresh_feeds` 拉取澳客必发盈亏页并写入 `Bf*`（与 `--okooo-bifa` 等价；`--no-okooo-bifa` 可覆盖关闭） |
| `OKOOO_COOKIE` | 访问澳客 HTML 的 Cookie（未设则用 `TITAN007_COOKIE`）；**betfa 列表**与 **``/soccer/match/{okooo_id}/exchanges/detail/`` 成交明细**（Titan007 ``price_volume`` / 必发成交走势）共用 |
| `TITAN007_ASIAN_SIGN_FROM_WATER` | 默认 `1`：当 ``AsianAvrLet`` 为正且主客均水可解析时，用 **主队水位低于客队** 推断 **主让** 并把盘口改为 **负号**（与 [亚指页](https://vip.titan007.com/AsianOdds_n.aspx)「主队|盘|客队」一致）；设 `0` 关闭 |
| `TITAN007_ASIAN_SIGN_FROM_ML` | 默认 `1`：在 **1x2 欧赔均值** 与（可选）**澳客 `BfOdds*`** 写入 `raw` 之后二遍校正——当 **半球~一球** 常见 **受让方低水** 使水位启发式仍保留 **正盘**、但胜赔明示 **主队更热** 时，将盘口改为 **负号**（与 `upper_lower_teams` 的「主让」一致）；客队明显更热而盘口为负则翻成正盘。设 `0` 关闭 |
