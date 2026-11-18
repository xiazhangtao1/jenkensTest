# X-CN-CLOUD 发布构建

入口：Jenkins → **X-CN-CLOUD** → **Build with Parameters**，任务 ID 仍为 `xcn`。独立于 `x-ran-cloud`，共用六个构建槽位和磁盘压力回收。

升级前先完成运行中和排队的构建，并保留 Jenkins HOME 备份。初始化会将原发布任务原地迁移为 `xcn`，
保留原构建编号、日志、参数和归档；已有资源配置自动迁移，具体数值不恢复为默认值。重复启动不会重复迁移。
如果新旧任务同时存在，初始化会拒绝覆盖历史，由管理员先处理名称冲突。

历史记录中的 Git 引用、commit、编译镜像及校验和保留当时的真实值。业务仓库仍通过配置的地址拉取，
内网使用 `xcn.gitUrl: ssh://xiazhangtao@192.168.10.82:29418/ICT_CN`。

| 参数 | 默认值 / 用途 |
|---|---|
| COMPILER_IMAGE | `192.168.10.35/xnet/xran-compile:latest`，编译环境 |
| COMPILER_IMAGE_PULL_POLICY | `Always`，可选 `IfNotPresent`、`Never` |
| REF_TYPE | `branch`，可选 `tag`、`commit` |
| GIT_REF | `main`，支持完整或唯一短 commit SHA |
| CHART_VERSION | 当天上海日期，如 `26.11.6`；无前导零的 SemVer |
| APP_VERSION | 同上，可单独填写 Docker tag |

页面打开时填写当天版本；通过 API 省略或传空值时，按构建开始日期计算。历史 Parameters 和 metadata.json 均保存实际使用的版本，显式填写的版本保留（去除两端空白）。升级时，已有历史中的空版本只按该次构建元数据回填，不会使用今天的日期替换旧版本。

HTTP 访问时，历史 Parameters 页的复制按钮使用浏览器兼容方式；HTTPS 下保留 Jenkins 原生复制功能。

离线模式由管理员在 **Manage Jenkins → 系统配置 → 构建平台设置 → xcn 离线构建** 统一配置，默认开启，构建表单不提供 `OFFLINE`。勾选时给业务脚本增加 `--offline`；外网调试时管理员取消勾选。保存后对新入场构建立刻生效。

`config/site.yaml` 中的 `xcn.offline` 只提供首次启动的初始值。
普通用户和旧 API 客户端传入的 `OFFLINE` 不影响此配置。每次构建的实际模式仍记录在摘要和 `metadata.json` 中；已有历史按原值保留。
这里控制的是业务脚本的 `--offline`，与部署工具自身的 `--offline`（基础设施拉取策略和网络配置）分别设置。

外网闭环使用验收脚本默认的发布开发分支，由管理员将上述配置设为 `false`。该分支已有发布入口；目前 `main` 缺少此入口，选择它会明确失败，不会切换分支，也不修改业务仓库。

流水线精确检出后，在所选提交的 `helm` 目录执行：

```bash
./build-release.sh --offline 26.11.6 26.11.6
# 管理员关闭 xcn 离线构建时执行：
./build-release.sh 26.11.6 26.11.6
```

归档前校验四个文件：`xcn-runtime.tar`、`xcn-webui.tar`、`xcn-monitor.tar`、`xcn-<CHART_VERSION>.tgz`。
检查 tar 完整性、镜像引用、Chart/app 版本及默认镜像 tag；下载目录同时提供 `metadata.json` 和 `SHA256SUMS`。
记录完整 Git SHA、构建人、时间、编译镜像实际 ID、策略、版本、offline 标记、三个业务镜像 ID 和各自推送状态。

保持业务脚本原有语义：它会尝试推送 `192.168.10.35/xcn/xcn-{runtime,webui,monitor}:APP_VERSION`。
编译成功而推送失败时仍可下载四个有效产物，构建摘要、日志和元数据明确提示推送未全部成功。
同 APP_VERSION 的发布先串行等待，再申请全局槽位，避免两个构建同时覆盖相同镜像标签。

