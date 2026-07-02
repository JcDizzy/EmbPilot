# 嵌入式调试 MCP 服务器设计方案 (Specification for Embedded Debugging MCP Server)

本项目旨在构建一个基于 Model Context Protocol (MCP) 的专用服务器，使大语言模型（AI Client）能够直接连接、控制并调试嵌入式物理设备（如通过串口、Telnet 或 SSH 连接的单片机、RTOS 板卡及嵌入式 Linux 系统）。

---

## 一、 核心功能设计 (Core Features)

MCP 服务器通过三大支柱（Tools, Resources, Prompts）向 AI 暴露底层硬件交互能力。

### 1. Tools (AI 可调用的主动操作)
* **`connect_device(interface_type, config)`**：建立硬件连接。支持 `serial`（参数：端口号、波特率）及 `telnet/ssh`（参数：IP、端口、用户名/密码）。
* **`disconnect_device()`**：主动断开连接，释放系统串口或网络句柄。
* **`send_command(command, expect_regex, timeout_ms)`**：向设备发送指令（如 `help`, `ifconfig`）。支持传入正则表达式 `expect_regex`；返回值应是该条命令窗口内采集到的输出，在本地匹配成功后立即截断返回，若未匹配则在超时点返回已收集窗口内容，以优化响应时间并避免把无关刷屏日志混进结果。
* **`reset_target(method)`**：复位目标板。支持通过串口 DTR/RTS 引脚电平控制或发送 `reboot` 文本命令。

### 2. Resources (AI 可读取的数据源)
* **`device://live_log`**：实时日志流，支持 AI 客户端通过 `resources/subscribe` 机制动态监听物理设备的输出。
* **`device://session_info`**：会话元数据快照。返回当前连接的 `session_id`、接口类型、设备名、连接摘要、启动时间、状态、最近日志时间、日志计数等运行时真实信息。这里不再假设 EmbPilot 能对所有嵌入式目标统一发起一组“通用 sysinfo 探测命令”。
* **`device://analytics`**：基于本地数据库的异常聚合统计。返回近期错误日志频次表，避免 AI 检索海量原始文本。

### 3. Prompts (场景引导提示词模版)
* **`analyze_crash_log`**：引导 AI 自动捕获 `HardFault`、`Panic` 或 `Segmentation fault` 的上下文并进行根因分析。
* **`hardware_sanity_check`**：引导 AI 执行一系列基准测试命令，评估单片机外设状态。

---

## 二、 系统架构与数据流 (Architecture & Data Flow)

为应对嵌入式调试中常见的高频“日志刷屏”压力，架构采用**MCP 装配层与 runtime 执行层分离**、以及 **dispatcher 扇出** 的日志管线模型，确保 Python 进程在高吞吐量下不掉帧、不卡死，同时把协议注册与运行时状态管理解耦。

### 1. 数据流向图
```
[物理硬件/网络] (Serial / Telnet 持续输出)
       │
       ▼ (Async Read Loop)
[ FrameAssembler + SessionDispatcher ] (打上高精度时间戳后显式扇出)
       │
       ├───► [ RingBufferSink ] ──► `device://live_log`
       ├───► [ ExpectManager ] ──► `send_command(..., expect_regex=...)`
       ├───► [ SessionInfoSink ] ──► `device://session_info`
       └───► [ DbSink ] ──► (每 50ms / 批量刷盘) ──► 写入本地 session .db 文件
