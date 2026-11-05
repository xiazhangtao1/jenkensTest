# 验证结果

## open5gs 扩展（2026-11-06）

主实例已部署 Controller `2.580.2-8`，新增独立 `open5gs` 任务，原 `x-ran-cloud` 保持可用。
编译镜像已增加 Helm 3.17.3、PyYAML 和发布适配器，最终配置 ID：
`sha256:c35fc07fbb6ac293b26bf4dbb222dc5e014b07304708e56adf814307520e2b51`。
断网重建得到同一 ID，导出的 image.tar/manifest.json 校验通过，原有 SDK 两个 CTest 和附加检查通过。

| 验证 | 实际结果 |
|---|---|
| Python 自动化测试 | 19 项通过，包括产物缺失/损坏/版本不符、OFFLINE 参数和临时空间边界 |
| 完整 open5gs 发布 | 主实例 #1 SUCCESS；真实 develop 提交 `49a9671f78814b27c171c681a8acb75ecf255f94`，约 37 分钟 |
| 四个产物 | 普通账号下载 runtime/webui/monitor tar 和 `xcn-26.11.6.tgz`，全部 SHA256/大小匹配 |
| 下载后使用 | 三个 tar 均实际导入、断网运行检查通过；Chart 使用所选提交的站点测试 values，lint 和 34 个模板对象通过 |
| 引用和失败提示 | 真实 tag/短 SHA 精确检出；#2 非法 Chart 版本失败；#3 默认 main 缺少发布入口明确失败 |
| 镜像策略 | #1 Never；#4 Always 实际访问临时演示 Registry；#5 IfNotPresent 使用缓存，Pod imageID 与元数据一致 |
| 资源额度 | 实际 DinD cgroup 为 4GiB，嵌套构建为 3GiB，内存失败计数为 0；Pod 总额度 4 CPU/8GiB/20GiB |
| 共享能力 | 独立 PVC：27 个模板组合、相同构建号 Pod 隔离、8 请求共用 6 槽、同 app 版本串行、跨项目压力回收均通过 |
| 离线首启 | 另一全新 PVC：73 个插件启用、两任务参数/权限正确、所有 Agent/init 容器 Never、相同编号元数据正确 |
| 离线网络 | DNS 正常，NetworkPolicy 阻止公网 HTTP；源码检出使用命名空间内网 Git 镜像 |
| 全新 Docker 基础镜像恢复 | 镜像列表为空且 `--network none`，自动加载包内 Ubuntu/VPP 并重新导出成功，无预热镜像依赖 |
| X-RAN-CLOUD 回归 | 主实例 #81–93，共 16 项检查通过，包含服务、镜像、分支/tag/完整及短 SHA、下载运行、推送失败留存和权限 |
| 页面 | 七个 open5gs 参数顺序/默认值正确，模拟另一天重新打开表单生成新日期；两项目历史 Parameters 保留原行为 |

完整证据见 [open5gs.json](verification/open5gs.json)。共享锁测试保留真实参数校验和两层锁，在获得槽位后用短等待代替编译；
压力测试仅作用于专用验收 PVC，验证运行中、标记保留的版本不被清理以及提示/审计。两个临时验收 Controller 已暂停。
主实例两任务的流水线 SHA256 与仓库一致、没有按天数/次数丢弃策略，Jenkins 已恢复正常接单。

本次完整发布使用真实 `open5gs-basev277-develop` 且 OFFLINE=false；没有修改 open5gs 业务源码或 main。
内网 Registry `192.168.10.35` 不可达，三个业务镜像推送均记录为 failed，构建摘要/日志明确提示，四个有效包仍可下载。
未声称向内网仓库推送成功；内网 APT `192.168.10.36` 下的完整 OFFLINE 业务编译需迁入后验证。
平台默认编译镜像及策略仍为指定内网地址和 Always，本机已导入镜像可选择 Never。

## 2026-11-05 平台原有验收记录

2026-11-05 在 Ubuntu 22.04 amd64、Kubernetes 1.26.8/containerd 1.6.32 上完成实际验收。
Jenkins 2.580.2、官方 Helm chart 5.10.10，锁定的 73 个插件全部启用。

