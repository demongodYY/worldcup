```
python3 worldcup_ah_cli.py --help
python3 worldcup_ah_cli.py predict --help
python3 worldcup_ah_cli.py upcoming --help
python3 worldcup_ah_cli.py snapshot --help
python3 worldcup_ah_cli.py trend --help
python3 worldcup_ah_cli.py watch --help
python3 worldcup_ah_cli.py sources --help
```

核心信号：

- 必发指数、成交额、盈亏指数、必发价格确认
- 必发成交走势
- 亚盘水位和主流公司一致性
- 欧赔/Kelly 与平局风险
- 盘口合理性、盘口深度、深盘打穿能力、赢盘门槛风险
- 资金/盘口弹性：热度变化后，盘口是否降水、升盘或反向放松
- 高低水价值：把水位转换成隐含赢盘率，比较模型概率是否给出足够赔率补偿
- 外部赔率/实力校验：预留 The Odds API、Elo、伤停/阵容模型字段
- 本地 `.spdex_snapshots/` 快照趋势

推荐逻辑：

- `score` 是基础模型综合分，仍按阈值形成原始模型判断：上盘、下盘或观望。
- 最终 `推荐` 是二元购买方，只输出上盘或下盘。
- 当原始分偏上盘但出现中深盘赢盘门槛高、快照走弱、热度升高但上盘升水、盘口未升深、主流公司分歧、外部源背离等信号时，会把 `purchase` 分数向下盘侧修正。
- 高水只有在模型估计赢盘率明显高于市场隐含概率时才加分；低水如果已经偏贵但缺少模型溢价，也会扣分。
- `置信度` 表示最终二元购买方的把握，不等于命中概率；数据越完整、基础分越强、快照和盘口越同向，置信度越高。风险反向或数据缺口会降低置信度。

自动快照：

```bash
python3 worldcup_ah_cli.py watch --limit 20 --verbose
```

`watch` 会扫描未来 24 小时比赛，按 T-24h/T-8h/T-4h/T-60m/T-30m/T-15m 自动保存快照；后三个窗口同时打印预测。
