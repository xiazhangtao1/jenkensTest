# 验证结果

2026-11-05 在 Ubuntu 22.04 amd64、Kubernetes 1.26.8/containerd 1.6.32 上完成实际验收。
Jenkins 2.580.2、官方 Helm chart 5.10.10，锁定的 73 个插件全部启用。

| 验证 | 结果 | 证据 |
|---|---|---|
| 运行时/资源配置测试 | 7 项通过；包含真实 Git HTTP 检出、C 编译和 tar 校验 | tests/test_runtime.py、tests/test_config.py |
| 维护任务权限 | 7 项通过；管理员访问、手动/定时维护、普通账号隔离及构建下载均正常 | [maintenance-permissions.json](verification/maintenance-permissions.json) |
| Jenkins 主实例 | 20 项通过，30 次实际构建包含预期失败与取消场景 | [jenkins.json](verification/jenkins.json) |
| 原生参数页 | 服务/镜像整行条件显示、双向切换、输入保留、普通账号页面提交均通过 | [parameters.json](verification/parameters.json) |
| 全新 PVC 离线首启 | 15 项通过，全部服务与镜像模式、引用选择、下载、失败留存 | [offline.json](verification/offline.json) |
| 离线网络与镜像策略 | Controller、init、Agent、编译、DinD、Git、Registry 均 Never；公网连接超时被阻止 | [offline-egress.json](verification/offline-egress.json) |
| 新 PVC 备份恢复 | 8 项通过；41 次历史、审计、参数、脚本一致，历史服务/镜像可下载使用，恢复后新构建 #42 成功 | [restore.json](verification/restore.json) |
| 工具链离线重建 | Docker build --network none 实际成功 | docker/builder.Dockerfile、config/images.lock.json |
| 离线打包 | 已实际导出并校验文件 SHA256、73 个插件发布校验值、8 个运行镜像 ID | 最终目录的 MANIFEST.json、SHA256SUMS |

`storage-maintenance` 使用项目权限矩阵并关闭普通任务权限继承，仅具有 Overall/Administer 的管理员可见。
builder1、builder2 的首页和 API 清单仅显示 x-ran-cloud，维护任务直链、API、配置、日志和构建请求均返回 404。
管理员手动维护 #22、TimerTrigger 维护 #23 成功；builder1 的 serviceB 构建 #42 成功并核对下载产物。
只读权限回归可执行 `python3 tests/verify.py --permissions-only --report .state/jenkins/maintenance-permissions.json`，
不会提交构建或执行磁盘压力清理测试。

页面模式：BUILD_KIND=service 仅显示 SERVICE_TARGET；BUILD_KIND=image 仅显示 IMAGE_TARGET、IMAGE_TAG。
REF_TYPE/GIT_REF 始终可见。实际从页面提交的 #34 serviceC 和 #35 imageB 均成功，
镜像已完成 build/save/push/download/load/run；截图留在验证机 output/playwright/。
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
