# EmbPilot 第三次 Review：问题、建议与发布前 Checklist

> Review 对象：`JcDizzy/EmbPilot`，默认分支 `feat/runtime-rearchitecture`  
> Review 时间：2026-07-07  
> 说明：本 review 基于 GitHub 最新源码、提交记录与测试文件审阅；未在本地实际执行完整 pytest。

---

## 1. 总体结论

这轮“选择性改进”质量较高。上一轮 release hardening 中提到的高优先级问题，大部分已经被正面处理，尤其是依赖声明、SSH host-key 默认行为、session DB sidecar 删除、RAG doc_id 安全、`reset_target` 确认、FTS rebuild 策略、CLI 安全参数暴露等。

当前评价：

| 维度 | 评分 | 说明 |
|---|---:|---|
| 原型完成度 | 8.7 / 10 | MCP surface、runtime、driver、日志、RAG、测试都已经成型 |
| 0.1.0 实验室/内测可用度 | 7.7 / 10 | 已适合接入实验室设备试跑，但仍建议发布前补少量 hardening |
| 正式生产可用度 | 约 7 / 10 | 主要差在动态 schema、审计脱敏、真实设备矩阵验证和更严格 human confirmation |

我的判断是：**已经接近 0.1.0 RC，但发布前建议再做一个小的 follow-up commit。**

---

## 2. 已确认改好的问题

### 2.1 core / RAG 依赖声明已补齐

`pyproject.toml` 已加入：

```toml
"jsonschema>=4.0"
```

同时 RAG extra 已显式加入：

```toml
"pandas>=2.0"
"pyarrow>=14"
```

这解决了之前 clean install 后 `embpilot.mcp_app` 可能因为缺少 `jsonschema` 而 import 失败的问题，也避免 RAG 模块依赖 transitive dependency。

此外，测试已增加 clean install 后实际 import `embpilot.mcp_app` 的 smoke test，而不只是执行 `embpilot --version` 这种不会加载 MCP 层的轻路径。

**状态：已解决。**

---

### 2.2 SSH host-key verification 默认值已变安全

`SshDevice` 现在使用 `_UNSET` 区分：

- 未传 `known_hosts`：不把 `known_hosts` 参数传给 AsyncSSH，使用 AsyncSSH 默认 host-key 行为；
- 显式传 `known_hosts=None`：才跳过 host-key verification。

`SessionManager.build_device()` 也只有在 config 中明确包含 `known_hosts` 时才转发该参数。

测试也覆盖了：

- 默认不传 `known_hosts` 时，`asyncssh.connect()` 参数中不包含 `known_hosts`；
- 显式传 `known_hosts=None` 时，确实会传入 `None`。

**状态：已解决。**

---

### 2.3 session 删除现在会同时处理 `.db`、`-wal`、`-shm`

数据库层新增了类似以下逻辑：

```python
def _session_db_sidecars(db_path: Path) -> list[Path]:
    return [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
```

`delete_session()` 和 retention cleanup 都通过统一 helper 删除 session DB 主文件及 WAL/SHM sidecar，并且每个目标路径都会经过 managed-directory 校验。

已有测试覆盖：创建 `.db`、`-wal`、`-shm` 后删除 session，确认三者都被移除。

**状态：已解决。**

---

### 2.4 RAG doc_id 注入风险已收敛

MCP schema 已对 `ingest_doc.doc_id` 和 `delete_doc.doc_id` 增加：

```json
{
  "minLength": 1,
  "maxLength": 128,
  "pattern": "^[A-Za-z0-9_.:-]+$"
}
```

`RagEngine.delete_document()` 也对单引号做了 escape：

```python
safe_doc_id = doc_id.replace("'", "''")
await self._table.delete(f"id = '{safe_doc_id}'")
```

MCP 层测试已覆盖非法 doc_id 被 schema validation 拒绝。

**状态：已解决。**

---

### 2.5 `reset_target` 已改为显式确认

`reset_target` schema 现在要求：

```json
"required": ["confirm"]
```

