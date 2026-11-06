# 运维与存储

管理员通过 **Manage Jenkins → 系统配置 → 构建平台设置** 修改并发、三类 Pod 资源、默认编译环境、Open5gs 离线模式、超时和磁盘下限，无需重新部署。生效时机、缩容与恢复规则见 [构建平台设置](platform-settings.md)。

## 历史回收

`storage-maintenance` 每两小时执行一次，Cron 为 `H H/2 * * *`，分钟和小时由 Jenkins 哈希错开。
Jenkins 启动时另执行一次；每次构建及归档前也检查实际空间。没有按天数、次数或固定产物总容量淘汰。
容量预留串行协调并持久化，重启排除已结束构建留下的预留；归档前按实际文件大小再次检查。
临时盘与数据盘不同且临时盘不足时，不会通过删除另一块盘的产物误处理。

压力时按完成时间删除最早的未保留归档。使用 Jenkins ArtifactManager API，保留轻量构建信息和清理标记。
运行中及 Keep forever 构建不参与回收，只处理 x-ran-cloud 和 open5gs，不清理宿主机其他目录。

清理提示在首页、触发任务日志、storage-maintenance 和数据卷 xran-storage/cleanup.jsonl 中，
记录构建号、commit 摘要、字节数和前后余量。无法满足安全空间时拒绝继续构建。
`storage-maintenance` 仅具有 Overall/Administer 权限的管理员可见和操作，普通账号无法通过
首页、直链或 API 查看和触发。定时维护以 Jenkins 系统身份运行，不受此限制。
首页 CI 说明和最近清理摘要同样仅管理员可见，来自受限维护任务；不再使用所有账号可见的 systemMessage。
最近清理摘要保存在维护任务描述中，重启后保留。构建日志和历史产物清理标记仍按原规则供构建使用者查看。
日志、历史元数据也占用空间；没有可删产物时，由管理员根据审计决定扩容或解除保留。

```bash
kubectl -n jenkins get pods,pvc
kubectl -n jenkins exec jenkins-0 -- df -h /var/jenkins_home
kubectl -n jenkins exec jenkins-0 -- cat /var/jenkins_home/xran-storage/reservations.json
kubectl -n jenkins exec jenkins-0 -- tail -20 /var/jenkins_home/xran-storage/cleanup.jsonl
```

20Gi 是整个构建 Pod 上限，工作区/Docker 缓存共用总预算，超限可能被 kubelet 驱逐。
默认镜像 Pod 配额拆分为编译 2.5 CPU、Docker 1.25 CPU、Agent 0.25 CPU，总计仍为 4 CPU；
当前集群的整数 CPU 专核分配不会被这三个容器触发。资源总量受节点调度和其他业务占用约束。
250Gi 声明不会为 local-path 创建配额；更换具有容量限制的存储驱动后须重新核对容量。
演示 Registry 使用独立 PVC。Registry 层不会随 Jenkins tar 归档删除，需单独管理空间和垃圾回收；
同标签覆盖后可产生无引用层，须按 Registry 官方流程在停止推送或只读状态下 GC。

## 服务器重启与数据持久化

Jenkins HOME 挂载独立 local-path PVC，包含账号、凭据、任务、构建日志、历史产物、构建平台设置与审计。
同一服务器正常重启、Controller Pod 重建会重新挂载原 PVC，已完成构建和配置保留；运行中的编译 Pod 使用
emptyDir 临时工作区，服务器重启可能使该次构建中断，需要重新构建，不能保证正在执行的编译继续。

StorageClass 使用 Retain，PVC 有 Helm keep 标记；数据实际位于节点磁盘，并非其他节点上的副本。
必须确保 local-path 指向的数据盘先挂载，再启动 kubelet/containerd；节点故障、磁盘损坏或手工删除数据目录
需要从备份恢复。仅搬运源码和通用离线包不会带走运行中的 Jenkins HOME。

```bash
kubectl -n jenkins get pvc jenkins -o wide
kubectl get pv "$(kubectl -n jenkins get pvc jenkins -o jsonpath='{.spec.volumeName}')" -o yaml
```

## 同事账号

