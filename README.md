# 前端作品和组件代码采集

## 多来源采集

`frontend_sources_config.json` 配置本次指定的 34 个去重来源。执行：

```powershell
.\.venv\Scripts\python.exe crawl_frontend_sources.py
```

默认 96 个外站任务、384 个连接/请求上限、24 个目录任务、4 个 sitemap 任务、
4 个公开 JSON 数据任务和 2 个公开源码仓库任务。每个网络端点最多 4 个连接，
目录同源间隔至少 0.8 秒，继续遵守 robots 的更长间隔。
不使用浏览器、不执行网站 JavaScript、不截图。

采集路径：公开目录 / sitemap → 作品详情 → 被收录网站或演示页 → HTML 和静态引用资源。
组件站提取公开代码块、iframe srcdoc、公开 JSON 内嵌代码，并下载其明确链接的 GitHub 源码包。
FWA 使用其前端公开调用的 `/api/cases/` 分页 GET；MaxiBestOf 等读取页面明确引用的 Nuxt JSON。
Neocities 仅使用公开 browse 页面 / sitemap，不调用其禁止社区采集的开发者 API。
Framer 下载公开演示页，不购买、登录、创建 Remix 或导出私有项目。

这是一套持续处理公开可达作品的任务，不能保证所有平台、所有作品均可获取。
robots 获取失败、HTTP 429/403、DNS、失效外链、付费和纯截图条目会记录原因。
`frontier_exhausted` 只表示已发现队列用尽，不证明网站收录已经百分之百覆盖。
目录选择器按当前公开页面验证，结构变化或未公开的分页会造成覆盖缺口。
来源状态为 `active` 只表示入口解析成功，不表示该来源所有作品采集成功。
原始工程、依赖数量、美学质量和离线效果都不能由下载成功自动推断。

输出在 `E:\workspaces\Ant\data\frontend_archive`，或由 `--output` 指定：

- `sites_manifest.csv`：**实际目标页面**的 URL、来源和绝对本地文件路径；成功和部分成功分别标记。
- `sites/<slug>/source.html` 和 `assets/`：外站公开交付的前端文件。
- `components/<source>/`：公开组件代码及逐文件来源元数据。
- `components_manifest.jsonl`：组件文件、语言、来源页面和 SHA256；不同来源相同 hash 可后续统一去重。
- `repositories/<owner>--<repo>/source/`：公开仓库源文件；`source.zip` 和 `metadata.json` 记录快照和许可证文件位置。
- `repositories_manifest.jsonl`：源码仓库、绝对路径和抓取信息。
- `directory/<source>/`：发现用的目录 / 详情 / XML / JSON，**不作为外站代码样本**。
- `progress.json`、`source_report.json`：全局进度、逐来源队列和失败原因。
- `catalog.jsonl`：每个发现、下载任务的状态与错误。
- `provenance.jsonl`：目标和收录来源的对应关系。
- `state.sqlite`：持久队列与索引。

按规范化目标 URL 去重，保留多个收录来源；下载后还记录 HTML SHA256 供内容去重。
自动接入已有 `data/onepagelove/state.sqlite` 的已知目标与下载文件，不复制已有数据。
旧目录保留 `STOP`，旧单来源爬虫无需单独启动。多来源任务的 One Page Love 队列会继续处理该来源。
新的目标在多来源 `sites/` 下，复用的旧文件路径可直接在清单查看。
旧任务的失败和跳过会作为新队列中的首次尝试重新检查。

```powershell
# 立即暂停多来源任务；已完成文件保留，未完成任务回到 pending
New-Item -ItemType File -Path data/frontend_archive/STOP

# 恢复，先确认上一次进程已退出
Remove-Item -LiteralPath data/frontend_archive/STOP
.\.venv\Scripts\python.exe crawl_frontend_sources.py

# 只验证部分来源，运行 120 秒后退出；请使用独立输出目录
.\.venv\Scripts\python.exe crawl_frontend_sources.py --sources httpster,hyperui --output data/sample --run-seconds 120

# 有限验证也可按每来源 HTML 页面数、目标尝试数、仓库尝试数限制
.\.venv\Scripts\python.exe crawl_frontend_sources.py --output data/sample2 --discovery-limit 10 --site-limit 20 --repo-limit 2

# 本地集成与提取/去重/暂停/ZIP 路径安全测试
.\.venv\Scripts\python.exe -m unittest -v test_http_crawler test_multisource_crawler
```

未指定限额时默认持续处理至已发现队列用尽。`--retry-failed` 会把失败和 robots 跳过任务重新入队，
不绕过限制。每站超时 120 秒、500 个静态引用、单文件 12 MiB、单站 96 MiB。
仓库压缩包上限 512 MiB、解压总量上限 4 GiB，拦截路径逃逸并跳过符号链接。
磁盘可用空间低于 10 GiB 时暂停。`crawl.lock` 不能在进程仍运行时移除。

## 旧 One Page Love 单来源入口