runtime 中未传 `confirm=True` 会抛 `PermissionError`：

```python
if not confirm:
    raise PermissionError("reset_target requires confirm=true")
```

测试覆盖了：

- 未确认时拒绝；
- 确认后写入 `reboot\n`。

**状态：已解决。**

---

### 2.6 FTS rebuild 已优化为条件触发

`SessionDatabase.open()` 现在会先判断：

- FTS 表是否已经存在；
- schema migration 是否发生；
- FTS5 integrity check 是否通过。

只有需要时才 `_rebuild_fts()`。这解决了之前每次打开 historical session 都可能 rebuild 的性能风险。

后续提交还修复了一个细节：不能仅依赖 row count 判断 external-content FTS 是否 stale，因为 SQLite 可能在 FTS index stale 时仍返回 content table 的 row count。因此现在使用 FTS5 `integrity-check` 判断是否需要 rebuild。

测试覆盖了：

- 正常情况下不 rebuild；
- external-content FTS stale 时会 rebuild，且 search 可以命中。

**状态：已解决。**

---

### 2.7 CLI 已暴露 safety limits

CLI 现在已支持：

```bash
--command-timeout-max-ms
--search-limit-max
--export-limit-max
--audit-export-limit-max
--tool-rate-limit-per-minute
```

`EmbPilotConfig.from_args()` 也会把这些参数写入配置对象。

**状态：已解决，但与 MCP schema 的动态一致性还有剩余问题，见下一节。**

---

## 3. 当前仍建议修的主要问题

### 3.1 MCP tool schema 的 limit 上限仍是静态常量

#### 问题

`mcp_app.py` 里 tool schema 的上限来自静态常量：

```python
_DEFAULT_COMMAND_TIMEOUT_MAX_MS = 60_000
_DEFAULT_SEARCH_LIMIT_MAX = 1_000
_DEFAULT_EXPORT_LIMIT_MAX = 10_000
_DEFAULT_AUDIT_EXPORT_LIMIT_MAX = 5_000
```

但 CLI 已允许用户通过参数修改这些上限，例如：

```bash
embpilot --export-limit-max 50000
embpilot --search-limit-max 100
```

当前 `list_tools()` 和 `_handle_call_tool_request()` 仍使用 `build_tool_catalog()` 返回的静态 schema。结果会出现 schema 与 runtime config 不一致：

#### 情况 A：CLI 上限调大

```bash
embpilot --export-limit-max 50000
```

用户以为可导出 50000 行，但 MCP schema 仍在 10000 处拒绝。

#### 情况 B：CLI 上限调小

```bash
embpilot --export-limit-max 1000
```

MCP schema 仍告诉 client 最多可传 10000，但 runtime 又会在 1000 处返回 tool error。

#### 建议

改成 config-aware tool catalog：

```python
def build_tool_catalog(config: EmbPilotConfig | None = None) -> list[Tool]:
    limits = config or EmbPilotConfig()
    command_timeout_max_ms = limits.command_timeout_max_ms
    search_limit_max = limits.search_limit_max
    export_limit_max = limits.export_limit_max
    audit_export_limit_max = limits.audit_export_limit_max
    ...
```

在 `create_mcp_app()` 内构建一次 catalog：

```python
def create_mcp_app(config: EmbPilotConfig) -> tuple[Server, SessionManager]:
    manager = SessionManager(config)
    app = Server("embpilot", version=__version__)
    tool_catalog = build_tool_catalog(config)

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return tool_catalog

    async def call_tool_handler(request: CallToolRequest) -> ServerResult:
        return await _handle_call_tool_request(
            manager,
            request,
            rate_limiter,
            tool_catalog,
        )
```

并修改 `_tool_by_name()`：

```python
def _tool_by_name(name: str, catalog: list[Tool]) -> Tool | None:
    return next((tool for tool in catalog if tool.name == name), None)
```

#### 建议测试

新增测试：