管理员打开 **Manage Jenkins → Users → Create User（管理 Jenkins → 用户 → 新建用户）**，
为每位同事设置独立用户名、姓名、密码和邮箱。当前使用 Jenkins 本地用户数据库，自助注册关闭，不依赖外网或邮件激活。

新账号登录后自动继承 authenticated 的构建权限，可查看两个业务任务、发起/取消构建及下载产物；
构建元数据记录本人的 Jenkins 用户名。无需逐人修改流水线或配置 Git 私钥。
普通账号不能管理系统、修改流水线、使用脚本控制台或查看 storage-maintenance。
业务仓库统一使用管理员配置的 SSH 凭据，Jenkins 登录账号与 Gerrit 账号分开管理。

个人账号和密码哈希存放在 Jenkins HOME，随 PVC 和完整备份保留。`.state/jenkins/users.json` 只记录初始
admin、builder1、builder2 的密码，不是后续全部账号的名单。初始账号密码由 xran-users Secret/JCasC 管理，
修改这些初始账号密码时应同步 Secret；新增普通个人账号无需修改 Secret。
如需额外管理员，必须将该账号明确加入部署配置的 Overall/Administer 授权并持久化，不能只依赖一次网页授权。

## 备份恢复

先完成或取消运行/等待锁的任务。脚本在停止接收新任务后检查仍有构建则拒绝备份，并恢复接收。

```bash
python3 scripts/ci.py backup --archive /mnt/backup/jenkins-home.tar.gz
```

备份期间停止 Controller，由临时 Pod 挂同一 PVC 执行，结束后恢复 Controller。
包含账号、凭据密钥、任务、历史、产物、清理记录、构建平台设置及修改审计，0600 权限，建议保存到另一块物理磁盘。
同时保管 users.json 或 xran-users Secret，确保恢复后的 JCasC 初始账号密码一致。
Registry 数据卷单独备份，不包含在 Jenkins_HOME 归档中。

恢复只允许空 PVC，防止混入旧任务/凭据。目标 namespace、xran-config、xran-users 配套资源须先创建，
使用目标站点配置，将相同 chart/镜像安装为零副本：

```bash
python3 scripts/ci.py render --site config/restore-site.yaml --offline
# render 不会访问集群；以下准备 namespace、ConfigMap 和已保存的初始账号 Secret。
# 先将原 users.json 安全地放到 .state/目标namespace/users.json，权限设为 0600。
python3 - <<'PY'
import argparse
import sys
sys.path.insert(0, 'scripts')
import ci
cfg = ci.config(argparse.Namespace(site='config/restore-site.yaml', namespace=None, offline=True))
ci.create_namespace(cfg)
ci.users_secret(cfg)
ci.kubectl('apply', '-f', ci.state_dir(cfg) / 'resources.yaml')
PY
helm upgrade --install jenkins .cache/charts/jenkins-5.10.10.tgz -n jenkins-restore \
  -f .state/jenkins-restore/values.yaml --set controller.replicas=0
python3 scripts/ci.py restore --site config/restore-site.yaml \
  --offline --archive /mnt/backup/jenkins-home.tar.gz
kubectl -n jenkins-restore rollout status statefulset/jenkins --timeout=600s
```

恢复的临时 init 容器仅以 root/CHOWN 权限调整目标 PVC 根目录属主，随后由 UID 1000 解包，
不挂载宿主机目录。失败时 Controller 保持 0 副本，检查日志后使用新的空 PVC 重试。
运行账号 JSON 放到 `.state/目标命名空间/users.json` 并设为 0600，以便管理脚本继续使用原账号。

恢复后核对账号、指定构建 metadata 和实际下载校验和。升级前保留旧镜像与离线目录，
核心/插件不兼容时恢复与旧版本匹配的数据备份。

## 镜像导入

make import-images 优先使用免交互 sudo ctr；本机无 sudo 密码时使用一次性导入 Pod。
它只挂本项目缓存目录和 containerd socket，用已缓存 glibc 镜像运行 ctr，完成即删除。
本机识别现有 xcn-runtime；其他单节点可指定 --importer-image，正式多节点请逐节点由管理员导入。
