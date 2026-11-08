# 将临时验证环境升级为正式构建环境

本次版本为构建平台插件 1.0.5、Controller `xran-ci/controller:2.580.2-17`。
支持两个编译 Dockerfile，保留 `pkgconf`、DPDK 非交互输入、服务串行及取消 `all` 的修复；新增旧版业务镜像缺包自动导出和项目可见性开关。

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
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder-compat.Dockerfile IMAGE_TAG=26.11.08-compat-r2
sudo make compile-image OFFLINE=1 DOCKERFILE=docker/builder.Dockerfile IMAGE_TAG=26.11.08-sdk-r2
sudo make push-compile-image IMAGE_TAG=26.11.08-compat-r2
sudo make push-compile-image IMAGE_TAG=26.11.08-sdk-r2
```

Harbor 需要登录时，使用与推送相同的用户登录；上述 sudo 示例对应 `sudo docker login 192.168.10.35`。

默认镜像名称为 `192.168.10.35/xnet/xran-compile`；名称和 tag 都可另行指定。
构建命令会自动断网验证镜像。每个 tag 的导出包位于 `dist/transfer/03-generated-images/compiler/镜像引用转换后的目录名/image.tar`，
已有成品也可直接 `docker load` 后推送到 Harbor。两版是完整编译环境，切换只需在构建参数填写对应 `COMPILER_IMAGE`。

完成兼容版验收后，如果要继续使用默认的 `latest` 地址：

```bash
sudo docker tag 192.168.10.35/xnet/xran-compile:26.11.08-compat-r2 192.168.10.35/xnet/xran-compile:latest
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
  -f docker/controller.Dockerfile -t xran-ci/controller:2.580.2-17 .
sudo docker save -o /tmp/xran-controller-2.580.2-17.tar xran-ci/controller:2.580.2-17
sudo ctr -n k8s.io images import /tmp/xran-controller-2.580.2-17.tar
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
