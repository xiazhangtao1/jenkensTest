# Ubuntu 22.04 编译镜像

## 构建、导出与发布

```bash
make compile-image
make compile-image DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.08-compat-r2
make compile-image DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.08-sdk-r2
make compile-image IMAGE_NAME=192.168.10.35/xnet/xran-compile IMAGE_TAG=26.11.05 BUILD_JOBS=4
make compile-image APT_SOURCES=config/sources.intranet.list
make push-compile-image IMAGE_NAME=192.168.10.35/xnet/xran-compile IMAGE_TAG=26.11.05
```

镜像名称与 tag 独立配置；名称支持仓库端口，例如 `registry.example:5000/xnet/xran-compile`。
默认名称 `192.168.10.35/xnet/xran-compile`，默认 tag `latest`。单独构建编译镜像无需 Jenkins 插件、chart 或模拟业务仓库。
Python 入口为 `python3 scripts/compile-image.py build --dockerfile Dockerfile路径 --image-name 名称 --image-tag 标签 --jobs 4`。

| Dockerfile | 生成器 | 默认开发库 | 用途 |
| --- | --- | --- | --- |
| `docker/builder-compat.Dockerfile`（默认） | protobuf-c 1.5.2 | Ubuntu 1.3.3 | 固化本次内网已验证的组合 |
| `docker/builder.Dockerfile` | protobuf-c 1.5.2 | 源码构建的 1.5.2 | 对照测试全套新版 SDK |

`DOCKERFILE` 支持相对项目根目录的路径及绝对路径；路径有空格时用引号包围整个参数。
构建上下文固定为项目根目录。自定义 Dockerfile 需使用项目预备的缓存，并提供兼容的运行适配器及镜像验证入口。
两个版本共用 Ubuntu 22.04、源码、安装脚本和公共缓存；不同的 protobuf-c 安装步骤位于镜像末尾。
默认镜像地址仍是 `192.168.10.35/xnet/xran-compile:latest`，选择 Dockerfile 不自动改写 tag；两版并存时请使用不同 tag。

首次命令自动校验、还原源码，准备 Docker 基础镜像和完整 APT 依赖缓存，再执行断网 Docker 构建。
制作 SDK 时默认并行数为 4，可通过 `BUILD_JOBS` 修改；它与 Jenkins 的服务串行开关、xcn 内层并行数分别配置。
Docker 构建完成后自动执行镜像验证。导出目录为 `dist/transfer/03-generated-images/compiler/镜像引用转换后的目录名/`，
包含 `image.tar`、`manifest.json`、`SHA256SUMS`，记录 Dockerfile 路径及校验值、实际工具版本、镜像 ID、依赖清单校验值和导出包校验值。

推送命令单独执行。私有仓库需要登录时，先执行 `docker login 192.168.10.35`；凭据只保存在操作者自己的 Docker 配置中。
仓库使用 HTTP 或自签名证书时，Docker daemon 必须配置对应仓库的访问方式或 CA。

## 依赖和原始资料

资料来源为原 `compileDep`，整理后的源码、二进制及原始脚本位于 `vendor/compile/`。
`manifest.json` 记录来源、版本、SHA256、原始归档校验值及分片校验值；单个分片最大 50MiB，普通 Git 即可同步。
`legacy/` 仅保留原始装机资料供追溯，镜像构建执行 `docker/install-compile-deps.sh`。

主要组件：DPDK 22.03、ROHC 1.7.0、Boost 1.83.0、protobuf 3.21.11、protobuf-c 生成器 1.5.2、
libwebsockets 4.5.8、cpprestsdk 2.10.19、etcd-cpp-apiv3 0.15.4、nlohmann JSON 3.11.3、spdlog 1.14.1、
asn1c 0.9.29 和资料中的 x86-64 `libairtracer.so`。该二进制没有随附许可证文件，按原内部资料保留。