从 One Page Love 公开列表和 sitemap 发现网站，用纯 HTTP 并发下载其落地页、CSS、JS、图片与字体。
**不启动浏览器、不执行 JavaScript、不渲染、不截图。**
保存的是网站公开发布的前端文件，不是作者私有的 React/Vue/Next.js 原始工程。

## 安装与运行

需要 Python 3.11 或更新版本。Windows PowerShell：

```powershell
git clone git@github.com:Ylr9933/aesthetic_spider.git
cd aesthetic_spider
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe crawl_onepagelove.py
```

默认 96 个网站任务、384 个全局 HTTP 请求/连接、每站 8 个资源任务、每个网络端点最多 4 个连接。
文件流式写盘，减少图片和字体在内存中的占用。并发配置是上限，不代表始终有同样数量的活跃请求。
One Page Love 目录和详情请求统一至少间隔 0.8 秒；外站同源请求至少间隔 0.06 秒，并遵守 robots 的更长间隔。
跨域并发用于加速不同网站，同一 CDN 和目录域名仍共享连接及限速。

```powershell
# 调整并发
.\.venv\Scripts\python.exe crawl_onepagelove.py --workers 96 --connections 384 --per-host 4 --asset-workers 8

# 处理 20 个待采集条目，然后退出
.\.venv\Scripts\python.exe crawl_onepagelove.py --limit 20

# 继续未完成条目，并把之前 failed/http_failed 条目重试一次
.\.venv\Scripts\python.exe crawl_onepagelove.py --retry-failed
```

其他参数：`--output data/onepagelove`、`--max-resources 500`、`--site-timeout 120`（每站总秒数）。
`--limit 0` 为全部候选。单文件上限 12 MiB，单站传输预算 96 MiB，引用递归最多 8 层。
429/503 来源暂停至本次运行结束；robots 无法确认、访问限制、失效链接单独记录。
不登录、不提交表单、不绕过验证码，只抓对应落地页及静态引用资源，不遍历整个外站所有内页。

## 下载内容与局限

解析 HTML 中的脚本、样式、图片、常见懒加载属性、srcset、预加载与内嵌样式。
递归解析 CSS 的 `url()`/`@import`，以及 JS 的静态 import/export、字面量动态 import 和 `new URL(..., import.meta.url)`。
跟踪重定向，按最终资源 URL 解析相对引用；保存资源映射和原始字节。跳过流媒体。

没有执行 JS，无法保证找到运行时拼接的资源地址、打包器 chunk 映射、接口数据和动态 DOM。
模块 import map、完整 JS/CSS 语法及所有懒加载实现并未完全解析。
`downloaded` 仅表示静态发现的资源下载结束且没有已知错误，不代表整站完整、无依赖、视觉合格或功能可用。
`downloaded_partial` 表示存在资源错误、规则阻挡或预算限制。
过去浏览器模式的 `captured`/`captured_partial` 和文件保留，继续运行不会重新截图或重下成功条目。

## 产物

- `data/onepagelove/progress.json`：阶段、模式、并发设置、本轮完成数及各状态数量。
- `catalog.csv`、`catalog.jsonl`：来源、目标地址、状态、输出目录及失败原因。
- `sites_manifest.csv`：已下载网站的目标 URL、HTML/资源/元数据的绝对本地路径，便于直接定位外站代码。
- `state.sqlite`：断点续跑状态。
- `sitemap_candidates.json`：公开 sitemap 候选，包含部分模板/文章，不等于有效网站总数。
- `directory/`：公开目录和详情 HTML。
- `sites/<slug>/source.html`：服务器返回的落地页 HTML。
- `sites/<slug>/assets/`：成功下载的原始 CSS、JS、图片、字体和引用资源。
- `sites/<slug>/page_text.txt`：HTML 可提取文本，不包含运行时生成的 DOM。
- `sites/<slug>/metadata.json`：资源 URL/本地文件映射、hash、字节数、耗时、错误与局限。
- `robots/`：使用的规则和获取状态。
- `index.html`：归档索引；旧截图仍可显示，新条目只有文本链接。

新 HTTP 条目不生成 `rendered.html`、截图或离线预览。`source.html` 保留原始地址，直接打开不保证离线可用。
数据、日志和虚拟环境由 `.gitignore` 排除，不上传仓库。

```powershell
.\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory data/onepagelove
```

打开 `http://127.0.0.1:8765/` 浏览索引。

## 停止、恢复与验证

在输出目录创建空文件 `STOP`，当前任务完成或超时后停止。恢复前移除 `STOP`，再运行相同命令。
`crawl.lock` 防止两个进程写入同一归档；确认旧进程退出后才能移除残留锁。
中断的 `running` 条目恢复到队列。成功条目跳过，失败默认不重试，使用 `--retry-failed` 显式重试。
目录后来发现原先缺失的目标地址时，会将 `no_public_target` 条目重新入队。

本地 HTTP 集成测试（不访问外网、不调用浏览器）：

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_http_crawler
```

第三方代码和素材的权利及训练使用条件需按作品核实；可下载不自动提供训练或再分发授权。
