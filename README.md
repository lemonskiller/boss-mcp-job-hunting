# boss-mcp-job-hunting

`job hunting` 的意思是“求职 / 找工作”。这个 MCP 用于在 Boss 直聘上按目标岗位关键词搜索职位，并过滤最近一段时间发布的岗位。

当前版本使用 **FastMCP + Playwright**。它不会硬编码 Boss 的内部接口，而是使用一个本地持久化浏览器资料目录复用登录态：

- 优先调用 `import_boss_cookies(cookie_header="...")` 导入正常浏览器里的 Cookie。
- 如果 Boss 对 Playwright profile 触发风控，可以用 `search_boss_jobs_chrome_debug()` 连接真实 Chrome 会话读取页面。
- `start_boss_qr_login()` / `complete_boss_qr_login()` 是备用方案；Boss 可能会让 App 显示“扫码失败”。
- `login_boss_interactive()` 仍可作为备用，但 Boss 可能会把浏览器登录页跳到 `about:blank`。
- 再调用 `search_boss_jobs(keyword="AI解决方案岗", days=30)` 搜索最近 30 天匹配岗位。

## 安装

```bash
cd boss-mcp-job-hunting
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
      "command": "boss-mcp-job-hunting",
      "args": []
    }
  }
}
```

## 工具

### `import_boss_cookies`

把已经登录 Boss 直聘的浏览器 Cookie 导入到 MCP 的持久化资料目录。这是当前最稳的登录方式。

```json
{
  "cookie_header": "复制浏览器请求头里的 Cookie 内容",
  "verify": true
}
```

也可以在 MCP 启动环境里设置 `BOSS_COOKIE`，搜索时会自动应用。

### `start_boss_qr_login`

生成 Boss 直聘登录二维码图片，不打开浏览器登录页。这个接口可能被 Boss App 判定为“扫码失败”，因此只作为备用方案。

```json
{}
```

返回里的 `qr_image_path` 是本地二维码图片路径。用 Boss 直聘 App 扫码后，继续调用 `complete_boss_qr_login`。

### `complete_boss_qr_login`

等待 App 扫码确认，并把拿到的登录 Cookie 导入 MCP 的持久化资料目录。

```json
{
  "timeout_seconds": 180,
  "verify": true
}
```

扫码登录后，登录态会保存在：

```text
./.boss-browser-profile
```

### `open_boss_login`

只打开 Boss 直聘登录页，不等待登录完成。更推荐使用 `login_boss_interactive`。

```json
{
  "headless": false
}
```

### `login_boss_interactive`

打开可见浏览器窗口，等待扫码登录和安全验证完成。默认会先给你 90 秒扫码时间，这段时间不会访问岗位搜索页，避免 Boss 在你扫码前就把页面跳到 `about:blank`。如果登录页变成 `about:blank`，工具会返回 `blank_redirect`，不会自动重开窗口；这时建议用 `import_boss_cookies`。

```json
{
  "timeout_seconds": 300,
  "qr_wait_seconds": 90,
  "check_interval_seconds": 5
}
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

### `search_boss_jobs_chrome_debug`

连接你真实的 Chrome 会话读取 Boss 页面。适合普通 MCP 浏览器 profile 被 Boss 风控拦截，但你自己的 Chrome 可以正常登录浏览时使用。

先关闭 Chrome，然后启动一个带调试端口的独立 Chrome：

```bash
open -na 'Google Chrome' --args --remote-debugging-port=9222 --user-data-dir=/tmp/boss-mcp-chrome-debug
```

在这个 Chrome 里正常登录 Boss 直聘，手动打开目标搜索页并等结果渲染出来，然后调用：

```json
{
  "keyword": "AI解决方案岗",
  "city": "全国",
  "days": 30,
  "pages": 3,
  "extra_keywords": ["大模型", "售前", "解决方案", "AI Solution"],
  "require_publish_date": true,
  "debug_url": "http://127.0.0.1:9222",
  "allow_navigation": false
}
```

这个工具只连接本机 Chrome DevTools，不会把 Cookie 写入 Git。默认 `allow_navigation=false`，只读取你已经打开的 Boss 标签页，不新开页面、不跳转 URL，避免 Boss 把页面变成 `about:blank`。

## 说明

Boss 直聘页面和风控策略可能变化。如果搜索结果为空，通常先尝试：

1. 调用 `import_boss_cookies` 导入正常浏览器的 Cookie。
2. 如果 Cookie 失效，重新在正常浏览器里登录 Boss 直聘并复制新的 Cookie。
3. 如果 Playwright profile 仍触发风控，用 `search_boss_jobs_chrome_debug` 连接真实 Chrome。
4. 如果想尝试二维码备用方案，调用 `start_boss_qr_login()` 和 `complete_boss_qr_login()`。
5. 把 `search_boss_jobs` 的 `headless` 改为 `false` 观察浏览器页面。
6. 减少 `pages`，避免过于频繁访问。