```

### 2. 关键设计点
* **协议层 / 运行时分层**：`mcp_app.py` 负责 MCP Tools / Resources / Prompts 注册与 stdio server 启动；具体连接生命周期、日志处理、expect 行为和资源组装由 `runtime/` 模块承接。
* **显式 dispatcher 扇出**：底层基于 `asyncio` 和 `pyserial-asyncio`（或 `telnetlib3`），读取任务仅负责接收原始字节、做 frame assembly、追加宿主机时间戳并分发到多个 sink；不再依赖“一个队列被多个消费者同时读取”的隐式行为描述。
* **宿主机绝对时间戳同步**：所有进入队列的日志行统一附加 `[YYYY-MM-DD HH:MM:SS.SSS]` 前缀，用以对齐 AI 动作与硬件异动的因果关系。
* **内存容量保护**：内存中的即时查看历史采用固定长度环形缓冲区（`collections.deque(maxlen=2000)`），旧数据自动溢出，海量历史完全交由本地数据库承载。
* **诚实资源语义**：资源只暴露 runtime 当前能稳定提供的数据。`device://session_info` 描述当前会话事实，而不是伪造一个跨设备通用的 `sysinfo` 采集承诺。

---

## 三、 本地数据库设计 (Database Schema)

服务器本地使用 SQLite 并开启 **WAL (Write-Ahead Logging)** 模式与 **NORMAL 同步级别**，以支持每秒数万条日志的高并发批量写入（Bulk Insert）。

### 1. 设备运行日志表 (`device_logs`)
用于高频存储设备产生的所有原始输出，供 RAG 和历史回溯使用。

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `id` | INTEGER PRIMARY KEY | 自增主键，建立索引 |
| `timestamp` | TEXT | 宿主机高精度绝对时间戳 (YYYY-MM-DD HH:MM:SS.SSS) |
| `source` | TEXT | 日志来源标识 (`serial` / `telnet` / `ssh`) |
| `text` | TEXT | 原始日志文本内容（自动剥离 `\r\n`） |

### 2. 操作与上下文历史表 (`operation_history`)
用于记录 AI 的决策链路和人类的干预动作。

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `id` | INTEGER PRIMARY KEY | 自增主键 |
| `timestamp` | TEXT | 记录产生时间 |
| `actor` | TEXT | 操作主体 (`AI` / `Human` / `System`) |
| `action_type` | TEXT | 动作类型 (`call_tool`, `connect`, `git_env`) |
| `detail` | TEXT | 详细上下文（如 AI 发送的完整参数或 Git Commit ID，存为 JSON） |

---

## 四、 Token 优化与本地 RAG 机制 (Token Optimization & RAG)

为严格控制大模型的上下文窗口成本，防止刷屏日志稀释关键信息，引入**本地混合 RAG（检索增强生成）**机制。

### 1. 过滤与拦截策略
* **本地 Expect 拦截**：AI 通过 `send_command` 查参数时，runtime 为该条命令打开独立窗口；匹配到指定正则后立即停止该窗口收集，仅返回命令窗口内捕获的输出。
* **结构化切片与搜索**：提供 `search_history_logs(keyword, time_window)` 工具。服务器在本地执行 SQL 模糊查询或 BM25 算法，仅将匹配到的核心上下文（如报错前后的 50 行）打包提交给 AI。

### 2. 本地知识库并联 (Vector DB)
* 引入轻量化本地向量数据库（如 `Chroma` 或 `LanceDB`），利用本地小模型（如 `bge-m3`）进行文本向量化。
* **知识库内容**：提前导入当前芯片的 **Datasheet（数据手册位域定义）**、**SDK Error Code 说明书** 以及**项目历史故障解决排错指南 (Troubleshoot KB)**。
* **运行逻辑**：当 `device_logs` 表中高频触发某一特定错误码时，RAG 管道自动检索对应的手册说明，将精确的物理硬件定义作为背景上下文随 Tool 一并注入 AI 客户端，彻底消除 AI 对硬件寄存器定义的幻觉。

---

## 五、 技术栈推荐 (Tech Stack)

* **核心语言**：Python 3.11+
* **MCP 协议框架**：`mcp` (Anthropic 官方 Python SDK)
* **异步驱动库**：`asyncio`, `pyserial-asyncio`, `telnetlib3`, `asyncssh`
* **异步数据库驱动**：`aiosqlite`
* **本地向量检索**：`fastembed` + `lancedb`
