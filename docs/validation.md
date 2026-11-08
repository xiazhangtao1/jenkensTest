# 分类交付与旧文件清理验收（2026-11-08）

外部依赖、代码及可选镜像成品已分为 `dist/transfer/01-dependencies`、`02-code`、`03-generated-images`。

- Python 回归 41 项通过；完整代码与补丁均排除 SDK 大文件和本地站点配置，归档可执行权限及确定性验证通过。
- 五个依赖包的 SHA256 和解包逐文件校验通过；重新打包全部复用，内网清单检查能识别已有依赖及内容变化。清理前保留的 6,232 个依赖文件全部核对一致。
- 仅由分类依赖包和项目源文件还原的独立目录，实际完成 `images --offline` 生产构建：SDK 两个 CTest、插件 16 项测试（1 项框架跳过）、SpotBugs 零问题；七个运行镜像归档与新锁文件一致。
- 独立构建使用不存在的两个业务目录且没有 Git 元数据，证明生产模式不再要求演示仓库；未部署或重启主 Jenkins。
- 拆分的上游运行镜像和单独的 Controller 压缩镜像均实际导入成功；仅从固定上游镜像导出依赖、不依赖定制镜像总包的路径也实测通过。
- 删除旧编译镜像、旧整包/增量包、历史 HOME 备份及调试缓存；保留完整 Git 历史、当前实例账号配置、两版最终编译镜像及必要离线输入。

记录见 [transfer.json](verification/transfer.json)，操作步骤见 [分类搬运](transfer.md)。本轮未连接真实内网 APT/Gerrit/Harbor。

---

# 镜像缺包兼容与项目可见性验收（2026-11-08）

Controller `2.580.2-17`、构建平台插件 `1.0.5` 在独立命名空间 `jenkins-formal-verify` 验收通过，主 Jenkins 未变更。

- Python 回归 36 项通过；插件断网构建 16 项测试中 15 项通过、1 项框架跳过，SpotBugs 零问题。原生系统表单实际保存两个新开关；缺少 Kubernetes Cloud 时访问限制仍生效。
- compat-r2、sdk-r2 两版编译镜像均实际断网构建，UID 1000 的两个 CTest、软件包及 SDK 检查通过，导出归档 SHA256 通过；两版均实际验证旧脚本只生成本地镜像时自动导出、下载、导入和断网运行。
- 独立 Jenkins 共 28 项不同检查通过：服务、分支/tag/完整和短 SHA、三种正常镜像、旧版缺包导出、异常包拒绝、权限及重启恢复。
- 损坏包、镜像不存在或目录不可写时不发布错误包；新版已有 tar 校验通过后归档。旧版 push 失败时仍导出有效包，构建保留失败结果。旧适配器明确提示更新编译镜像。
- 两个项目的四种可见性组合均验证：首页、API、直链、日志、产物及新建构建访问受限；管理员均可访问，维护任务始终隐藏。开启后已运行和等待槽位的构建继续完成。
- Controller 实际重启后开关、配置版本和原 ACL 备份不变；关闭后精确恢复用户、组和继承方式。单元测试另验证权限偏离自动修复、升级不开放已有受限项目。
- xcn 既有四包清单及元数据保持一致，重新下载 Chart 的大小和 SHA256 通过；本轮未重复完整 xcn 业务编译。
- 离线运行归档中 9 个镜像 ID 与锁文件一致；新增 Maven 缓存与两套编译 APT 缓存随增量目录交付。

证据见 [artifact-visibility.json](verification/artifact-visibility.json)，升级步骤见 [正式构建环境升级](formal-build-upgrade.md)。
测试 Registry 仅供独立验收，未连接真实内网 Gerrit、APT 或 Harbor，未向内网 Harbor 推送。
全套 protobuf-c 1.5.2 仍需真实内网业务验证。以下保留此前版本的验收记录。

---

# 正式构建兼容修复验收（2026-11-08）

本次 Controller 为 `2.580.2-16`、构建平台插件为 `1.0.4`。在独立命名空间
`jenkins-formal-verify` 完成验收，现有主 Jenkins 未变更。

