# mihomo-select

一个基于 Python 标准库的 Mihomo 终端管理工具，适合通过 SSH 管理没有桌面环境的 Ubuntu/Debian 主机。

它通过 Mihomo REST API 提供：

- 策略组树形浏览、展开和节点切换
- 单节点、策略组和全部节点延迟测试
- 订阅下载、切换、更新和删除
- 当前 shell 的 HTTP/SOCKS 代理环境变量管理
- 可审阅的 Ubuntu/Debian 初始化脚本生成

## 快速开始

目标主机是全新 Ubuntu/Debian、还没有 Mihomo 时，下载后按这个顺序执行：

```bash
git clone https://github.com/niexin-dev/mihomo-select.git
cd mihomo-select
python3 mihomo_select.py --init \
  --subscription-url 'https://example.com/subscription?token=<你的令牌>'
sudo bash ~/.config/mihomo/bootstrap.sh
~/.local/bin/mihomo_select.py --secret-file ~/.config/mihomo/api-secret
```

执行 `sudo bash` 之前请先审阅 `~/.config/mihomo/bootstrap.sh`。初始化脚本会自动查询 GitHub 最新稳定版、识别
CPU 架构并验证 SHA-256；订阅 URL 通常包含令牌，不能公开分享。

## 运行要求

- Python 3.9 或更新版本
- 已有 Mihomo 时需运行并启用 REST API；全新主机可先使用 `--init`
- 终端支持 `curses`
- `--init` 生成的安装脚本面向 Ubuntu/Debian，使用 `apt`、`systemd` 和 `curl`

如果主机没有 Git，也可以在 GitHub 页面下载 ZIP，解压后进入目录。这个项目只有一个 Python 脚本，不能替代
Mihomo 本身；请先按照 Mihomo 的官方文档安装并启动 Mihomo。

## 初始化细节

如果目标主机还没有安装 Mihomo，下载项目后按下面的顺序操作：

`--init` 是用户级操作，不会自动使用 root 权限。它会复制脚本并生成
`~/.config/mihomo/bootstrap.sh`。先审阅这个文件，确认下载地址、订阅 URL、用户和目录都正确，再执行：

```bash
sudo bash ~/.config/mihomo/bootstrap.sh
```

初始化脚本完成后，Mihomo 会由 systemd 启动；如果配置原本没有 API 密钥，初始化脚本会生成
`~/.config/mihomo/api-secret`，随后运行：

```bash
~/.local/bin/mihomo_select.py --secret-file ~/.config/mihomo/api-secret
```

如果配置原本已有 `secret`，初始化脚本不会覆盖它；此时省略 `--secret-file` 让程序交互式输入，或手动把
已有密钥写入该文件。

如果初始化时没有提供 `--subscription-url`，进入程序后按 `s` 添加订阅。若初始化时配置了订阅 URL，启动后
可以直接进入策略树，按 `Enter` 切换节点。

自动下载仅支持常见的 `amd64`、`arm64` 和 `armv7` Linux 主机；其他架构或无法访问 GitHub 时，使用
`--mihomo-url` 和 `--mihomo-sha256` 手动指定安装包。

初始化脚本优先读取 GitHub 发布资产的 SHA-256 校验值；没有有效校验值时，尝试同名 `.sha256` 或
`.sha256sum` 文件。无法获得有效 SHA-256 时停止安装。

更新本工具后，请重新运行原来的 `--init` 命令（保留原有的 `--subscription-url` 等参数），刷新
`~/.config/mihomo/bootstrap.sh`，再审阅并执行该脚本。

## 快捷流程：已有 Mihomo

如果目标主机已经安装并运行 Mihomo，可以跳过 `--init`，直接确认 REST API：

在 Mihomo 配置中确认 API 监听地址和密钥。例如：

```yaml
external-controller: 127.0.0.1:9090
secret: "请替换为随机密钥"
```

修改配置后重启 Mihomo，并确认端口正在监听：

```bash
ss -lnt | grep ':9090'
```

