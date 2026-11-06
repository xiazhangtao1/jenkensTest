# open5gs 发布构建

入口：Jenkins → **open5gs** → **Build with Parameters**。独立于 `x-ran-cloud`，两者共用六个构建槽位和磁盘压力回收。

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

离线模式由管理员在 **Manage Jenkins → 系统配置 → 构建平台设置 → Open5gs 离线构建** 统一配置，默认开启，构建表单不提供 `OFFLINE`。勾选时给业务脚本增加 `--offline`；外网调试时管理员取消勾选。保存后对新入场构建立刻生效。

`config/site.yaml` 中的 `open5gs.offline` 只提供首次启动的初始值。
普通用户和旧 API 客户端传入的 `OFFLINE` 不影响此配置。每次构建的实际模式仍记录在摘要和 `metadata.json` 中；已有历史按原值保留。
这里控制的是业务脚本的 `--offline`，与部署工具自身的 `--offline`（基础设施拉取策略和网络配置）分别设置。

外网闭环使用 `open5gs-basev277-develop`，由管理员将上述配置设为 `false`。该分支已有发布入口；目前 `main` 缺少此入口，选择它会明确失败，不会切换分支，也不修改业务仓库。

流水线精确检出后，在所选提交的 `helm` 目录执行：

```bash
./build-release.sh --offline 26.11.6 26.11.6
# 管理员关闭 Open5gs 离线构建时执行：
./build-release.sh 26.11.6 26.11.6
```

归档前校验四个文件：`xcn-runtime.tar`、`xcn-webui.tar`、`xcn-monitor.tar`、`xcn-<CHART_VERSION>.tgz`。
检查 tar 完整性、镜像引用、Chart/app 版本及默认镜像 tag；下载目录同时提供 `metadata.json` 和 `SHA256SUMS`。
记录完整 Git SHA、构建人、时间、编译镜像实际 ID、策略、版本、offline 标记、三个业务镜像 ID 和各自推送状态。

保持业务脚本原有语义：它会尝试推送 `192.168.10.35/xcn/xcn-{runtime,webui,monitor}:APP_VERSION`。
编译成功而推送失败时仍可下载四个有效产物，构建摘要、日志和元数据明确提示推送未全部成功。
同 APP_VERSION 的发布先串行等待，再申请全局槽位，避免两个构建同时覆盖相同镜像标签。

Pod 初始默认总上限为 4 CPU、8GiB 内存、20GiB 临时存储。DinD 为 **4GiB**，编译容器为 3584MiB，Agent 为 512MiB。
默认 open5gs 的嵌套 Docker build 额度为 3GiB、并行 2，给 DinD daemon 留出空间；这些是不同层级的限制，不叠加成额外内存。管理员可在 [构建平台设置](platform-settings.md) 单独修改 Open5gs 资源配置。
嵌套容器挂在 DinD cgroup 下，共同受 4GiB 上限约束。X-RAN-CLOUD 镜像任务的 DinD 同样为 4GiB。

编译镜像包含 Helm 3.17.3、PyYAML 及原 SDK。Ubuntu/VPP 基础镜像由独立 `open5gs-bases` init 容器复制到共享卷，每次导入自己的 DinD，不共享宿主 Docker socket。
离线包提供这些镜像以及包含 vendor 依赖的 open5gs Git bundle；APT 包仍通过业务脚本指定的软件源安装，内网应确认 `192.168.10.36` 可达。

迁移时配置 `config/site.yaml` 的 `open5gs.gitUrl`、Git/Registry 凭据及基础镜像。
`ubuntuImage`、`vppImage` 须与该业务提交 Dockerfile/build 脚本使用的镜像引用一致。

真实验证（默认使用 develop 和已导入镜像，构建模式跟随站点配置，不会修改业务源码）：

```bash
python3 tests/verify-open5gs.py
# 接续已有长时间构建并检查下载产物：
python3 tests/verify-open5gs.py --number 1 --online
```

脚本实际下载四个文件、校验 SHA256，执行 Helm lint/template，导入并断网检查三个镜像。
Chart 要求显式站点网络地址，渲染默认读取所选提交的 `helm/tests/site-values.yaml`，也可用 `--values /路径/values.yaml` 指定。
测试 values 只用于渲染检查，不部署业务。结果位于 `.state/jenkins/open5gs-release.json`。
`--offline` / `--online` 仅断言预期模式，不会改变 Jenkins 后台配置。

默认版本保存与历史回填的专项验收使用 `python3 tests/verify-parameters.py --site /路径/验收-site.yaml`。
该脚本只接受带 `verificationInstance: true` 的独立命名空间，在槽位内跳过业务编译，并在结束时恢复流水线定义。
