# proxyfleet

[![test](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml)
[![license](https://img.shields.io/github/license/godsonkg/proxyfleet)](LICENSE)

[English](README.md) | **简体中文**

用一份清单文件管理多台自建代理服务器，并规划配套的 Surge、Loon、Mihomo（Clash Meta）订阅。

> **状态：早期开发（0.1.0.dev0）。** 目前可用 `validate`、`plan`、`collect` 和 `build`；版本比较和发布是下一步，见 [设计文档](docs/design.md)。

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
pipx install git+https://github.com/godsonkg/proxyfleet
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

## 路线图

| 版本 | 范围 |
|---|---|
| 0.1 | `validate`、`plan`、`collect`、`build`（已完成）；`diff` 与上一版比较（只显示名称和数量） |
| 0.2 | `publish` 经 SFTP 发布到静态 HTTPS 主机，服务端校验后原子切换；秘密路径轮换；生命周期命令（`add`、`promote`、`retire`） |
| 0.3 | 退役引用发布门禁；把用途分组追加进已有客户端配置，不改变原有默认出口 |

## 安全

- 清单只放地址和角色。**不要写入密码、UUID、密钥或订阅地址。**
- 生成的订阅文件会包含完整节点凭据，不要放进 Git、网盘或聊天。
- 清单校验的错误会指出出错的字段和值，清单本身不含秘密。后续处理订阅文件的阶段只会报告固定错误码，不回显文件内容。

## 许可证

[MIT](LICENSE)