不要把 REST API 监听到公网；远程使用时先通过 SSH 登录目标主机，或使用受控 VPN。

启动策略树：

在项目目录执行：

```bash
python3 mihomo_select.py --api http://127.0.0.1:9090
```

如果 API 返回 `401`，程序会在终端中安全地提示输入密钥。也可以把密钥放入仅当前用户可读的文件：

```bash
install -d -m 700 ~/.config/mihomo
install -m 600 /dev/null ~/.config/mihomo/api-secret
printf '%s\n' '请替换为你的密钥' > ~/.config/mihomo/api-secret
python3 mihomo_select.py --secret-file ~/.config/mihomo/api-secret
```

添加和切换订阅：

进入程序后按 `s` 打开订阅管理：

1. 按 `a`，粘贴 HTTPS 订阅链接。
2. 输入订阅名称，或直接使用默认名称。
3. 选中订阅后按 `Enter` 下载、加载并切换。
4. 以后按 `u` 更新，按 `d` 删除。

订阅 URL 通常包含访问令牌，等同于密码。不要把它发到公开 Issue、聊天记录或 Git 仓库。

选择节点和测速：

回到策略树后，展开实际生效的策略组，选中节点按 `Enter` 切换。按 `r` 测试当前组或节点，按 `R` 测试全部
节点；测速结果只用于当前运行，不会修改订阅文件。

## 代理环境变量

程序中的 `e` 只会写入 `~/.config/mihomo/env.sh`，不会改变已经打开的 shell。使用命令行时可以这样操作：

```bash
python3 mihomo_select.py --env on
source ~/.config/mihomo/env.sh
python3 mihomo_select.py --env status
python3 mihomo_select.py --env off
```

也可以让程序直接输出当前 shell 可执行的命令：

```bash
eval "$(python3 mihomo_select.py --env on --shell)"
```

`eval` 只应执行本机脚本的输出，不要把不可信程序的输出交给 `eval`。

## 常用参数

```text
--api URL                 Mihomo API 基础地址，默认 http://127.0.0.1:9090
--secret-file FILE        从文件读取 API 密钥
--profiles-dir DIR        覆盖订阅配置目录
--config-link FILE        覆盖 active.yaml 软链接路径
--no-link                 切换订阅时不更新配置软链接
--proxy-url URL           订阅下载失败后的 HTTP 代理
--test-url URL            延迟测试目标
--request-timeout SEC     API 和订阅请求超时
```

订阅链接必须使用 HTTPS。订阅索引和配置文件默认保存在 `/etc/mihomo/profiles`；可用 `--profiles-dir` 和
`--config-link` 覆盖。请把订阅 URL 当作凭据处理，不要提交配置文件或命令输出。

## 按键

`j/k` 或方向键移动，`h/l` 折叠或展开，`Enter` 切换节点，`r/R` 测速，`s` 管理订阅，`e` 开关代理环境，`q` 退出。

## 常见问题

- **提示连接被拒绝**：检查 Mihomo 是否运行、`--api` 端口是否正确，以及 API 是否只监听在可访问的地址。
- **提示 401**：确认 `secret` 与 `--secret-file` 内容一致；密钥文件不要带多余空格。
- **订阅下载失败**：确认链接仍有效、使用 HTTPS，并检查目标主机能否访问订阅站点；必要时使用 `--proxy-url`。
- **切换后 Mihomo 没有生效**：确认当前策略模式和实际使用的策略组；检查 Mihomo 日志与 `active.yaml` 指向的文件。
- **终端显示异常**：使用支持 UTF-8 和 `curses` 的 SSH 终端，并适当放大窗口。

## 安全边界

工具只接受无凭据、无查询参数的 API 基础 URL；API 密钥从交互输入或权限受限文件读取。它不会上传订阅索引
到第三方服务，但订阅内容会按 Mihomo 配置写入本机文件。请在防火墙或本机监听范围内保护 REST API。

## 验证

```bash
python3 -m py_compile mihomo_select.py
python3 -m unittest discover -s tests -v
```

## 许可证

MIT License，见 [LICENSE](./LICENSE)。
