# DeterminFlow Plugins

[`DeterminFlow`](https://github.com/alikon-art/DeterminFlow) 的官方 Plugin 仓库。
Plugin 可以扩展 Workflow、Agent、Prompt、Script Library（脚本库）和模型服务接入，
并由 Core 锁定到具体版本运行。

## 官方案例

| Plugin | 能力 |
|---|---|
| [`bishu-novel`](plugins/bishu-novel) | 无数据库、无 UUID 的本地小说生产流程 |
| [`public-api`](plugins/public-api) | 笔枢公益模型登录、完整模型目录、额度状态、独立公告、余额档位和 Provider（供应商）接入 |

## 安装

DeterminFlow 的内置官方来源优先从签名的 R2 Registry（注册表）读取 Catalog 和安装包，
并校验 Registry 签名、Plugin 内容摘要与精确 Git Commit。R2 不可用或校验失败时会自动
回退到 GitHub/Gitee Git 源；自定义第三方来源仍使用 Git。

手动添加本仓库时，在 DeterminFlow 的 Plugin 页面填写：

- Git URL：`https://github.com/alikon-art/DeterminFlow-Plugins.git`
- Ref：精确 Commit 或 Release Tag
- Subdirectory：按需填写 `plugins/bishu-novel` 或 `plugins/public-api`

Core 会检查 Manifest 和资源，锁定精确 Commit，并在重启后启用 Plugin。

Plugin 当前与 Core 同机运行，拥有主进程可用的系统权限。请只安装可信来源，并为重要
工作区建立独立备份。

## License

本仓库使用 [GNU AGPL v3](LICENSE)（`AGPL-3.0-only`）许可证。
