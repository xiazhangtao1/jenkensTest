# 内网离线迁移

目标已有 Kubernetes/containerd 和 Ubuntu 22.04 amd64，不包含集群安装。
外部依赖、代码和可选的自建镜像分别交付；启动时不安装插件，临时 DinD 加载预置基础镜像。

## 搬运哪些内容

推荐先阅读 [分类搬运](transfer.md)：`make transfer-deps` 准备固定外部输入，`make transfer-code` 准备小型代码包，
在 `dist/transfer/01-dependencies` 和 `02-code` 分别生成。先携带依赖清单检查内网已有内容，只拷缺少或变化的包。
准备在内网自行构建时不拷贝 `03-generated-images`，依赖还原后按分类文档运行 `make images SITE=config/site.local.yaml OFFLINE=1`。
生产站点关闭 demo，不需要本地模拟业务库。仅复制 Git 仓库仍不足以离线部署，需准备上游镜像、插件、chart 和构建缓存。
`make bundle` 仍可生成含成品的完整快照，适用于明确需要一起搬运的场景。

如果还要保留外网 Jenkins 的账号、历史包和网页设置，另做 Jenkins HOME 备份，并单独搬运
`.state/jenkins/users.json`（0600）及所需 Kubernetes Secret。运行数据和私钥不放入通用离线包。

## 校验和导入

```bash
cd /data/jenkensTest
# 先按分类文档还原并校验依赖，再在内网完成自建镜像。
sudo apt-get install -y python3 python3-yaml make
export PATH="$PWD/.cache/tools:$PATH"
sudo ctr -n k8s.io images import .cache/k8s-images.tar
```

每个可能运行 Controller、构建、演示或存储辅助 Pod 的节点都须导入。K3s 使用其自带 ctr 和正确 socket。
包内 kubectl 为验证环境 v1.26.8；目标集群跨大版本时优先使用与其兼容的已有客户端。
离线目录可直接 rsync 或用移动硬盘搬运，大型归档不放 Git/Git LFS。

没有 local-path 时才安装包内清单，已有实例复用：

```bash
kubectl apply -f vendor/local-path-storage.v0.0.37.yaml
kubectl -n local-path-storage rollout status deployment/local-path-provisioner
```

清单来源 rancher/local-path-provisioner v0.0.37，辅助镜像固定 busybox:1.37.0。
默认数据目录 /opt/local-path-provisioner，安装前按实际数据盘修改 ConfigMap。
Jenkins 使用独立 Retain StorageClass，已有默认类的回收策略保持原配置。

## 站点配置

使用已提供的内网示例，保留当前外网配置不动：

```bash
cp config/site.intranet.yaml config/site.local.yaml
editor config/site.local.yaml
```

