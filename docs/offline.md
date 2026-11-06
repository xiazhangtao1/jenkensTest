# 内网离线迁移

目标已有 Kubernetes/containerd 和 Ubuntu 22.04 amd64，不包含集群安装。
所有运行镜像、完整插件依赖和可选 local-path 镜像均在包内；启动时不安装插件，临时 DinD 加载预置基础镜像。

## 校验和导入

```bash
cd /data/xran-jenkins-实际时间
sha256sum -c SHA256SUMS
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

编辑 config/site.yaml：jenkinsUrl、NodePort、namespace、服务/镜像清单、内网 gitUrl、registry 和 imageRepositories；
open5gs 另配置 `open5gs.gitUrl`、Git/Registry 凭据、Ubuntu/VPP 镜像与 `offline: true`。
先保留 demo 验证，正式切换时设 demo: false。镜像脚本真实仓库路径应与映射一致。
TLS Registry 设置 registryInsecure: false；proxy 通常留空，noProxy 包含 API、Pod/Service CIDR、内网域名与 apt 源。

Git 凭据在 Jenkins Credentials 新增，填 gitCredentialsId；HTTPS 用用户名/token，SSH 用私钥。
SSH 主机公钥通过可信渠道核对后设置：

```bash
kubectl -n jenkins create secret generic xran-build-trust \
  --from-file=known_hosts=/data/verified_known_hosts
```

Registry 凭据同样使用 Credentials，填 registryCredentialsId。内网 CA 可配置：

```bash
kubectl -n jenkins create secret generic xran-registry-ca \
  --from-file=ca.crt=/data/internal-ca.crt
```

增加 caSecret: xran-registry-ca，CA 同时供 Git/HTTP 和 DinD 的 Registry TLS 使用。
运行密码、私钥不进入通用离线包。已有用户和历史通过 Jenkins_HOME 备份恢复。

## 安装和首启验证

```bash
python3 scripts/ci.py deploy --offline
```

离线模式下基础设施、Agent 和 DinD 使用 Never；编译容器使用本次构建选择的策略，默认 Always。
内网 Registry 可达时可继续使用 Always；仅依赖节点预导入镜像时，明确选择 Never，或将站点 compilerImagePullPolicy 配为 Never。
Pod 出口限制为本命名空间与 RFC1918 内网。
需要支持 NetworkPolicy 的 CNI（当前为 Calico）；其他内网网段须先加入白名单。
节点拉取不受 Pod NetworkPolicy 控制，因此必须先导入镜像。
若 CPU webhook 直接指定 Pod.nodeName，脚本只给 Jenkins Pending PVC 补齐实际节点注解，兼容延迟绑定。

必须在全新 PVC 上首次启动，再完成服务包及镜像 build/save/push/download，不能依赖旧 Jenkins_HOME 的缓存。

## 编译环境重建

已按 compileDep 资料准备完整 SDK、固定源码及 deb 缓存。运行中的任务不执行 apt 安装。
原始基础镜像、完整 deb 缓存及插件文件已准备：

```bash
git clone --mirror repos/X-RAN-CLOUD.bundle ../X-RAN-CLOUD.git
git clone --mirror repos/open5gs.bundle ../open5gs.git
docker load -i .cache/build-base-images.tar
docker load -i .cache/k8s-images.tar
make compile-image OFFLINE=1 APT_SOURCES=config/sources.intranet.list
python3 scripts/ci.py images --offline --source ../X-RAN-CLOUD.git \
  --open5gs-source ../open5gs.git --apt-sources config/sources.intranet.list
```

SDK 构建始终使用 --network none，离线入口不会更新 APT 或拉取基础镜像。
包内新增 Helm 二进制及固定 SHA256、PyYAML 的额外 APT 缓存、open5gs Git 镜像/bundle 和 Ubuntu/VPP 基础镜像归档。
`images --offline` 自动将 `.cache/open5gs-base-images.tar` 加载到构建机 Docker，随后制作基础镜像携带容器；无需预先访问内网镜像仓库。
运行镜像共九个，包含独立 `open5gs-bases` 镜像；两项目业务源码镜像都从 Git 的已提交文件生成，不复制工作区未跟踪文件。
open5gs 业务构建由管理员在 `config/site.yaml` 设置 `open5gs.offline: true`，构建页面不提供 OFFLINE 参数。
离线构建仍需要内网 APT 软件库；不会访问 GitHub/npm/pip 下载 vendor 依赖。
新增依赖时先在能访问所选源的准备机执行 compile-image 更新缓存；新增 FROM 镜像须下载并补充清单后重新打包。
镜像发布更新时提升 versions.env 自定义标签，保留旧标签及旧离线目录用于回滚。

编译镜像制作、按构建选版本/策略及节点拉取凭据配置见 [编译镜像说明](compile-image.md)。

版本与内容见 config/versions.env、plugins.lock、images.lock.json、编译镜像 /opt/packages.lock 及 SHA256SUMS。

```bash
git clone repos/jenkensTest.bundle ../jenkensTest
make test
python3 tests/verify.py --quick --source ../X-RAN-CLOUD.git
# 内网 APT / Registry 配置完成后验证 open5gs 发布：
python3 tests/verify-open5gs.py --offline --source ../open5gs.git
```

真实站点将源码推至内网 Git 后，配置其地址与凭据。通用离线包不携带任何生产项目密钥。
