# CLI Agent 化改造计划（面向不支持 MCP 的 Agent）

> 状态：**已实现（M1-M4 + 收尾，2026-08-12）**。RAG 相关项 deferred。
> 动机：pi 等编码 agent 不内置 MCP 客户端（pi 文档明确 "no built-in MCP"），只能通过 bash 调用 CLI。
> 本文档整理全部合理且需要的改动项，按优先级分组；每项给出动机（含实测证据）、接口契约、影响范围、测试与成本。

## 1. 背景与现状

EmbPilot 的 CLI 与 MCP 共享 `dispatch_tool` + `build_tool_definitions` 同一分发层，
已提供三种模式：

- `embpilot`（无参）→ MCP stdio server（对不支持 MCP 的 agent 无用）。
- `embpilot tools` → 工具目录。
- `embpilot tool <name> --json '<args>'` → one-shot，**每次全新进程**。
- `embpilot shell` → REPL，进程内保持 `SessionManager`。

### 1.1 已实测的缺口

| # | 缺口 | 证据 |
|---|------|------|
| G1 | one-shot 无法跨调用保持连接 | `_run_one_shot` 每次新建 `SessionManager` 并 `shutdown()`；`connect` 与 `send_command` 无法跨两次 bash 调用 |
| G2 | CLI 无只读捕获：只能看"命令响应"，不能看"设备自吐日志" | ring buffer 仅通过 MCP resource `device://live_log` 暴露，CLI 侧无对应工具 |
| G3 | 无"等到某模式/超时"的观察原语 | `monitor` 只能手动 `stop`，无 `--until` / `--timeout` |
| G4 | 跨轮次保活无可靠机制 | FIFO 后台喂 `embpilot shell` 实测在 Git Bash/Windows 上进程悬挂、输出为空、残留僵尸进程 |
| G5 | `search_history_logs` 绑定活动连接 | 断开后无法搜索历史会话（`NO_ACTIVE_DEVICE`），"列会话→搜旧日志→导出"工作流断裂 |
| G6 | 参数引号在不同 shell 下脆弱 | `--json '{"port":"COM3"}'` 在 bash / PowerShell / GBK cmd 引号规则不同 |
| G7 | shell 输出混入 banner/帮助文本 | `--json-output` 下结果虽为 JSON，但 banner 与帮助行混在 stdout，agent 需过滤 |

### 1.2 本质限制

设备连接无法跨进程复活。进程退出后 ring buffer（2000 行）与 expect 上下文丢失，
SQLite 只能补历史、补不了活动连接。因此"跨轮次持续交互"只有两条路：
**daemon 保活** 或 **每次重连（接受状态损失）**。本文档以 daemon 保活为主路径。

## 2. 改动项总览

| 优先级 | 改动项 | 解决缺口 | 成本 |
|--------|--------|----------|------|
| P0 | `embpilot batch`（JSONL in / JSONL out） | G1、G7 | 小 |
| P0 | `read_output` 只读捕获工具 | G2、G3、F2 | 小 |
| P1 | `embpilot serve` daemon + `--socket` 客户端转发 | G1、G4（保活） | 中 |
| P2 | `search_history_logs` 支持 `session_id` | G5 | 中 |
| P2 | schema 驱动的 `--flag` 参数 | G6 | 中 |
| P2 | `embpilot run` 便捷模式 | 人类易用性 | 小 |
| P2 | Prompts 扩展（5 个新场景模板） | H1 | 小 |
| P2 | tool description / error suggestion 增强 | H2、H3 | 小 |
| P2 | CLI 侧提示增强（`embpilot help <tool>` 等） | H4 | 小 |
| P3 | monitor 增强（`--until` / `--timeout`） | G3（人类终端） | 小 |
| P3 | `device://session_info` 资源 | F3 | 小 |
| — | ~~RAG 工具暴露~~（**本轮不做**，deferred） | F1 | 小 |

