# One Page Love 前端归档

采集 One Page Love 的公开 Inspiration 列表，并保存每个收录链接对应的落地页。
这是**网站发布后的前端代码**，不是作者原始的 React/Vue/Next.js 工程。

## 安装与运行

需要 Python 3.11 或更新版本。Windows PowerShell：

```powershell
git clone git@github.com:Ylr9933/aesthetic_spider.git
cd aesthetic_spider
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

开始采集：

```powershell
.\.venv\Scripts\python.exe crawl_onepagelove.py
```

脚本先用公开 posts sitemap 建立候选队列，再并行翻页读取 Inspiration 列表并采集目标网站。
候选队列包含部分模板和其他非 Inspiration 条目，没有公开目标地址的条目会记录并跳过。
可断点续跑：重复运行同一命令，已完成条目不会再次抓取。
默认四个浏览器任务，One Page Love 请求间隔至少 0.8 秒。
遵守各来源 robots.txt，遇到无法确认的 robots、登录、验证码或访问限制会记录并跳过。
不提交表单或执行购买。只抓对应页面，不遍历所有外站内页。

可选参数：`--workers 4` 设置浏览器任务数，`--limit 10` 只处理 10 个待采集条目，
`--output data/onepagelove` 设置输出目录。`--limit 0`（默认）处理全部候选条目。
输出数据和本地虚拟环境已被 `.gitignore` 排除，不随代码上传。

## 产物

- `data/onepagelove/progress.json`：当前阶段、数量、进程号。
- `data/onepagelove/catalog.csv` 和 `catalog.jsonl`：条目、外站地址、结果和失败原因。
- `data/onepagelove/sitemap_candidates.json`：公开 sitemap 中的候选 URL 清单，包含部分非 Inspiration 条目。
- `data/onepagelove/index.html`：本地截图浏览页。
- `data/onepagelove/sites/<slug>/source.html`：服务器实际返回的 HTML。
- `rendered.html`：浏览器执行后的 DOM。
- `assets/`：浏览器实际请求且成功保存的 CSS、JS、图片、字体及数据；原始字节保留。
- `desktop.png` 和 `mobile.png`：1440px 和 390px 视口的截图，长度上限 32,000px。
- `offline.html`：移除脚本并重写已保存资源地址的静态预览，不承诺完整交互或完全离线。
- `metadata.json`：来源、时间、最终 URL、资源映射、错误及采集限制。
- `robots/`：本次采集使用的 robots 规则与获取状态。

`captured` 代表页面与截图采集成功；`captured_partial` 代表存在资源阻挡或脚本错误。
这两种状态都**不代表通过了功能测试、美学评分或训练授权核验**。
失效链接、停放域名、访问限制、机器人规则阻挡、加载超时单独记录。
按网页预算跳过流媒体、单文件超过 12MB 的资源，以及每站超过 96MB/500 个请求的资源。

浏览本地归档可执行：

```powershell
.\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory data/onepagelove
```

打开 `http://localhost:8765/`。预览中的未归档资源仍可能引用原网站。

## 停止与恢复

在输出目录创建名为 `STOP` 的空文件，当前任务结束后停止。
恢复前移除自己创建的 `STOP` 文件，再执行运行命令。
`crawl.lock` 防止两个进程写同一归档。只有确认旧进程已退出，才能移除残留锁。
断点续跑会重试中断时的 `running` 条目，不会自动重试终态失败；失败原因在清单中。

One Page Love 的 API 页面目前显示 Private Beta，所以这里使用公开 HTML 列表与公开 sitemap。
历史收录页面可能已经失效或改变设计；当前截图无法替代收录时的历史截图。
第三方站点和模板的代码与素材权利分别归其权利人；采集记录不自动提供训练授权。
