# boss-mcp-job-hunting

`job hunting` 的意思是“求职 / 找工作”。这个 MCP 用于在 Boss 直聘上按目标岗位关键词搜索职位，并过滤最近一段时间发布的岗位。

当前版本使用 **FastMCP + Playwright**。它不会硬编码 Boss 的内部接口，而是使用一个本地持久化浏览器资料目录复用登录态：

- 先调用 `open_boss_login(headless=false)`，在浏览器里扫码登录 Boss 直聘。
- 再调用 `search_boss_jobs(keyword="AI解决方案岗", days=30)` 搜索最近 30 天匹配岗位。

## 安装

```bash
cd ~/Documents/boss-mcp-job-hunting
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## 运行

作为 stdio MCP server：

```bash
boss-mcp-job-hunting
```

或者：

```bash
python -m boss_mcp_job_hunting.server
```

## MCP 客户端配置

```json
{
  "mcpServers": {
    "boss-mcp-job-hunting": {
      "command": "/Users/yangzhiyu/Documents/boss-mcp-job-hunting/.venv/bin/boss-mcp-job-hunting",
      "args": []
    }
  }
}
```

## 工具

### `open_boss_login`

打开 Boss 直聘登录页。首次使用建议设置：

```json
{
  "headless": false
}
```

扫码登录后，登录态会保存在：

```text
~/Documents/boss-mcp-job-hunting/.boss-browser-profile
```

### `get_boss_login_status`

检查本地浏览器资料目录里是否看起来已经登录。

### `search_boss_jobs`

搜索岗位并过滤最近 N 天发布的结果。

示例：

```json
{
  "keyword": "AI解决方案岗",
  "city": "全国",
  "days": 30,
  "pages": 3,
  "extra_keywords": ["大模型", "售前", "解决方案", "AI Solution"],
  "require_publish_date": true,
  "headless": true
}
```

常用城市：`全国`、`北京`、`上海`、`广州`、`深圳`、`杭州`、`成都`、`南京`、`武汉`、`西安`、`苏州`、`天津`、`重庆`。

如果某些岗位卡片没有显示发布时间，可以把 `require_publish_date` 设为 `false`，这样会保留没有发布时间但关键词匹配的岗位。

## 说明

Boss 直聘页面和风控策略可能变化。如果搜索结果为空，通常先尝试：

1. 调用 `open_boss_login(headless=false)` 完成登录或安全验证。
2. 把 `headless` 改为 `false` 观察浏览器页面。
3. 减少 `pages`，避免过于频繁访问。