依赖关系：P1 复用 P0 的行协议与 envelope；P2 的 `run` 基于 batch 实现；
P3 依赖 `read_output` 的 expect/timeout 语义，可作为其终端包装。
RAG 工具暴露（F1）本轮不做，deferred 到后续版本。

## 3. P0 — `embpilot batch`（JSONL in / JSONL out）

### 3.1 动机

Agent 需要"一次 bash 调用完成 connect → 多命令 → disconnect 完整生命周期"，
且输出逐条可对应、可解析。当前只能靠 `printf ... | embpilot shell`，混入 banner、
无法区分命令边界、中途失败无局部信号。

### 3.2 接口契约

```
printf '%s\n' \
  '{"tool":"connect_serial","args":{"port":"COM3","baudrate":115200}}' \
  '{"tool":"send_command","args":{"command":"help","timeout_ms":5000}}' \
  '{"tool":"disconnect_device","args":{}}' \
| embpilot batch --json-output
```

- stdin 每行一个请求对象：`{"tool": <name>, "args": <object>}`，args 缺省为 `{}`。
- stdout 每行一个结果 envelope（复用 `ok/data/error` 结构），**无 banner、无 prompt**。
- 同一 `SessionManager` 顺序执行所有行；某行失败不中止后续行（可加 `--fail-fast`）。
- 空行与 `#` 注释行忽略；支持 `exit` 语义 = 提前结束。
- 退出码：全成功 0；任一工具失败 1；解析/未知工具 2（与 one-shot 契约一致）。
- 进程退出前自动 `disconnect_device`（幂等，与 `shutdown()` 一致）。
- 复用 `shell_loop` 的读行/解析/dispatch 机制，新增 `batch` 子命令与 `batch_loop`。

### 3.3 影响范围

- `src/embpilot/cli.py`：新增 `batch` 子命令。
- `src/embpilot/cli_shell.py`：提取可复用的"读一行 → 解析 → dispatch → 输出"循环，
  shell 与 batch 共用；batch 抑制 banner。
- 测试：`tests/test_cli_shell.py` 增加 batch 用例（fake manager）：成功/失败/未知工具/
  注释/空行/提前 exit/退出码。

### 3.4 成本

小。约 1–2 小时 + 测试。

## 4. P0 — `read_output` 只读捕获工具

### 4.1 动机

CLI 侧无法被动观察设备日志流（G2）。boot 日志、设备自报、周期性状态输出都只能
"发一条命令附带捕获"，污染设备且语义错误。需要"只看不发、等到条件或超时"的原语。

### 4.2 接口契约

新 MCP 工具 + CLI 工具（与其它工具一致地进入 `build_tool_definitions`，
MCP 侧同时获得该能力，保持一致）：

```json
{"duration_ms": 1000, "max_chars": 20000, "expect_regex": "Login:"}
```

- `duration_ms`（默认 1000）：收集窗口；窗口内持续读取 ring buffer 的新行。
- `expect_regex`（可选）：一旦匹配即提前返回（语义与 `send_command` 的 expect 一致）。
- `max_chars`（默认 20000）：输出截断上限。
- 语义：**不向设备发送任何字节**；只消费 LogProducer 写入 ring buffer 的新行。
- 结果 envelope：`data: {output, matched, timed_out, truncated}`，
  与 `CommandResult` 字段对齐，便于 agent 统一处理。
- 无活动连接时返回 `NO_ACTIVE_DEVICE`（与现有契约一致）。

### 4.3 影响范围

- `src/embpilot/mcp_contracts.py`：注册工具 + `SessionOperations` 协议方法。
- `src/embpilot/server.py`：`SessionManager.read_output()`（基于 `active_ring`，
  用 `snapshot_since(cursor)` 轮询，等价于现有 monitor 的实现逻辑）。
- `src/embpilot/core/engine.py`：如需避免轮询，可给 `RingBuffer` 增加条件等待；
  首版允许 100ms 轮询（与 monitor 一致），不引入额外机制。
- 测试：fake ring 注入新行，验证 duration/expect/truncation/无连接路径。

