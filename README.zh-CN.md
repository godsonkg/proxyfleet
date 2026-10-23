# proxyfleet

[![test](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml)
[![license](https://img.shields.io/github/license/godsonkg/proxyfleet)](LICENSE)

[English](README.md) | **简体中文**

用一份清单文件管理多台自建代理服务器，并规划配套的 Surge、Loon、Mihomo（Clash Meta）订阅。

> **状态：0.1.0，早期版本。** `validate`、`plan`、`collect`、`build`、`diff` 均可使用；发布到 HTTPS 主机将在 0.2 提供，见 [设计文档](docs/design.md) 和 [更新记录](CHANGELOG.md)。

## 解决什么问题

VPS 多了以后，难的不再是装节点，而是让每个客户端配置保持同步：哪些机器在用、每个客户端支持哪些协议、哪些节点可以用于 AI 或流媒体，以及确保已退役的服务器不会又混回来。

proxyfleet 把这些都放在**一份文件** `fleet.yaml` 里，其余一切由它推导：

- **生命周期状态**：新机器先是 `observing`（只进隔离 canary），通过后转为 `active`，之后可以 `disabled` 或 `retired`。
- **按客户端的协议矩阵**：Surge、Loon、Mihomo 支持的协议不同，每台机器分别声明各客户端能用哪些。
- **显式加入的用途角色**：节点只有在所属机器明确允许时才进入 `ai`、`streaming` 等角色，不靠节点名或地区猜测。角色还可以限定协议和国家。
- **节点契约**：每个客户端的节点名称、顺序和数量，与清单文件的 SHA-256 绑定。发布时可以据此拒绝用过期或被改过的清单生成的批次。
- **退役保护**：已退役机器和历史节点的名称永远不能被复用。

proxyfleet 运行在你自己的电脑上（Windows、macOS、Linux），不需要装到服务器上。它和管理单台服务器节点的 [frm-node](https://github.com/godsonkg/frm-node) 搭配使用，但不依赖它。

## 安装

需要 Python 3.9+。

```bash
pipx install git+https://github.com/godsonkg/proxyfleet@v0.1.0
```

开发安装：

```bash
git clone https://github.com/godsonkg/proxyfleet
cd proxyfleet
python -m pip install -e ".[test]"
```

## 使用

从示例开始，**真实清单不要放进任何公开仓库**：

```bash
cp examples/fleet.example.yaml ~/private/fleet.yaml
proxyfleet validate ~/private/fleet.yaml
proxyfleet plan ~/private/fleet.yaml
proxyfleet plan ~/private/fleet.yaml --include-observing   # canary
proxyfleet plan ~/private/fleet.yaml --json
```

`validate` 一次列出所有问题：未知字段、重复的机器名或地址、无效国家代码、同时 allow 和 deny 的角色、观察期机器试图加入受限角色、节点名冲突或复用了退役名称等。

`plan` 按客户端列出有序的节点名称、组成的分组和契约数量。所有选项及注释见 [`examples/fleet.example.yaml`](examples/fleet.example.yaml)。

### 管理机器

不用手工编辑 `fleet.yaml` 就能修改机器，注释和格式都会保留，只改动相关的行：

```bash
proxyfleet machine list    ~/private/fleet.yaml
proxyfleet machine add     ~/private/fleet.yaml SG-NEW --host 192.0.2.50 --country SG --discover --ssh-user root --allow general
proxyfleet machine promote ~/private/fleet.yaml SG-NEW --allow telegram
proxyfleet machine disable ~/private/fleet.yaml JP-TYO     # 用 enable 恢复
proxyfleet machine retire  ~/private/fleet.yaml HK-OLD     # 永久生效，会先确认
proxyfleet machine set-host ~/private/fleet.yaml JP-TYO 198.51.100.21
```

- `add` 新增的机器一律是 **observing** 状态。`--discover` 通过 SSH 调用服务器上的 `frm-node sub fragment`，自动识别各客户端支持的协议；不用它时，用 `--protocol surge=AnyTLS,Snell-v6` 手动列出。
- 每个命令都会显示**正式发布**的节点列表和分组会怎么变；加 `--dry-run` 只显示、不写入。
- 每次修改都会先按完整清单校验，再原子写入并保留文件权限。会让清单失效的修改（比如地址重复）不会写入。
- 如果你的文件格式无法原样保留，proxyfleet 会拒绝修改，而不是重新排版整个文件。

### 采集节点片段

`collect` 采集每台启用机器的节点片段，并检查每个片段是否恰好包含清单期望的节点（命名为 `<机器名>-<协议>`）：

```bash
# 通过 SSH 在每台服务器上运行 `frm-node sub fragment`（服务器上需要 root）
proxyfleet collect ~/private/fleet.yaml --out ~/private/collected --ssh-user root

# 或使用已有的片段：<目录>/<机器名>/surge.txt、loon.txt、mihomo.yaml
proxyfleet collect ~/private/fleet.yaml --out ~/private/collected --from ~/private/fragments
```

- 所有片段都通过才会写入；**任何一个失败都不写入**，上一次的输出保持原样，所有失败一次列出。
- 输出目录和文件只有你自己能读（Unix 上是 `0700`/`0600`，Windows 上是仅当前用户的 ACL）。
- 已有的 `--out` 目录只有在为空或是上一次 proxyfleet 的输出时才会被替换，指错目录也不会删掉你的文件。
- SSH 使用批处理模式，连接超时 10 秒，每个片段最多尝试 3 次；额外的 ssh 参数（如端口）用 `--ssh-option` 传入。
- 错误只指出机器、客户端和出错的节点名，不回显片段内容。

### 生成订阅文件

`build` 把采集结果生成为各客户端的文件：

```bash
proxyfleet build ~/private/fleet.yaml --fragments ~/private/collected --out ~/private/built
```

```text
built/
  providers/surge.txt     Surge policy-path 用的节点列表
  providers/loon.txt      Loon 远程节点订阅用的节点列表（尚未实机导入验证）
  providers/mihomo.yaml   Mihomo proxy-provider 用的 proxies: 列表
  snippets/surge.conf     [Proxy] + [Proxy Group]，可粘贴进手写的 Surge 配置
  snippets/loon.conf      Loon 版本
  snippets/mihomo.yaml    Mihomo 的 proxies: + proxy-groups:
  manifest.json           契约（节点名、顺序、数量）、清单 SHA-256、各文件哈希
```

- 节点按命名模板改名，并按清单中的顺序排列，与服务器返回的顺序无关。
- 采集结果必须来自**同一份清单文件**（按 SHA-256 比对），且采集后片段没有被改动过，否则需要重新 `collect`。
- 含 PEM 私钥或 GitHub token 的片段会被拒绝。
- 只修改每个节点的名称，其余每一行原样保留；Mihomo 的每次改名都会在改名前后各解析一次进行核对。
- 替换 `--out` 之前会重新读取结果并与 `plan` 比对。和 `collect` 一样，失败时上一版输出保持不变。

### 比较两次生成

生成到新目录，用之前先和上一版比较：

```bash
proxyfleet diff ~/private/built ~/private/built-new
```

```text
inventory: changed
[surge] 4 -> 5 nodes
  + 🇺🇸 US-NEW-AnyTLS
  ~ 🇯🇵 JP-TYO-AnyTLS: details changed (e.g. password, port or SNI; not shown)
  group 🛰️ Fleet-Telegram
    + 🇺🇸 US-NEW-AnyTLS
```

- 显示新增和删除的节点、顺序变化、分组成员变化，以及详情有变化的节点。**详情本身永远不显示**，只显示节点名。
- 旧目录不存在时视为第一次生成。
- `build` 之后被手工改过的输出会被拒绝（按 manifest 中的文件哈希核对）。
- 退出码与 `diff` 命令一致：`0` 无变化，`1` 有变化，`2` 出错。`--json` 输出机器可读格式。

### 发布到自己的 HTTPS 主机（尚未发布，将在 0.2 提供）

`publish` 把生成结果上传到你自己控制的一台静态服务器，客户端通过订阅地址自动更新。

**在一台 Debian/Ubuntu 服务器上做一次**（可以是你的某台 VPS）：

```bash
git clone https://github.com/godsonkg/proxyfleet && cd proxyfleet
sudo PF_DOMAIN=sub.example.com PF_UPLOAD_PUBKEY="$(cat 你的上传公钥.pub)" bash server/setup.sh
```

它会创建一个**只能 SFTP、被 chroot 限制**的上传账号（`proxyfleet`）、一个负责发布上传批次的 systemd 定时器，以及一个 Caddy 站点：只在随机的 64 位路径下提供 `surge.txt`、`loon.txt`、`mihomo.yaml`，其他请求一律 404。已有的 Caddy 站点会保留：proxyfleet 写入自己的配置文件并 import，校验失败会还原你的 Caddyfile。

**在你自己的电脑上**，把 [`examples/publish.example.yaml`](examples/publish.example.yaml) 复制到 `~/.config/proxyfleet/publish.yaml`（Windows 为 `%APPDATA%\proxyfleet\publish.yaml`），填好后 `chmod 600`。然后：

```bash
proxyfleet publish ~/private/built --dry-run   # 只检查
proxyfleet publish ~/private/built
```

- 拒绝发布 canary、含空客户端的生成结果，以及 `build` 之后被改过的结果。
- 批次的 `.ready` 标记最后上传。服务器核对 SHA-256 清单、契约（客户端、数量、清单哈希）和每个文件的结构，再用一次原子重命名切换线上文件。被拒绝的批次不会影响客户端看到的内容；保留最近 5 个版本。
- `publish` 随后通过 HTTPS 下载每个文件，直到 SHA-256 一致；90 秒内未一致即报错（`--verify-timeout`）。
- 秘密路径不会出现在任何输出中。

### 轮换秘密路径

订阅地址泄露时（发进了聊天、截图或日志），换一个新的：

```bash
proxyfleet rotate-path            # 会要求确认；加 --yes 跳过
proxyfleet rotate-path --admin me # 用有免密 sudo 的用户代替 root
```

1. 服务器同时在旧路径和新路径下提供当前文件，proxyfleet 核对两边完全一致。
2. 只替换 `publish.yaml` 里 `base_url` 的 token，保留注释和权限。
3. 服务器停用旧路径；proxyfleet 确认新路径可用、**旧路径返回 404**。

第 3 步之前任何一步失败，服务器都会回滚、`publish.yaml` 也会还原，旧地址继续可用。中途被打断时用 `proxyfleet rotate-path --rollback` 撤销。轮换期间如果正好有新的发布，两个路径都会提供新内容，不会出现过期文件。token 只通过 SSH 的标准输入传递，不出现在命令行，也不会被打印。完成后把 `publish.yaml` 里的新地址更新到每个客户端。

## 路线图

| 版本 | 范围 |
|---|---|
| 0.1 | `validate`、`plan`、`collect`、`build`、`diff`（已完成） |
| 0.2 | `publish` 经 SFTP 发布到静态 HTTPS 主机，服务端校验后原子切换、秘密路径轮换、生命周期命令（`machine add`、`promote`、`disable`、`enable`、`retire`、`set-host`）（已完成，尚未发布） |
| 0.3 | 退役引用发布门禁；把用途分组追加进已有客户端配置，不改变原有默认出口 |

## 安全

- 清单只放地址和角色。**不要写入密码、UUID、密钥或订阅地址。**
- 生成的订阅文件会包含完整节点凭据，不要放进 Git、网盘或聊天。
- 清单校验的错误会指出出错的字段和值，清单本身不含秘密。`collect`、`build`、`diff` 的输出只提到机器、客户端和节点名，不回显片段或文件内容。

## 许可证

[MIT](LICENSE)
