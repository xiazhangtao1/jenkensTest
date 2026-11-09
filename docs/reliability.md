# 完整回归与持续构建验收

`make verify-release` 在独立 Jenkins 上验证构建、权限、资源配置、故障恢复和连续负载。任何阶段失败都会以非零状态退出；只有全部阶段通过才生成 `PASS` 总报告。真实 xcn 完整编译与后续至少 30 分钟持续构建分别计时。

## 准备独立实例

需要已经可用的 Kubernetes、local-path-storage、Docker、Helm、kubectl、Python 3/PyYAML，以及项目已有的离线插件、Maven、基础镜像缓存。使用对测试集群有操作权限的账户；Ubuntu 上可按部署命令的方式使用 `sudo env "PATH=$PATH:$PWD/.cache/tools"`。

本机 Docker 与测试节点需要当前 Controller、两套编译镜像、agent、DinD 和测试业务基础镜像。准备脚本会将自己的 demo、旧适配器、Controller 和两个编译镜像导入单节点 containerd；多节点测试须预先向每个候选节点导入相同镜像。测试浏览器使用可选的 `.cache/verification-tools` 缓存，包含 Node、Playwright CLI 和 Chromium；它不参与日常部署。
直接导入交付的 Controller 时沿用镜像锁文件。若自行重新构建，先用 `docker image inspect --format '{{.Id}}' 镜像引用` 取得实际 ID，并同步 `config/images.lock.json` 中该 Controller 的记录，再执行验收；相同 tag 不能替代镜像 ID 核对。
Ubuntu 22.04 的无桌面主机若缺少浏览器系统库，可从内网 APT 安装：

```bash
sudo apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libasound2 libpango-1.0-0 libcairo2
```

在联网验证环境中，准备本地 X-RAN-CLOUD 模拟库和 xcn 业务库，xcn 使用已经验证的发布开发分支，不修改业务代码或 main：

```bash
python3 tests/reliability_setup.py \
  --site .state/reliability-site.yaml \
  --base-site config/site.local.yaml \
  --source ../X-RAN-CLOUD \
  --xcn-source /实际路径/xcn
```

脚本只允许新建 `jenkins-reliability-verify`，默认 NodePort `32180`。已存在 namespace、站点文件或端口时直接拒绝覆盖。它复制两个 Git 库，在副本中增加孤立的 `reliability-soak` 分支，用独立 PVC、Registry、账号和 Secret 启动 Jenkins。真实业务分支和宿主机原库不变。
`--base-site` 指向已配置好本机地址的站点文件，省略时使用 `config/site.yaml`。它不会更新该站点的部署；主实例只读核对使用集群 ConfigMap 中的实际地址。

验收另使用 NodePort `32182` 和全新 namespace `jenkins-reliability-restore` 验证备份恢复，端口也应空闲。不要把验收参数换成生产站点；入口会检查 namespace、实际服务端口、Controller 版本和 Jenkins 中的站点身份。

## 执行

真实 xcn 在线构建需要可达的 Ubuntu Jammy 软件源，通过参数注入本次验收构建：

```bash
make verify-release SITE=.state/reliability-site.yaml \
  VERIFY_APT_MIRROR=http://可达的软件源/ubuntu \
  SOAK_MINUTES=30
```

本次外网测试使用 `tests/apt_mirror.py` 将公开 Ubuntu 软件包缓存为 Pod 可访问的内网 HTTP 地址。它只允许读取固定上游的 Jammy 索引和 deb，不能转发任意站点、上传或 CONNECT；不可变索引会核对 by-hash。它沿用进程的代理环境，未修改测试 Pod 的离线出口策略，也不参与正式部署。

真实 xcn 阶段保留业务脚本原有的镜像仓库地址，使用唯一的 `reliability-real-*` 标签，不带入生产推送凭据；脚本仍会尝试推送。本轮外网环境无法访问内网 Harbor，三个推送失败如实记为警告，下载包仍需通过全部检查。持续负载和 X-RAN-CLOUD 测试使用独立 Registry，另行验证成功推送。

```bash
python3 tests/apt_mirror.py --listen 本机内网IP --port 18083 \
  --cache .state/jenkins-reliability-verify/ubuntu-cache
# 在另一终端运行 make，将 VERIFY_APT_MIRROR 指向 http://本机内网IP:18083/ubuntu。
```

`COMPAT_IMAGE`、`SDK_IMAGE` 可覆盖两套编译镜像引用。持续时间只能设为 30 分钟或更长。真实 xcn 构建可能更久，期间可以查看 Jenkins 日志；镜像 tar、Chart、下载校验和断网运行检查完成后，才进入后续验收。

意外中断后，可以接续同一份代码和实例上已经通过的阶段：

```bash
make verify-release SITE=.state/reliability-site.yaml \
  VERIFY_APT_MIRROR=http://可达的软件源/ubuntu VERIFY_RESUME=1
# 若完整 xcn 构建仍在运行，可额外指定 VERIFY_XCN_NUMBER=本轮构建号，避免重复提交。
```