### 4.4 成本

小。约 1–2 小时 + 测试。

## 5. P1 — `embpilot serve` daemon + `--socket` 客户端转发

### 5.1 动机

G4 的完整答案：让连接活在一个常驻进程里，agent 每次 bash 调用都只是"连上去执行一次"。
调用形式与现有 one-shot 完全一致，仅增加一个 `--socket` 指向 daemon，
对 agent 是**零学习成本**的平滑升级；人类终端里则可选择继续用 `shell`。

### 5.2 接口契约

```
embpilot serve --socket /tmp/emb.sock --data-dir ./.embpilot-data
# 或 Windows：--socket \\.\pipe\embpilot-<name>
```

- daemon 持有唯一 `SessionManager`；一次 connect 后跨客户端调用保持。
- 行协议（与 batch 同构，追加 `id` 关联请求/响应）：
  - 请求：`{"id": 1, "tool": "send_command", "args": {...}}`
  - 响应：`{"id": 1, "ok": true, "data": {...}}` / `{"id": 1, "ok": false, "error": {...}}`
- 传输：POSIX 用 unix socket；Windows 用 named pipe（`\\.\pipe\...`）。
  首版**不引入 TCP**，避免局域网暴露面；如需 TCP 仅绑定 127.0.0.1 且文档明确风险。
- 客户端转发：
  ```
  embpilot --socket /tmp/emb.sock tool send_command --json '{"command":"help"}'
  embpilot --socket /tmp/emb.sock tools
  ```
  `--socket` 存在时，`tool`/`tools` 子命令把请求发给 daemon；不存在时行为不变。
- daemon 生命周期：前台运行（agent 用 `&`/`nohup` 拉起）或 `--daemon` 后台化
  并把 socket 路径写入 `<data-dir>/daemon.sock` 文件供后续调用发现。
- 并发：单连接串行执行；多客户端由 OS 队列排队（named pipe/unix socket 均支持），
  每个连接独立 `id` 空间，响应按连接回写，互不串扰。
- 安全：仅本机 socket；不鉴权（与本地 MCP stdio 同级信任）；文档明示
  "任何能访问该 socket 的本机进程都可向设备发命令"。

### 5.3 影响范围

- `src/embpilot/cli.py`：`serve` 子命令 + `--socket` 全局选项 + 客户端转发逻辑。
- `src/embpilot/server.py`：`SessionManager` 增加并发安全（asyncio lock 串行化
  工具执行；当前单进程单会话模型天然串行，仅需显式声明与测试确认）。
- `src/embpilot/cli_shell.py`：把 shell 循环泛化为"任意行来源 → dispatch → 行输出"，
  serve 与 batch 复用。
