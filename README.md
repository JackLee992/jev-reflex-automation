# JEV Engineering Skill for Codex

把 [TypeSafe JEV](https://typesafe.ai) 作为 Codex 工作流里的轻量判断层，用于开发分诊、
调试与日志分析、授权范围内的逆向工程，以及浏览器、桌面和设备自动化。

JEV 只负责有边界的 `Choice`、`Noul` 和 `Score` 判断。事实采集、计算、修改、权限控制与
结果验证仍由 Codex 和确定性工具完成。

## 能做什么

- 开发：任务分类、变更风险、评审优先级和完成证据判断；
- 调试：本地筛选日志、脱敏、定位第一处可操作原因和选择下一条只读诊断；
- 逆向：在用户已授权的目标范围内，对静态或动态证据做分类和排序；
- 自动化：支持 `observe → decide → policy → act → verify` 闭环；
- 上下文保留：判断旧工具调用/结果是否仍有价值，保留内容原样而不是生成摘要；
- 治理：版本化阈值策略、decision/outcome 配对、Brier/ECE 报告与漂移回退；
- 工程化：固定模型版本、严格校验响应、24 小时缓存和可回放审计记录。

## 参考的开源实现

本技能直接审阅并吸收了这篇
[`Jev Engineering - How to make money with Jev: Best repos`](https://x.com/me_barnyx/status/2101976764779999381?s=46)
点名项目的源码模式，而不是只复述文章观点：

- [`fast-jev-compaction`](https://github.com/tamaratran/fast-jev-compaction)：工具调用/结果成对保留、只选择不改写、请求拟合和失败回退；
- [`jev-ultrafast`](https://github.com/browser-use/jev-ultrafast)：operation/target 同请求扇出、状态 freshness、一次性决策和执行后重观察；
- [`typesafe-computer-use`](https://github.com/awlevin/typesafe-computer-use)：感知/判断/执行分层、dry-run、卡死检测和逐步回放；
- [`jev-codex-router`](https://github.com/0xNatoshi/jev-codex-router)：bounded dossier、版本化路由、decision lease、shadow/kill switch/fallback；
- [`jev-mcp`](https://github.com/BYK/jev-mcp)：批量 map、标注集 eval、阈值扫描、Brier/ECE 和 worst misses；
- [`typesafe-ai/skills`](https://github.com/typesafe-ai/skills)：live docs 优先、select-not-generate、自包含问题和共享 state fan-out。

精确审阅 commit、采用项、本地加强项和明确未照搬的部分见
[`open-source-patterns.md`](skills/jev-engineering/references/open-source-patterns.md)。本仓库没有静默安装
这些项目；helper 均为独立实现，并继续使用现有的脱敏、固定模型和严格响应校验边界。

## 一键配置

克隆仓库后运行：

```bash
git clone https://github.com/JackLee992/jev-reflex-automation.git
cd jev-reflex-automation
bash scripts/setup_jev.sh
```

按照提示粘贴 TypeSafe API key 即可。输入过程不可见，key 不会出现在命令行参数或
shell history 中。脚本会自动：

1. 校验 key 的基本格式；
2. 原子写入 `~/.config/typesafe/api_key` 并设置为 `600` 权限；
3. 将本仓库的 `skills/jev-engineering` 链接到 Codex skills 目录；
4. 保留已有的其他技能，并拒绝覆盖同名但来源不同的技能。

若凭证文件已经存在，脚本会在提示后用通过格式校验的新 key 原子替换它。脚本会主动
关闭 shell xtrace，避免用户误用 `bash -x` 时把输入打印到终端。

配置后只检查文件和权限，不显示 key：

```zsh
test -s "$HOME/.config/typesafe/api_key" && \
  stat -f '%Lp %N' "$HOME/.config/typesafe/api_key"
```

正常输出应以 `600` 开头。

如果设置了 `CODEX_HOME`，技能会安装到 `$CODEX_HOME/skills/jev-engineering`；否则安装
到 `~/.codex/skills/jev-engineering`。配置完成后，新开一个 Codex 任务即可使用：

```text
使用 $jev-engineering 分析 ./logs/app.log，直接调用 JEV，
定位第一处可操作原因，并给出下一条只读诊断。
```

如果 key 曾经出现在聊天、截图、终端命令或 Git 历史中，应先在 TypeSafe 后台撤销，
再用脚本写入新 key。

### 临时会话配置（不落盘）

永久配置应使用一键脚本，避免非原子写入或意外跟随符号链接。如需只在当前 zsh 会话
临时使用 key，可以关闭 xtrace 后读取到环境变量：

```zsh
set +x
read -rs "TYPESAFE_API_KEY?粘贴 TypeSafe API key: "
printf '\n'
export TYPESAFE_API_KEY
```

运行时 helper 会优先读取当前进程的 `TYPESAFE_API_KEY`，否则读取
`~/.config/typesafe/api_key`。临时变量会提供给当前 shell 启动的子进程；关闭该 shell
即可失效。读取落盘 key 时会拒绝符号链接、非普通文件、非当前用户所有、组/其他用户
可读写或异常大的文件，而不是只相信路径名。

如果只需要手动安装技能链接：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$(pwd)/skills/jev-engineering" \
  "${CODEX_HOME:-$HOME/.codex}/skills/jev-engineering"
```

## 在 Codex 中使用

直接在任务中点名 `$jev-engineering`，并描述证据来源、判断目标和允许的动作范围。例如：

```text
使用 $jev-engineering 分析这次测试失败，判断最可能的故障类别，
先执行只读诊断，不要修改代码。
```

```text
使用 $jev-engineering 检查这段自动化轨迹是否卡住；允许继续、重试或升级给我，
每次动作后重新观察并验证结果。
```

```text
使用 $jev-engineering 对这个我有权分析的 APK 做证据分诊，
区分已观察事实和 JEV 推断，不执行破坏性操作。
```

JEV 调用成本不是确认点：完成本地最小化和脱敏后，Codex 可以直接调用。数据是否可以
外发、用户是否授权以及动作是否高风险，仍然是独立的安全门槛。

## 命令行助手

设置技能目录：

```bash
JEV_SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/jev-engineering"
```

仅校验、脱敏并预览请求，不访问网络：

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_judge.py" check request.json
```

发送请求，同时启用按 endpoint、模型和校验器版本隔离的缓存与审计：

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_judge.py" run request.json \
  --cache-dir .jev/cache \
  --audit .jev/events.jsonl \
  --output .jev/judgment.json
```

TypeSafe key 只会发往规范化后的官方 endpoint，HTTP 重定向和任意自定义 origin 都会在发送前
被拒绝。仅测试 `jev_judge.py` 时可显式传 `--allow-localhost --endpoint http://127.0.0.1:PORT/...`；
该模式永远不读取或发送 TypeSafe key，也不具备自动决策信任。

先在本地生成紧凑的日志证据窗口：

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_log_triage.py" app.log \
  --goal "find the first actionable cause"
```

确认数据边界后直接发送给 JEV：

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_log_triage.py" app.log \
  --goal "find the first actionable cause" \
  --send \
  --cache-dir .jev/cache \
  --audit .jev/events.jsonl
```

### 批量 map 与离线评测

`jev-mcp` 启发的本地 helper 把单条试验、批量 map 和标注集 eval 分开。数据集每行至少包含
稳定 `id` 与对象型 `state`；eval 数据再包含 `label`。契约文件包含 `questions`（map）或
`variants`（eval）。默认只做离线预览，只有 `--send` 才会调用 JEV：

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" map items.jsonl questions.json \
  --output .jev/map-preview.json

python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" map items.jsonl questions.json \
  --send --output .jev/map.json \
  --cache-dir .jev/cache --audit .jev/events.jsonl
```

先在 tuning 集选择问题写法和 Noul 阈值，再把阈值冻结到不相交的 holdout 集。holdout 不允许
根据自己重新选阈值：

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" eval tuning.jsonl variants.json \
  --dataset-role tuning --send --output .jev/tuning.json

python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" eval holdout.jsonl variants.json \
  --dataset-role holdout --threshold 0.75 --send --output .jev/holdout.json
```

`--threshold` 只适用于恰好一个 Noul variant；多个 Noul variant 必须使用精确逐项映射，
例如 `--thresholds '{"actionable_v1":0.72,"actionable_v2":0.81}'`，避免把一个写法的阈值
误套到另一个写法。

### Tool trace 选择式压缩

`fast-jev-compaction` 启发的 helper 只处理完整、唯一、顺序正确且共享 `call_id` 的
`tool_call` / `tool_result` 对。默认只预览；保留内容不会由模型重写，任何批次失败都会保留
整条原始脱敏轨迹。配对的两项都必须显式带有 `"complete": true`；缺失或 `false` 会保护
整对而不是猜测它已经结束。包含 traceback、error、panic 或 timeout 的正文也会保护整对，
直到同一记录带有结构化 `resolved: true` 或明确 resolved 状态；正文自称“fixed”不算。
输入示例：

```json
{"id":"call_1","kind":"tool_call","call_id":"c1","content":"run tests","complete":true}
{"id":"result_1","kind":"tool_result","call_id":"c1","content":"2 failed","complete":true}
```

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_trace_compact.py" trace.jsonl \
  --output .jev/trace-preview.json

python3 "$JEV_SKILL_DIR/scripts/jev_trace_compact.py" trace.jsonl --send \
  --policy-version trace-v1 \
  --truncate-min-confidence 0.90 \
  --drop-min-confidence 0.95 \
  --truncate-bytes 1024 \
  --cache-ttl-seconds 0 --audit .jev/events.jsonl \
  --output .jev/trace-compacted.json
```

这些阈值只展示 CLI 形状，不是通用生产阈值。真实 lossy policy 必须用同一模型、问题契约、
state projection 和风险类别的标注集校准；`drop` 阈值必须大于或等于 `truncate` 阈值。

### 版本化 policy 与 outcome 台账

`jev-codex-router` 与 `jev-mcp` 启发的 policy helper 会验证模型、问题契约 hash、state
projection、动作/风险、审批、到期时间、校准报告和显式阈值；缺失、过期、漂移或高风险时
降为 shadow/review。decision 与后来观测到的 outcome 通过唯一 ID 追加配对：

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" check-policy policies.json

python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" record-decision \
  policies.json route_policy .jev/judgment.json \
  --model jev-1.13.0 \
  --proposed-action inspect_route \
  --events .jev/policy-events.jsonl

python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" record-outcome \
  .jev/policy-events.jsonl DECISION_ID \
  --outcome-json '"inspect"'

python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" report \
  policies.json route_policy .jev/policy-events.jsonl --bins 10
```

这里的 outcome 必须是后来确定的 ground-truth label；Choice 示例只能写契约允许的
`"inspect"` 或 `"review"`。治理原则、概率口径、Brier/ECE 与上线清单见
[`governance.md`](skills/jev-engineering/references/governance.md)，可校验的完整 registry 字段
见下面的示例文件。

可直接验证 schema 的起点在
[`policy-shadow.example.json`](skills/jev-engineering/examples/policy-shadow.example.json)，对应请求在
[`route-request.json`](skills/jev-engineering/examples/route-request.json)。示例中的 baseline digest
和指标只是格式占位，且 policy 明确为 draft + uncalibrated + shadow，绝不能当作上线批准。
生产 `auto` 只接受 `jev_judge.py run --output` 生成的官方、实时、非缓存完整 judgment
envelope；裸 typed answer、localhost 测试结果和本地 cache 命中仍可做预览或评测，但会被
强制留在 shadow，任意其他自定义 endpoint 则直接拒绝。trace 压缩同样不允许 cache 驱动
drop/truncate。若 policy 要求健康门禁，应传 `--health-events`
让 helper 从当前 ledger 重新计算，外部自报的 `stable` report 只能降级、不能开启自动执行。
需要生成 auto 候选判断时，在对应 `jev_judge.py run` 上使用 `--cache-ttl-seconds 0` 强制实时调用。

`.jev/` 中是缓存和脱敏后的审计记录，仍可能包含项目上下文。不要提交到 Git；将它加入
业务项目的 `.gitignore`，或把缓存与审计路径放在仓库外。

## 默认配置

| 配置 | 默认值 |
|---|---|
| API endpoint | `https://api.typesafe.ai/v1/systemone` |
| 模型 | `jev-1.13.0` |
| 缓存有效期 | 24 小时 |
| API key 环境变量 | `TYPESAFE_API_KEY` |
| API key 文件 | `~/.config/typesafe/api_key` |
| endpoint 参数/环境变量 | `--endpoint` / `JEV_ENDPOINT`；生产只接受上述官方值 |
| 模型默认值覆盖 | `JEV_MODEL` |

命令行的 `--endpoint` / `--model` 优先于环境默认值，但 endpoint 覆盖不会放宽官方服务
绑定；通用判断请求中显式写入的
`request.model` 也优先于 `JEV_MODEL`。固定版本模型是阈值校准和可回放判断的一部分，
通常不应改成滚动别名。

## 工作原则

1. 事实先在本地采集；能由代码计算或工具观察的内容不交给 JEV 猜测。
2. 只发送回答当前问题所需的最小状态，并在发送前脱敏。
3. 问题必须自包含，候选项由代码定义；不确定时保留 `abstain` 或人工接管选项。
4. JEV 概率只用于路由和排序，不能证明事实或授权高风险动作。
5. 所有修改通过正常工具完成，随后重新观察并验证明确的后置条件。
6. 阈值属于版本化 policy；缺策略、样本不足、过期或漂移时退回 shadow/review。
7. 先记录唯一 decision，再追加真实 outcome；request hash 不能代替 decision ID。
8. 保存模型版本、问题契约、答案分布、动作、证据与结果，持续补充回放样本。

`Choice` / `Score` 的 `confidence` 是分布集中度，不等同于决策正确概率。Brier/ECE 使用
与实际标签对应的概率：例如 `Choice.probabilities[selected]`，而不是直接套用
`confidence`。上下文压缩也遵循同一边界：决定哪些旧工具记录保留，但不改写保留下来的
内容；用户目标、授权、安全约束、未解决错误和验证证据始终固定保留。

新集成应先运行在 shadow mode：记录 JEV 会如何判断，但暂不让它控制既有确定性流程；
只有代表性回放集达到要求后才提升权限。

## 安全边界

- 不发送密码、cookie、私钥、授权头、会话信息或未筛选的仓库与日志全文；
- 不让 JEV 生成代码、命令、利用载荷或对外发布内容；
- 删除、付款、发消息、发布、授权和其他高影响动作必须由确定性策略与用户授权控制；
- `confidence` 不是正确性证明，也不提供权限；
- 逆向工程只能在用户提供并明确授权的目标范围内进行；
- helper 遇到残留凭据、非官方远端、重定向、模型漂移、异常响应或不安全缓存时会 fail closed；
- cache 是私有但未认证的本地加速层，不能授权 policy auto 或有损 trace 压缩。

## 验证

```bash
bash -n scripts/setup_jev.sh
python3 -m unittest discover \
  -s skills/jev-engineering/scripts \
  -p 'test_jev_*.py'
python3 skills/jev-engineering/scripts/jev_judge.py --help
```

测试 helper 不需要 API key，也不会产生网络请求。`jev_judge.py run`，以及日志、batch、
trace helper 的 `--send` 才会调用 JEV；其余默认预览或本地计算。

## 目录

```text
scripts/setup_jev.sh                         # 一键安装技能并安全写入 key
skills/jev-engineering/SKILL.md             # Codex 技能入口和强制工作流
skills/jev-engineering/agents/openai.yaml   # 技能元数据
skills/jev-engineering/examples/            # 可验证但始终 shadow 的起步示例
skills/jev-engineering/references/           # 工作流、治理、schema 与开源模式映射
skills/jev-engineering/scripts/jev_judge.py # 通用校验、脱敏、调用、缓存和审计 helper
skills/jev-engineering/scripts/jev_log_triage.py # 日志预过滤与判断 helper
skills/jev-engineering/scripts/jev_batch.py # 批量 map、tuning/holdout 评测
skills/jev-engineering/scripts/jev_trace_compact.py # tool trace 成对选择式压缩
skills/jev-engineering/scripts/jev_policy.py # 版本化路由与 decision/outcome 台账
skills/jev-engineering/scripts/test_jev_*.py # 全部离线回归测试
```

## License

MIT