- 两个 Dockerfile 均实际断网构建并验证：兼容版使用 1.5.2 生成器 / Ubuntu 1.3.3 开发库，SDK 版使用全套 1.5.2；两个 CTest 和附加 SDK 检查均通过。
- 实际验证绝对路径且含空格的 Dockerfile，两个镜像导出包及三套 APT 缓存的 SHA256 校验通过。
- 更新后的离线运行归档中 9 个镜像 ID 与锁文件一致；从旧内网基线 `23777c1` 应用增量补丁后，34 个改动文件逐字节匹配正式源码。
- Python 回归 28 项通过；插件断网构建 12 项测试中 11 项通过、1 项框架跳过，SpotBugs 无问题。
- 独立 Jenkins 共 24 项不同检查通过：分支/tag/完整及短 SHA、服务/总包/三种镜像、下载和断网运行、推送失败保留有效包、权限隔离。
- 两版编译环境可并发运行；串行开关的 true/false 均实际传入并记录，Controller 重启后关闭状态、配置版本及历史保留。旧适配器及已取消的 `all` 目标明确拒绝。
- 用真实 GNU Make 递归 `-j32` 复现建目录和依赖生成的竞争，加入 `.NOTPARALLEL:` 后通过；非交互 DPDK 路径输入及业务脚本失败退出码均验证通过。
- xcn 开发分支在线完整发布成功：四包下载和 SHA256、Helm lint/render、三个镜像导入后的断网运行共 7 项检查通过。临时公网策略已删除。

xcn 使用演示 Git 服务固定的 `5975f865`；本地业务目录已前进到 `7019cb034`，接续验收使用实际镜像仓库核对，未修改业务分支。
内网 Harbor 不可达，三个镜像推送均如实记为失败，有效包仍可下载；没有声称完成内网推送或 `--offline` 业务编译。

证据见 [formal-build.json](verification/formal-build.json)，内网增量更新见 [正式构建修复升级](formal-build-upgrade.md)。
X-RAN-CLOUD 使用模拟业务仓库；全套 1.5.2 仍需真实内网业务验证。本次未连接真实内网 Gerrit、APT 或 Harbor。

---

# xcn 命名迁移验收（2026-11-06）

主实例已部署 Controller `2.580.2-15`、内部插件 `1.0.3`，业务入口为 `x-ran-cloud` 和 `xcn`。
原发布任务原地重命名，102 次既有业务构建的参数、结果、长期保留标记、根元数据 SHA256 和产物清单均未变。
管理员保存的资源、离线模式及其他设置保留原值；原始审计保留，新增一次命名迁移事件。

- Python 回归 21 项通过；插件完全断网构建 11 项测试中 10 项通过、1 项框架跳过，SpotBugs 无问题。
- 独立实例重命名、重复启动与配置审计幂等验证通过；六组版本参数及历史回填、六个共享槽位、同版本串行和跨项目存储回收通过。
- 原成功发布的四个包通过新任务 URL 下载，SHA256、Helm 渲染及三个镜像导入后的断网运行检查通过。
- 新旧两个真实编译镜像的适配器均验证离线参数、四包归档及 xcn 元数据兼容；测试使用有效小型归档，并未重新编译完整业务。
- 独立实例实际创建 xcn 编译 Pod，基础镜像挂载与元数据使用新名称；所选 main 缺少发布入口，按预期失败并保留诊断。
- 主实例服务 #98、镜像 #99 成功，实际 Pod 与入场快照均为 4 CPU / 8GiB / 20GiB，编译镜像 ID 和元数据一致。
- 普通用户浏览器首页显示 xcn、维护任务隐藏；构建表单六参数与默认日期正确；原历史 Chart/app 版本可见并完成 HTTP 实际复制粘贴。

证据：[xcn-renaming.json](verification/xcn-renaming.json)。历史 Git 引用与原始验收数据保留真实值，当前说明统一使用 xcn 项目名称。

---

# 内网迁移与两小时维护验收（2026-11-06）

主实例已更新为 Controller `2.580.2-13`，`storage-maintenance` 定时计划为 `H H/2 * * *`。
使用 Jenkins 实际 Cron 解析器检查完整一天，共触发 12 次、间隔两小时；启动时及构建/归档前的空间检查保留。

- 新增 `config/site.intranet.yaml`，使用 `192.168.10.82:29418` 的 X-RAN-CLOUD 和 ICT_CN SSH 地址。
- 仅监听本机的临时 SSH 服务实际验证严格主机校验、29418 端口及两个仓库的分支/短 SHA 检出；ICT_CN 检出后发布入口路径正确。
- 独立离线 Jenkins 在全新 PVC 首启；通过原生创建用户接口新增普通账号，实际构建和下载成功，记录构建人，管理接口及维护任务不可访问。
- 重启该独立 Controller 后，个人账号原密码可登录，同一 PVC、历史包校验和和构建平台完整状态均保留。
- 主实例升级前已完整备份；升级后 102 次业务历史的参数、结果、长期保留标记、元数据 SHA256 和产物清单未变，网页配置未变，插件及权限验证通过。
- 原有 20 项 Python 回归测试通过；内网配置渲染验证通过，示例关闭 demo、启用离线并使用 Retain/keep PVC。