原提供的 etcd 和 OpenTelemetry 动态库依赖不同 protobuf/gRPC ABI，因此重新构建整条依赖链，
补充固定版本 gRPC 1.49.1、Abseil 20220623.1、OpenTelemetry C++ 1.17.0 和 proto 1.3.2；
gRPC 所需外部 proto 源码也已锁定提交并入库。`wls_mod.tar` 和 Intel oneAPI 不参与构建。
protobuf、gRPC、Abseil、OpenTelemetry 和 spdlog 同时提供共享库与静态归档，兼容原装机资料中直接链接 `.a` 的用法；CMake 默认使用共享版本。

APT 包拆为通用工具、编译依赖、额外工具及兼容版 protobuf-c 清单；额外工具同时提供 `pkgconf` 和 xcn 发布需要的 PyYAML。
公共缓存为 `.cache/compile-apt`、`.cache/compile-extra-apt`，兼容版专用缓存为 `.cache/compile-compat-apt`。
准备步骤会校验、准备两种 Dockerfile 所需的缓存，方便迁入后切换；两版自行选择安装内容。
Helm 3.17.3 二进制及 SHA256 固定于 `config/helm.lock.json`，缓存随离线包交付。
protobuf、gRPC、Boost 使用上述源码版本，避免再安装 APT 中不同版本的开发库。
全套 1.5.2 Dockerfile 中，现有工具、头文件和运行库由 `docker/package-protobuf-c.sh` 打成真实的
`protobuf-c-compiler`、`libprotobuf-c-dev`、`libprotobuf-c1` 软件包，再通过 `dpkg --install` 安装。
这样原业务脚本的 `dpkg -s` 检查可以通过，无需进入 `read` / `sudo apt-get` 交互安装。
软件包包含实际 SDK 文件、许可证和校验值，保留 `/usr/local/protobuf-c-1.5.2` 路径，
同时提供标准 `/usr/bin`、`/usr/include` 和多架构库目录入口；版本仍为 1.5.2。
这些软件包用于完整 SDK 镜像，编译器的 protobuf 3.21.11 依赖由该镜像提供。
默认兼容版不执行此打包脚本，而是安装 Ubuntu 的 `protobuf-c-compiler`、`libprotobuf-c-dev`、`libprotobuf-c1`
三个 `1.3.3-1ubuntu2.1` 软件包。`protoc-c` / `protoc-gen-c` 仍优先使用源码安装的 1.5.2，
`pkg-config libprotobuf-c`、标准 include/library 路径使用 1.3.3，复现内网已通过的编译环境。
两版都验证代码生成、编译及头文件/运行库一致性；全套 1.5.2 仍需用真实内网业务进一步验证。
Docker `ENV` 设置 PATH、PKG_CONFIG_PATH、CMAKE_PREFIX_PATH，动态库通过 ldconfig 配置，非交互 Jenkins shell 可以直接使用。
DPDK 源码和 build 路径保留在 `/opt/dpdk-22.03`。镜像内 `/opt/packages.lock` 记录 APT 及源码生成的软件包版本，
`/opt/xran/compile-sources.lock.json` 记录源码依赖。
默认使用已安装 SDK 的 pkg-config 文件，避免 DPDK 的 uninstalled 元数据引用可选构建目录导致 CMake 拒绝配置。

## DPDK 逻辑核上限

两个 Dockerfile 共用的安装脚本显式设置 `-Dmax_lcores=256`，不依赖源码包的默认值。
`RTE_MAX_LCORE=256` 表示逻辑核编号范围为 **0–255**。镜像自检同时核对 Meson 选项、build/安装目录的头文件，
并通过编译期断言及实际 EAL 初始化检查编号 144、255 可用，256 被拒绝。
自检将逻辑核映射到容器允许使用的一个 CPU，无需 256 核宿主机、网卡或 hugepages。

