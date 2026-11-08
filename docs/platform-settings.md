# 构建平台设置

管理员进入 **Manage Jenkins → 系统配置（System）→ 构建平台设置**，修改后点击页面底部的保存或应用。普通用户无此配置权限，预览、状态及重新应用接口也只允许 Overall/Administer。

| 设置 | 初始默认值 |
| --- | --- |
| 两个项目共享的最大并发 | 6，支持 1–32 |
| 服务包 / 镜像 / xcn 三套 Pod 总上限 | 各 4 CPU、8GiB 内存、20GiB 临时存储 |
| 镜像 / xcn DinD 内存 | 各 4GiB，计入所属 Pod 总内存 |
| xcn 内层构建内存 / 并行数 | 3GiB / 2 |
| 默认编译环境 / 拉取策略 | 192.168.10.35/xnet/xran-compile:latest / Always |
| xcn 离线构建 | 开启，业务脚本增加 --offline |
| X-RAN-CLOUD 服务编译串行执行 | 开启，服务包及镜像任务中的服务 Makefile 使用串行 |
| 入场后总超时 / Pod 启动超时 | 240 分钟 / 15 分钟 |
| 磁盘可用空间下限 | max(50GiB, 所在文件系统总容量的 10%) |

CPU 只填整数核，内存和临时存储填整数 GiB。内部 jnlp、builder、DinD 的 requests/limits 自动计算；点击“预览内部资源分配”查看候选值。内存总预算必须容纳 Agent、编译容器与 DinD；xcn 内层构建额度必须低于 DinD 内存。非法预算整次拒绝保存。

服务编译串行开关对应 `xranServiceSerial`，通过 `XRAN_SERVICE_SERIAL=true/false` 传给编译镜像。
默认对本次检出的 `services/makefile` 幂等加入 `.NOTPARALLEL:`，适用于脚本显式 `make -j32` 的情况。
仅修改隔离工作副本，不提交业务仓库；没有该 Makefile 的版本仍执行原入口。
关闭开关后，新工作副本遵循业务脚本原有并行设置。该开关不限制 Jenkins 同时运行的任务数量，也不改变 xcn 内层并行数。
服务 Pod 默认仍为 8GiB；这是当前内网串行编译已验证的额度，实际项目或镜像任务仍按自身需求配置。

每次修改都有递增配置版本和用户、时间、修改前后值。页面展示已保存版本、已应用版本及运行中数量。两个管理员同时打开旧表单时，后保存者会收到刷新提示，避免覆盖他人的修改。完整配置、审计和槽位位于 Jenkins 数据 PVC 的 `xran-platform/state.json`；正常备份 Jenkins HOME 会一起备份。

xcn 命名升级会保留已有设置的数值、追加一次迁移审计；旧审计原始数据保留，网页按当前字段名称显示。
升级到插件 1.0.4 时，旧配置只补充缺失的串行开关为开启，追加 `build-compatibility-upgrade` 审计；已有资源值和关闭状态不被覆盖。

设置在**构建取得全局槽位**时读取。排队构建使用最新版本；正在运行的构建保留入场时的版本、资源及超时，重启后继续使用同一快照。普通用户在构建参数中填写的编译镜像和拉取策略仍优先；修改管理员默认值会更新后续构建表单。

同镜像同标签 / xcn 同 app 版本先等待标签锁，再申请全局槽位。上述两种等待都不计入构建总超时；取得槽位后开始计时，包含 Pod 启动。取消、失败和完成都会释放持久化槽位，重启时根据实际运行记录恢复。

降低并发不会终止现有构建。例如 6 个正在运行时改为 2：它们继续完成，只有运行数量低于 2 时才能放行新任务。Kubernetes Cloud 的 Pod 上限同步更新；节点可调度资源仍会约束实际 Pod 数量。

磁盘检查始终使用当前下限，已经入场的任务按各自资源快照预留空间。历史包继续长期保存，只在磁盘压力时回收最早完成且未标记长期保留的产物。清理提示、审计及保留保护见 [运维与存储](operations.md)。

PVC、StorageClass、namespace、Controller 资源、Git/Registry 地址和凭据等基础设施设置继续在 `config/site.yaml` / Helm 管理。旧的并发、资源、超时、磁盘和离线字段仅用于**首次导入**；已存在网页配置时不会再次覆盖。要更改已部署实例的这些参数，请使用网页。

应用配置失败时，页面显示原因，新构建停止入场，已有构建继续。修正 Kubernetes Cloud 等原因后，点击“重新应用配置”。

## 插件构建与离线迁移

源码在 `plugins/xran-build-platform`。固定使用 Maven 3.9.9 / JDK 21 容器、Jenkins 2.580.2 API，运行完整 `clean verify`，无需主机安装 Java/Maven：

```bash
make platform-plugin
# 离线包已包含 Maven 镜像和完整依赖缓存
docker load -i .cache/build-base-images.tar
make platform-plugin OFFLINE=1
```

离线构建同时启用 Maven `-o` 和 Docker `--network none`。插件发布物 `.cache/local-plugins/xran-build-platform.jpi` 与 `config/platform-plugin.lock.json` 单独校验，不经过官方更新中心解析。Controller 镜像内置此 JPI。离线包包含插件源码、JPI、Maven 缓存和构建镜像；官方 73 个插件保持原版本。

专用实例的入场和恢复验收：

```bash
python3 tests/verify-platform.py --site /路径/独立验收站点.yaml
```

该脚本会临时替换两项任务的构建主体并重启 Controller，要求站点显式设置 `verificationInstance: true`，禁止在主 namespace `jenkins` 执行。
