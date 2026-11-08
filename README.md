# X-RAN-CLOUD / xcn Jenkins 构建平台

使用官方 Jenkins Helm chart 部署 Controller，按需创建 Ubuntu 22.04 编译 Pod。
支持 X-RAN-CLOUD 服务包、镜像及 xcn 发布包、分支/tag/commit、实名构建记录和下载，并交付可搬入内网的离线目录。

## 默认行为

| 项目 | 默认值 |
|---|---|
| 构建入口 | Jenkins → x-ran-cloud / xcn → Build with Parameters |
| 登录 | 个人本地账号，匿名不可查看和下载 |
| 并发 | 两项目合计最多 6 个；同镜像同标签 / xcn 同 app 版本先等待标签锁，再申请槽位 |
| 每个构建 Pod 总上限 | 4 CPU、8GiB 内存、20GiB 临时存储 |
| 编译镜像 | `192.168.10.35/xnet/xran-compile:latest`，每次构建可填写其他版本 |
| 编译镜像拉取策略 | `Always`，可选 `IfNotPresent` / `Never` |
| 数据 PVC | 独立 local-path，声明 250Gi，Retain，卸载保留 |
| 历史产物 | 不按天数、次数或固定总容量淘汰 |
| 清理 | 空间不足时删除最早完成、未标记保留的产物 |
| 定时磁盘维护 | 每两小时一次；启动时及构建、归档前另检查 |
| 最低余量 | 50GiB 与实际文件系统总容量 10% 中的较大值 |
| 提示 | 管理员首页摘要、构建日志、storage-maintenance 和持久清理审计 |

local-path 的容量声明不是实际配额。20Gi 临时空间不会预先占满；源码、中间文件、Docker 层、
导出 tar 和日志共同计入 Pod 上限，持久化归档另计。六个构建 Pod 总上限为 24 CPU/48GiB/120GiB，
实际调度受节点资源约束。管理员在 **Manage Jenkins → 系统配置 → 构建平台设置** 调整并发、三类 Pod 资源、默认编译环境、超时和磁盘下限，保存后新入场构建立即生效。详情见 [构建平台设置](docs/platform-settings.md)；[config/site.yaml](config/site.yaml) 负责首次默认值及基础设施配置。

## 当前环境安装

需要可访问集群的 kubeconfig、Docker、Python 3/PyYAML 和 make。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-yaml make git curl
cd /home/xiazhangtao/code/jenkensTest
make prepare
make images
make import-images
make deploy
```

首次准备依赖需要联网及可用代理，准备容器默认使用主机网络；实际 SDK Docker 构建始终使用 `--network none`。
下载缓存位于 `.cache/`，运行凭据位于 `.state/`，均不提交 Git。编译源码以校验归档及分片放在 `vendor/compile/`。
业务源码目录可通过 `make images XCN_SOURCE=/路径/ICT_CN` 指定。已有发布任务升级时原地迁移到 `xcn`，保留历史编号和归档。

## 一键制作编译镜像

```bash
make compile-image
make compile-image IMAGE_NAME=192.168.10.35/xnet/xran-compile IMAGE_TAG=26.11.05
make push-compile-image IMAGE_NAME=192.168.10.35/xnet/xran-compile IMAGE_TAG=26.11.05
```

默认镜像为 `192.168.10.35/xnet/xran-compile:latest`。支持 `BUILD_JOBS=4`、
`APT_SOURCES=config/sources.intranet.list` 和 `OFFLINE=1`；推送前按实际仓库执行 `docker login`。
镜像及校验清单导出到 `dist/compile/`。详细依赖、离线重建和镜像兼容要求见 [编译镜像说明](docs/compile-image.md)。

默认入口 `http://10.2.0.119:32080/`，可通过 jenkinsUrl 修改。初始账号为 admin、builder1、builder2，
随机密码保存于 `.state/jenkins/users.json`（0600）。通过 **Manage Jenkins → Users → Create User** 创建正式个人账号，登录后自动获得普通构建权限；
验收账号可在交付后停用。普通账号没有任务配置、脚本控制台和流水线重放权限。
`storage-maintenance` 及首页 CI/清理提示仅管理员可见；普通账号使用 `x-ran-cloud` 和 `xcn`，自动磁盘维护照常运行。