只接续八小时内、源码提交和编译镜像与本轮固定输入一致的 xcn 构建。发布源文件变化时拒绝复用；Python 测试输入变化会重跑。已通过的持续测试也必须满足本次要求的时长、任务数和原始采样 SHA256，不能用较短测试冒充较长测试。失败阶段修复后会重新执行。

## 覆盖范围

| 范围 | 实际检查 |
|---|---|
| 基础测试 | Python 回归；插件断网 clean verify、Surefire、SpotBugs；两套 SDK 断网自检 |
| 构建与产物 | 分支、tag、完整/短 SHA；单服务、`services_all`、三个业务镜像；真实 xcn 完整编译及四个产物 |
| 编译镜像 | Always、IfNotPresent、Never；同 tag 更新后缓存/刷新行为；不同 SDK 并发；非法、缺少或不兼容镜像的失败诊断 |
| 页面与权限 | BUILD_KIND 条件字段与输入保留；历史 Chart/app 版本；HTTP 复制；管理员原生表单；普通账号隔离；项目管理员可见开关 |
| 并发与配置 | 六个全局槽位、额外排队、同标签互斥；动态降并发；三类修改后真实 Pod 的 CPU/内存/临时空间、入场快照与元数据一致；取消和超时后释放 |
| 存储 | 实时阈值、预留预算、最早未保留版本回收、审计提示；运行中及长期保留版本保护；空间不足拒绝 |
| 故障与持久化 | 删除指定构建 Pod；96MiB 测试容器内存不足；后续正常构建；Controller 重启后历史、账号及网页设置一致 |
| 备份恢复 | 备份 HOME，恢复到全新 PVC；核对历史、设置、用户与产物清单；恢复后真实构建和下载校验 |
| 持续负载 | 至少 1800 秒；服务、镜像、xcn 各至少 10 个；实际六个运行 Pod和两个等待任务；逐件校验所有产物 |

持续测试中的 xcn 使用独立的短负载分支，实际执行 Docker build/save/push 和 Helm package，避免用完整业务重复编译代替平台持续负载。真实业务完整编译另有独立阶段。测试采集 Controller UID/重启次数、堆内存、线程、磁盘余量、队列、槽位、预留及 Pod 状态；非预期失败、重启、OOM、驱逐或产物不一致都会失败。结束后必须没有遗留任务、锁、预留和构建 Pod。
报告中的 `maximumRunningPods` 按 Kubernetes phase 统计，包含已收到删除请求、仍在终止宽限期的 Pod，可能短暂超过六个；六个同时构建的限制按持久化槽位核对，终止中的 Pod 在彻底退出前仍占用节点资源。收尾必须等待它们全部清空。
xcn 基础镜像初始化复制使用 1GiB 内存上限，给归档读写的文件缓存留出空间；它先于常规容器运行，仍在默认 8GiB Pod 总预算内。持续采样也检查初始化容器的退出原因。

测试会暂时修改验收实例的流水线和网页配置，结束后恢复。生产启动时按仓库模板更新 Pipeline 的现有行为保持不变；网页临时改动仍会在 Controller 重启后被模板覆盖。

## 证据与范围限制

结果位于 `.state/jenkins-reliability-verify/`：

- `reliability-report.json`：阶段结果、源码指纹、固定提交、镜像 ID、产物和资源摘要。
- 各阶段 `.log` 和专项 JSON：详细断言与 SHA256。
- `soak-progress.json`、`soak-samples.jsonl`：持续测试实时进度和原始采样。
- `browser-verification.json` 及 `output/playwright/reliability/`：真实页面检查。

`.state` 中含测试账号和临时浏览器认证文件，不应整目录提交或装入通用离线包。通过后仅导出不含凭据的验收报告到 `docs/verification/`。成功恢复后删除本次 HOME 备份，恢复实例缩容为零，保留 PVC 便于复核。

验收不会重启宿主机、填满宿主机磁盘或修改已有主 Jenkins。报告还核对主实例的 Pod/PVC 身份、历史与网页设置未发生变化。30 分钟结果说明本次受控负载下的表现，不能代替长期运行或真实内网 Gerrit/APT/Harbor 的现场验收；内网全新部署后仍需检查实际凭据、软件源、镜像仓库和业务清单。

可选浏览器工具可单独搬运，避免重复携带部署依赖和镜像：

```bash
tar -czf optional-verification-tools.tar.gz .cache/verification-tools
sha256sum optional-verification-tools.tar.gz > optional-verification-tools.SHA256SUMS
# 内网项目根目录中核对后解压，仅完整浏览器验收需要。
sha256sum -c optional-verification-tools.SHA256SUMS
tar -xzf optional-verification-tools.tar.gz
```

工具缓存只包含公开的 Node/Playwright/Chromium 程序和许可证，不包含 `.state`、浏览器个人资料或账号。
