# 将临时验证环境升级为正式构建环境

当前版本为构建平台插件 1.0.5、Controller `xran-ci/controller:2.580.2-19`。
Controller 19 仅统一 xcn 与 X-RAN-CLOUD 的任务描述；已有部署须按 [升级 Jenkins](#升级-jenkins) 更新 Controller，编译镜像不变。
支持两个编译 Dockerfile，保留 `pkgconf`、DPDK 非交互输入、服务串行及取消 `all` 的修复；新增旧版业务镜像缺包自动导出和项目可见性开关。
此前 Controller 18 关闭离线页面的 Jenkins 使用统计请求，固定 local-path helper 镜像 tag，并将 xcn 基础镜像复制初始化容器从 128MiB 调整为 1GiB，避免文件缓存触发 OOM；新增 [完整回归与持续构建验收](reliability.md)。
此前平台测试与 root 兼容修复无需重建编译镜像；业务使用 zstd、hwloc 或 readline 时须按下面章节更新编译镜像。
初始化预算由部署脚本生成，更新代码后须执行 `make deploy` 更新模板；仅导入 Controller 或重启旧部署不会生成新模板。

## CLI 编译缺少 readline/readline.h（2026-11-10）

`fatal error: readline/readline.h: No such file or directory` 需要补装 `libreadline-dev`。
已有 `libreadline8` 运行库不包含开发头文件；`cli_client.o`、`cli` 缺失及后续复制失败都是此错误的结果。
公共额外依赖清单已增加开发包，两套 Dockerfile 都会安装，完整缓存同时包含其 `libncurses-dev` 等依赖。
镜像自检新增 readline 头文件编译、链接、从文件读取一行及历史记录 API 检查，不等待人工输入。

已有完整内网环境，本次同步两项：

1. 最新 `dist/transfer/02-code/`，校验并解包，保留内网 `config/site.local.yaml`。
2. 外网项目中完整的 `.cache/compile-extra-apt/`，包括全部 `.deb` 和 `manifest.json`，
   完整替换内网同名目录；将旧目录移走备份，不混合不同版本的包。

此次缓存同时保留此前新增的 zstd、hwloc 和其他额外依赖，不是只提供 readline 单包。
其他 APT 缓存、SDK 源码和基础镜像不变。全新部署用新版 `01-dependencies/` 和 `02-code/`；
本轮只有 `compiler-deps.tar.gz` 及依赖清单变化，其他依赖包可继续复用。

在内网项目根目录构建并推送新 tag：

```bash
sudo make compile-image OFFLINE=1 \
    APT_SOURCES=config/sources.intranet.list \
    DOCKERFILE=docker/builder-compat.Dockerfile \
    IMAGE_TAG=26.11.10-compat-readline
sudo make push-compile-image IMAGE_TAG=26.11.10-compat-readline
```

全套 SDK 版使用 `DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.10-sdk-readline`。
Jenkins 的 `COMPILER_IMAGE` 选择实际推送的完整新地址，例如
`192.168.10.35/xnet/xran-compile:26.11.10-compat-readline`，拉取策略选 `Always` 后重试。
无需重建 Controller 或重新部署 Jenkins；仅更新代码和缓存不会改变已推送的镜像。

本地 SDK 自检无法替代当前不可访问的私有业务编译。原 Makefile 的 `Error 1 (ignored)` 说明错误被忽略，
继续打印链接命令不代表前一步成功；现有业务脚本在 CLI 文件缺失时停止制作镜像是正确行为。
包说明见 [Ubuntu libreadline-dev](https://packages.ubuntu.com/jammy/libreadline-dev)。

## 业务编译缺少 hwloc.h（2026-11-10）

`fatal error: hwloc.h: No such file or directory` 表示编译环境缺少 `libhwloc-dev`。
上一轮额外依赖只补了 zstd，未包含 hwloc；按当时说明拷贝完整缓存并使用 `OFFLINE=1`，仍会遇到此错误。
这不是拷贝步骤或拉取策略的问题。`OFFLINE=1` 禁止下载，安装内容仍由软件清单和缓存决定。

现在公共额外依赖清单同时包含 `libzstd-dev`、`libhwloc-dev`，两套 Dockerfile 均会安装。
镜像验证新增 hwloc 头文件编译、链接和合成拓扑运行检查，缺少开发包时不能通过验证。
合成拓扑检查不执行绑核，也不依赖宿主机 CPU 数量。

**已经拷贝过完整编译依赖的内网目录，本次只需同步两项：**

1. 最新 `dist/transfer/02-code/`，校验并解包代码，保留内网 `config/site.local.yaml`。
2. 外网项目中的完整 `.cache/compile-extra-apt/`，包括全部 `.deb` 和 `manifest.json`，
   替换内网项目的同名目录。替换前将旧目录移走备份，不要将新旧 `.deb` 混放。

`.cache/compile-apt/`、`.cache/compile-compat-apt/`、SDK 源码及基础镜像未变，不必重复拷贝。
全新部署仍使用新版 `01-dependencies/` 和 `02-code/`；其中本轮只有 `compiler-deps.tar.gz` 及依赖清单变化。
历史 `optional-zstd-dev/` 小补包仅补 zstd，不包含 hwloc，不能解决本次错误。

在内网项目根目录重建并推送新的编译镜像 tag：

```bash
sudo make compile-image OFFLINE=1 \
    APT_SOURCES=config/sources.intranet.list \
    DOCKERFILE=docker/builder-compat.Dockerfile \
    IMAGE_TAG=26.11.10-compat-hwloc
sudo make push-compile-image IMAGE_TAG=26.11.10-compat-hwloc
```

全套 SDK 版使用 `DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.10-sdk-hwloc`。
在 Jenkins 的 `COMPILER_IMAGE` 填实际推送的新地址，例如
`192.168.10.35/xnet/xran-compile:26.11.10-compat-hwloc`，策略选 `Always` 后重新构建。
**本次不用重建 Controller，也不用重新部署 Jenkins。** 仅替换代码或缓存，不会改变 Harbor 中已推送的编译镜像。

镜像验证和实际业务使用同一 tag；可再次检查：

```bash
sudo docker run --rm --network none \
    192.168.10.35/xnet/xran-compile:26.11.10-compat-hwloc \
    bash /opt/xran/verify-compile.sh
```

该检查覆盖已知 SDK 依赖，不能替代当前不可访问的内网私有业务编译。
包说明见 [Ubuntu libhwloc-dev](https://packages.ubuntu.com/jammy/libhwloc-dev)。

## 内网 APT 依赖冲突与小补包（2026-11-10）

`apt update` 成功后，若出现 `libc6-dev` 要求 `2.35-0ubuntu3.8`、实际选中 `libc6 2.35-0ubuntu3.13`，
以及 OpenSSL、Perl 等类似严格版本冲突，说明当前源、基础镜像与 APT 选中的包版本不能组成完整开发环境。
`gcc is not installable` 需要检查 GCC 依赖链的候选版本；`held broken packages` 本身不能证明人为设置了 hold。
不能用单纯重试安装来补齐缺失的配套版本；直接使用该源全新安装开发环境，需要先补齐一致的软件仓库或选择相容基础镜像。

旧脚本在 `OFFLINE=0` 下切换源地址会重新准备全部 APT 依赖。现已修复：同基础镜像、同软件清单、所有文件校验通过时跨源复用缓存，
不会因 URL 不同刷新整套依赖；新增包、更换基础镜像或损坏文件仍不会复用不匹配的缓存。APT 准备失败保留原缓存。
仅临时手改旧脚本时，将 `scripts/compile-image.py` 中的 `matching_inputs = ...` 改为 `matching_inputs = same_packages` 即可；
本项不替代新增 zstd 所需的 `.deb`。如果本地手动添加了 `openssh`，请核对清单；本仓库安装 SSH 客户端使用 `openssh-client`。

**已有可用编译镜像，本次只需补 zstd 时，优先用约 400KB 的小补包。** 外网准备机已提供：

```text
dist/transfer/01-dependencies/optional-zstd-dev/
```

仅复制该小目录到内网，进入目录后执行。`COMPILER_IMAGE` 填之前成功使用或此次缺头文件任务实际使用的镜像，输出为新 tag：

```bash
sha256sum -c SHA256SUMS
sudo docker build --network none --pull=false \
    --build-arg COMPILER_IMAGE=192.168.10.35/xnet/xran-compile:latest \
    -t 192.168.10.35/xnet/xran-compile:test-zstd .
sudo docker push 192.168.10.35/xnet/xran-compile:test-zstd
```

该 Dockerfile 使用 `dpkg --install` 只安装本地 `libzstd-dev`；不访问 APT，不重新安装 GCC/libc 等包。
包为 Ubuntu amd64 `1.4.8+dfsg-3build1`，要求现有镜像中的 `libzstd1` 同版本；不匹配时明确失败。
构建内会编译、链接并运行 C 压缩解压检查；Jenkins 选择完整新 tag、`Always` 后重试，Controller 无需调整。
该可选目录不替代通用离线依赖包，不属于全新基础 SDK 构建输入。

若要从正式两个 SDK Dockerfile 重建，复制新版 `02-code` 和完整编译依赖，使用：

```bash
sudo make compile-image OFFLINE=1 \
    APT_SOURCES=config/sources.intranet.list \
    DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.10-compat-zstd
```

原完整编译缓存已存在时，新增 zstd 可只同步 `.cache/compile-extra-apt/` 的完整新版目录及其 manifest，
替换前将旧目录移走备份；不要把不同版本的 `.deb` 混在同一目录。其余依赖不变。
已有内网源但没有完整缓存时，只有确认源与基础镜像能解析全部所需开发包，才使用 `OFFLINE=0` 准备。

参考：[APT 选项](https://manpages.ubuntu.com/manpages/jammy/man8/apt-get.8.html)、
[Ubuntu SSH 客户端包](https://packages.ubuntu.com/jammy/openssh-client)。

## 业务编译缺少 zstd.h（2026-11-10）

`fatal error: zstd.h: No such file or directory` 表示编译环境缺少 `libzstd-dev`。
此前两套 r2 镜像只有 `libzstd1` 运行库；公共额外依赖清单现已补上开发包，两套 Dockerfile 均会安装。
自检新增 C 语言的头文件、链接、压缩解压验证。业务 Makefile 的 `Error 1 (ignored)` 表示忽略了错误，继续输出日志不代表编译成功。

本项需更新编译镜像，Controller 和 Jenkins 部署无须调整。完整编译依赖缓存齐全时，
同步代码和缓存后使用断网重建：

```bash
sudo make compile-image OFFLINE=1 \
    APT_SOURCES=config/sources.intranet.list \
    DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.10-compat-zstd
sudo make push-compile-image IMAGE_TAG=26.11.10-compat-zstd
```

`OFFLINE=1` 不访问 APT；原有基础镜像、SDK 源码、工具缓存及更新后的额外 APT 缓存需已备齐。
全套 SDK 版可改用 `DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.10-sdk-zstd`。
在 Jenkins 的 `COMPILER_IMAGE` 选择实际推送的完整新 tag，策略选 `Always`，重新提交构建。

全套缓存还原可同步本次更新的 `01-dependencies/compiler-deps.tar.gz`、`MANIFEST.json` 和 `SHA256SUMS`，
以及 `02-code`；其他依赖包未变。先校验、解包新版编译依赖，再执行上述构建命令并改为 `OFFLINE=1`。
只更新代码、继续使用旧 `.deb` 缓存时，会因依赖清单不匹配被明确拒绝。

内网源可正确解析 zstd 单包的依赖时，也可用现有镜像直接从源补包。以下 `FROM` 应填写失败任务实际选择的编译镜像，使用新的输出 tag：

```bash
cat > /tmp/Dockerfile.zstd <<'EOF'
FROM 192.168.10.35/xnet/xran-compile:latest
USER root
COPY config/sources.intranet.list /etc/apt/sources.list
RUN apt-get update \
    && apt-get install -y --no-install-recommends libzstd-dev \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000
EOF
sudo docker build --network host --pull=false -f /tmp/Dockerfile.zstd \
    -t 192.168.10.35/xnet/xran-compile:test-zstd .
sudo docker push 192.168.10.35/xnet/xran-compile:test-zstd
```

将 Jenkins 的 `COMPILER_IMAGE` 改为 `192.168.10.35/xnet/xran-compile:test-zstd` 后重试。
临时 Dockerfile 直接访问内网 APT，不使用 `make compile-image OFFLINE=1` 的断网构建阶段。

## 业务镜像脚本要求 root 的兼容修复（2026-11-10）

出现 `Loaded image: ubuntu:22.04` 后紧接“请以 root 权限运行此脚本”，说明编译镜像已经启动，业务脚本被 UID 1000 拦住。
随后“缺少镜像包，且本次 DinD 中找不到镜像”是脚本提前退出的结果。Harbor 拉取和编译依赖与这个权限错误无关。

现在仅 X-RAN-CLOUD 镜像任务的 Agent、编译容器使用 UID/GID 0；服务包和 xcn 继续使用 1000。
两容器保持相同 UID，兼容私钥临时文件、共享工作区及权限为 0600 的镜像包；SSH 信任 Secret 相应挂载到 `/root/.ssh`，严格校验不变。
builder 不新增 privileged 或宿主机挂载，仍连接本次 Pod 的独立 DinD。

**已有 Controller 19 和两套 r2 编译镜像不需要重建、导入或推送。** 本项只需更新 `02-code` 中代码，保留本地站点配置后执行：

```bash
sudo env "PATH=$PATH:$PWD/.cache/tools" \
    make deploy SITE=config/site.local.yaml OFFLINE=1
```

部署会按完整运行配置的哈希自动滚动更新 Controller，再次提交镜像任务即可；无需卸载。
升级前等待在运行的构建结束。更早的 Controller 若还未升级，仍按后文完成对应版本升级。

不方便传文件时，可先在现有 `scripts/ci.py` 的 `pod_template(cfg, kind)` 中手动验证四处修改：

1. 在 `shared = ...` 前增加 `build_uid = 0 if kind == 'image' else 1000`。
2. builder 的 `securityContext` 中将 `runAsUser` 和 `runAsGroup` 的 `1000` 均改为 `build_uid`。
3. 最后返回的 Pod `spec.securityContext` 中同样将两处 `1000` 改为 `build_uid`，`fsGroup` 保留 `1000`。
4. builder 的 `build-trust` 挂载路径改为 `'/root/.ssh' if kind == 'image' else '/home/jenkins/.ssh'`。

手动验证版没有新增运行配置哈希，部署后还需执行一次 `sudo kubectl -n jenkins rollout restart statefulset/jenkins`，
再执行 `sudo kubectl -n jenkins rollout status statefulset/jenkins` 等待就绪。不要删除脚本的 root 检查或把所有任务都改成 root。

## DPDK 与内网构建目标更新（2026-11-09）

本次在共享 DPDK 安装脚本中显式固定 `max_lcores=256`，并增加边界自检。
内网模板改为 40 个服务目标（39 个单服务和 `services_all`），默认 `services_all`；
镜像目标改为 `usp-core`、`xran-simuphy`，默认 `usp-core`，对应仓库为
`192.168.10.35/xnet/usp-core:<tag>`、`192.168.10.35/xnet/xran-simuphy:<tag>`。
单服务仍校验 `<服务名>.tar.gz`；总包仍校验唯一的 `L2L3_时间_commit前9位.tar.gz`。
外网 `config/site.yaml` 的演示目标不变。

仅更新 DPDK 参数和目标列表时无需更换此前 Controller 18；当前代码另外包含 Controller 19 的 xcn 文案更新，
已有站点搬运新的 **02-code** 后，按本文升级 Controller 即可，或另带 Controller 成品。
插件、编译镜像和外部依赖未变，无需重复搬运依赖、编译镜像。
在已有内网目录更新代码后，只把这三个目标配置合入本地站点文件，保留节点地址、Git、凭据 ID 及其他配置值：

```bash
python3 - <<'PY'
from pathlib import Path
import yaml

path = Path('config/site.local.yaml')
site = yaml.safe_load(path.read_text())
template = yaml.safe_load(Path('config/site.intranet.yaml').read_text())
for key in ('services', 'images', 'imageRepositories'):
    site[key] = template[key]
path.write_text(yaml.safe_dump(site, allow_unicode=True, sort_keys=False))
PY
sudo env "PATH=$PATH:$PWD/.cache/tools" \
    make deploy SITE=config/site.local.yaml OFFLINE=1
```

部署后 Jenkins 重启加载目标列表，无需卸载；PVC 中的账号、构建历史和网页保存的平台设置继续保留。
上面的合并命令会重新排版 YAML，原注释不保留。新部署直接复制内网模板，再填写实际节点等配置即可。

本地两套 r2 镜像已用新版断网自检确认都是 256；不更改其已有 tag 或内容。
内网 `latest`/`test` 的实际内容需要在现场核对，可只读挂载新自检到已经拉取或导入的镜像：

```bash
COMPILER_IMAGE=192.168.10.35/xnet/xran-compile:latest
sudo docker run --rm --network none \
    --mount "type=bind,source=$PWD/runtime/compile-check,target=/opt/xran/compile-check,readonly" \
    --mount "type=bind,source=$PWD/runtime/verify-compile.sh,target=/opt/xran/verify-compile.sh,readonly" \
    "$COMPILER_IMAGE" bash /opt/xran/verify-compile.sh
```

若实际镜像为 128，新自检会明确失败；使用新代码和已有依赖按需离线重建，并用新 tag 测试：

```bash
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.09-compat-dpdk256
sudo make push-compile-image IMAGE_TAG=26.11.09-compat-dpdk256
# 全套 protobuf-c 1.5.2 对照版可另行选择：
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.09-sdk-dpdk256
sudo make push-compile-image IMAGE_TAG=26.11.09-sdk-dpdk256
```

在 Jenkins 的 `COMPILER_IMAGE` 选择实际推送的新 tag，拉取策略按需选择（默认 `Always`）。
无需在临时构建 Pod 中手动运行 Meson/Ninja。以下章节是完整平台升级流程，尚未完成此前升级时再按需执行。

## 同步源码和缓存

联网环境正常更新 Git。离线更新按 [分类搬运](transfer.md) 交付，外部依赖与代码分开。
先在内网确认实际代码基线；外网执行 `make transfer-code BASE_COMMIT=该基线` 生成 `02-code/code.patch`，
例如 `23777c1` 或 `b9773ef`。历史临时改动需与正式修改合并。

```bash
cd /交付路径/02-code
sha256sum -c SHA256SUMS
cd /实际路径/jenkensTestOffline
git diff > /tmp/jenkins-local-before-formal.patch
git rev-parse --short HEAD
git apply --check /交付路径/02-code/code.patch
git apply /交付路径/02-code/code.patch
cp /交付路径/02-code/CODE.json TRANSFER_SOURCE.json
```

如果检查提示冲突，先保留临时文件副本，再合并正式修改；不要直接覆盖本地 `config/site.local.yaml`。
补丁不携带 Jenkins HOME、私钥或密码，也不重复携带 SDK 源码大文件。
先将新的 `01-dependencies/MANIFEST.json` 带入内网，用 `scripts/transfer.py check` 检查，再只拷缺少或变化的依赖包。
解开相应依赖包到项目根目录；同一类缓存以校验清单为准，不混用旧 manifest 和新 deb。
已有相同基础镜像和依赖可以继续使用，完整分类和命令见分类搬运文档。

## 制作和测试两版编译镜像

```bash
sudo docker load -i .cache/build-base-images.tar
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.10-compat-zstd
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.10-sdk-zstd
sudo make push-compile-image IMAGE_TAG=26.11.10-compat-zstd
sudo make push-compile-image IMAGE_TAG=26.11.10-sdk-zstd
```

Harbor 需要登录时，使用与推送相同的用户登录；上述 sudo 示例对应 `sudo docker login 192.168.10.35`。

默认镜像名称为 `192.168.10.35/xnet/xran-compile`；名称和 tag 都可另行指定。
构建命令会自动断网验证镜像。每个 tag 的导出包位于 `dist/transfer/03-generated-images/compiler/镜像引用转换后的目录名/image.tar`，
已有成品也可直接 `docker load` 后推送到 Harbor。两版是完整编译环境，切换只需在构建参数填写对应 `COMPILER_IMAGE`。

完成兼容版验收后，如果要继续使用默认的 `latest` 地址：

```bash
sudo docker tag 192.168.10.35/xnet/xran-compile:26.11.10-compat-zstd 192.168.10.35/xnet/xran-compile:latest
sudo make push-compile-image IMAGE_TAG=latest
```

## 升级 Jenkins

直接使用交付的新 Controller 镜像时：

```bash
sudo ctr -n k8s.io images import /交付路径/03-generated-images/controller/controller-image.tar.gz
sudo env "PATH=$PATH:$PWD/.cache/tools" make deploy SITE=config/site.local.yaml OFFLINE=1
```

如需在内网自行制作 Controller，使用已有 Maven/插件缓存：

```bash
sudo make platform-plugin OFFLINE=1
sudo env DOCKER_BUILDKIT=0 docker build --network none --pull=false \
  -f docker/controller.Dockerfile -t xran-ci/controller:2.580.2-19 .
sudo docker save -o /tmp/xran-controller-2.580.2-19.tar xran-ci/controller:2.580.2-19
sudo ctr -n k8s.io images import /tmp/xran-controller-2.580.2-19.tar
sudo env "PATH=$PATH:$PWD/.cache/tools" make deploy SITE=config/site.local.yaml OFFLINE=1
```

每个可能运行 Controller 的节点都要导入新镜像。使用内网镜像仓库分发时按站点配置处理。
这是 Helm 升级，无需卸载；已有 PVC、用户、构建历史和 Secret 保留。正常重启后同样保留。
升级前等待正在运行的构建完成，再按现有备份流程备份 Jenkins HOME。

管理员系统配置中的“X-RAN-CLOUD 服务编译串行执行”默认开启；旧配置只补上缺失字段。
管理员已保存的 CPU、内存及全局并发数不会被站点默认值覆盖。
单次服务编译串行与 Jenkins 同时运行多个任务分别控制；服务 Pod 默认 8GiB，全局默认最多 6 个任务。

新增“X-RAN-CLOUD 仅管理员可见”和“xcn 仅管理员可见”开关，默认沿用原权限；开启后立即限制访问，取消后恢复原授权。
权限备份与开关随 HOME 保存。若退回旧 Controller，先在新版本取消开关恢复原授权。

## 现场验收

先选择兼容版和 `Always`，分别验证单个服务及 `services_all` 的下载包、SHA256、commit、构建人和时间。
再切换到 SDK 版测试真实业务；如果失败，切回兼容版即可。
两版均默认启用服务串行执行。业务 Makefile 并行依赖尚未修复前，保持该开关开启。
检查业务镜像构建及 xcn 四个产物，确认平台升级未影响已有任务。

镜像任务分别测试正常出 tar 和旧版只留本地镜像的情况，检查 `imageArchiveSource`、下载包和 SHA256。
自动导出按站点 `registry`、`imageRepositories` 和本次 tag 精确匹配，配置需与脚本一致，示例映射需按内网实际修改。
用普通账号确认受限项目的历史、日志和产物均不可访问；取消后原授权恢复，重启后状态保持。
