# Arxiv VLN MCP Server

> 一个面向**视觉语言导航（Visual Language Navigation, VLN）**研究者的 MCP（Model Context Protocol）服务，封装 Arxiv API，让你在 Claude Code / Claude Desktop / Cursor 等任何支持 MCP 的客户端中，用自然语言搜索、阅读和梳理 VLN 领域的最新论文。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-1.12%2B-green)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](#license)

---

## 目录

- [功能特性](#功能特性)
- [技术方案](#技术方案)
- [快速开始](#快速开始)
- [接入 MCP 客户端](#接入-mcp-客户端)
  - [Claude Code (CLI)](#claude-code-cli)
  - [Claude Desktop](#claude-desktop)
  - [Cursor / 其他客户端](#cursor--其他客户端)
- [工具参考](#工具参考)
- [资源与提示词](#资源与提示词)
- [配置](#配置)
- [运行测试](#运行测试)
- [设计决策与权衡](#设计决策与权衡)
- [项目结构](#项目结构)

---

## 功能特性

- 🔍 **语义搜索**：按关键词搜索 VLN 论文，支持按相关性 / 提交时间 / 更新时间排序
- 📄 **详情获取**：获取单篇论文的完整元数据（作者、摘要、分类、DOI、PDF 链接等）
- 📚 **批量摘要**：一次性获取多篇论文并生成结构化阅读报告（按年份分组）
- 👤 **作者检索**：按作者名在 AI / CV / RO / LG 分类下检索论文
- 📊 **趋势分析**：内置提示词，引导模型自动完成领域研究趋势分析
- ⚖️ **论文对比**：内置提示词，引导模型从方法 / 数据集 / 指标等维度对比多篇论文
- ⚡ **智能缓存**：相同查询 5 分钟内直接命中缓存，减少 API 请求
- 🛡️ **限流保护**：遵守 Arxiv 官方推荐，强制 3 秒请求间隔
- 🔀 **并发控制**：批量请求最多 5 个并发，避免过载
- 🔁 **自动重试**：网络错误自动重试 3 次

---

## 技术方案

### 架构总览

项目采用清晰的**三层架构**，职责分离，便于测试与扩展：

```
┌─────────────────────────────────────────────┐
│              main.py  (服务层)               │
│   FastMCP 服务器 · 工具 · 资源 · 提示词      │
│   · search_vln_papers                        │
│   · get_paper_details                        │
│   · batch_summarize_papers                   │
│   · search_papers_by_author                  │
│   · arxiv://* 资源 · 2 个提示词              │
└──────────────────┬──────────────────────────┘
                   │  调用
┌──────────────────▼──────────────────────────┐
│         arxiv_client.py  (客户端层)          │
│   ArxivClient · 异步化 · 缓存 · 限流 · 并发   │
│   · RequestMetadata · ArxivAPIError          │
└──────────────────┬──────────────────────────┘
                   │  读取
┌──────────────────▼──────────────────────────┐
│            config.py  (配置层)                │
│   Settings (frozen dataclass) · 环境变量     │
└─────────────────────────────────────────────┘
```

### 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| [arxiv](https://pypi.org/project/arxiv/) | `>=2.1.0,<3` | Arxiv 官方 API 的 Python 封装 |
| [mcp[cli]](https://pypi.org/project/mcp/) | `>=1.12,<2` | MCP SDK，提供 `FastMCP` 服务器框架 |
| [pydantic](https://pypi.org/project/pydantic/) | `>=2.7,<3` | 工具参数声明与运行时校验 |

> ⚠️ **版本约束说明**：代码基于 MCP 1.x 的 `mcp.server.fastmcp.FastMCP` API 编写。MCP 2.x 中该入口已改名，因此 `requirements.txt` 中严格限定 `mcp[cli]<2`，避免安装到不兼容的 2.x 版本。

### 关键设计

#### 1. STDIO 传输模式

服务通过 **STDIO**（标准输入 / 输出）与 MCP 客户端通信，这是本地 MCP 集成最通用、最轻量的方式，无需网络端口、无需认证。

- **所有日志输出到 `stderr`**，绝不影响 `stdout` 上的 MCP 协议数据流
- 通过 `mcp.run(transport="stdio")` 启动

#### 2. 同步库异步化

`arxiv` 库本身是**同步阻塞**的。为避免阻塞 MCP 服务器的事件循环，所有同步调用都通过 `loop.run_in_executor()` 放到线程池中执行：

```python
loop = asyncio.get_event_loop()
results = await loop.run_in_executor(
    None,
    lambda: list(self._client.results(search)),
)
```

这使得 `ArxivClient` 的所有方法都是 `async`，可被 MCP 工具的异步处理器无缝调用。

#### 3. 智能缓存

- **缓存键**：对查询参数（query、max_results、sort_by、sort_order）做 MD5 哈希，生成唯一的缓存文件名
- **存储**：JSON 文件，位于 `arxiv_mcp/cache/`
- **TTL**：默认 300 秒（5 分钟），过期自动失效
- **收益**：相同查询二次请求直接命中缓存，显著降低延迟

#### 4. 限流与并发控制

```python
async def _rate_limit(self) -> None:
    """确保请求间隔不小于配置值（默认 3 秒）"""
    now = time.time()
    elapsed = now - self._last_request_time
    if elapsed < self.settings.request_interval:
        await asyncio.sleep(self.settings.request_interval - elapsed)
    self._last_request_time = time.time()
```

并发控制使用 `asyncio.Semaphore`：

```python
self._semaphore = asyncio.Semaphore(settings.max_concurrent)  # 默认 5
```

#### 5. 统一的错误处理

所有异常被转换为统一的结构化错误格式，并捕获为 `ArxivAPIError`：

```python
class ArxivAPIError(Exception):
    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"type": "arxiv_api_error", "message": str(self)},
        }
```

- 搜索 / 获取失败时返回 `{"ok": false, "error": {...}}`
- **批量请求中，单篇论文失败不影响其他论文**（`asyncio.gather` + 容错包装）

#### 6. 日志脱敏

`RecentLogHandler` 维护一个内存日志缓冲（最近 100 条），供 `arxiv://service/logs` 资源查询；并在写入前**自动脱敏**包含 `api_key` / `token` 的日志条目。

---

## 快速开始

### 前置条件

- Python **3.10+**
- 可访问 `https://export.arxiv.org`（Arxiv API 完全开放，**无需 Token**）

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/Accerlator/arxiv-vln-mcp.git
cd arxiv-vln-mcp

# 2. 创建虚拟环境（可选但推荐）
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt
```

### 验证安装

```bash
python test_arxiv_mcp.py
```

全部输出 `[OK]` 且末尾为 `[SUCCESS] All tests passed!` 即表示安装成功。

### 手动启动（调试用）

```bash
python -m arxiv_mcp.main
```

服务会启动并等待 STDIO 输入，按 `Ctrl+C` 退出。

---

## 接入 MCP 客户端

### Claude Code (CLI)

**方式一：命令行添加（推荐）**

```bash
# 项目级（仅当前项目生效）
claude mcp add arxiv-vln-research -e LOG_LEVEL=INFO -- python -m arxiv_mcp.main

# 用户级（所有项目全局生效）
claude mcp add arxiv-vln-research -s user -e LOG_LEVEL=INFO -- python -m arxiv_mcp.main
```

**方式二：如果模块不在当前目录，需显式指定 `PYTHONPATH`**

```bash
claude mcp add arxiv-vln-research -s user \
  -e LOG_LEVEL=INFO \
  -e "PYTHONPATH=C:\path\to\arxiv-vln-mcp" \
  -- python -m arxiv_mcp.main
```

> 💡 **作用域说明**：`claude mcp add` 默认是 `local`（仅当前项目目录）。若希望在所有目录都能调用，请加上 `-s user`；若无法连接，通常是运行时 `cwd` 不含模块，用 `PYTHONPATH` 指向项目根目录即可解决。

**验证**

```bash
claude mcp list          # 应看到 arxiv-vln-research · ✔ Connected
claude mcp get arxiv-vln-research
```

### Claude Desktop

编辑 `claude_desktop_config.json`：

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "arxiv-vln-research": {
      "command": "python",
      "args": ["-m", "arxiv_mcp.main"],
      "cwd": "/绝对路径/arxiv-vln-mcp",
      "env": {
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

> 保存后**完全退出并重启** Claude Desktop。

### Cursor / 其他客户端

支持 MCP 的 IDE 都在项目根目录使用 `.mcp.json`：

```json
{
  "mcpServers": {
    "arxiv-vln-research": {
      "command": "python",
      "args": ["-m", "arxiv_mcp.main"],
      "cwd": ".",
      "env": {"LOG_LEVEL": "INFO"}
    }
  }
}
```

---

## 工具参考

所有工具返回 JSON；成功 `ok` 为 `true`，失败 `ok` 为 `false` 并附带 `error`。

### 1. `search_vln_papers`

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `query` | string | `"visual language navigation"` | 搜索查询，最长 500 字符 |
| `max_results` | int (1–100) | `20` | 最大返回数量 |
| `sort_by` | enum | `"submittedDate"` | `relevance` / `lastUpdatedDate` / `submittedDate` |
| `sort_order` | enum | `"descending"` | `ascending` / `descending` |

### 2. `get_paper_details`

| 参数 | 类型 | 说明 |
|---|---|---|
| `arxiv_id` | string | Arxiv ID，如 `"2301.12345"` 或 `"2301.12345v2"` |

### 3. `batch_summarize_papers`

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `arxiv_ids` | list[string] | — | 论文 ID 列表（最多 50 篇） |
| `include_categories` | bool | `true` | 是否包含分类标签 |

### 4. `search_papers_by_author`

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `author_name` | string | — | 作者名，如 `"Peter Anderson"` |
| `max_results` | int (1–50) | `10` | 最大返回数量 |

---

## 资源与提示词

### 资源（Resources）

| URI | 内容 |
|-----|------|
| `arxiv://search/latest` | 最近一次搜索的缓存结果 |
| `arxiv://service/status` | 服务状态与统计（请求数、缓存大小、配置） |
| `arxiv://service/logs` | 最近 100 条内存日志（已脱敏） |

### 提示词（Prompts）

| 名称 | 用途 |
|------|------|
| `analyze_vln_research_trends` | 引导模型按时间分布、研究方向、作者合作等维度分析领域趋势 |
| `compare_vln_papers` | 引导模型从方法、数据集、指标等维度对比多篇论文 |

**使用示例**：

```
用户：分析 2024 年 VLN 领域的研究趋势，重点关注 embodied AI
用户：比较这三篇论文：2301.12345, 2302.67890, 2303.11111
```

---

## 配置

通过环境变量自定义行为：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ARXIV_CACHE_DIR` | `./arxiv_mcp/cache` | 缓存目录 |
| `ARXIV_MAX_RESULTS` | `100` | 单次搜索最大结果数 |
| `ARXIV_REQUEST_INTERVAL` | `3.0` | 请求间隔（秒），遵守 Arxiv 推荐 |
| `ARXIV_REQUEST_TIMEOUT` | `30.0` | 单次请求超时（秒） |
| `ARXIV_CACHE_TTL` | `300` | 缓存有效期（秒） |
| `ARXIV_MAX_CONCURRENT` | `5` | 批量请求最大并发数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 运行测试

```bash
python test_arxiv_mcp.py
```

脚本逐项验证：配置模块、arxiv 库、`ArxivClient`、`main` 服务是否可正常导入与实例化。

---

## 设计决策与权衡

| 决策 | 选择 | 理由 | 可能的代价 |
|------|------|------|-----------|
| 传输模式 | 仅 STDIO | 本地 MCP 最通用，无需端口 / 认证 | 无法远程调用 |
| 是否下载 PDF | 否 | 摘要已足够判断价值；下载慢且占空间 | 无法提取正文细节 |
| 缓存策略 | TTL-only | 实现简单 | 缓存可能无限增长，需手动清理 |
| 同步库调用 | `run_in_executor` 线程池 | 避免阻塞事件循环 | 高并发时受线程池上限约束 |

---

## 项目结构

```
arxiv-vln-mcp/
├── arxiv_mcp/
│   ├── __init__.py       # 包标识
│   ├── config.py         # 环境变量驱动的配置（Settings dataclass）
│   ├── arxiv_client.py   # Arxiv API 客户端（缓存 / 限流 / 并发 / 重试）
│   └── main.py           # FastMCP 服务器（4 工具 + 3 资源 + 2 提示词）
├── requirements.txt      # 依赖声明
├── test_arxiv_mcp.py     # 安装验证脚本
├── .gitignore
└── README.md
```

---

## License

MIT License —— 本项目为教学 / 研究用途。Arxiv API 数据请遵守 [Arxiv Terms of Use](https://arxiv.org/help/api/tou)。

如果本工具对你有帮助，请在研究中遵守学术规范并正确引用相关论文。