本地已验证 `26.11.08-compat-r2` 和 `26.11.08-sdk-r2` 实际都是 256；本次只读挂载新版自检，两者均通过。
相同内容的既有镜像无需为此重建。内网镜像若来源不同，先检查实际值；检查和按需重建步骤见
[本次内网更新](formal-build-upgrade.md#dpdk-与内网构建目标更新2026-11-09)。

## 离线重建与验证

先按 [分类搬运](transfer.md) 还原编译依赖和基础镜像；无需携带编译镜像成品。迁入后：

```bash
docker load -i .cache/build-base-images.tar
make compile-image OFFLINE=1
make compile-image OFFLINE=1 DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.08-sdk-r2
docker run --rm --network none 192.168.10.35/xnet/xran-compile:latest bash /opt/xran/verify-compile.sh
```

`OFFLINE=1` 禁止准备步骤拉取基础镜像或更新 APT；缺少缓存或校验失败时直接报错。可同时指定内网 sources.list，
已有完整缓存可以用于断网安装。需要新增或调整 APT 依赖时，应在能够访问所选软件源的准备机上重新制作缓存和离线包。

验证脚本以镜像默认 UID/GID 1000 执行 protobuf 代码生成、主要 SDK 的编译/链接/运行、ASN.1 工具和共享库加载检查。
还会检查上述软件包的安装状态及实际文件校验值，并用标准 include/library 路径验证 protobuf-c 头文件与运行库一致。
完整 Jenkins 镜像构建、渲染和离线打包支持同样的 `DOCKERFILE` / `IMAGE_NAME` / `IMAGE_TAG` 参数，定制版本时各命令应使用相同参数。

已部署环境的完整升级流程见 [正式构建修复升级](formal-build-upgrade.md)。
先同步源码和新增 APT 缓存，再重建所选镜像：

```bash
make compile-image OFFLINE=1 DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.08-compat-r2
make push-compile-image IMAGE_TAG=26.11.08-compat-r2
```

选择直接搬运成品时，拷贝 `dist/transfer/03-generated-images/compiler/192.168.10.35_xnet_xran-compile_26.11.08-compat-r2/image.tar`，
在内网执行 `docker load -i 文件路径` 后推送同名镜像到 Harbor。
Jenkins 构建参数 `COMPILER_IMAGE` 填该完整新版本地址，拉取策略使用 `Always` 即可，无需重新部署 Jenkins。

## 新旧镜像构建脚本的产物兼容

新版在 `platform/k8s/images-build/目标.tar` 出包时直接使用。
旧版只在本次 Pod 的 DinD 留下镜像时，收集阶段会自动 `docker save` 为同名包，校验后归档。
构建前仅清理本次目标的旧同名文件；导出先写临时文件，失败时清理临时文件，不发布残缺包。
已有包损坏、Docker manifest 的引用不符或镜像 ID 与本次 DinD 不符时明确失败，不用自动导出掩盖问题。

期望镜像地址为 `registry/imageRepositories[IMAGE_TARGET]:IMAGE_TAG`，按业务脚本配置，例如：

```yaml
registry: 192.168.10.35
imageRepositories: {imageA: xnet/imagea, imageB: xnet/imageb, imageC: xnet/imagec}
```

只匹配该完整地址和 tag，不扫描宿主机 Docker、不猜测其他标签，也不会执行 `docker pull`。
找不到镜像时检查站点映射是否与该业务版本一致。
元数据 `imageArchiveSource` 为 `script` 或 `docker-save`；镜像包包含名称、大小、SHA256 和镜像 ID。
脚本返回失败时，自动导出的有效包仍可下载，任务保持失败；取消构建时不会启动新的缺包导出。

该能力由新版编译镜像内的适配器提供，镜像任务检查 `xranImageArchiveFallback: 1`。
需要更新两版 Dockerfile 制作的镜像；只升级 Controller 而继续选择旧适配器时会明确提示更新。
服务和 xcn 的适配器兼容规则保持原有行为。

## Jenkins 每次构建选择

`COMPILER_IMAGE` 填完整地址，例如 `192.168.10.35/xnet/xran-compile:26.11.05`，也支持 `@sha256:摘要`。
`COMPILER_IMAGE_PULL_POLICY` 可选：

| 策略 | 行为 |
|---|---|
| Always（默认） | 启动时联系 Registry 解析镜像，复用已有内容层 |
| IfNotPresent | 节点已有该镜像时直接使用，否则拉取 |
| Never | 只使用节点已导入的镜像，缺少时失败 |

这两个参数在 X-RAN-CLOUD 服务、镜像及 xcn 构建表单和历史 Parameters 页均显示，仅影响编译容器。
`IMAGE_TAG` 仍表示产出的业务镜像标签。每次构建保存实际 Pod 返回的编译镜像 ID，不能仅凭可变 tag 判断版本。
服务包与 xcn 的 Agent、编译容器使用 UID/GID 1000；X-RAN-CLOUD 镜像任务使用 UID/GID 0，兼容业务脚本的 root 检查。
同一个 Pod 的 Agent 与编译容器保持相同 UID，确保共享工作区、SSH 凭据及仅属主可读的产物能正常处理。
`xran-build-trust` 在镜像任务挂载到 `/root/.ssh`，其他任务仍挂载到 `/home/jenkins/.ssh`，继续严格校验 Git 主机。
这由部署脚本生成的 Pod 模板控制，无需修改 Dockerfile 的 `USER`；builder 仍不使用 privileged，Docker 仍连接本次 Pod 的独立 DinD。
所选镜像须包含 `/bin/sh`、Python、Git、编译工具和本项目 `/opt/xran/run-build.py`；
业务镜像模式还需 Docker CLI 及 `/opt/xran/base-images.tar`；xcn 还需 Helm、PyYAML 和更新后的发布适配器。
X-RAN-CLOUD 新任务会检查 `python3 /opt/xran/run-build.py capabilities` 返回的 `xranServiceCompatibility: 1`，
旧镜像缺少此能力时明确提示重新制作。xcn 保留原适配器兼容逻辑。
服务目标只有单个服务和 `services_all`；前者校验对应单包，后者校验唯一的 `L2L3_时间_commit前9位.tar.gz`。
xcn 的基础镜像通过独立 init 容器挂载到 `/opt/xran/xcn-bases/images.tar`。缺少工具时流水线明确失败。

编译镜像拉取凭据使用命名空间内的 Kubernetes Secret，在 `config/site.yaml` 配置 `compilerImagePullSecrets`。
可以从管理员已有 Docker 登录配置创建 Secret：

```bash
kubectl -n jenkins create secret generic xran-compile-registry \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=/安全路径/config.json
```

然后设置 `compilerImagePullSecrets: [xran-compile-registry]` 并重新部署。
Pod 中业务镜像推送使用的 Jenkins Credentials 不能替代此拉取凭据；节点 containerd 还须信任仓库 CA 或配置该 HTTP 仓库。
`agentStartupTimeoutMinutes` 默认 15，控制编译 Pod 启动等待；现有整体构建超时仍为 240 分钟。

当前演示环境无法路由到 `192.168.10.35`。联调时临时转发现有演示 Registry，并显式填写测试镜像：

```bash
kubectl -n jenkins port-forward --address 127.0.0.1 service/xran-registry 15050:5000
```

该转发供当前单节点验证使用；测试镜像为 `localhost:15050/xnet/xran-compile:latest`。
项目与 Jenkins 参数的默认值仍为指定内网地址和 Always；迁入后须确认内网仓库已发布镜像、节点网络及拉取凭据可用。

依据：[Kubernetes 镜像拉取策略](https://kubernetes.io/docs/concepts/containers/images/#image-pull-policy)、
[私有仓库 Secret](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/)。