## 使用

以下为 X-RAN-CLOUD。xcn 独立入口、版本日期、管理员离线模式配置、四个产物及资源配置见 [xcn 发布构建](docs/xcn.md)。

1. 填写公共参数 COMPILER_IMAGE，选择 COMPILER_IMAGE_PULL_POLICY；分别默认为上述编译镜像和 Always。
2. 选择 BUILD_KIND=service 或 image。
3. 选择 REF_TYPE=branch/tag/commit，输入 GIT_REF；短 SHA 必须唯一。
4. 服务模式显示 SERVICE_TARGET（单个服务或 services_all）；镜像模式显示 IMAGE_TARGET 和 IMAGE_TAG。all 已取消。
5. 在该次构建的 Artifacts 下载文件，核对 SHA256SUMS 和 metadata.json。

摘要显示构建人、时间、引用、完整 commit 和所选编译镜像；元数据另含实际编译镜像 ID、拉取策略、耗时、产出镜像 ID/推送结果、大小和 SHA256。
镜像已导出但 push 失败时，构建失败且保留有效 tar。下载按构建号隔离，同名包不互相覆盖。
管理员在构建详情点击 **Keep this build forever** 可保护版本。

切换 BUILD_KIND 会立即隐藏无关字段并保留此前输入，Git 和编译镜像参数始终显示。
联动脚本随 Controller 镜像提供，离线可用；API 调用参数保持一致，流水线只读取当前构建类型需要的字段。
构建详情的只读 `Parameters` 页面也按该次 BUILD_KIND 筛选，包括已有历史构建；分支/tag/commit 字段保留显示。

模拟项目位于 ../X-RAN-CLOUD，提供 main、demo/feature、demo-v1.0.0 和历史 commit。
另提供带注释的 demo-v1.0.0-annotated tag。
镜像脚本实际执行编译、build、save、push；演示模式使用独立 Registry 和只读 Git 镜像。
更新模拟源码后重新构建/导入 demo 镜像并重启 xran-demo Deployment，刷新内网模拟 Git。

## 验证与迁移

```bash
make test
make bundle
```

`make verify SITE=/路径/验收站点.yaml` 会提交真实构建并测试并发和清理。
磁盘压力测试要求独立验收实例的配置包含 `verificationInstance: true`；日常仅验证功能可执行 `python3 tests/verify.py --quick`。
两项目共享资源的专用验证为 `python3 tests/verify-shared.py --site /路径/验收站点.yaml`，同样只允许独立实例。
离线目录位于 dist/offline/xran-jenkins-时间，含完整镜像、插件、chart、工具、源码 bundle、apt 缓存和校验清单，
不含运行密码和 SSH 私钥。
内网两个 Gerrit 地址已提供在 [站点示例](config/site.intranet.yaml)，`ICT_CN` 的外层仓库名不影响 xcn 构建路径。
内网首次部署、可选 SDK 重建及迁移已有历史见 [离线迁移](docs/offline.md)；仅复制 Git 仓库不足以离线启动。

- [离线迁移](docs/offline.md)
- [运维与备份恢复](docs/operations.md)
- [验证结果](docs/validation.md)

X-RAN-CLOUD 使用模拟仓库验收；xcn 使用验收脚本默认的发布开发分支，未改动业务源码或 main。
X-RAN-CLOUD 的真实服务清单、依赖、FROM 镜像和 Registry 路径须按内网项目补充。
业务仓库无需新增 Jenkinsfile，保持原构建脚本接口。

依据：[Jenkins Kubernetes](https://www.jenkins.io/doc/book/installing/kubernetes/)、
[官方 Helm chart](https://github.com/jenkinsci/helm-charts)、
[离线安装](https://www.jenkins.io/doc/book/installing/offline/)、
[local-path 容量说明](https://github.com/rancher/local-path-provisioner#cons)。