Pod 初始默认总上限为 4 CPU、8GiB 内存、20GiB 临时存储。DinD 为 **4GiB**，编译容器为 3584MiB，Agent 为 512MiB。
默认 xcn 的嵌套 Docker build 额度为 3GiB、并行 2，给 DinD daemon 留出空间；这些是不同层级的限制，不叠加成额外内存。管理员可在 [构建平台设置](platform-settings.md) 单独修改 xcn 资源配置。
嵌套容器挂在 DinD cgroup 下，共同受 4GiB 上限约束。X-RAN-CLOUD 镜像任务的 DinD 同样为 4GiB。

编译镜像包含 Helm 3.17.3、PyYAML 及原 SDK。Ubuntu/VPP 基础镜像由独立 `xcn-bases` init 容器复制到共享卷，每次导入自己的 DinD，不共享宿主 Docker socket。
离线包提供这些镜像以及包含 vendor 依赖的 xcn Git bundle；APT 包仍通过业务脚本指定的软件源安装，内网应确认 `192.168.10.36` 可达。

迁移时配置 `config/site.yaml` 的 `xcn.gitUrl`、Git/Registry 凭据及基础镜像。
`ubuntuImage`、`vppImage` 须与该业务提交 Dockerfile/build 脚本使用的镜像引用一致。

真实验证（默认使用 develop 和已导入镜像，构建模式跟随站点配置，不会修改业务源码）：

```bash
python3 tests/verify-xcn.py
# 接续已有长时间构建并检查下载产物：
python3 tests/verify-xcn.py --number 1 --online
```

脚本实际下载四个文件、校验 SHA256，执行 Helm lint/template，导入并断网检查三个镜像。
Chart 要求显式站点网络地址，渲染默认读取所选提交的 `helm/tests/site-values.yaml`，也可用 `--values /路径/values.yaml` 指定。
测试 values 只用于渲染检查，不部署业务。结果位于 `.state/jenkins/xcn-release.json`。
`--offline` / `--online` 仅断言预期模式，不会改变 Jenkins 后台配置。

默认版本保存与历史回填的专项验收使用 `python3 tests/verify-parameters.py --site /路径/验收-site.yaml`。
该脚本只接受带 `verificationInstance: true` 的独立命名空间，在槽位内跳过业务编译，并在结束时恢复流水线定义。

## cgroup v2 节点上的 DinD

