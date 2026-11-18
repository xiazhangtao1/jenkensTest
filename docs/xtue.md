# XTUE-CLOUD 与项目扩展

Jenkins 入口 `xtue-cloud`，显示名称 **X-TUE-CLOUD**。默认 `main`、`nrUE`，支持分支、tag、完整及唯一短 commit SHA。
默认编译环境为 `192.168.10.35/xnet/xran-compile:latest`，拉取策略 Always，均可在提交构建时修改。

| 目标 | 工作目录 | 命令 | 仓库内预期产物 |
| --- | --- | --- | --- |
| nrUE | cmake_targets | ./build_oai -k --nrUE | cmake_targets/tue.tar.gz |
| tuePhy | cmake_targets | ./build_oai -k --tuePhy | cmake_targets/tue_simuphy.tar.gz |

共用服务包 Pod 资源（初始 4 CPU、8GiB 内存、20GiB 临时存储）、三个项目共享全局并发名额，初始最多 6 个。
超时、磁盘预留、历史保留与压力回收规则相同；编译 Pod 默认 UID 1000，不给 XTUE 注入 X-RAN 的 Makefile 串行补丁。
业务脚本若自行覆盖并行参数，仍以脚本行为为准；可在项目命令/环境配置中调整。

管理员在系统配置的“构建平台设置”看到“X-TUE-CLOUD 仅管理员可见”。保存、重启后保持，关闭后恢复原项目授权。
构建详情记录用户、时间、完整 commit、实际编译镜像及项目规则哈希；HOME 中的每次构建还保存 `xran-project.json` 规则快照。

## 修改构建规则与新增项目

`config/projects.yaml` 是平台维护的规则表，业务分支不提供平台规则。`workdir` 和 `artifacts` 均相对仓库根目录；
`command` 是参数数组，执行时不通过 shell 拼接。通用适配器的产物为一个或多个明确的 `.tar.gz` 文件，不能重名。

新增同类项目时复制 `xtue-cloud` 配置段，修改任务 ID、显示名、Git 地址、目标、工作目录、命令和产物。
支持 `enabled: false` 禁用。已有任务从注册表移除时禁用新构建，历史及压力清理仍保留。
同类项目无需改 Java/Python/Groovy/JS，不需要重做镜像；依赖齐全时修改配置并重新部署即可。
仅新增特殊构建行为时扩展适配器。现有 X-RAN 镜像的兼容导出、服务串行处理和 xcn Chart 校验仍使用专用逻辑。
项目和目标均可配置 `env` 字符串映射；目标的同名变量覆盖项目变量。规则更新对后续构建生效，已入场构建使用自己的快照。

站点覆盖示例（显式覆盖优先；现有顶层 X-RAN 与 xcn 字段继续兼容）：

```yaml
projectsFile: config/projects.yaml
projects:
  xtue-cloud:
    gitUrl: ssh://xiazhangtao@192.168.10.82:29418/XTUE-CLOUD
    gitCredentialsId: intranet-gerrit-ssh
```

等待现有构建完成后执行：

```bash
sudo env "PATH=$PATH:$PWD/.cache/tools" make deploy SITE=config/site.local.yaml OFFLINE=1
```

本次首次升级涉及内部插件和适配器，需要先重做 Controller 及编译镜像。此后仅改项目规则不需要重做镜像。
编译镜像过旧时会明确提示不支持项目规则；不要仅更换 tag 而继续使用旧镜像内容。

## XTUE 依赖和 ASN.1 来源

新增依赖集中在 `config/compiler-extra-apt-packages.txt`，已有软件保留；pkg-config 命令由 pkgconf 提供，
`libscpp-dev` 经确认按已有 libsctp-dev 处理。支持注释、去重和固定版本 `包名=版本`。

## 补齐 xxd 与 LAPACKE（2026-11-18）