```python
def test_configured_limits_are_reflected_in_tool_schema(tmp_path):
    config = build_config(tmp_path)
    config.export_limit_max = 1234
    app, manager = create_mcp_app(config)
    tool = next(t for t in build_tool_catalog(config) if t.name == "export_session")
    assert tool.inputSchema["properties"]["limit"]["maximum"] == 1234
```

再补一个 call_tool validation 测试：

```python
# export_limit_max = 2
# limit = 3 应该在 schema validation 阶段被 INVALID_PARAMS 拒绝
```

#### 优先级

**高。建议发布 0.1.0 前修。**

---

### 3.2 操作审计仍可能记录命令里的明文 secret

#### 问题

`send_command()` 会把原始命令写入 operation history：

```python
"command": command
```

现有 `redact_sensitive()` 只按 dict key 递归脱敏，比如 key 是 `password`、`token`、`secret`、`key_file` 时会替换；但普通字符串值不会做内容级脱敏。

这意味着以下命令可能被原样记录到审计日志：

```text
AT+CWJAP="ssid","wifi_password"
fw_setenv bootargs "... token=xxx ..."
curl -H "Authorization: Bearer eyJ..."
setenv password my_secret
```

#### 建议

增加命令文本脱敏函数：

```python
def redact_command_text(command: str) -> str:
    patterns = [
        # key=value / key: value
        (r"(?i)(password|passwd|token|secret|authorization)\s*[:=]\s*[^ \t\r\n]+", r"\1=***REDACTED***"),

        # Bearer token
        (r"(?i)(Bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 ***REDACTED***"),

        # 常见 ESP/AT Wi-Fi 命令
        (r'AT\+CWJAP="([^"]*)","([^"]*)"', r'AT+CWJAP="\1","***REDACTED***"'),
    ]
    redacted = command
    for pattern, repl in patterns:
        redacted = re.sub(pattern, repl, redacted)
    return redacted
```

在 `send_command()` 审计记录中使用：

```python
"command": redact_command_text(command)
```

如果需要可追溯性，可额外记录 hash：

```python
"command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest()
```

#### 建议测试

```python
def test_send_command_audit_redacts_inline_secrets(...):
    await manager.send_command('AT+CWJAP="ssid","secret"', confirm_dangerous_command=True)
    exported = await manager.export_operation_history()
    assert "secret" not in exported
    assert "***REDACTED***" in exported
```

#### 优先级

**高。建议发布 0.1.0 前修。**

---

### 3.3 FTS 搜索语义变化仍未处理

#### 问题

当前搜索逻辑用 FTS5：

```python
WHERE device_logs_fts MATCH ?
```

关键词会被 `_fts_phrase(keyword)` 包成 phrase。性能比旧版 `LIKE "%keyword%"` 好很多，但语义不同：

- 搜 `ERR` 未必匹配 `ERROR`；
- 搜带符号的寄存器名、错误码、路径、地址时可能不符合直觉；
- 用户可能期待“子串包含”，但实际是 FTS token/phrase 匹配。

#### 建议

在 `search_history_logs` 增加搜索模式：

```json
"mode": {
  "type": "string",
  "enum": ["fts", "substring"],
  "default": "fts"
}
```

runtime：

```python
async def search_logs(..., mode: str = "fts"):
    if mode == "fts":
        ... MATCH ...
    elif mode == "substring":
        ... LIKE ? ...
```

`substring` 模式必须保留严格 limit 上限，避免大库全表扫过重。

#### 优先级

**中。可以作为 0.1.1，但如果希望搜索体验稳定，建议 0.1.0 前补。**

---

### 3.4 RAG `[rag]` clean install / import 仍未完整验证

#### 问题

依赖声明已经补齐，但目前 clean install smoke test 主要验证 core install 后 import `embpilot.mcp_app`。RAG extra 的实际安装和 import 仍未被完整验证。

#### 建议

增加可选 CI job：

```bash
pip install .[rag]
python -c "from embpilot.core.rag import RagEngine; print('ok')"
```

更进一步，做一次临时目录 smoke：

