# 纯阅 Pure Novel

手机优先的个人小说阅读器。Python 3.11+ 标准库后端，原生 HTML/CSS/JavaScript 前端；主服务无需第三方依赖。

支持本地独立运行，或使用自己的 Vercel + Supabase 部署邀请制同步实例。项目不提供公共服务、共享账号、预置书库或数据库凭据。

## 本地运行

下载仓库 ZIP 解压或克隆仓库，在项目根目录运行：

```sh
python --version
python reader.py
```

打开 http://127.0.0.1:8000 。部分系统使用 `python3`。保持终端运行，Ctrl+C 停止。不要直接双击 HTML 或使用纯静态服务器。

未配置 Supabase 且不在 Vercel 时使用无账号本地模式；之前配置过认证环境变量的，请先移除。程序**不会自动读取 .env**。

同一 Wi-Fi 下手机访问：

```sh
python reader.py --host 0.0.0.0 --port 8000
```

手机打开 `http://电脑局域网IP:8000`，必要时仅允许 Python 的专用网络入站连接。**不要向公网开放无账号开发服务。** 普通局域网 HTTP 不保证文件导入、PWA 和离线体验；完整体验需 HTTPS 或 localhost。

## 使用与限制

- 输入完整书名，检查来源并选择后开始阅读；也可粘贴有权访问的具体目录或章节 URL，而非站点首页。
- 搜索尝试 Bing、360、百度、搜狗公开网页，并非稳定搜索 API 或穷尽全网。改版、验证、403、网络出口和跳转都可能使入口不可用。查看详情或直接提供合法公开链接。
- 首次自动检查最多 8 个网站，主动继续寻找可检查更多新网站，找到两个样本可读网站后暂停。样本通过不代表全书完整、免费、授权或长期可用。同名作品需人工核对。
- 解析常见 HTML 正文、目录与分页，不支持任意网站、图片正文或必须执行源站 JavaScript 的内容。不绕过付费、登录、验证码或 DRM。
- 最近完整章节保存在本机 IndexedDB；预取最多同源下一章，不批量下载全书。
- 手动导入最多 24 MiB 无 DRM 文字 EPUB / TXT，预览后确认。不支持 PDF、MOBI、AZW3、加密 EPUB 或格式转换。编码推测可能不准。
- 导入正文只保存在本机；清理浏览器数据会丢失，需自行备份文件。
- 云端仅同步书架元数据、设置和进度，不同步正文、大目录。其他设备需重新导入相同文件。同步为整份书架版本化保存，冲突需选择版本，非实时订阅。
- 云端模式离线重启页面暂不开放书架；PWA、缓存保留和手机体验取决于浏览器及系统。

## 自行部署

见 [DEPLOY.md](DEPLOY.md)：准备自己的 Supabase、执行迁移、创建成员，再部署到 Vercel。无需维护者项目或聊天助手在线。

## 测试

在项目根目录运行：

```sh
python -m unittest discover -s tests -v
node --check public/app.js
node --check public/ebooks.js
node --check public/sw.js
```

单元测试不依赖外网。浏览器模拟冒烟需 Node.js 22+、Edge / Chromium 和运行在 8000 端口的本地服务：

```sh
node tests/browser-smoke.mjs
```

可用 `BROWSER_PATH` 指定浏览器。模拟测试不代替真实来源、数据库和手机验收。可选线上测试见部署指南。

## 文件

- `reader.py`：HTTP 服务、安全抓取、搜索、正文解析。
- `public/`：界面、导入、书架、缓存和 PWA。
- `cloud.py`：认证、成员检查、同步和配额。
- `api/index.py`、`vercel.json`：Vercel 函数与路由。
- `supabase/migrations/`：表、RLS 与函数。
- `setup_account.py`、`manage_user.py`：管理员本地开户。
- `tests/`：单元测试、模拟浏览器及可选联网诊断。

`acquire.py`、`openbooks.py`、`publications.py` 及部分历史测试为实验模块，不代表当前首页功能。主服务不需要 `requirements-assistant.txt`；历史自动获取脚本不属于启动或部署步骤。

## 安全与许可

见 [SECURITY.md](SECURITY.md)。URL 和内容会经过自己的服务端，不能称为完全匿名或全本地处理。

代码使用 [MIT License](LICENSE)。代码许可不授予第三方书籍、站点内容或商标权利；仅处理有权访问且站点条款允许处理的内容。