| 验证 | 结果 | 证据 |
|---|---|---|
| 运行时/资源配置测试 | 7 项通过；包含真实 Git HTTP 检出、C 编译和 tar 校验 | tests/test_runtime.py、tests/test_config.py |
| 维护任务权限 | 7 项通过；管理员访问、手动/定时维护、普通账号隔离及构建下载均正常 | [maintenance-permissions.json](verification/maintenance-permissions.json) |
| 管理员首页提示 | 三账号真实浏览器、API 隔离、清理摘要保留及普通构建下载通过 | [admin-notice.json](verification/admin-notice.json) |
| Jenkins 主实例 | 20 项通过，30 次实际构建包含预期失败与取消场景 | [jenkins.json](verification/jenkins.json) |
| 原生参数页 | 服务/镜像整行条件显示、双向切换、输入保留、普通账号页面提交均通过 | [parameters.json](verification/parameters.json) |
| 历史 Parameters 页 | 已有服务 #43、镜像 #35 按类型筛选；新建表单双向切换及输入保留再次通过 | [parameters-history.json](verification/parameters-history.json) |
| 全新 PVC 离线首启 | 15 项通过，全部服务与镜像模式、引用选择、下载、失败留存 | [offline.json](verification/offline.json) |
| 离线网络与镜像策略（早期版本） | 当时全部容器使用 Never；公网连接超时被阻止。当前编译容器策略由构建参数选择 | [offline-egress.json](verification/offline-egress.json) |
| 新 PVC 备份恢复 | 8 项通过；41 次历史、审计、参数、脚本一致，历史服务/镜像可下载使用，恢复后新构建 #42 成功 | [restore.json](verification/restore.json) |
| 工具链离线重建 | Docker build --network none 实际成功 | docker/builder.Dockerfile、config/images.lock.json |
| 离线打包 | 已实际导出并校验文件 SHA256、73 个插件发布校验值、8 个运行镜像 ID | 最终目录的 MANIFEST.json、SHA256SUMS |

## compileDep 编译镜像与按次选择

原装机资料中有效输入已迁入本项目，整理为 22 份有版本和 SHA256 的源码/二进制归档；
大文件按最多 50MiB 分片入 Git。原文件的搬迁记录保存在验证机 `.cache/migrated-compileDep/MIGRATION.json`。
WLS 和 Intel oneAPI 不安装。补齐统一 protobuf/gRPC ABI 后，所有 SDK 安装均在 Docker `--network none` 中执行。

最终 Ubuntu 22.04 编译镜像 ID 为
`sha256:7f25bb936c6456d1a2e64306b9663f65e78457dddd8f330651e55cb2be2123f7`。
已实际执行默认名称、`26.11.05` 标签及带仓库端口的自定义名称构建和导出，三个引用对应相同镜像内容。
默认 UID 1000 的镜像验证通过两个 CTest：主要 SDK 共同编译/链接/运行、protobuf 静态链接运行；
另检查 protobuf-c 生成代码、ASN.1 编译器、静态归档存在及 airtracer/ROHC/DPDK/gRPC/etcd 动态库加载。

本轮 13 项 Python 测试通过；真实 Kubernetes 插件环境验证了 18 个模板组合及非法输入拒绝。
Jenkins 专项验收 16 项通过（#55–66），覆盖 Always/IfNotPresent/Never、不同编译镜像并发、
同 tag 更新后的缓存和新摘要差异、实际 Pod imageID 元数据、非法引用和不兼容镜像。
首次缺失镜像测试 #54 暴露插件会重复创建 Pod、单 Pod 连接超时无法限制整体启动等待，已停止该测试并补充整体启动看门狗；
修复后 #65/#66 在一分钟测试配置下分别于 61.0/60.5 秒失败并留下镜像与 Pod 诊断。生产默认启动等待仍为 15 分钟。

真实浏览器检查了服务/镜像表单的公共编译参数、默认值、双向切换与输入保留；
新服务 #55、新镜像 #58，以及旧服务 #43、旧镜像 #35 的只读 Parameters 页面均按 BUILD_KIND 隐藏无关项。
专项及最终部署验收证据见 [compiler.json](verification/compiler.json)。

最终 Controller `2.580.2-6` 部署后，再用最终 SDK 完成 16 项功能验收（13 次构建 #67–80，期间 #76 为独立页面提交）：
服务 A/B/C、all、services_all、分支/tag/完整及短 SHA、三个业务镜像 build/save/push/download/load/run，
以及无效引用、无效标签和推送失败后有效 tar 留存。普通账号从原生表单提交 #76 serviceC，
Always 实际拉取的 Registry 摘要对应最终 SDK 配置 ID，下载包内容和校验值正确，历史 Parameters 显示正确。
最终部署的流水线 SHA256 与仓库三段 Groovy 拼接结果一致；并发 6、临时存储 20GiB、PVC 250Gi 保持原配置。

从已提交版本 `28dd104` 的干净检出运行独立 SDK 离线入口，仅提供 APT 缓存、基础镜像归档及主机已加载的镜像，
不使用原 compileDep 或 Jenkins 插件缓存；复用 Docker 构建层后得到同一最终镜像 ID，两个 CTest 和附加 SDK 检查再次通过。

