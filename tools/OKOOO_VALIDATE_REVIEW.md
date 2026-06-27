# Okooo 快照重放复盘（`validate-snapshots`）

比分表：`tools/okooo_validate_scores.json`。命令：

```bash
python3 okooo_ah_cli.py --no-dotenv validate-snapshots
```

存在「有方向但未中」场次时，**默认退出码为 1**（便于脚本/CI 发现回测未覆盖）。若仅查看统计、不要求零未中，可加：

```bash
python3 okooo_ah_cli.py --no-dotenv validate-snapshots --allow-miss
```

## P0 验证口径（2026-06-25 起）

- 每场只验证一次：取最后两条合格赛前快照，以两次预测 score 的中位数决定方向。
- 结算使用两条中最新一条的真实盘口和水位；不足两条的比赛不进入验证。
- 四分之一盘拆成两半结算，输出全赢、半赢、走水、半输、全输。
- 按预测时刻水位计算一单位下注净收益和 ROI；缺水位时方向仍可展示，但收益标为 unavailable。
- 赛后快照、历史导入和旧版固定占位字段自动排除。
- `--walk-forward` 只读取快照当时保存的预测，并要求版本与 `tools/okooo_model_freeze.json` 一致。

当前本地历史重放：全赢 21、半赢 3、走水 2、半输 4、全输 8，净收益 **+10.980u / 38 注，ROI 28.9%**。本轮“临场极值回撤 + 深盘退浅保护”优化前同批样本为 **+7.259u / 38 注，ROI 19.1%**；仅加入极值回撤但未加深盘保护时为 **+9.125u / 38 注，ROI 24.0%**。历史重放仍属于样本内验证，正式结论以后续冻结版本 walk-forward 为准。

**对阵来源**：表中 `event_id` 与比分须能对照 [FIFA 世界杯官方赛程](https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/scores-fixtures) 及公开战报核实。此前一批「历史复盘」页签下的 Jun12–14 合成快照曾绑定错误对阵，已从本仓库比分表与快照中移除。

### 2026-06-15 竞彩页导入（betfa + 盘口 + 凯利）

以下四场由 [`tools/import_okooo_jingcai_snapshots.py`](import_okooo_jingcai_snapshots.py) 从澳客 [`betfa/2026-06-15`](https://www.okooo.cn/jingcai/shuju/betfa/2026-06-15/)、[`pankou/2026-06-15`](https://www.okooo.cn/jingcai/shuju/pankou/2026-06-15/)、[`peilv/2026-06-15`](https://www.okooo.cn/jingcai/shuju/peilv/2026-06-15/) 合并生成 `.okooo_snapshots/{id}.jsonl`（需联网执行脚本；缓存 HTML 在 `.okooo_import_cache/`）。

| event_id | 对阵 | 澳客展示比分（复盘） |
|----------|------|----------------------|
| 1315893 | 西班牙 vs 佛得角 | 0-0 |
| 1315887 | 比利时 vs 埃及 | 1-1 |
| 1315894 | 沙特 vs 乌拉圭 | 1-1 |
| 1315888 | 伊朗 vs 新西兰 | 2-2 |

## 旧版全量重放（已废弃口径，仅留档）

| event_id | 对阵 | 模型/推荐 | 比分 | 有方向时 |
|----------|------|-------------|------|----------|
| 1315853 | 墨西哥 vs 韩国 | 上盘 | 1-0 | hit |
| 1315854 | 捷克 vs 南非 | 观望 | 1-1 | na（平局） |
| 1315857 | 美国 vs 澳大利亚 | 上盘 | 2-0 | hit |
| 1315858 | 土耳其 vs 巴拉圭 | 观望 | 0-1 | na |
| 1315863 | 加拿大 vs 卡塔尔 | 上盘 | 6-0 | hit |
| 1315864 | 瑞士 vs 波黑 | 上盘 | 4-1 | hit |
| 1315871 | 巴西 vs 海地 | 上盘 | 3-0 | hit |
| 1315872 | 苏格兰 vs 摩洛哥 | 上盘 | 0-1 | hit |
| 1315887 | 比利时 vs 埃及 | 上盘 | 1-1 | miss |
| 1315888 | 伊朗 vs 新西兰 | 上盘 | 2-2 | miss |
| 1315893 | 西班牙 vs 佛得角 | 上盘 | 0-0 | miss |
| 1315894 | 沙特 vs 乌拉圭 | 观望（模型上盘） | 1-1 | na |
| 1316314 | 乌兹别克斯坦 vs 哥伦比亚 | 上盘 | 1-3 | hit |
| 1316320 | 加纳 vs 巴拿马 | 上盘 | 1-0 | hit |

**统计**：有方向 **11** 场，**命中 8 / 未中 3**，命中率 **73%**；观望/走水 **3**。

### 观望场次（复盘口径）

| event_id | 说明 |
|----------|------|
| 1315854 | 捷克 -0.5 平南非：综合分近阈值、欧赔偏下盘，**观望**避免浅盘平局噪声。 |
| 1315858 | 土耳其 -0.5 负：模型与购买门控偏谨慎，**观望**优于追浅热上盘。 |
| 1315894 | 沙特受让对乌拉圭平局：模型偏上盘但购买门控为**观望**（与临场门槛类信号一致时计入 na）。 |

### 一球盘「不强吃上盘」门控（单元测）

- 逻辑见 `purchase_decision_from_signals`：**整数 +1、上盘为客场让球方**且门槛/平局风险已预警时，购买侧不强吃上盘（净胜 1 走水带）。
- 回归用例：`fixtures/okooo_positive_one_ball_gate.jsonl`（`WorldCupAhCliTests.test_positive_one_ball_guest_favorite_does_not_force_upper_bet`）。

## 算法改动要点（便于后续继续调）

1. **深盘强队**：`盘口合理性` 在「实际盘口偏深」且 1X2 概率仍明显倾向上盘时，对极端负分回拉。  
2. **深盘 + 欧赔挺上盘**：`亚盘水位` 在必发/亚盘背离后，用已有 `handicap_upper_water_rise_mitigated_by_euro` 判定欧赔支撑时回拉负分；`赢盘门槛风险` 对欧赔/Kelly 仍挺上盘时整体扣分打折。  
3. **美国类边际**：`marginal_model_score_lift_for_mid_deep_upper` — 综合分在阈值下沿、盘口深度 0.75–1.25、欧赔/市场平衡较强、平局风险未极端（`draw_risk > -0.22`）时小幅上调，避免漏掉净胜盘。  
4. **深让净胜边际**：`marginal_deep_upper_cover_score_lift` — 深度 ≥1.25、综合分略偏下盘（-0.12～0.05）、（欧赔 Kelly ≥0.30 **或** 欧赔中性且深度≥2 且外部≥0.15）且外部挺上盘时小幅上调。  
5. **阈值**：`UPPER_THRESHOLD` 保持 **0.12**，避免浅盘大热（如土耳其）被抬成上盘。  
6. **一球走水带**：`purchase_decision_from_signals` — 盘口为 **+1**（上盘=客场让球方）、赢盘门槛与平局风险已预警且综合分未拉开时，购买侧 **不强吃上盘**，减少净胜 1 走水类推荐。

### 必发页已赛展示（无 VS）解析

`okooo_ah_cli.match_meta` / `parse_betfa_html` 已兼容澳客必发列表里 **`<span>主队</span><em>(盘)</em><strong>比分</strong><b>客队</b>`** 结构（与未赛的 `VS` 行并存）。单元测：`test_okooo_bifa.OkoooAhCliBetfaParseTests`。