- 平台差异：named pipe 在 Windows 的实现（`win32pipe` 或标准库 `os` 层的
  `\\.\pipe\` 支持需调研）；POSIX unix socket 用标准库即可。
- 测试：fake manager 下 serve 循环的请求/响应关联、多连接排队、断开重连、
  `--socket` 转发命中 daemon 的集成测试（进程级，可用真实 CLI + 假目标）。

### 5.4 成本

中。约 0.5–1 天（含 Windows named pipe 调研与跨平台测试）。

### 5.5 备选（若 Windows named pipe 成本过高）

退化为"本地 TCP 绑定 127.0.0.1 + 随机端口写 `<data-dir>/daemon.json`"，文档声明
仅限本机、无鉴权、生产环境需叠加防火墙。

## 6. P2 — `search_history_logs` 支持 `session_id`

### 6.1 动机

断开后无法搜索历史会话（G5）。"列出会话 → 搜旧日志 → 导出"是 agent 高频工作流，
当前必须保持连接才能搜当前会话，历史会话只能 `export_session` 后由外部查 SQLite。

### 6.2 接口契约

- `search_history_logs` 增加可选 `session_id`（字符串）；缺省 = 当前活动会话
  （**向后兼容**，现有行为不变）。
- 提供 `session_id` 时：经 `MainDatabase` 解析该会话的 db 路径，以只读方式打开
  对应 `SessionDatabase` 搜索，完成后关闭；**不得干扰活动会话的写连接**。
- 会话不存在 → `NOT_FOUND`；路径缺失 → `NOT_FOUND`（复用现有错误码）。
- CLI/batch/serve 自动获得该能力（共享契约层）。

### 6.3 影响范围

- `src/embpilot/core/database.py`：`MainDatabase` 暴露按 `session_id` 查路径的只读
  打开辅助（注意 WAL：只读连接需正确使用 `mode=ro` 或共享现有连接策略）。
- `src/embpilot/server.py`：`SessionManager.search_history_logs(session_id=None)`。
- `src/embpilot/mcp_contracts.py`：schema 增加可选字段 + 示例。
- 测试：构造两个 session DB，验证缺省/指定 session_id/不存在/路径丢失各路径。

### 6.4 成本

中。约 0.5 天（WAL 只读打开的正确性需专门测试）。

## 7. P2 — schema 驱动的 `--flag` 参数

### 7.1 动机

G6：`--json '{"port":"COM3"}'` 在 bash / PowerShell / GBK cmd 引号规则不同，
agent 生成命令与人类手输都易错。

### 7.2 接口契约

- 由 `build_tool_definitions()` 的 `inputSchema` **自动生成** argparse 参数
  （`--port`、`--baudrate`、`--command` …），类型/枚举/default/required 映射自 schema；
  不手写任何工具的参数表。
- `--json` 仍为 canonical 形式；两者同传时 `--json` 优先，其余 flag 合并覆盖
  （后者覆盖前者，文档写明优先级）。
- 例：`embpilot tool connect_serial --port COM3 --baudrate 115200 --line-ending crlf`
  （kebab-case 映射 snake_case）。
- 生成规则集中在一个模块（如 `cli_flags.py`），并加 schema 驱动的单测
  （每个工具生成合法、枚举值正确、unknown flag 报 usage 错）。

### 7.3 影响范围

- `src/embpilot/cli.py`：flag 生成挂接；`src/embpilot/cli_flags.py`（新）。
- 测试：全工具 schema → flags 的往返一致性（`flags → args` 再走 jsonschema 校验）。

### 7.4 成本

中。约 0.5 天（argparse 动态生成 + 一致性测试）。

## 8. P2 — `embpilot run` 便捷模式

### 8.1 动机

人类高频场景是"连上一个设备，跑几条命令"。batch 需拼 JSON 行，
`run` 提供最小语法。

### 8.2 接口契约

```
embpilot run --connect '{"port":"COM3","baudrate":115200}' help version uname -a
```

- `--connect`：JSON 对象（与 `connect_serial` 等共享 schema）；缺省不连接。
- 位置参数依次作为 `send_command` 的 `command` 执行；`--timeout-ms` / `--line-ending`
  作为所有命令的默认参数。
- 内部实现 = batch 的语法糖（生成 `connect → 各命令 → disconnect` 请求序列）。
- 输出：每命令一个 envelope（与 batch 一致）。

### 8.3 影响范围

- `src/embpilot/cli.py`：`run` 子命令，复用 batch 的请求生成与执行。
- 测试：fake manager 下验证命令序列与 disconnect 收尾。

### 8.4 成本

小。约 1 小时 + 测试（依赖 batch 落地）。

## 9. P3 — monitor 增强（`--until` / `--timeout`）

### 9.1 动机

G3 的终端侧补全：人类在 `shell` 里希望"盯着日志直到 `Login:` 出现"，
而不必手动 `stop`。agent 侧需求已被 P0 的 `read_output` 覆盖，故本项仅服务终端。

### 9.2 接口契约

- `shell` 增加 `monitor --until <regex> --timeout <seconds>`（或作为启动参数
  `embpilot shell --monitor-until ... --monitor-timeout ...`，二选一，倾向后者避免
  污染 REPL 词法）。
- 语义：进入 monitor；匹配 `--until` 或超时即自动退出并打印一行
  `monitor stopped: matched|timeout`。
- 实现基于 `read_output` 的 expect/timeout 逻辑，不重复实现。

### 9.3 影响范围

- `src/embpilot/cli_shell.py` + `src/embpilot/cli.py` 参数。
- 测试：fake ring 下 matched 提前退出 / timeout 退出。

### 9.4 成本

小。约 1 小时 + 测试（依赖 P0 的 `read_output`）。

## 10. MCP 功能与提示完善（本轮追加）

### 10.1 功能缺口评估结论

核心闭环（连接/命令/重置/会话/搜索/导出）齐全；按设计 spec
（`docs/mcp_embedded_debug_spec.md`）对照存在未兑现承诺，其中
RAG 工具暴露最严重：`RagEngine`（ingest/search/delete/list_sources）
实现完整但**没有任何工具层入口**，知识库检索对 agent 完全不可用。

| 编号 | 缺口 | 结论 |
|------|------|------|
| F1 | RAG 知识库无工具暴露 | **本轮不做**（deferred）：见 10.2 |
| F2 | 无 expect 式观察（live_log 仅全量快照） | 由 P0 `read_output` 覆盖，MCP 侧同步获得 |
| F3 | `device://session_info` 资源（rearchitecture spec 规划） | 可选，见 13.6 |
| F4 | `resources/subscribe` 推送（PROGRESS.md 已承认未广告） | **不做**：轮询足够，协议升级成本高 |
| F5 | reset dtr/rts 未实现 | 已知项，schema 只广告 reboot，维持诚实 |