```python
engine = RagEngine(tmp_path / "lancedb")
await engine.open()
doc_id = await engine.ingest_document("DMA error 0x42 means underrun", metadata={"source": "test"})
results = await engine.search("DMA underrun")
assert results
await engine.close()
```

#### 优先级

**中低。core 0.1.0 不阻塞；如果 README 强推 RAG tools，则建议补。**

---

## 4. 低优先级 / 发布后优化

### 4.1 boolean confirm 不是强 human confirmation

当前危险命令、reset、delete 都通过 boolean confirm 控制。它比没有确认强很多，但模型仍可以自己传 `confirm=True`。

如果未来要用于昂贵设备、生产线或远程生产环境，建议接入 MCP client 的 human approval / elicitation 流程，而不是只靠工具参数。

**优先级：发布后。**

---

### 4.2 `list_doc_sources()` 在大 RAG 库上可能较重

当前 `RagEngine.list_sources()` 会 count 所有文档，再拉取所有 rows 或 pandas dataframe 后取 distinct source。

小库没问题，但长期使用后可能会重。

后续可考虑：

- 单独维护 source index；
- ingestion 时写入 source registry；
- 只投影 source 列；
- 增加分页或缓存。

**优先级：发布后。**

---

### 4.3 `read_resource()` 无连接时返回普通内容而非结构化状态

当前：

- `device://live_log` 无连接时返回普通文本：`No active device connection.`；
- `device://session_info` 无连接时返回 JSON：`{"error": "No active device connection."}`。

这可以接受，因为资源存在，只是当前没有活动连接。但为了 client 更容易处理，可以统一成结构化状态：

```json
{
  "connected": false,
  "error": "No active device connection."
}
```

**优先级：低。**

---

## 5. 发布前建议 Checklist

### 必做，建议 0.1.0 前完成

- [ ] `build_tool_catalog(config)` 使用实际 `EmbPilotConfig` 生成 schema max/default。
- [ ] `list_tools()` 和 `call_tool` validation 共享同一份 config-aware tool catalog。
- [ ] 对 `send_command` 审计记录中的 command 字符串做内容级 secret 脱敏。
- [ ] 补测试：CLI 自定义 limit 后，tool schema 与 runtime validation 一致。
- [ ] 补测试：命令中包含 inline password/token/Bearer/AT Wi-Fi 密码时，operation history 不泄露明文。

### 可选，0.1.0 或 0.1.1

- [ ] `search_history_logs` 增加 `mode="fts" | "substring"`。
- [ ] RAG extra clean install/import smoke test。
- [ ] RAG ingest/search 临时目录 smoke test。

### 发布后优化

- [ ] 接入真正的 human approval / elicitation 机制。
- [ ] 优化 `list_doc_sources()` 的大库性能。
- [ ] 统一无连接资源状态格式。
- [ ] 增加真实硬件矩阵验证文档，例如 Serial loopback、Telnet shell、SSH BusyBox、bootloader prompt、AT command shell。

---

## 6. 推荐的下一个小提交

建议提交名：

```text
fix: align tool schemas with runtime config and redact command audit text
```

建议内容：

1. `build_tool_catalog(config: EmbPilotConfig | None = None)`。
2. `create_mcp_app()` 中构建并复用 config-aware `tool_catalog`。
3. `_handle_call_tool_request()` 使用传入 catalog 做 schema validation。
4. 增加 `redact_command_text()`。
5. `send_command()` 审计使用脱敏后的 command。
6. 补对应测试。

做完这个小提交后，我认为 EmbPilot 可以进入 **0.1.0 RC**。

---

## 7. 最终建议

当前项目已经具备较扎实的 MCP server 雏形：

- driver 契约已经统一；
- MCP schema 和错误语义已经明显增强；
- session / SQLite / FTS / audit / RAG 都有实质实现；
- 测试覆盖从单元、集成到 clean install smoke 都有所加强。

剩余问题主要集中在发布 polish，而不是架构性缺陷。建议再修一个小的 release-hardening follow-up，然后进入 RC。若后续目标是生产环境，重点应转向真实设备矩阵、human approval、审计策略和长期数据规模验证。
