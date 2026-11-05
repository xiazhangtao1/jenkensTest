# X-RAN-CLOUD Jenkins 构建平台

使用官方 Jenkins Helm chart 部署 Controller，按需创建 Ubuntu 22.04 编译 Pod。
支持服务包、镜像、分支/tag/commit、实名构建记录和下载，并交付可搬入内网的离线目录。

## 默认行为

| 项目 | 默认值 |
|---|---|
| 构建入口 | Jenkins → x-ran-cloud → Build with Parameters |
| 登录 | 个人本地账号，匿名不可查看和下载 |
| 并发 | 最多 6 个；同镜像同标签先等待标签锁，再申请槽位 |
| 每个构建 Pod 总上限 | 4 CPU、8GiB 内存、20GiB 临时存储 |
| 数据 PVC | 独立 local-path，声明 250Gi，Retain，卸载保留 |
| 历史产物 | 不按天数、次数或固定总容量淘汰 |
| 清理 | 空间不足时删除最早完成、未标记保留的产物 |
| 最低余量 | 50GiB 与实际文件系统总容量 10% 中的较大值 |
| 提示 | 首页摘要、构建日志、storage-maintenance 和持久清理审计 |

local-path 的容量声明不是实际配额。20Gi 临时空间不会预先占满；源码、中间文件、Docker 层、
导出 tar 和日志共同计入 Pod 上限，持久化归档另计。六个构建 Pod 总上限为 24 CPU/48GiB/120GiB，
实际调度受节点资源约束。配置入口为 [config/site.yaml](config/site.yaml)。

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

首次下载需要联网及可用代理；编译镜像使用主机网络访问当前代理。准备成功后再次执行复用缓存。
大文件位于 `.cache/`，运行凭据位于 `.state/`，均不提交 Git。

默认入口 `http://10.2.0.119:32080/`，可通过 jenkinsUrl 修改。初始账号为 admin、builder1、builder2，
随机密码保存于 `.state/jenkins/users.json`（0600）。通过管理员的 Users 页面创建正式个人账号；
验收账号可在交付后停用。普通账号没有任务配置、脚本控制台和流水线重放权限。
`storage-maintenance` 仅管理员可见和操作；普通账号使用 `x-ran-cloud`，自动磁盘维护照常运行。

## 使用

1. 选择 BUILD_KIND=service 或 image。
2. 选择 REF_TYPE=branch/tag/commit，输入 GIT_REF；短 SHA 必须唯一。
3. 服务模式仅显示 SERVICE_TARGET（serviceA/B/C、all、services_all）；镜像模式仅显示 IMAGE_TARGET 和 IMAGE_TAG。
4. 在该次构建的 Artifacts 下载文件，核对 SHA256SUMS 和 metadata.json。

摘要显示构建人、时间、引用和完整 commit；元数据另含耗时、镜像 ID/推送结果、大小和 SHA256。
镜像已导出但 push 失败时，构建失败且保留有效 tar。下载按构建号隔离，同名包不互相覆盖。
管理员在构建详情点击 **Keep this build forever** 可保护版本。

切换 BUILD_KIND 会立即隐藏无关字段并保留此前输入，REF_TYPE/GIT_REF 始终显示。
联动脚本随 Controller 镜像提供，离线可用；API 调用参数保持一致，流水线只读取当前构建类型需要的字段。

模拟项目位于 ../X-RAN-CLOUD，提供 main、demo/feature、demo-v1.0.0 和历史 commit。
另提供带注释的 demo-v1.0.0-annotated tag。
镜像脚本实际执行编译、build、save、push；演示模式使用独立 Registry 和只读 Git 镜像。
更新模拟源码后重新构建/导入 demo 镜像并重启 xran-demo Deployment，刷新内网模拟 Git。

## 验证与迁移

```bash
make test
make verify
make bundle
```

make verify 会向当前配置的验证实例提交真实构建，并测试并发和清理；正式环境请使用独立验收实例。
离线目录位于 dist/offline/xran-jenkins-时间，含完整镜像、插件、chart、工具、源码 bundle、apt 缓存和校验清单，
不含运行密码和 SSH 私钥。

- [离线迁移](docs/offline.md)
- [运维与备份恢复](docs/operations.md)
- [验证结果](docs/validation.md)

真实项目暂不可见，本轮以模拟项目验收。真实服务清单、依赖、FROM 镜像和 Registry 路径须按内网项目补充。
业务仓库无需新增 Jenkinsfile，保持原构建脚本接口。

依据：[Jenkins Kubernetes](https://www.jenkins.io/doc/book/installing/kubernetes/)、
[官方 Helm chart](https://github.com/jenkinsci/helm-charts)、
[离线安装](https://www.jenkins.io/doc/book/installing/offline/)、
[local-path 容量说明](https://github.com/rancher/local-path-provisioner#cons)。