### 10.2 RAG 工具暴露（本轮不做，deferred）

> 决定（2026-08-12）：本轮不做。`RagEngine` 已实现，只差契约层；
> 后续版本单独落地。方案保留如下：

`rag.py` 的 `RagEngine` 已实现 ingest/search/count/delete/list_sources，
只差契约层，成本小但价值大（兑现设计 spec 第四章承诺）。

- `search_kb {"query": "HardFault handler", "top_k": 5}`：语义检索知识库（datasheet/错误码手册/KB），返回带得分与来源的片段。
- `list_kb_sources {}`：列出已导入知识源与文档数。
- `ingest_doc {"path": "docs/err_codes.pdf"}`：导入一份文档，供 datasheet/手册入库；要求 `[rag]` extra 安装。
- RAG 未安装（缺 fastembed/lancedb）时返回明确的 `RAG_UNAVAILABLE` 错误码 + `pip install "embpilot[rag]"` 指引，与轻量核心安装策略一致。
- 与 `search_history_logs`（设备日志）互补：KB 搜手册/经验，日志搜本机历史。

### 10.3 Prompts 扩展（P2）

当前 2 个 prompt 本质是"一段用户消息模板"，且未给出具体工具调用序列。新增 5 个，
prompt 文本直接给出可执行的工具序列与参数示例：

- `connect_and_explore`：connect_serial/ssh/telnet → version/help → 汇总设备能力与下一步建议。
- `capture_boot_log`：reset_target(reboot) → read_output(expect_regex=启动完成标志) → 归档。
- `diagnose_connection`：按错误码分支给排查步骤（端口占用/baudrate/防火墙/认证）。
- `design_expect`：教 agent 为 send_command 设计锚定正则（提示符锚点、避免贪婪、超时联动）。
- `session_handoff`：基于 list_sessions + operation_history 生成会话复盘与交接摘要。

每个 prompt 参数保持最少（0–2 个），返回消息含工具调用建议（user 角色文本，与现有实现风格一致）。

### 10.4 tool description / error suggestion 增强（P2）

- **description 模板**：每个工具统一加 `When to use / When not / Typical flow / Pitfalls` 四段，
  在 `mcp_contracts.py` 集中维护、一个函数生成，避免手写不一致。
  - 例：`send_command` 注明"设备输出持续流式时应配合 expect_regex 或 read_output，而非等待全量超时"。
  - 例：`connect_serial` 注明 line_ending 选择依据（交互式 shell 常用 crlf，Linux 内核 console 常用 lf）。
  - 例：`reset_target` 注明 reboot 是软件复位、会打断会话中正在执行的命令。