必须将 `JENKINS_NODE_IP` 改成实际 Jenkins 节点 IP，并核对 NodePort、StorageClass、服务/镜像清单、
Registry 仓库映射和拉取凭据。内网示例已包含确认的 40 个服务目标及 `usp-core`、`xran-simuphy` 两个镜像目标；
`config/site.yaml` 保留外网演示名称。新增业务服务按 [新增服务构建目标](operations.md#新增服务构建目标) 修改实际本地站点文件后部署。
示例已设置 `demo: false`、部署离线模式和 xcn 首次默认离线模式。

两个源码地址分别为：

| Jenkins 配置项 | 内网地址 |
| --- | --- |
| `gitUrl` | `ssh://xiazhangtao@192.168.10.82:29418/X-RAN-CLOUD` |
| `xcn.gitUrl` | `ssh://xiazhangtao@192.168.10.82:29418/ICT_CN` |

两项目都检出到工作区 `source/`，脚本通过相对路径执行，不依赖远端仓库外层名字。
Jenkins 业务任务名仍为 `x-ran-cloud` 和 `xcn`，保留各自历史；无需改成 `ICT_CN`。
分支/tag/commit 选择方式与外网一致，默认 main；内网 main 必须已有 `helm/build-release.sh` 等所需入口。
内网模板已默认使用 HTTPS Harbor：顶层和 `xcn.registryInsecure` 均为 `false`，并启用 `caSecret: xran-registry-ca`。
使用该模板时，须在首次构建前按下文创建含 `ca.crt` 的同名 Secret；否则构建 Pod 无法挂载证书。
已有 `config/site.local.yaml` 不会被更新模板自动覆盖，需同步这三个字段；`make deploy SITE=config/site.local.yaml` 读取本地站点文件。
proxy 通常留空，noProxy 包含 API、Pod/Service CIDR、内网域名与 apt 源。

首次部署完成后，管理员在 **Manage Jenkins → Credentials → System → Global credentials → Add Credentials**
新增 `SSH Username with private key`：ID 为 `intranet-gerrit-ssh`，Username 为 `xiazhangtao`，
私钥对应公钥须已登记到 Gerrit 的该账号，并拥有两个库的读取权限。两个项目可以共用该凭据。
私钥口令填到凭据的 Passphrase；不要把私钥写入 YAML 或 Git。HTTPS 仓库才使用用户名/token。

SSH 主机公钥必须按非默认端口收集并通过 Gerrit 管理员等可信渠道核对 SHA256 指纹，
核对后的 known_hosts 中主机项应为 `[192.168.10.82]:29418`：

```bash
# 如准备在部署前创建 Secret，先创建 namespace。
kubectl create namespace jenkins --dry-run=client -o yaml | kubectl apply -f -
ssh-keyscan -p 29418 192.168.10.82 > /tmp/gerrit_known_hosts.candidate
ssh-keygen -lf /tmp/gerrit_known_hosts.candidate -E sha256
# 经可信渠道确认指纹后，保存为 /data/verified_known_hosts，再创建 Secret。
kubectl -n jenkins create secret generic xran-build-trust \
  --from-file=known_hosts=/data/verified_known_hosts
```

Secret 会自动只读挂载到每个编译 Pod；流水线启用严格主机校验，并通过 ssh-agent 注入所选 Jenkins 凭据。
离线出口策略允许 RFC1918 内网的所有端口，包含 `192.168.10.82:29418`，仍需目标网络/防火墙允许节点与 Pod 访问。

Registry 凭据同样使用 Credentials，填 registryCredentialsId。内网 Harbor CA 通过 Secret 挂载给每个新构建 Pod；
宿主机 `/usr/local/share/ca-certificates/` 中的证书不会自动出现在容器中。
先使用原始证书文件验证格式；聊天中粘贴的证书可能缺失或损坏，不要靠修改 Base64 字符修补：

```bash
sudo openssl x509 -in /usr/local/share/ca-certificates/harbor-ca.crt \
  -noout -subject -issuer -dates
```

确认命令成功后，创建或更新 Secret。文件名可以是 `harbor-ca.crt`，Secret 内的键必须叫 `ca.crt`：

```bash
set -o pipefail
sudo kubectl -n jenkins create secret generic xran-registry-ca \
  --from-file=ca.crt=/usr/local/share/ca-certificates/harbor-ca.crt \
  --dry-run=client -o yaml | sudo kubectl apply -f -
```

新版 `config/site.intranet.yaml` 已启用下面配置；已有 `config/site.local.yaml` 需同步修改：

```yaml
caSecret: xran-registry-ca
registryInsecure: false
```

`registry` 保持实际地址 `192.168.10.35`。xcn 使用同一 HTTPS Harbor 时，也将已有 `xcn.registryInsecure` 改为 `false`，
保留其他 xcn 配置。更新代码后在项目根目录执行，无需卸载或重建镜像：

```bash
sudo env "PATH=$PATH:$PWD/.cache/tools" \
  make deploy SITE=config/site.local.yaml OFFLINE=1
```

CA 只读挂载到编译容器 `/var/xran/ca`，Git/curl 使用相应 CA 变量；Buildx 的 Go 令牌客户端通过
`SSL_CERT_DIR=/var/xran/ca:/etc/ssl/certs` 同时信任该 CA 和系统证书。DinD 继续使用
`/etc/docker/certs.d/192.168.10.35/ca.crt`。仅给 DinD 挂证书，或仅设置 Git/curl 的变量，不能覆盖客户端所有 TLS 请求。
重新提交构建，新 Pod 会读取 Secret。更新 CA 后也应重新创建构建 Pod，不要依赖运行中的进程重新加载信任池。
这与 Kubernetes 节点 containerd 拉取编译镜像的信任配置分别处理。
参考：[Docker 容器 CA 信任](https://docs.docker.com/engine/network/ca-certs/)、
[Go SystemCertPool](https://pkg.go.dev/crypto/x509#SystemCertPool)。
运行密码、私钥不进入通用离线包。已有用户和历史通过 Jenkins_HOME 备份恢复。

## 安装和首启验证

```bash
make deploy SITE=config/site.local.yaml OFFLINE=1
```

以上命令使用包内 Helm chart 和预导入镜像创建/更新 Jenkins。初始密码位于 `.state/jenkins/users.json`。
首次安装后先完成 Git/Registry 凭据配置，再提交业务构建；开通同事账号见 [账号管理](operations.md#同事账号)。

离线模式下基础设施、Agent 和 DinD 使用 Never；编译容器使用本次构建选择的策略，默认 Always。
使用默认 Always 时，必须先将 `192.168.10.35/xnet/xran-compile:latest` 发布到内网仓库，
并让每个构建节点的 containerd 配好仓库 HTTP/TLS 信任及必要拉取凭据。制作 SDK 的 Docker daemon 也要配置相应信任：

```bash
docker load -i .cache/k8s-images.tar
docker login 192.168.10.35
docker push 192.168.10.35/xnet/xran-compile:latest
```

仅依赖节点预导入镜像时，首次部署将站点 compilerImagePullPolicy 配为 Never；已有 HOME 在管理员网页设置中改为 Never，
或在本次构建选择 Never。即使节点已有镜像，Always 仍需联系仓库。
Pod 出口限制为本命名空间与 RFC1918 内网。
需要支持 NetworkPolicy 的 CNI（当前为 Calico）；其他内网网段须先加入白名单。
节点拉取不受 Pod NetworkPolicy 控制，因此必须先导入镜像。
若 CPU webhook 直接指定 Pod.nodeName，脚本只给 Jenkins Pending PVC 补齐实际节点注解，兼容延迟绑定。

首次上线先在验收 namespace 的全新 PVC 上验证，再完成服务包及镜像 build/save/push/download；
xcn 检查三个镜像 tar 和 Chart 四个下载文件、SHA256、构建人/时间/完整 commit。
验收不能依赖旧 HOME 缓存；生产迁移可以按备份恢复流程保留原有 HOME。

## 保留已有 Jenkins 的迁移

在外网先等待构建完成，再执行：

```bash
python3 scripts/ci.py backup --site config/site.yaml --archive /mnt/backup/jenkins-home.tar.gz
```

搬入内网后，按 [备份恢复](operations.md#备份恢复) 安装零副本 Controller，恢复到空 PVC，再启动。
原 HOME、初始账号 `xran-users`、SSH 主机信任 `xran-build-trust`、Registry CA 和镜像拉取 Secret
是独立于源码的迁移材料；完整 HOME 备份包含 Jenkins 中保存的 Git/Registry Credentials。
不要先正常启动一个全新 HOME 再把备份混进去；恢复脚本会拒绝非空目标。
恢复使用内网 site 文件，启动后管理员确认 **xcn 离线构建开启、编译环境地址及拉取策略正确**。
恢复的网页设置优先于 site 首次默认值，旧实例若保存 offline=false，必须在网页改为开启。

## 编译环境重建

已按 compileDep 资料准备完整 SDK、固定源码及 deb 缓存。运行中的任务不执行 apt 安装。
原始基础镜像、完整 deb 缓存及插件文件已准备：

```bash
docker load -i .cache/build-base-images.tar
docker load -i .cache/runtime-base-images.tar
docker load -i .cache/xcn-base-images.tar
make images SITE=config/site.local.yaml OFFLINE=1 APT_SOURCES=config/sources.intranet.list
```

SDK 构建始终使用 --network none，离线入口不会更新 APT 或拉取基础镜像。
依赖包含 Helm 二进制及固定 SHA256、PyYAML 的额外 APT 缓存和 Ubuntu/VPP 基础镜像归档。
`images --offline` 自动将 `.cache/xcn-base-images.tar` 加载到构建机 Docker，随后制作基础镜像携带容器；无需预先访问内网镜像仓库。
生产配置生成七个运行镜像，包含编译环境和独立 `xcn-bases`；演示模式另外生成 Git 服务和 Registry 两个镜像。
演示业务源码镜像仅从 Git 的已提交文件生成，不复制工作区未跟踪文件。
xcn 业务构建的离线模式在“构建平台设置”中由管理员控制，默认开启；`config/site.yaml` 的 `xcn.offline` 仅首次导入，构建页面不提供 OFFLINE 参数。迁移已有 HOME 会保留网页配置及审计。
离线构建仍需要内网 APT 软件库；不会访问 GitHub/npm/pip 下载 vendor 依赖。
新增依赖时先在能访问所选源的准备机执行 compile-image 更新缓存；新增 FROM 镜像须下载并补充清单后重新打包。
镜像发布更新时提升 versions.env 自定义标签；依赖未变化时只搬运代码，不累计旧交付目录。

编译镜像制作、按构建选版本/策略及节点拉取凭据配置见 [编译镜像说明](compile-image.md)。
切换全套 protobuf-c 1.5.2 时，改用 `DOCKERFILE=docker/builder.Dockerfile` 并指定另一个 tag。
离线包同时携带两套 Dockerfile 的 APT 缓存，运行镜像按所选版本交付；另一版可在内网自行制作。
从旧临时验证目录升级见 [正式构建修复升级](formal-build-upgrade.md)。

内部构建平台插件的源码、JPI、Maven 完整缓存和固定构建镜像也随包提供；`make platform-plugin OFFLINE=1` 实际使用 Maven `-o` 与 Docker `--network none`。

版本与内容见 config/versions.env、plugins.lock、platform-plugin.lock.json、images.lock.json、编译镜像 /opt/packages.lock 及 SHA256SUMS。

```bash
make test
```

完整 Git 历史按需执行 `make transfer-git` 单独交付；不与日常代码包混在一起。
真实站点将源码推至内网 Git 后，配置其地址与凭据。通用离线包不携带任何生产项目密钥。
