# 微博批量清理脚本

一个基于 Python + 微博网页 API 的批量删除工具，用于清理自己的微博，支持：

- **一键清空**所有可获取的微博
- **按时间段清理**微博（`--since` / `--until`）
- **默认预览模式**：不加 `--confirm` 不会删除任何东西

## 普通用户使用（Windows）

从 GitHub Releases 下载 `微博清理工具-Edge版.zip`，解压后双击 `微博清理工具.exe`。

- 无需安装 Python 或 Docker。
- 电脑需要已安装 Microsoft Edge（Windows 10/11 默认自带）。
- 登录 Cookie 仅保存在本次程序运行的内存中，关闭程序即清除。

## 原理

脚本直接调用微博网页版的两条接口：

- `GET https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0` 拉取微博列表
- `POST https://weibo.com/ajax/statuses/destroy` 删除单条微博

需要你自己提供**已登录微博的 Cookie**，脚本并不存储或上传任何信息。

## 安装

1. 确保已安装 Python 3.9+。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

## 获取 Cookie 和 UID

### UID

打开你的微博个人主页，地址形如：

```
https://weibo.com/u/7910373640
```

其中 `7910373640` 就是你的 UID。

### Cookie

1. 在 Chrome 中打开 `https://weibo.com/u/<你的UID>`，确认已登录。
2. 按 F12 → Network（网络）→ 刷新页面。
3. 任意一个 `weibo.com` 请求右键 → Copy → Copy as cURL(bash)。
4. 把 `Cookie:` 后面那一大串字符串复制出来。

> Cookie 中**必须包含 `XSRF-TOKEN`、`SUB`、`SCF`、`WBPSESS` 等登录态字段**，否则接口会返回 `ok=-100`（未登录）。
> 注意：JavaScript 无法读取 `HttpOnly` Cookie，所以**不要**从 `document.cookie` 复制。

#### 使用 `--cookie-file`（推荐）

把 Cookie 存到文件里，避免它出现在命令行历史或被旁人看到：

```bash
echo "你的完整Cookie字符串" > cookie.txt
python weibo_cleanup.py --uid 7910373640 --cookie-file cookie.txt --since 2025-04-01 --until 2025-04-30
```

> ⚠️ Cookie 是敏感信息，不要上传、截图或发送给他人。

## 用法

### 推荐：打开本地 Web 界面

Windows 用户可以直接双击目录里的 `启动微博清理工具.bat`。首次运行会自动安装 Python 依赖；也可以使用命令启动：

```bash
python weibo_app.py
```

程序会自动打开 `http://127.0.0.1:8765`。点击“打开扫码窗口”，在弹出的微博登录窗口中扫码；登录成功后选择“全部微博”“某月之前”或“自定义时间段”，先预览数量，再确认删除。

扫码登录通过本机已安装的 Microsoft Edge 完成。程序只监听本机，Cookie 和任务数据只在内存中存在，关闭程序后清除。微博的 HttpOnly Cookie 无法由网页 JavaScript 直接读取，因此扫码过程使用独立浏览器窗口完成。

### 1. 预览将要删除的微博（dry-run）

```bash
python weibo_cleanup.py \
  --uid 7910373640 \
  --cookie "XSRF-TOKEN=xxx; SUBP=yyy; ..."
```

### 2. 按时间段清理

```bash
python weibo_cleanup.py \
  --uid 7910373640 \
  --cookie "..." \
  --since 2025-01-01 \
  --until 2025-04-30
```

### 3. 真正删除

上一步确认无误后，加上 `--confirm`：

```bash
python weibo_cleanup.py \
  --uid 7910373640 \
  --cookie "..." \
  --since 2025-01-01 \
  --until 2025-04-30 \
  --confirm
```

### 4. 全部清空

不加 `--since` / `--until`，拉取到的所有微博都会被删除：

```bash
python weibo_cleanup.py \
  --uid 7910373640 \
  --cookie "..." \
  --confirm
```

## 参数说明

| 参数 | 说明 |
|------|------|
| `--uid` | 微博 UID（必填） |
| `--cookie` | 登录 Cookie 字符串（与 `--cookie-file` 二选一） |
| `--cookie-file` | 存放 Cookie 的文件路径，更安全（与 `--cookie` 二选一） |
| `--since` | 起始日期，格式 `YYYY-MM-DD`（可选，包含当天） |
| `--until` | 结束日期，格式 `YYYY-MM-DD`（可选，包含当天） |
| `--confirm` | 确认执行删除；不加则只预览 |
| `--feature` | 微博类型：0=全部（默认）、1=原创、2=图片、3=视频、4=音乐 |
| `--delay` | 删除间隔秒数，默认 1.5，过快可能触发风控 |

## 注意事项

1. **先预览再加 `--confirm`**：脚本默认安全，不会误删。
2. **Cookie 会过期**：如果接口返回 `ok=-100`，请重新复制 Cookie。
3. **feature=0 实测返回原创微博**：如果你的账号有大量转发微博，可能需要另外抓取；脚本会在未来版本补充。
4. **删除不可逆**：确认后再执行。
5. **风控**：建议保持默认 `--delay=1.5`，大量删除时不要调太小。

### 清空极限与可见性

脚本最多读取 500 页，并对重复页去重。实际每页数量由微博接口决定，常见情况下约能覆盖数千条到一万条；这不是微博官方承诺的上限。Web 界面会显示已读取页数、已读取条数、接口返回的约数，并在触及 500 页时明确提示。若接口总量大于已读取数量或页面触顶，说明仍可能有未获取到的微博，不要把“完成”理解为账号历史微博绝对为零。

## 目录结构

```
微博/
├── weibo_cleanup.py   # 主脚本
├── weibo_app.py       # 本地 Web 服务与扫码登录
├── weibo_ui.html      # 本地前端界面
├── 启动微博清理工具.bat # Windows 双击启动入口
├── requirements.txt   # 依赖
├── build_release.ps1  # 生成 Windows 发布包
└── README.md          # 本说明
```

## 已知限制

- 依赖微博网页版未公开的 AJAX API，若微博调整接口，脚本可能需要更新。
- 部分特殊类型微博（如转发、隐藏微博、推广微博）可能不在 `mymblog` 接口返回范围内。