证据：[intranet-migration.json](verification/intranet-migration.json)。没有重启宿主机，也未访问真实内网 Gerrit/Registry/APT；
真实内网服务的连通性、凭据与完整离线业务构建仍须迁入后按部署文档验收。以下保留此前版本的验证记录。

---

# 构建平台网页设置验收（2026-11-06）

主实例已部署 Controller `2.580.2-12`、内部插件 `xran-build-platform:1.0.1`。管理员入口为 **Manage Jenkins → System（系统配置）→ 构建平台设置**。
默认并发 6，三套 Pod 预算各 4 CPU / 8GiB / 20GiB，默认编译环境 latest / Always，xcn 离线构建开启。

| 验收 | 结果与证据 |
| --- | --- |
| Python 回归 | 20 项通过 |
| 插件完整断网构建 | 固定 Maven 3.9.9 / JDK 21，Docker network none + Maven -o clean verify；10 项测试中 9 项通过、1 项由框架跳过，SpotBugs 零问题；[构建报告](verification/platform-plugin.json) |
| 独立源码副本重建 | 没有 Git 元数据、没有官方插件原始归档，仅靠交付的 Maven 缓存与构建镜像，断网 clean verify 成功；[报告](verification/platform-independent-offline.json) |
| 原生配置与权限 | 浏览器实际保存、资源预览、旧表单返回 400 冲突；普通用户预览/应用/状态接口均 403；[报告](verification/platform-browser.json) |
| 动态并发与超时 | 6→2→4，统计所有运行中槽位；排队用新版本，运行中保留旧快照；重启、取消、同标签等待与超过 1 分钟的排队测试通过；[报告](verification/platform-admission.json) |
| 三类实际 Pod | 服务 5 CPU/9GiB/8GiB、镜像 6 CPU/10GiB/12GiB、xcn 4 CPU/8GiB/20GiB；容器资源及工作区 sizeLimit 与快照相符；[服务](verification/platform-resources-service.json)、[镜像](verification/platform-resources-image.json)、[xcn](verification/platform-resources-open5gs.json) |
| 实时磁盘阈值与预留 | 管理员修改阈值立即被检查使用；预留按入场预算累计；最早回收、运行中/长期保留保护、提示审计及空间不足拒绝通过；[报告](verification/platform-shared-verification.json) |
| xcn 完整发布 | develop 分支在线构建成功，四个文件可下载且 SHA256 一致，Helm lint/render、三个镜像导入后的断网运行检查通过；[报告](verification/platform-open5gs-release.json) |
| 参数与页面回归 | 版本保存/历史修复/默认日期、隐藏 OFFLINE、BUILD_KIND 条件显示、HTTP 实际复制粘贴、维护任务隔离通过；[参数报告](verification/platform-parameters-verification.json) |
| 全新离线 PVC | 74 个插件全部启用，分支/tag/commit、服务、多包、大包、三镜像及失败产物保留等 16 项通过；[报告](verification/platform-offline.json) |
| 数据恢复 | 配置版本 21、21 条审计和完整配置与备份一致；站点默认并发 6/离线 true 不覆盖保存的并发 3/离线 false；[报告](verification/platform-restore.json) |
| 恢复后实际构建 | 使用保存的 5 CPU/9GiB 临时空间构建成功，元数据与 Pod 一致；[报告](verification/platform-restored-service.json) |
| 主实例最终验收 | #96 服务、#97 镜像成功，默认预算与元数据正确；原有 98 次构建参数、结果、保留标记及根元数据 SHA256 全部未变；[报告](verification/platform-main.json) |

实际 Pod 验收发现 Kubernetes 插件会覆盖 YAML 中的工作区卷配置，已显式设置 workspaceVolume.sizeLimit 并复验。
存储验收发现 GroovyShell 对复合加法中的 long 转换存在类型异常，已改为显式 long 累计，并以运行中的 2GiB 预留验证。

xcn 完整发布使用验收脚本默认的发布开发分支，未改动业务 main；内网 Registry 的非致命推送失败如实记入元数据，有效本地包仍可下载。
主实例保持离线业务默认值；当前外网环境的真实编译显式选择 Never，迁入内网后仍需验证内网 APT/Registry 连通性。

---

以下保留早期版本的验收记录。

# 验证结果

## xcn 站点离线模式与参数文案（2026-11-06）

