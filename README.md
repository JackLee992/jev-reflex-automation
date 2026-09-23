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
- 工程化：固定模型版本、严格校验响应、24 小时缓存和可回放审计记录。

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
即可失效。

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
  --audit .jev/events.jsonl
```

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
| endpoint 默认值覆盖 | `JEV_ENDPOINT` |
| 模型默认值覆盖 | `JEV_MODEL` |

命令行的 `--endpoint` / `--model` 优先于环境默认值；通用判断请求中显式写入的
`request.model` 也优先于 `JEV_MODEL`。固定版本模型是阈值校准和可回放判断的一部分，
通常不应改成滚动别名。

## 工作原则

1. 事实先在本地采集；能由代码计算或工具观察的内容不交给 JEV 猜测。
2. 只发送回答当前问题所需的最小状态，并在发送前脱敏。
3. 问题必须自包含，候选项由代码定义；不确定时保留 `abstain` 或人工接管选项。
4. JEV 概率只用于路由和排序，不能证明事实或授权高风险动作。
5. 所有修改通过正常工具完成，随后重新观察并验证明确的后置条件。
6. 保存模型版本、请求哈希、答案、阈值、动作、证据与结果，持续补充回放样本。

新集成应先运行在 shadow mode：记录 JEV 会如何判断，但暂不让它控制既有确定性流程；
只有代表性回放集达到要求后才提升权限。

## 安全边界

- 不发送密码、cookie、私钥、授权头、会话信息或未筛选的仓库与日志全文；
- 不让 JEV 生成代码、命令、利用载荷或对外发布内容；
- 删除、付款、发消息、发布、授权和其他高影响动作必须由确定性策略与用户授权控制；
- `confidence` 不是正确性证明，也不提供权限；
- 逆向工程只能在用户提供并明确授权的目标范围内进行；
- helper 遇到残留凭据、不安全远端、模型漂移、异常响应或过期跨域缓存时会 fail closed。

## 验证

```bash
bash -n scripts/setup_jev.sh
python3 skills/jev-engineering/scripts/test_jev_tools.py
python3 skills/jev-engineering/scripts/jev_judge.py --help
```

测试 helper 不需要 API key，也不会产生网络请求。真实 `run` 或日志工具的 `--send` 才会
调用 JEV。

## 目录

```text
scripts/setup_jev.sh                         # 一键安装技能并安全写入 key
skills/jev-engineering/SKILL.md             # Codex 技能入口和强制工作流
skills/jev-engineering/agents/openai.yaml   # 技能元数据
skills/jev-engineering/references/           # 开发、调试、逆向、自动化与 schema 指南
skills/jev-engineering/scripts/jev_judge.py # 通用校验、脱敏、调用、缓存和审计 helper
skills/jev-engineering/scripts/jev_log_triage.py # 日志预过滤与判断 helper
skills/jev-engineering/scripts/test_jev_tools.py # 离线回归测试
```

## License

MIT
