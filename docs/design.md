# proxyfleet 设计

本文说明 proxyfleet 的目标、核心概念、命令规划和安全原则。实现以本文为准；改变设计时先改本文。

## 1. 目标与非目标

**目标**

- 用一份清单（`fleet.yaml`）描述整个机队：机器、状态、各客户端协议、用途角色。
- 从清单推导出每个客户端的节点名称、顺序、数量和用途分组，并能证明某批订阅确实来自这份清单。
- 在本地电脑上跑，Windows、macOS、Linux 都支持，不要求在服务器上安装任何东西。
- 默认保守：拿不准就拒绝，失败时不覆盖上一版正式产物。

**非目标**

- 不在服务器上安装或配置代理协议。这是 [frm-node](https://github.com/godsonkg/frm-node) 或其他节点管理器的事。
- 不托管完整客户端配置的规则和 DNS。proxyfleet 只负责节点和与机队相关的分组。
- 不做 Web 面板、数据库或常驻服务。

## 2. 核心概念

### 2.1 清单

清单是唯一的事实来源，格式为 YAML（JSON 也可，因为 JSON 是 YAML 的子集）。完整示例见 [`examples/fleet.example.yaml`](../examples/fleet.example.yaml)。

校验规则：

- 拒绝未知字段和重复键，避免拼写错误被静默忽略。
- 机器名唯一（不区分大小写），格式 `^[A-Za-z0-9][A-Za-z0-9-]{0,31}$`。
- `host` 是 IP 或主机名，唯一；IP 会规范化后再比较（`2001:DB8:0::1` 等于 `2001:db8::1`）。
- `country` 为 ISO 3166-1 两位代码，用于生成国旗和角色的国家限制。
- 每台机器必须为 `clients` 中的每个客户端声明协议列表（不支持就写 `[]`）；启用的机器至少要有一个协议。
- 一次报告全部错误，而不是遇到第一个就停。

### 2.2 生命周期状态

| 状态 | 正式批次 | canary 批次 | 用途分组 | 说明 |
|---|---|---|---|---|
| `observing` | ✗ | ✓ | ✗ | 新机器默认状态；不能 allow `lifecycle.observing.deny_roles` 中的角色 |
| `active` | ✓ | ✓ | ✓ | 正式使用 |
| `disabled` | ✗ | ✗ | ✗ | 暂停，可恢复 |
| `retired` | ✗ | ✗ | ✗ | 永久退役；其机器名和节点名进入退役名单 |

canary 批次（`--include-observing`）用于在不影响正式订阅的前提下测试新机器。

### 2.3 节点命名

节点名由模板生成，占位符为 `{flag}`、`{name}`、`{protocol}`、`{country}`，必须包含 `{name}` 和 `{protocol}`。可按客户端覆盖，例如 Loon 用工具图标代替国旗，避免自建节点被归进机场地区池。

生成后的名称会校验：

- 同一客户端内不重复（不同机器名和协议组合可能拼出相同名称）。
- 不含 `=`、`,`、`"`、`\` 或换行，这些字符会破坏 Surge/Loon 的节点行或策略组成员列表。
- 不与退役名单冲突。

### 2.4 用途角色

角色是用途标签，例如 `general`、`ai`、`streaming`。规则：

- **只能显式加入**：机器的 `roles.allow` 包含该角色、且 `roles.deny` 不包含时才加入。不根据节点名或地区猜测。
- 同一角色不能同时出现在 allow 和 deny 中。
- 角色可以限定 `protocols`（例如 AI 只用 AnyTLS、Reality、Snell-v6）和 `countries`（例如 AI 只用美国机器）。
- 只有 `active` 机器进入用途分组；observing 机器只出现在 canary 批次的机队总组中。
- 成员为空的分组不输出。

分组名由 `groups.fleet`（全部节点）和 `groups.role`（含 `{role}`，用角色的 `label` 替换）决定，所有分组名必须唯一。

### 2.5 节点契约

契约描述一批订阅必须满足的条件：

```json
{
  "version": 1,
  "inventory_sha256": "<清单文件的 SHA-256>",
  "include_observing": false,
  "counts": {"surge": 4, "loon": 4, "mihomo": 4}
}
```

`build` 生成订阅时会把契约一起写出；`publish` 和服务端都会核对节点名称、顺序、数量，以及清单哈希是否与当前清单一致。契约不是凭据，可以公开。

### 2.6 退役名单

退役名单 = `retired` 机器的机器名和节点名 + 清单中的 `retired_names`（已从清单删除的历史节点名）。后续的发布门禁会检查生成物中节点、分组、provider 和前置代理引用，任何命中都拒绝发布。

## 3. 命令规划

| 命令 | 版本 | 作用 | 是否接触凭据 |
|---|---|---|---|
| `validate <清单>` | 0.1 ✅ | 校验清单 | 否 |
| `plan <清单> [--include-observing] [--json]` | 0.1 ✅ | 显示各客户端的节点名、分组和契约 | 否 |
| `collect <清单> --out <目录> [--from <目录>]` | 0.1 ✅ | 采集各机器节点片段；来源可插拔：SSH 调用 `frm-node sub fragment`，或读取本地目录；见 3.1 | 是 |
| `build <清单> --fragments <目录> --out <目录>` | 0.1 ✅ | 生成三端 provider 文件、分组片段和契约；名称、顺序、数量不符即失败；见 3.2 | 是 |
| `diff <旧目录> <新目录> [--json]` | 0.1 ✅ | 比较节点名、顺序、数量、分组成员和节点详情；只显示节点名；见 3.3 | 本地读取，不显示 |
| `publish <生成目录> [--config] [--dry-run]` | 0.2 ✅ | SFTP 上传批次，服务端校验 SHA-256、大小、数量和结构后原子切换，再经 HTTPS 回读核对；见 3.4 | 是 |
| `rotate-path [--admin] [--yes] [--rollback]` | 0.2 ✅ | 新旧秘密路径短暂并存，验证新路径后关闭旧路径，全程不打印路径；见 3.5 | 否（只读 HTTPS 内容做比较） |
| `add` / `promote` / `disable` / `retire` | 0.2 | 修改清单中的机器状态；`add` 可经 SSH 自动发现协议 | 否 |
| `gate` | 0.3 | 退役引用与秘密泄露门禁，用于任意客户端配置 | 是 |
| `bind` | 0.3 | 把用途分组追加到已有客户端配置的业务组末尾，不改变默认出口，可重复运行 | 是 |

### 3.1 collect 的约定

- 只采集启用机器（加 `--include-observing` 时含观察期机器）中协议列表非空的客户端。
- 片段中的节点名必须恰好是 `<机器名>-<协议>`，即 `frm-node sub fragment --prefix <机器名>` 的输出；顺序不限，多一个或少一个都失败。改名为清单的命名模板由 `build` 负责。
- 片段原样保存为 `<输出>/<机器名>/surge.txt`、`loon.txt`、`mihomo.yaml`，另写 `manifest.json`，记录清单哈希、采集时间、来源、各片段的 SHA-256 和节点名。
- 先写到同级隐藏临时目录，全部通过后才替换输出目录；失败时删除临时目录，上一版保持不变。
- 不符合 `<机器名>-<协议>` 格式的"节点名"不会出现在错误信息里，因为格式错误的行中，等号前的部分可能其实是凭据。
- 片段上限 1 MiB；SSH 远程命令只由已校验的机器名和客户端名拼成，`--ssh-user` 和 `--remote-command` 也有格式校验。

### 3.2 build 的约定

- 输入必须是 `collect` 的输出：`manifest.json` 的 `kind` 为 `collect`，清单哈希与当前清单一致，`--include-observing` 与采集时一致，每个片段的 SHA-256 与采集记录一致。任何一项不符都要求重新采集。
- 片段中出现 PEM 私钥或 GitHub token 即拒绝。节点本身的密码、UUID、PSK 属于正常内容。
- 输出：`providers/` 是纯节点列表（Surge policy-path、Loon 远程节点、Mihomo proxy-provider）；`snippets/` 带 `[Proxy]`/`[Proxy Group]` 或 `proxies:`/`proxy-groups:`，用于粘贴进手写配置；分组类型目前固定为 `select`。
- 节点顺序以清单为准（机器顺序，再按协议顺序）。
- 只改名，不重新序列化：Surge/Loon 只替换等号前的名称；Mihomo 只替换每项自己的 `name:` 行，其余行逐字节保留，因为 PyYAML（YAML 1.1）与 Mihomo（YAML 1.2）对 `0123` 这类未加引号的值解析不同。每次改名后解析改名前后的条目，除名称外必须完全相同。只支持块状（block）布局，流式（flow）布局直接拒绝。
- 写出前把即将写入的内容重新解析，与 `plan` 的节点和分组逐项比对。
- `manifest.json` 记录契约（数量和有序节点名）、清单哈希、采集记录的哈希和每个输出文件的 SHA-256，供后续 `diff` 和 `publish` 使用。

### 3.3 diff 的约定

- 两边都必须是 `build` 的输出，且 provider 文件的 SHA-256 与各自 manifest 一致，被手工改过即拒绝。旧目录不存在时视为第一次生成。
- 比较内容：各客户端的节点增删和顺序、分组的新增、删除和成员变化，以及**同名节点的详情变化**（逐个节点比较除名称外的内容）。
- 详情只在本地比较，输出（含 `--json`）只包含节点名和分组名，不包含任何节点详情。
- 清单哈希不同时提示 `inventory: changed`；一边是 canary、一边不是时给出警告。
- 退出码沿用 `diff(1)`：0 无变化，1 有变化，2 出错，方便脚本判断。

### 3.4 publish 的约定

**客户端**
- 只发布 `build` 的输出，且文件哈希与 manifest 一致；拒绝 canary 和节点数为 0 的客户端。
- 配置 `publish.yaml` 含秘密路径：未知字段拒绝；`base_url` 必须是 `https://<主机>/<64 位小写十六进制>`；Unix 上文件权限必须为仅本人可读；任何错误信息都不回显 `base_url`。
- 批次名 `YYYYMMDDTHHMMSSZ-<12 位十六进制>`，文件依次为各客户端文件、`contract.env`、`.sha256`，最后是 `.ready`。暂存目录私有，结束后删除。
- `contract.env` 为 `KEY=VALUE`：`PF_CONTRACT_VERSION`、`PF_CLIENTS`、`PF_INVENTORY_SHA256` 和每个客户端的 `PF_<CLIENT>_COUNT`，便于服务端只用 bash/awk 解析。
- 上传后轮询 HTTPS（默认 90 秒），每个文件的 SHA-256 一致才算成功；失败信息只含文件名和原因（如 `HTTP 404`）。

**服务端**（`server/setup.sh`、`server/proxyfleet-publish.sh`）
- 上传账号只能 SFTP，chroot 到 `/srv/proxyfleet/sftp/proxyfleet`，只能写 `upload/`。
- systemd 定时器每 20 秒运行发布脚本，`flock` 防止并发，每次只处理一个带 `.ready` 的批次。
- 校验顺序：批次名格式、控制文件大小、契约格式和键、清单逐行、文件存在且非符号链接、大小上限 1 MiB、`sha256sum -c`、每个客户端的结构和节点数。任何一步失败都标记为 `.failed`，线上不变。
- 通过后写入新的 release 目录，`ln -s` + `mv -T` 原子切换 `current`；保留最近 5 个 release，失败残留 7 天后清理。
- Caddy：写入 `/etc/caddy/proxyfleet.caddy` 并在 Caddyfile 中 import；若 Caddyfile 只是安装包自带的欢迎页则替换，否则追加；`caddy validate` 失败即还原备份。站点只放行三个订阅文件路径。
- 秘密路径在 `/etc/proxyfleet/path-token`，安装脚本不打印它，只提示用 `sudo cat` 读取一次。

### 3.5 rotate-path 的约定

- 服务端助手 `/usr/local/libexec/proxyfleet-rotate`（由 `setup.sh` 安装），三个阶段：`prepare`、`finalize`、`rollback`。客户端通过 `ssh <admin>@<host>` 调用（非 root 时用 `sudo -n`），**旧/新 token 只经标准输入传递**，不进命令行参数和进程列表；助手只输出 `PF_ROTATE_*` 标记，错误信息以 `rotate: ` 开头且不含 token。
- 助手持有发布脚本的同一把 `flock` 锁，任何阶段都不会与发布交错。
- `prepare`：核对服务器当前 token 与 `publish.yaml` 一致、没有进行中的轮换；备份站点文件；写入 `path-token.next`；在当前 release 中建立 新 → 旧 的符号链接；把站点文件的 `@subscription` 行改为同时匹配新旧路径；`caddy validate` 失败则全部还原。
- 轮换期间发布：发布脚本发现 `path-token.next` 时，给新 release 也建立 新 → 旧 链接，所以新路径始终提供最新内容。
- 客户端检查新路径时与旧路径**实时比较**（而不是与轮换前的快照比较），这样轮换期间正好有发布也不会误判。
- `finalize`：`@subscription` 只匹配新路径，校验通过后写入 `path-token` 并删除 `.next` 和备份。之后的发布直接使用新 token。
- `rollback`：只需要旧 token，新 token 从服务器的 `.next` 读取；还原站点文件，删除所有 release 中的新路径链接。
- 客户端：只在 `publish.yaml` 中替换 token 字符串（必须恰好出现一次），原子写入并保留权限；`finalize` 之前任何失败都会回滚服务器并还原 `publish.yaml`；`finalize` 之后的检查失败只报告，不回滚。

**0.1 的发布条件**（已于 0.1.0 满足）：`collect`、`build`、`diff` 完成并有测试，能用 frm-node 的 `frm sub fragment` 输出端到端生成三端文件。

## 4. 安全原则

1. **清单不含秘密**：只允许地址、地区、状态、协议和角色。
2. **处理凭据的命令**（collect、build、diff、publish、gate、bind）：
   - 输出目录先设为仅当前用户可访问（Unix `0700`，Windows 当前用户 ACL），再写入内容；
   - 错误只报告固定错误码和位置，不回显文件内容或外部进程的原始输出；
   - 失败时不覆盖上一版产物，临时文件在原目录内原子替换。
3. **发布服务器**只放静态文件：SFTP-only 低权限账号，不部署面板、数据库、订阅转换器或持有其他服务器私钥的程序。
4. **订阅地址**一旦出现在聊天、截图或普通日志中即视为泄露，应执行 `rotate-path`。

## 5. 与 frm-node 的关系

- frm-node 管单台服务器：安装节点、导出片段（`frm sub fragment`）。
- proxyfleet 管整个机队：清单、命名、分组、契约、发布。
- frm-node 自带的 `tools/frm-sub-collect.sh` 保留为不装 proxyfleet 时的轻量选择。
- proxyfleet 的采集来源是可插拔的，不使用 frm-node 的用户也可以提供本地片段目录。

## 6. 尚未决定

- CLI 输出语言：目前为英文；是否增加中文输出待定。
- 片段格式的规范化：不同节点管理器导出的 Surge/Loon/Mihomo 片段细节不同，0.1 先支持 frm-node 的格式。