验证机无法路由到 `192.168.10.35`，Always 联调使用现有演示 Registry 的临时本机转发；
没有向指定内网 Registry 推送镜像。最终部署保留 `192.168.10.35/xnet/xran-compile:latest` 和 Always 默认值，
仅使用节点导入镜像时须选择 Never。其他离线基础设施容器仍使用 Never。

`storage-maintenance` 使用项目权限矩阵并关闭普通任务权限继承，仅具有 Overall/Administer 的管理员可见。
builder1、builder2 的首页和 API 清单仅显示 x-ran-cloud，维护任务直链、API、配置、日志和构建请求均返回 404。
管理员手动维护 #22、TimerTrigger 维护 #23 成功；builder1 的 serviceB 构建 #42 成功并核对下载产物。
只读权限回归可执行 `python3 tests/verify.py --permissions-only --report .state/jenkins/maintenance-permissions.json`，
不会提交构建或执行磁盘压力清理测试。

首页 CI 说明和最近清理摘要同样仅管理员可见；管理员浏览器可见，builder1、builder2 的浏览器、首页 HTML 和 API 均无该提示。
更新清理摘要后公共 systemMessage 保持为空，摘要重新初始化后保留；验证使用临时文字并恢复原文，未删除产物。
新增的 serviceC 构建 #43 由 builder2 成功执行并下载校验，参数双向切换和输入保留也再次通过真实浏览器检查。

页面模式：BUILD_KIND=service 仅显示 SERVICE_TARGET；BUILD_KIND=image 仅显示 IMAGE_TARGET、IMAGE_TAG。
REF_TYPE/GIT_REF 始终可见。实际从页面提交的 #34 serviceC 和 #35 imageB 均成功，
镜像已完成 build/save/push/download/load/run；截图留在验证机 output/playwright/。
构建详情的只读 Parameters 页采用不同的行标识与值展示，现已覆盖该入口；旧历史记录也会按 BUILD_KIND 显示整行，
服务记录隐藏 IMAGE_TARGET/IMAGE_TAG，镜像记录隐藏 SERVICE_TARGET。已用普通账号实际打开 #43、#35 并逐项验证。
静态联动脚本来自 Controller 镜像的 userContent，包含内容版本号，不依赖公网脚本。
重启、恢复后参数定义只保留一套，脚本 SHA256 与仓库相同。

构建覆盖 main、demo/feature、轻量 tag、带注释 tag、历史完整 SHA、唯一短 SHA；
serviceA/B/C、all 多包、services_all 总包和 imageA/B/C 都核对了产物内容、哈希、构建人及时间。
push 失败时任务为 FAILURE，但有效 tar 仍可下载、加载和运行。
七个服务任务提交时最多六个 Pod，另实际验证六个不同标签镜像同时构建，峰值均为六。
同镜像同标签串行且其他目标可并行；取消后任务结束。
磁盘验证仅暂时保护其余历史，用专用测试版本验证正常不清理、最早回收、保留保护、审计与空间不足拒绝。

验证中发现并修复的实际问题：

- 构建容器无法读取 root 所有的基础镜像归档：COPY 指定 Jenkins UID/GID。
- 多次初始化叠加参数定义：初始化先移除旧定义，再添加一套。
- 新版主题插件配置分类不同，以及原生表单的参数控件与完整行层级不同：按实际插件和 DOM 修正。
- 当前集群对整数 CPU 配额分配专核，离线首次 #9 被专核池不足拒绝。镜像 Pod 改为编译 2.5、Docker 1.25、Agent 0.25 CPU，
  总计仍为 4 CPU；六镜像并发及离线重试均成功，首次失败保留在报告中。
- local-path 根目录属于 root，非 root tar 恢复根目录属性失败：恢复专用 init 仅调整目标卷根目录属主；失败保持 Controller 为零副本。
  两次失败的临时恢复实例保留证据，第三个全新 PVC 恢复命令成功退出并完成验证。

离线验证通过本命名空间 NetworkPolicy 禁止公网，并使用全新 Jenkins_HOME；未关闭宿主机和其他业务的网络。
四个临时验证实例及其演示依赖已暂停，保留测试 PVC 和报告；主实例继续提供构建入口。
通用离线目录不含运行密码、私钥或 Jenkins_HOME；后者单独备份且权限为 0600。

本轮业务源码是模拟项目。真实内网项目仍需补齐服务/镜像清单、实际依赖、基础镜像和 Registry 映射后验收。
