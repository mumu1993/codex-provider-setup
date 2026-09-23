# Codex Provider Setup

把兼容 OpenAI 的网关接入 Codex，并保留个人 ChatGPT 登录。支持可选的 CC Switch 供应商切换。

**公共仓库不包含真实服务地址、公司配置或 API Key。** `config.example.json` 里的地址和模型能力仅用于展示格式，请替换为你自己的已授权配置。

## 能做什么

- 用一个命令配置 CLIProxyAPI、Codex 和可选的 CC Switch。
- 同时接入 Responses 与 Chat Completions 上游，向 Codex 提供统一的 Responses 接口。
- 按真实模型 ID、上下文和思考档位生成模型目录。
- 保留个人 `auth.json`、插件、MCP 和其他供应商配置。
- 在写入前备份；启动或验证失败时恢复；支持手动回滚。
- 自动下载 CLIProxyAPI **7.3.15**，核对官方 release 的 SHA-256；也可传入可信的现有二进制。

## 使用前

- macOS、Python **3.11+**、已安装并登录的 Codex。
- 如需 CC Switch：先安装并打开一次，关闭其 **Codex 本地路由**，然后完全退出 CC Switch。已验证版本：**3.24.0**；不兼容的数据结构会被拒绝。
- 你自己的上游 API Key，以及该 Key 确实有权限使用的模型清单。
- 可访问 GitHub release 和上游服务。无需 Go 或 Docker。

当前版本自动安装 macOS LaunchAgent；Linux 可运行单元测试和配置预览，尚不支持一键服务安装。

## 快速开始

```bash
git clone https://github.com/mumu1993/codex-provider-setup.git
cd codex-provider-setup
cp config.example.json ~/gateway.private.json
```

编辑 `~/gateway.private.json`，填写服务地址、准确模型 ID 和真实能力。**不要把 AK 写进 JSON。** 然后预览：

```bash
bash install.sh setup --config ~/gateway.private.json --with-cc-switch --dry-run
```

确认预览后，一键配置：

```bash
bash install.sh setup --config ~/gateway.private.json --with-cc-switch --yes
```

脚本会在终端以隐藏输入方式询问 AK。也支持已有的 `GATEWAY_API_KEY` 环境变量，或 `--key-file /path/to/private.key`；密钥文件权限须为 `600`。不要把密钥作为命令行参数或发到 AI 聊天里。

不使用 CC Switch 时，去掉 `--with-cc-switch`。安装成功后重开 Codex，新建对话。

## 配置格式

| 字段 | 含义 |
| --- | --- |
| `name` / `port` | 网关显示名 / 本机端口；默认 8317，冲突时换一个 |
| `default_model` | 默认模型，必须在 `models` 内 |
| `personal_model` | 可选的个人账号模型；未指定时保留已有个人默认值或使用 Codex 默认值 |
| `models[].id` | 上游的准确模型 ID；保留版本日期，不猜测别名 |
| `protocol` | `responses` 或 `chat` |
| `base_url` | 上游基础地址；工具追加 `/responses` 或 `/chat/completions`。支持 `{model}` 路径占位符 |
| `api_key_header` | 可选附加认证头，例如 `api-key`；值由隐藏输入的 AK 填入 |
| `headers` | 可选的非敏感协议头；遵循 CLIProxyAPI 的头配置语义 |
| `context_window` | 可选；只填写已确认的上下文容量 |
| `reasoning_efforts` | 真实支持的档位，例如 `["low","medium","high"]`；`[]` 使用上游默认策略 |

不要给不支持的模型补上 `max` 或 `ultra` 菜单。模型返回成功不代表它支持全部工具或与其营销名称对应的实际部署。

## 日常切换

使用 CC Switch 时，选择 Codex 下的 **Personal ChatGPT** 或 **Team Gateway · CLIProxyAPI**，按提示重新打开 Codex。无需再次执行安装脚本。

不使用 CC Switch 时：

```bash
bash install.sh switch personal
bash install.sh switch gateway
```

切换后重开 Codex 并新建对话。工具不会强行结束正在运行的 Codex 任务。启用了 CC Switch 集成后，命令行切换会被拒绝，避免两个配置来源互相覆盖。

## 验证与回滚

```bash
# 核对模型目录，并向默认模型发送一个短请求
bash install.sh verify

# 逐个验证全部模型：会产生少量上游用量
bash install.sh verify --all

# 退出 CC Switch 后，恢复最近一次 setup 前的配置
bash install.sh rollback --yes
```

安装被中断且尚未生成完整状态时，可使用安装开始时打印的备份路径：

```bash
bash install.sh rollback --backup ~/.local/share/codex-provider-setup/backups/<snapshot> --yes
```

`--verify-all` 也可用于 `setup`，在写入 Codex 配置前验证全部模型；默认只验证默认模型。

运行文件和备份位于 `~/.local/share/codex-provider-setup/`，目录权限为 `700`，凭据文件为 `600`。AK 仅写入该目录的代理配置；Codex 只接触独立生成的本机代理口令。回滚可能覆盖安装后对相关配置的修改，请选择正确备份点。

## 边界

- 当前工具管理一个本机网关实例，不接管正在占用端口的其他服务。
- CC Switch 只新增或更新 `provider-kit-personal` 和 `provider-kit-gateway` 两个条目，不复制旧供应商的请求改写规则。
- 已有的 CC Switch Codex 代理接管、未知数据库结构会阻止安装；即使未要求 CC Switch 集成，也会检查已有接管状态。
- 管理中的配置文件若为符号链接，脚本会停止，避免破坏 dotfiles 管理方式。
- SQLite 备份和恢复使用数据库 backup API，包含已提交的 WAL 内容。
- 校验和用于校验下载完整性；官方 release 与校验和属于同一个发布信任源。
- Fast 图标不代表上游实际提供加速，实际服务档位以响应和服务方说明为准。

## 给 AI 配置

把 [AI-SETUP.md](AI-SETUP.md) 的提示词及你自己的非敏感配置资料交给 AI。密钥通过终端隐藏输入或本机私密文件提供。

## 开发

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

工具本身只使用 Python 标准库。未修改或捆绑上游源码；[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 和 [CC Switch](https://github.com/farion1231/cc-switch) 遵循各自许可证。

MIT License.