- **suggestion 按错误码细分**：
  - `CONNECTION_FAILED`：区分超时（地址/防火墙/baudrate）/ 认证（凭据/key 权限）/ 拒绝（端口服务未起），
    分别给不同建议；串口额外提示"端口被占用则换 COM 或关闭占用程序"。
  - `send_command` 超时：改为"若命令有完成标志请加 expect_regex；否则增大 timeout_ms"。
  - `RAG_UNAVAILABLE`：提示 `pip install "embpilot[rag]"`。
- 保持 envelope 结构不变（只改文案），CLI/format_result 自动受益。

### 10.5 CLI 侧提示增强（P2）

- `embpilot tools` 输出增加每个工具的 example 行（schema 已有 examples 字段）。
- 新增 `embpilot help <tool>`：完整描述 + 示例 + 常见错误与 suggestion 对照。
- `embpilot tool <name> --help` 透传同一内容；shell `help` 扩展为可输入 `help <tool>`。

### 10.6 session_info 资源（可选，P3）

`device://session_info`：当前会话元信息（session_id、接口、设备、开始时间、日志行数、
最近错误统计）。数据已由 SessionManager 持有，成本低，价值中等；列为可选视时间决定。

## 11. 明确不做

- **RAG 工具暴露**（F1）：本轮不做，deferred 到后续版本（实现已存在，只差契约层）。
- **跨进程会话恢复**：设备连接不可复活，不做"重连旧会话"；由 serve 保活替代。
- **`resources/subscribe` 推送**（F4）：轮询已满足观察需求，协议升级成本高，不做。
- **reset dtr/rts**（F5）：不在本轮范围，schema 维持只广告 reboot 的诚实状态。
- **TCP 远程访问 / 鉴权体系**：仅本机 socket；无密码/密钥认证（信任本机用户）。
- **GUI / 交互式配置向导**。
- **改动既有契约**：退出码 0/1/2、`ok/data/error` envelope、
  `--json-output`、`tools` 目录格式全部保持不变。
- **monitor 的 `[log]`/`[cmd]` 前缀格式**：终端既有行为不变。

## 12. 实施顺序与里程碑

| 里程碑 | 内容 | 回退点建议 |
|--------|------|-----------|
| M1 | P0：batch + read_output（含测试） | 提交前创建 commit 回退点 |
| M2 | P1：serve + --socket 转发（含 Windows 调研） | M1 合并后 |
| M3 | P2：prompts 扩展、description/suggestion 增强、CLI help | M2 合并后 |
| M4 | P2：search_history_logs session_id、flags、run | 逐项独立提交 |
| M5 | P3：monitor 增强（可选） | 可选 |

每里程碑独立提交，遵循 Conventional Commit（`feat:`），并同步
`README.md`（CLI 章节）、`docs/`、`PROGRESS.md`、`change.log`。

## 13. 验收清单

- [x] `embpilot batch` 一次调用完成 connect → 多命令 → disconnect，输出逐行 JSON。
- [x] `read_output` 不发送字节即可捕获设备日志，expect 提前返回、超时截断正确。
- [x] `serve` 跨多次客户端调用保持连接；多连接排队不串扰；断连后可重连。
- [x] `--socket` 转发与无 socket 行为完全等价（同一契约层）。
- [x] 断开后可 `search_history_logs --json '{"session_id":"..."}'` 搜索历史会话。
- [x] 5 个新 prompts 可获取，文本含可执行工具序列。
- [x] 所有工具 description 含 When to use / Pitfalls；suggestion 按错误码细分。
- [x] 全工具 schema → flags 往返经 jsonschema 校验一致。
- [x] `run` 生成的请求序列与 batch 等价。
- [x] 全量 `python -m pytest -q` 绿；无活体目标依赖（全部 mock/fake）。