镜像构建的第一条 `RUN` 若报 `cannot enter cgroupv2 ... with domain controllers -- it is in an invalid state`，
说明嵌套容器无法建立所需的资源控制组，此时 APT 命令还没有执行。
v2 的 domain 控制器要求父组不直接包含进程；旧启动脚本直接把含有 daemon 进程的 Kubernetes 容器组作为父组，
在暴露宿主 cgroup 命名空间的节点上可能触发此错误。参见 [内核约束](https://docs.kernel.org/admin-guide/cgroup-v2.html#no-internal-process-constraint)。

`runtime/dind-start.sh` 按实际挂载的层级区分 v1/v2，避免把混合模式中的 `0::` 误判成 v2。
v1 继续使用当前容器的 memory cgroup。v2 把该容器组内的 daemon 进程移入 `xran-daemon` 子组，
启用可用控制器，使用同一组内的 `xran-builds` 承载嵌套构建，并明确使用 cgroupfs 驱动。
两类子组仍继承原 DinD 容器的 CPU/内存限额，不把构建移到 Pod 之外；初始化失败时有界退出。

已有 Controller 22 的环境只需更新此脚本并重新部署，无需重做 Controller、编译镜像或 xcn 基础镜像：

```bash
# 在内网项目根目录执行；先将新 runtime/dind-start.sh 覆盖到同名路径。
sudo env "PATH=$PATH:$PWD/.cache/tools" make deploy SITE=config/site.local.yaml OFFLINE=1
```

新脚本由 ConfigMap 挂载到新构建 Pod。部署完成后重新提交失败的构建。
构建 Pod 存活时可以检查（POD 替换为本次 Pod 名）：

```bash
sudo kubectl -n jenkins logs POD -c dind --tail=100
sudo kubectl -n jenkins exec POD -c dind -- \
  docker -H unix:///var/run/xran/docker.sock info \
  --format 'CgroupVersion={{.CgroupVersion}} CgroupDriver={{.CgroupDriver}}'
```

v2 日志中的父路径应以 `/xran-builds` 结束，Docker 应报告 `2` 和 `cgroupfs`。
若提示 `controller is not delegated`，需检查节点是否给该容器层级开放了 cpu/memory 控制器。
当前准备机仅有 v1：已执行带 CPU/内存限额的真实嵌套 run/build 回归，v2 的宿主/私有命名空间有脚本测试，
v2 内核及真实内网业务构建仍需在目标节点复测。

## 内网 APT 源冻结时的基础镜像更新

如果 `libc6-dev` 要求 `2.35-0ubuntu3.8`，而基础镜像已安装 `libc6 2.35-0ubuntu3.13`，
说明基础镜像与软件源的版本不匹配。公共 `xran-compile` 是三个项目共用的编译环境；
XCN 的业务 Dockerfile 还会从自己的 Ubuntu/VPP 基础镜像开始安装依赖，这一层与公共编译镜像分别维护。

无法更新内网源时，本次提供官方 `ubuntu:jammy-20240111` 的独立离线归档，
其 linux/amd64 manifest、镜像 ID 和核心库版本记录在 `config/xcn-ubuntu.lock.json`。
该镜像的核心库版本不高于本次日志中的依赖要求，最终安装版本由内网源解析。
它是适配冻结源的候选基础镜像；准备机无法访问实际内网源，因此替换前必须在目标机预检。

只拷贝最新 `02-code` 和 `01-dependencies/optional-xcn-ubuntu/`。
先校验补包目录的 `SHA256SUMS`，更新代码时保留本地 `config/site.local.yaml`。
在已有 Jenkins 项目根目录执行（归档路径按实际拷贝位置替换）：

```bash
sudo make xcn-bases SITE=config/site.local.yaml OFFLINE=1 \
  APT_SOURCES=config/sources.intranet.list \
  XCN_UBUNTU_ARCHIVE=/data/jenkins-update/optional-xcn-ubuntu/ubuntu-jammy-20240111.tar.gz

sudo docker save -o /tmp/xcn-bases-2.tar xran-ci/xcn-bases:2
sudo ctr -n k8s.io images import /tmp/xcn-bases-2.tar

sudo env "PATH=$PATH:$PWD/.cache/tools" \
  make deploy SITE=config/site.local.yaml OFFLINE=1
```

`make xcn-bases` 只制作基础镜像载体。指定 `XCN_UBUNTU_ARCHIVE` 时先核对镜像 ID/架构，
再启动一次性容器读取指定软件源，执行 `apt-get update` 和代表性基础依赖的模拟安装。
`OFFLINE=1` 的预检直接访问内网源，不访问外网；这不是 `--network none`，也不在宿主机安装软件。
预检失败时停止，不替换已有归档或构建载体。预检通过后加载已有 `.cache/xcn-base-images.tar`，
保留其中的 VPP，替换 Ubuntu，原子保存新归档并断网构建 `xran-ci/xcn-bases:2`。
新的归档继续使用 `xcn.ubuntuImage` 指定的业务引用，公共 `ubuntu:22.04` 标签不变。

离线部署对载体镜像使用 `Never`，每个可运行构建 Pod 的节点都需要导入新镜像，之后再提交新的 XCN 构建。
无需重做 Controller 或公共编译镜像，也无需修改业务 Git 分支。
代表性依赖预检不等于完整发布验收；三个业务镜像和 Chart 仍需在内网重新构建验证。
原有 `xcn-bases:1` 和已运行 Pod 不会自动切换。回退可恢复原归档并将 `config/versions.env` 的载体版本改回 `1` 后部署。

准备机重新获取官方归档时沿用当前代理：

```bash
python3 scripts/fetch-image.py ubuntu:jammy-20240111 .cache/images/ubuntu_jammy-20240111.tar
```

来源：[Ubuntu 官方镜像](https://hub.docker.com/_/ubuntu)。版本解析规则见 [APT preferences](https://manpages.ubuntu.com/manpages/jammy/man5/apt_preferences.5.html)。