主实例已部署 Controller `2.580.2-10`，构建页面移除 `OFFLINE`，现为 6 个参数。
模式统一读取管理员站点配置 `xcn.offline`，默认 `true`；新构建仍记录实际模式，已有历史保持原值。
两个版本说明已分别改为“Chart 版本，默认当天日期”和“应用版本，默认当天日期”。

20 项 Python 测试通过，包括配置默认值、布尔类型校验，以及配置变化对应的 Pod 哈希。
独立 Jenkins #21–26 验证 `offline=true`，#27–32 验证 `offline=false`：
普通用户 API 故意传入相反的 `OFFLINE`，每次实际模式仍跟随站点配置，默认/手填版本保存正确。
两组测试均保留真实参数处理、锁和元数据保存，在槽位内跳过业务编译；历史回填与磁盘重载检查通过。
两次部署间 Pod UID 和配置哈希均变化，确认管理员改配置后能实际滚动更新。

主实例恢复接单，5 条已有历史的全部参数及元数据哈希一致。
普通用户浏览器核对六参数表单、两处精简文案及默认日期，历史 HTTP 复制、X-RAN-CLOUD 条件显示和权限回归通过。
使用 `--number 1 --online` 复验原在线成功发布，四包 SHA256、Helm lint/template、三个镜像导入和断网运行均通过。
本轮没有在不可达的内网 APT 上重复完整业务编译。证据见 [xcn-site-mode.json](verification/open5gs-site-mode.json)。

## xcn 历史版本与 HTTP 复制修复（2026-11-06）

主实例已升级 Controller `2.580.2-9`。新构建在参数校验后将实际 Chart/app 版本写入历史 Parameters；
启动时按各次构建保存的元数据补齐空版本，已回填主实例 4 条历史。成功发布 #1 的两个版本均为 `26.11.6`，
其余参数、原产物清单和全部历史元数据哈希保持一致；显式填写的版本不被迁移覆盖。

独立 Jenkins 普通账号完成 6 组短流程验证：省略版本、传空、仅空白、显式版本、混合默认及两端空白。
测试保留真实参数解析/保存和锁逻辑，在取得槽位后跳过业务编译。额外验证旧日期回填、显式值保护、
缺失元数据跳过、重复迁移幂等，以及 build.xml 重新加载后的值；不修改主实例测试数据。

普通用户真实 HTTP 浏览器中，#1 两个版本可见且均实际点击复制、粘贴校验成功，HTTPS 限制提示已移除。
X-RAN-CLOUD 服务/镜像历史的条件显示、复制，新建表单双向切换和输入保留，以及普通用户首页权限均通过。
HTTPS 下继续使用 Jenkins 原生剪贴板功能。19 项 Python 测试通过；复验 #1 下载的四个产物 SHA256、
Helm lint/template 和三个镜像断网运行全部通过，没有重新编译业务。证据见 [parameters-xcn.json](verification/parameters-open5gs.json)。

## xcn 扩展（2026-11-06）

主实例已部署 Controller `2.580.2-8`，新增独立 `xcn` 任务，原 `x-ran-cloud` 保持可用。
编译镜像已增加 Helm 3.17.3、PyYAML 和发布适配器，最终配置 ID：
`sha256:c35fc07fbb6ac293b26bf4dbb222dc5e014b07304708e56adf814307520e2b51`。
断网重建得到同一 ID，导出的 image.tar/manifest.json 校验通过，原有 SDK 两个 CTest 和附加检查通过。

| 验证 | 实际结果 |
|---|---|
| Python 自动化测试 | 19 项通过，包括产物缺失/损坏/版本不符、OFFLINE 参数和临时空间边界 |
| 完整 xcn 发布 | 主实例 #1 SUCCESS；真实 develop 提交 `49a9671f78814b27c171c681a8acb75ecf255f94`，约 37 分钟 |
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
| 页面 | 七个 xcn 参数顺序/默认值正确，模拟另一天重新打开表单生成新日期；两项目历史 Parameters 保留原行为 |

完整证据见 [xcn.json](verification/open5gs.json)。共享锁测试保留真实参数校验和两层锁，在获得槽位后用短等待代替编译；
压力测试仅作用于专用验收 PVC，验证运行中、标记保留的版本不被清理以及提示/审计。两个临时验收 Controller 已暂停。
主实例两任务的流水线 SHA256 与仓库一致、没有按天数/次数丢弃策略，Jenkins 已恢复正常接单。

本次完整发布使用验收脚本默认的发布开发分支且 OFFLINE=false；没有修改 xcn 业务源码或 main。
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
