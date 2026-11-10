# 分类搬运到内网

当前项目目录是构建工作区，`.cache` 中既有依赖也有构建时使用的镜像归档。日常搬运统一使用 `dist/transfer/`，无需反复复制整个项目或 `.cache`。

此前 DPDK 固定参数和内网目标列表更新只需新的 `02-code`；既有 r2 镜像已验证为 256，无需重复拷贝编译镜像。
本次 xcn 描述随 Controller 19 更新；旧站点带入 `02-code` 后只需重建 Controller，也可另带其成品，见 [升级 Jenkins](formal-build-upgrade.md#升级-jenkins)。
业务编译新增 readline、hwloc 和 zstd 开发依赖时，已有完整环境需同步新版 `02-code` 和完整
`.cache/compile-extra-apt/`，再重建、推送编译镜像；不能只带代码。具体见 [readline 更新](formal-build-upgrade.md#cli-编译缺少-readlinereadlineh2026-11-10)。

## 全新服务器最少携带

准备在内网自行制作镜像时，**`01-dependencies` 和 `02-code` 两个目录足够作为 Jenkins 项目的离线输入**：

- `01-dependencies`：`compiler-deps.tar.gz`、`jenkins-deps.tar.gz`、`build-base-images.tar`、`runtime-base-images.tar`、`xcn-base-images.tar`，以及 `MANIFEST.json`、`SHA256SUMS`。
- `02-code`：完整的 `code.tar.gz`，以及 `CODE.json`、`DEPENDENCIES.json`、`SHA256SUMS`。全新目录不能只带 `code.patch`；外网执行 `make transfer-code` 生成完整包。

`optional-verification-tools*` 仅供浏览器验收，可省略。`03-generated-images` 是可选成品，内网执行 `make images` 会制作 Controller、编译镜像和 xcn 基础镜像载体。
`01-dependencies/optional-zstd-dev/` 是约 400KB 的可选小补包，只用于给已有 SDK 镜像补 zstd 开发文件，不包含 hwloc；完整离线重建不需要它。
全新安装无需旧 Jenkins HOME 备份、外网账号文件、模拟业务仓库或 `.git` 历史。
目标机仍需先有 Kubernetes/containerd；本项目不提供集群安装离线包。Python/PyYAML、make、Git、Docker 等基础软件可从内网 apt 安装。
站点 IP/存储、Gerrit 私钥与主机信任、Harbor 登录和实际需要的 CA/拉取 Secret 在目标环境另配，不包含在通用包中。

| 分类 | 路径 | 内容 | 何时拷贝 |
| --- | --- | --- | --- |
| 外部依赖 | `01-dependencies/compiler-deps.tar.gz` | compileDep 整理的固定源码/分片、deb、Helm、Ubuntu 业务基础镜像 | 首次或编译依赖变化 |
| 可选小补包 | `01-dependencies/optional-zstd-dev/` | 单个 zstd 开发包、校验值、断网增量 Dockerfile 和 C 自检 | 已有编译镜像仅缺 `zstd.h` 时；不替代完整依赖包 |
| 外部依赖 | `01-dependencies/jenkins-deps.tar.gz` | 官方 Jenkins 插件、Maven 缓存、Helm chart、kubectl/helm/ctr | 首次或平台依赖变化 |
| 外部基础镜像 | `01-dependencies/build-base-images.tar` | Ubuntu 22.04、Jenkins 官方基础镜像、Docker CLI、Maven/JDK | 首次或基础镜像版本变化 |
| 外部运行镜像 | `01-dependencies/runtime-base-images.tar` | Agent、DinD、Registry、local-path、busybox | 首次或版本变化；已有镜像可复用 |
| xcn 基础镜像 | `01-dependencies/xcn-base-images.tar` | 固定的 Ubuntu/VPP 镜像 | 使用 xcn 且内网缺少这些镜像时 |
| 项目代码 | `02-code/code.tar.gz` 或 `code.patch` | Dockerfile、脚本、配置示例、插件源码、文档 | 修改代码时；不重复携带 SDK 源码大文件 |
| 可自行生成的镜像 | `03-generated-images/` | 编译镜像、定制 Controller 等成品 | 可选；准备在内网自行构建时无需拷贝 |

不能只凭一个包是源码还是二进制判断能否省略。例如 Ubuntu、Jenkins、DinD 等上游镜像，Jenkins 插件和 Maven 依赖不会因为内网有 apt 源就自动齐全；这些需要提前准备。
编译镜像由两个 Dockerfile 制作，定制 Controller 由官方 Jenkins 基础镜像加本项目插件/脚本制作；依赖齐全后都能在内网断网重建。
宿主机的 `python3`、`python3-yaml`、`make`、`git`、Docker 等基础软件可按内网 apt 源安装。目标机需已有 Kubernetes/containerd；本项目不包含集群安装包。

## 外网准备

依赖已缓存的准备机执行：

```bash
make transfer-deps
make transfer-code
```

依赖包只包含上述外部输入，不含编译镜像、定制 Controller、业务源码镜像、Jenkins HOME 或运行凭据。
相同输入重复执行会复用依赖包，不按时间生成多个版本。每类保留一份当前包，`MANIFEST.json` 记录每个文件和镜像的校验值。
`code.tar.gz` 不含 `.git`、`.state`、本地站点配置和 SDK 源码大文件；依赖在另一类包内。
代码只打包已提交的内容；先提交本次修改。已有依赖清单会同时放到 `02-code/DEPENDENCIES.json`，日常先只带 `02-code` 就能检查是否还需搬运依赖。
编译依赖包中的 deb 是当前断网 Docker 构建的固定输入，部分虽可从内网 apt 获取，也不能直接删除缓存后继续使用 `OFFLINE=1`；要省略这些文件，需先用内网源重新准备并校验完整缓存。

已有内网代码目录时，按其实际基线生成更小的代码补丁：

```bash
# 内网先用 git rev-parse HEAD 查看基线；没有 .git 的代码包可查看 TRANSFER_SOURCE.json。
make transfer-code BASE_COMMIT=23777c1
```

生成 `02-code/code.patch` 和 `CODE.json`，不再同时保留完整代码包。先校验 `SHA256SUMS`，在内网项目目录执行：

```bash
git diff > /tmp/jenkins-local-before-update.patch
git apply --check /搬运路径/02-code/code.patch
git apply /搬运路径/02-code/code.patch
cp /搬运路径/02-code/CODE.json TRANSFER_SOURCE.json
```

有本地临时改动时先保留副本并处理冲突，`config/site.local.yaml` 不在交付包内。
通过 `git apply` 应用后，Git HEAD 不自动前进，下一轮读取 `TRANSFER_SOURCE.json` 的 commit 作为基线；不要对同一未前进的 HEAD 重复应用累积补丁。
需要持续用 Git 管理时，建议通过内网 Git 同步提交；完整 Git 历史仅首次需要时执行 `make transfer-git`，产物在 `02-code/optional-git/`。

## 先查清单，再决定拷什么

先只把新的 `01-dependencies/MANIFEST.json` 带入内网，在已有项目根目录执行：

```bash
python3 scripts/transfer.py check --manifest /搬运路径/MANIFEST.json
# 日常仅携带代码目录时可直接用其中的小清单：
python3 scripts/transfer.py check --manifest /搬运路径/02-code/DEPENDENCIES.json
# 如镜像已导入 Docker、归档已删，可以同时检查本机镜像 ID：
sudo python3 scripts/transfer.py check --manifest /搬运路径/MANIFEST.json --docker
```

命令逐类输出“已有，无需拷贝”或“缺少或变化，需要拷贝”。只搬运后者及本次代码包即可。
未升级过此脚本的内网目录，先带入本次代码或单独的 `scripts/transfer.py`；它只使用 Python 标准库。
Docker 检查通过表示可用本机镜像重新导出，不代表其他 Kubernetes 节点已导入。

## 首次还原和内网自建

先解开代码，再将所需依赖还原到同一个项目目录。以下以已经校验的分类目录为例：

```bash
cd /搬运路径/02-code
sha256sum -c SHA256SUMS
mkdir -p /data/jenkensTest
tar --no-same-owner -xzf code.tar.gz -C /data/jenkensTest

cd /搬运路径/01-dependencies
sha256sum -c SHA256SUMS
tar --no-same-owner -xzf compiler-deps.tar.gz -C /data/jenkensTest
tar --no-same-owner -xzf jenkins-deps.tar.gz -C /data/jenkensTest
mkdir -p /data/jenkensTest/.cache
cp build-base-images.tar runtime-base-images.tar xcn-base-images.tar /data/jenkensTest/.cache/

cd /data/jenkensTest
sudo apt-get install -y python3 python3-yaml make git docker.io
sudo docker load -i .cache/build-base-images.tar
sudo docker load -i .cache/runtime-base-images.tar
sudo docker load -i .cache/xcn-base-images.tar
cp config/site.intranet.yaml config/site.local.yaml
editor config/site.local.yaml
```

`sha256sum -c SHA256SUMS` 示例假定该类五个依赖包都已搬入；只搬部分时可先用对应包的清单行校验。
确认 `demo: false`、节点地址、Git/Registry 地址、服务清单和存储配置。全新自建所有定制镜像：

```bash
sudo make images SITE=config/site.local.yaml OFFLINE=1 APT_SOURCES=config/sources.intranet.list
sudo docker login 192.168.10.35
sudo make push-compile-image IMAGE_TAG=latest
sudo ctr -n k8s.io images import .cache/k8s-images.tar
sudo env "PATH=$PATH:$PWD/.cache/tools" make deploy SITE=config/site.local.yaml OFFLINE=1
```

生产模式不构建演示 Git/Registry 服务镜像，也不要求准备本地业务仓库。业务源码仍在每次构建 Pod 内从站点配置的 Gerrit 检出。
每个可能运行相关 Pod 的节点都须导入所需镜像。Git 凭据、主机信任、Harbor 登录和用户开户见 [离线部署](offline.md)。

只重建编译镜像时：

```bash
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.08-compat-r2
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.08-sdk-r2
```

成品在 `dist/transfer/03-generated-images/compiler/`。两个目录是不同编译环境，均是当前版本。
`.cache/k8s-images.tar` 是本地构建后用于导入节点的工作归档，可能包含定制镜像；它不属于默认外部依赖交付。
`make bundle` 保留为兼容的完整快照命令，需要所有成品一起搬运时才使用；日常更新采用分类包。
