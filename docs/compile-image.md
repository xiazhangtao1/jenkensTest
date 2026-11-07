# Ubuntu 22.04 编译镜像

## 构建、导出与发布

```bash
make compile-image
make compile-image IMAGE_NAME=192.168.10.35/xnet/xran-compile IMAGE_TAG=26.11.05 BUILD_JOBS=4
make compile-image APT_SOURCES=config/sources.intranet.list
make push-compile-image IMAGE_NAME=192.168.10.35/xnet/xran-compile IMAGE_TAG=26.11.05
```

镜像名称与 tag 独立配置；名称支持仓库端口，例如 `registry.example:5000/xnet/xran-compile`。
默认名称 `192.168.10.35/xnet/xran-compile`，默认 tag `latest`。单独构建编译镜像无需 Jenkins 插件、chart 或模拟业务仓库。
Python 入口为 `python3 scripts/compile-image.py build --image-name 名称 --image-tag 标签 --jobs 4`。

首次命令自动校验、还原源码，准备 Docker 基础镜像和完整 APT 依赖缓存，再执行断网 Docker 构建。
构建默认并行数为 4，可通过 `BUILD_JOBS` 修改。导出目录为 `dist/compile/镜像引用转换后的目录名/`，
包含 `image.tar`、`manifest.json`、`SHA256SUMS`，记录镜像 ID、依赖清单校验值和导出包校验值。

推送命令单独执行。私有仓库需要登录时，先执行 `docker login 192.168.10.35`；凭据只保存在操作者自己的 Docker 配置中。
仓库使用 HTTP 或自签名证书时，Docker daemon 必须配置对应仓库的访问方式或 CA。

## 依赖和原始资料

资料来源为原 `compileDep`，整理后的源码、二进制及原始脚本位于 `vendor/compile/`。
`manifest.json` 记录来源、版本、SHA256、原始归档校验值及分片校验值；单个分片最大 50MiB，普通 Git 即可同步。
`legacy/` 仅保留原始装机资料供追溯，镜像构建执行 `docker/install-compile-deps.sh`。

主要组件：DPDK 22.03、ROHC 1.7.0、Boost 1.83.0、protobuf 3.21.11、protobuf-c 1.5.2、
libwebsockets 4.5.8、cpprestsdk 2.10.19、etcd-cpp-apiv3 0.15.4、nlohmann JSON 3.11.3、spdlog 1.14.1、
asn1c 0.9.29 和资料中的 x86-64 `libairtracer.so`。该二进制没有随附许可证文件，按原内部资料保留。

原提供的 etcd 和 OpenTelemetry 动态库依赖不同 protobuf/gRPC ABI，因此重新构建整条依赖链，
补充固定版本 gRPC 1.49.1、Abseil 20220623.1、OpenTelemetry C++ 1.17.0 和 proto 1.3.2；
gRPC 所需外部 proto 源码也已锁定提交并入库。`wls_mod.tar` 和 Intel oneAPI 不参与构建。
protobuf、gRPC、Abseil、OpenTelemetry 和 spdlog 同时提供共享库与静态归档，兼容原装机资料中直接链接 `.a` 的用法；CMake 默认使用共享版本。

APT 包拆为通用工具、编译依赖和额外运行工具清单；后者提供 xcn 发布需要的 PyYAML。
Helm 3.17.3 二进制及 SHA256 固定于 `config/helm.lock.json`，缓存随离线包交付。
protobuf、gRPC、Boost 使用上述源码版本，避免再安装 APT 中不同版本的开发库。
protobuf-c 1.5.2 的现有工具、头文件和运行库由 `docker/package-protobuf-c.sh` 打成真实的
`protobuf-c-compiler`、`libprotobuf-c-dev`、`libprotobuf-c1` 软件包，再通过 `dpkg --install` 安装。
这样原业务脚本的 `dpkg -s` 检查可以通过，无需进入 `read` / `sudo apt-get` 交互安装。
软件包包含实际 SDK 文件、许可证和校验值，保留 `/usr/local/protobuf-c-1.5.2` 路径，
同时提供标准 `/usr/bin`、`/usr/include` 和多架构库目录入口；版本仍为 1.5.2。
这些软件包用于完整 SDK 镜像，编译器的 protobuf 3.21.11 依赖由该镜像提供。
Docker `ENV` 设置 PATH、PKG_CONFIG_PATH、CMAKE_PREFIX_PATH，动态库通过 ldconfig 配置，非交互 Jenkins shell 可以直接使用。
DPDK 源码和 build 路径保留在 `/opt/dpdk-22.03`。镜像内 `/opt/packages.lock` 记录 APT 及源码生成的软件包版本，
`/opt/xran/compile-sources.lock.json` 记录源码依赖。
默认使用已安装 SDK 的 pkg-config 文件，避免 DPDK 的 uninstalled 元数据引用可选构建目录导致 CMake 拒绝配置。

## 离线重建与验证

准备机制作的离线目录已包含所有源码分片、APT 缓存、基础镜像和运行镜像。迁入后：

```bash
docker load -i .cache/build-base-images.tar
docker load -i .cache/k8s-images.tar
make compile-image OFFLINE=1
docker run --rm --network none 192.168.10.35/xnet/xran-compile:latest bash /opt/xran/verify-compile.sh
```

`OFFLINE=1` 禁止准备步骤拉取基础镜像或更新 APT；缺少缓存或校验失败时直接报错。可同时指定内网 sources.list，
已有完整缓存可以用于断网安装。需要新增或调整 APT 依赖时，应在能够访问所选软件源的准备机上重新制作缓存和离线包。

验证脚本以镜像默认 UID/GID 1000 执行 protobuf 代码生成、主要 SDK 的编译/链接/运行、ASN.1 工具和共享库加载检查。
还会检查上述软件包的安装状态及实际文件校验值，并用标准 include/library 路径验证 protobuf-c 头文件与运行库一致。
完整 Jenkins 镜像构建、渲染和离线打包支持同样的 `IMAGE_NAME` / `IMAGE_TAG` 参数，定制版本时各命令应使用相同参数。

已部署环境可使用新 tag 验证这次兼容修复：
旧离线项目副本需先同步本次源码修改；修复镜像交付目录中的 `source.patch` 可在项目根目录通过
`git apply --check 补丁路径/source.patch` 检查，再用 `git apply 补丁路径/source.patch` 应用。

```bash
make compile-image OFFLINE=1 IMAGE_TAG=26.11.08-protobufc
docker run --rm --network none 192.168.10.35/xnet/xran-compile:26.11.08-protobufc bash /opt/xran/verify-compile.sh
make push-compile-image IMAGE_TAG=26.11.08-protobufc
```

迁移时拷贝 `dist/compile/192.168.10.35_xnet_xran-compile_26.11.08-protobufc/image.tar`，
在内网执行 `docker load -i 文件路径` 后推送同名镜像到 Harbor。
Jenkins 构建参数 `COMPILER_IMAGE` 填该完整新版本地址，拉取策略使用 `Always` 即可，无需重新部署 Jenkins。

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
所选镜像须兼容 UID 1000，包含 `/bin/sh`、Python、Git、编译工具和本项目 `/opt/xran/run-build.py`；
业务镜像模式还需 Docker CLI 及 `/opt/xran/base-images.tar`；xcn 还需 Helm、PyYAML 和更新后的发布适配器。
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
