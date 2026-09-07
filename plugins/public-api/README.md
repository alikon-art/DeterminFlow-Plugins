# 笔枢公益模型 Plugin

这是一个可选 Plugin，为 Windows 和 macOS 桌面版申请、保存和续签普通
`base_url + api_key` Provider。未安装或未启用时，DeterminFlow Core 不加载任何公益模型逻辑。

## 边界

- Core 统一负责安装标识与账号登录状态；Plugin 只读取 Core 会话，负责凭据申请、提前续签、
  失败降级和轻量管理页面。
- Provider 写入只调用 DeterminFlow 已有的 `/api/model-providers` HTTP 契约，不 import Core
- 模型与价格目录由公益凭据访问中转 `/v1/public-models` 动态获取；页面向所有用户展示完整
  公益目录，写入 Provider 的可调用模型仍按 New API 分组与当前凭据白名单过滤
  内部模型管理实现。
- Portal、风控与香港中转负责身份、风险、额度和实际 Key 生命周期。
- 公益模型公告由 Portal 的独立公益模型公告域管理，Plugin 动态读取；不复用笔枢门户
  公告、站内信，也不替代长期服务风险说明。
- 匿名额度继续受单 Key、每日和每周限制；登录用户先使用每日 ¥3、每周重置
  ¥10 的公益额度，耗尽后才使用不随周期重置的充值余额。
- 登录用户按当前充值余额动态切组：余额大于 0 使用充值模型组，余额用尽立即回到
  免费模型组；历史充值记录不会永久保留充值模型权限。
- 登录由 Core 通过系统默认浏览器统一完成，Plugin 不接收账号密码、access token 或 refresh
  token，也不再保存独立账号 Session。Plugin 状态文件不重复保存模型 Key。
- 尚未申请公益凭据时，顶部浮窗显示“公益 · 未启用”，由用户明确点击“启用公益模型”后申请；
  申请失败时保留浮窗并提供重试。“模型列表”在右侧半屏打开 Plugin 页面，不占用插件详情抽屉。
- 公益充值由笔枢门户承接，Plugin 只打开门户 URL。浮窗与模型列表页的充值入口由
  Portal 两个独立开关控制，支付网关地址和鉴权信息不会进入 Plugin。
- 支持 Windows 和 macOS 桌面客户端；凭据请求如实标记平台。需要 Portal 接受 macos 平台的接口版本。

## 安装

从 DeterminFlow 的官方 Plugin Catalog 安装 `public-api`，启用后重启 Core。顶部公益状态入口提供
显式启用、额度查看和失败重试；登录和退出统一使用 Core 顶部账号入口。

真实服务仍受 Portal 与中转开关控制。安装 Plugin 不代表公益额度已经开放。

## 本地开发

非桌面运行环境只允许通过显式开发开关调试：

```bash
DETERMINFLOW_PUBLIC_API_DEVELOPMENT=1 .venv/bin/uvicorn src.web_server:app \
  --host 127.0.0.1 --port 8020
```

该开关只绕过客户端平台门禁，并把凭据请求标记为 `development`。开发模式允许 Portal 和
Provider 使用 `localhost` 或 loopback IP 的 HTTP 地址；其他 HTTP 地址仍会拒绝。Portal、
风控与中转仍须独立启用，正式部署不得设置该变量。

## 客户端兼容

支持统一账户服务的 Core 使用顶部统一登录。旧版 Core（包括桌面 v1.0.10）继续使用插件原有的浏览器登录和本地凭据，避免插件独立更新导致登录失效。升级 Core 后切换到统一账户，必要时重新登录。
