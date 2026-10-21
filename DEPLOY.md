# 自行部署：Vercel + Supabase

使用自己的项目、密钥和账号。本地无账号运行见 [README.md](README.md)。

## 1. Supabase

1. 创建专用项目，安全保管数据库密码。
2. 在 SQL Editor 执行 `supabase/migrations/202610190001_reader.sql` 全文。迁移创建表、RLS、同步和配额函数；已有项目请先检查同名对象并备份。
3. Authentication 设置关闭自由注册、关闭匿名登录，保留 Email / Password Provider。
4. 在项目设置取得项目 HTTPS URL、Publishable key、Secret key；也支持旧 anon / service_role key。

登录使用自定义用户名和密码，不需要真实邮箱。用户名支持 3～32 位中文、英文字母、数字和下划线，以中文或字母开头，英文不区分大小写。后台映射为内部 Auth 邮箱；不要改变映射及域名，否则已有账号无法正常登录。

## 2. 创建成员

在项目根目录的本地交互终端运行：

```sh
python setup_account.py
```

按提示输入自己的项目 HTTPS 地址、用户名、Secret key，以及两次密码（12～256 位）。密钥和密码隐藏输入，不显示星号属正常现象；不要发送到聊天、Issue 或截图。脚本不保存凭据文件，完成后恢复原环境变量。

脚本创建已确认 Auth 用户并加入 `members` 表。若账号已创建但成员授权失败，根据提示 UUID 设置 `members.active=true`，不要重复开户。仅有 Auth 用户并不代表已授权。停用成员设置 `active=false`。

也可事先通过进程环境配置三个 Supabase 参数，然后运行：

```sh
python manage_user.py reader01
```

不要把中文用户名直接填入控制台 Email。内部标识无法收信，不支持邮件找回；当前无自助注册或改密页面，重置需管理员处理。初始密码通过私密渠道交付，不要共享账号。

## 3. Vercel

1. Fork / 复制仓库到自己的 GitHub，登录 Vercel，Add New → Project，导入该仓库。
2. Root Directory 为仓库根目录，Framework Preset 选 Other。不要设置 `public` 为静态输出目录，不需要前端构建命令。
3. 保留 `vercel.json` 和 `.python-version`。Python 函数处理路由并包含静态文件；显式 builds 配置可能覆盖控制台构建设置。
4. 在服务端 Environment Variables 中配置下表，Production 生效；需要预览时为相应环境单独配置。
5. Deploy。更改环境变量后重新部署。

| 环境变量 | 配置 |
|---|---|
| SUPABASE_URL | 自己的项目 HTTPS 地址 |
| SUPABASE_PUBLISHABLE_KEY | Publishable 或旧 anon key |
| SUPABASE_SECRET_KEY | Secret 或旧 service_role key，仅服务端 |
| REQUIRE_AUTH | 1 |
| COOKIE_SECURE | 1 |

`.env.example` 只是参数清单，程序不自动加载。不要加 `PUBLIC_` / `NEXT_PUBLIC_` 前缀，不要将密钥放入 `public/`、Git 或日志。Vercel 环境强制认证，配置缺失时拒绝服务，不退回匿名模式。

也可使用官方 CLI：本机 `npx vercel login`，再在项目根目录 `npx vercel --prod`，按提示创建自己的项目并配置环境变量。不要在脚本里写 Access Token。设置平台用量和费用通知；免费套餐不保证永久可用。

## 4. 本地认证调试

通过进程环境配置三个 Supabase 参数、`REQUIRE_AUTH=1`，然后 `python reader.py`。本机 HTTP 不设置 `COOKIE_SECURE=1`，线上 HTTPS 必须开启。不要把生产密钥输入会保留明文历史的命令。

## 5. 上线验收

- `/api/health` 返回 `authRequired=true`；未登录访问搜索、读取、同步被拒绝。
- 活跃成员可以登录，非成员或停用成员不能访问。
- 两个独立账号书架互不可见，直接通过 Data API 也受 RLS 隔离。
- 两设备同步，并验证版本冲突；退出、切换账号不混用缓存。
- 从部署环境真实搜索、读正文和下一章，不以 HTTP 200 代替可读验收。云服务器可能被来源拒绝。
- 真机验收手机、文件导入、PWA、缓存和跨设备续读。

可选测试：将 `DEPLOYMENT_URL` 设置为自己的 HTTPS 地址，运行 `python tests/deployment-smoke.py`。临时提供 `SMOKE_USERNAME` / `SMOKE_PASSWORD` 后还会验证登录、同步、冲突及退出，会对现有书架做同内容保存，建议专用测试账号；不要写入脚本。

## 边界和运维

云端只同步元数据和进度，不保存全文或大目录。版本冲突按整份书架选择，不逐书合并。退出不能远程清除其他设备缓存；云端模式离线重启暂不开放书架。

数据库原子配额为全站每分钟 240 次，单用户搜索 6 次、同步 30 次、读取和检查各 24 次。仍需部署平台 WAF / 登录限流和费用通知，没有完整日/月预算控制。

网络失败不等于本机 Python 没运行；线上用户无需电脑开机。检查部署日志并区分超时、非 JSON、502/503/504、验证和 403。不关闭安全限制来兼容来源，不公开无认证本地服务，部署前核对内容权利及平台条款。