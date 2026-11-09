# 将临时验证环境升级为正式构建环境

当前版本为构建平台插件 1.0.5、Controller `xran-ci/controller:2.580.2-19`。
Controller 19 仅统一 xcn 与 X-RAN-CLOUD 的任务描述；已有部署须按 [升级 Jenkins](#升级-jenkins) 更新 Controller，编译镜像不变。
支持两个编译 Dockerfile，保留 `pkgconf`、DPDK 非交互输入、服务串行及取消 `all` 的修复；新增旧版业务镜像缺包自动导出和项目可见性开关。
此前 Controller 18 关闭离线页面的 Jenkins 使用统计请求，固定 local-path helper 镜像 tag，并将 xcn 基础镜像复制初始化容器从 128MiB 调整为 1GiB，避免文件缓存触发 OOM；新增 [完整回归与持续构建验收](reliability.md)。
已有 `26.11.08-compat-r2` / `26.11.08-sdk-r2` 编译镜像无需因本轮平台测试重新构建。
初始化预算由部署脚本生成，更新代码后须执行 `make deploy` 更新模板；仅导入 Controller 或重启旧部署不会生成新模板。

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