真实内网构建发现还需要 `xxd` 和 `liblapacke-dev`：前者生成 `T_messages.txt.h`，后者提供
`lapacke_utils.h` 和 LAPACKE C 接口；已有 `liblapack-dev` 不包含该头文件。
对应 Ubuntu 文件清单见 [xxd](https://packages.ubuntu.com/jammy/amd64/xxd/filelist) 和
[liblapacke-dev](https://packages.ubuntu.com/jammy/amd64/liblapacke-dev/filelist)。两种 Dockerfile 共用的补充清单已加入这两项。
镜像自检实际执行 `xxd -i`、编译运行生成的头文件，并包含 `lapacke_utils.h`、链接运行 LAPACKE 求解器。

已应用此前 XTUE 依赖的内网环境，本次只需最新 `02-code` 和最新 `01-dependencies/updates/`。
该差量只描述 `.cache/compile-extra-apt/`，包含新增的五个 deb 和缓存 manifest，复用原有已校验文件，
不检查 xcn/Controller 镜像归档，也不更新它们。完整依赖包同步更新，用于全新安装或缓存基线不匹配的情况。
此前约 61MiB 的 XTUE 首次升级差量不包含本次新增包，不能重复当成本次补包使用。

更新代码后，先在搬入的 `updates` 目录校验，再在内网项目根目录执行：

```bash
sha256sum -c SHA256SUMS
cd /内网项目目录
sudo make apply-dependency-delta DELTA_PACKAGE=/搬入路径/updates/dependencies-delta.tar.gz
sudo python3 scripts/transfer.py check --manifest DEPENDENCIES_APPLIED.json
sudo make compile-image OFFLINE=1 APT_SOURCES=config/sources.intranet.list \
  DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.18-compat-xtue-tools
sudo make push-compile-image IMAGE_TAG=26.11.18-compat-xtue-tools
```

另一个编译环境使用 `DOCKERFILE=docker/builder.Dockerfile` 和不同的 tag。
网页选择新 tag 和 `Always` 后重新构建。不需要重做 Controller；若一并更新了 DinD 启动脚本，另执行 `make deploy`。
此次 `DEPENDENCIES_APPLIED.json` 只记录补充 APT 范围；将来的完整依赖对比使用 `02-code/DEPENDENCIES.json`。
实际私有业务源码不可访问，完整 nrUE/tuePhy 构建仍需内网复测。

## ASN.1 来源

提供的 `asn1c.tar.gz`：20,697,300 字节，SHA256
`bb9b423fff6d73d00ebc420e5d64be7359be0e1309cc0ff2a3cc5e02cca76911`，包含 Git 提交
`f12568d617dbf48497588f8e227d70388fa217c9`。与现有锁定归档逐文件比较，所有手写 C/H（除生成的 config.h）、
Autotools 输入及 ASN.1 源文件相同，无新增手写源文件；变化为生成配置、构建产物及 Git 数据。
因此复用仓库的 asn1c 0.9.29 源码，不重复拷贝 20MB 原始包。镜像自检包含代码生成、DER/BER 与 UPER 编解码回环（自检关闭 OER）。

```bash
# 内网源可达时准备/更新依赖；Docker 编译步骤本身仍断网。
sudo make compile-image OFFLINE=0 APT_SOURCES=config/sources.intranet.list DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.18-compat-xtue-tools
# 已补齐匹配缓存后完全离线制作；全套新版改用 docker/builder.Dockerfile。
sudo make compile-image OFFLINE=1 APT_SOURCES=config/sources.intranet.list DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.18-compat-xtue-tools
sudo make push-compile-image IMAGE_TAG=26.11.18-compat-xtue-tools
```

新增/删除 APT 软件修改分组清单后重做对应缓存及镜像；旧缓存不会在软件清单变化后继续使用。
删除附加包后从 Dockerfile 重建，不是在旧成品上再叠一层；其他组件仍依赖的包可能继续存在。
内网源如缺少匹配版本会明确失败，可用已验证的完整缓存或差量依赖修复，不能用 `--fix-broken` 掩盖依赖冲突。

首次升级完整步骤（在依赖已还原、现有构建已结束后）：

```bash
sudo make images SITE=config/site.local.yaml OFFLINE=1 APT_SOURCES=config/sources.intranet.list
sudo env "PATH=$PATH:$PWD/.cache/tools" make import-images SITE=config/site.local.yaml OFFLINE=1
sudo env "PATH=$PATH:$PWD/.cache/tools" make deploy SITE=config/site.local.yaml OFFLINE=1
```

多节点须逐个节点导入所需镜像，或在站点及 Kubernetes 中配置可用的内网 Registry。
已有 HOME 的编译环境默认值以网页保存值为准；使用新 tag 时须在网页选择该 tag，或推送更新后的 latest。

本次已完成两套镜像的断网构建与 SDK 自检、隔离 Jenkins 的引用选择、下载、失败、超时、权限及重启验证，
并回归 X-RAN 镜像与 xcn 发布产物。真实 XTUE 私有源码当前不可访问；迁入后仍需用真实源码验收两个目标。
验收证据见 [平台记录](verification/xtue-projects.json) 和 [依赖与迁移记录](verification/xtue.json)。
