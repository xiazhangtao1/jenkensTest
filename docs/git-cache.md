# Git 本地缓存

管理员进入 **Manage Jenkins → 系统配置 → 构建平台设置**，管理“启用 Git 本地缓存”和
“所有项目 Git 缓存合计预算（GiB）”。默认开启，所有项目合计 20GiB；设置保存在 Jenkins 数据 PVC，重启后继续生效。
项目状态展示占用、最后成功更新时间及错误；清空按钮只允许管理员使用，更新或复制期间拒绝清空。

每个注册项目保存独立裸仓库，首次获取全部分支、tag 的完整历史，以后增量更新。
只同步 `refs/heads/*` 和 `refs/tags/*`，不复制 Gerrit 的 `refs/changes/*`。
每次先成功更新远端，再固定完整 commit，复制到临时 Pod 的独立 `.git` 和源码目录中编译。
主分支、相近分支复用已有对象，完整或唯一短 SHA 仍受支持；远端失联或认证失败直接报错，不用旧 main 继续构建。
子模块继续按原入口初始化；本次未增加子模块或 Git LFS 专用缓存。

更新、复制和清理按项目共用文件锁；编译阶段不持锁，原来的最多六个构建并发和 Pod 资源预算不变。
工作副本不使用硬链接或 alternates，保留 Git 历史；清空缓存不会影响已经完成检出的构建。
缓存按项目、Git 地址和凭据 ID 隔离，修改连接配置后不会串用旧仓库。
构建日志和产物中的 `metadata.json` 记录缓存命中及等待锁、更新、复制、检出、子模块耗时。
中断遗留的 Git 锁在取得项目锁后清理；缓存损坏时复查对象并重建，工作树展开失败最多重试一次。
完整 fsck 仅用于错误诊断，不增加正常缓存命中的全量扫描；本地传输的打包线程及窗口内存也受限。

## 容量与清理

缓存使用独立的 `xran-git-cache` PVC，默认声明 20Gi，与 Jenkins_HOME 分开，卸载保留。
local-path 声明不会预占 20Gi 或提供硬配额；网页预算是所有项目合计的逻辑回收阈值。
实际缓存占用小就只使用实际空间，不是每个项目预留 20Gi。

超预算按最近使用时间回收闲置缓存；持锁项目受保护，可能暂时超出预算。
若单库本身超过预算，完成复制后也可能被回收，后续重新下载；此时应在网页调大预算。
使用有硬配额的存储驱动时，调大网页预算前也要扩容 PVC，`gitCachePvcSize` 属于部署配置。
磁盘压力仍优先回收最旧完成、未标记长期保留的历史产物，然后回收闲置缓存。
不同磁盘分别判断，避免删除不能释放目标磁盘空间的产物；回收后仍不足则停止构建。
明细保存在管理员专用 `storage-maintenance` 和 Jenkins_HOME 的 `xran-storage/git-cache-cleanup.jsonl`。

缓存位于 Controller 的 `/var/xran-git-cache/<项目>/`；编译 Pod 仅挂载本项目子目录。
目录按统一组权限初始化，不在每次 Pod 启动时递归 chown。当前按单节点 local-path 使用；
多节点需要相应的节点缓存或共享存储，不能把 local-path 当成跨节点共享磁盘。
缓存可以重建，不进入 Jenkins_HOME 备份、`01-dependencies` 或 `02-code`。

## 内网升级

已有本项目离线依赖和编译镜像时，只需同步本次 `02-code`，不必重传依赖或重做编译镜像。
新增检出脚本随 ConfigMap 下发，现有带 Python/Git 的兼容编译镜像可以继续使用。
保留原 `config/site.local.yaml` 和 `.state`，在项目根目录单独制作新 Controller：

```bash
sudo make platform-plugin OFFLINE=1
sudo docker build --network none --pull=false \
  -f docker/controller.Dockerfile -t xran-ci/controller:2.580.2-22 .
sudo docker save -o /tmp/xran-controller-22.tar xran-ci/controller:2.580.2-22
sudo ctr -n k8s.io images import /tmp/xran-controller-22.tar
sudo env "PATH=$PATH:$PWD/.cache/tools" \
  make deploy SITE=config/site.local.yaml OFFLINE=1
```

如果原来通过 Registry 分发 Controller，按现有 Registry 地址推送并更新相应配置。
部署会创建缓存 PVC，并保留原 HOME、账号、凭据、设置和历史；第一次构建会建立缓存，后续才能复用。
无需先卸载 Jenkins，不删除 PVC。

只需停用缓存时，在网页关闭开关，后续构建恢复直接拉取。
需要回退整个平台时，恢复上一版代码与匹配的 Controller 镜像后重新部署，保留两个 PVC。
部署基础配置可选增加 `gitCachePvcSize: 20Gi`；已有站点没有此字段时自动使用默认值。

隔离验收使用 `tests/verify-git-cache.py`，报告位于 `docs/verification/git-cache.json`。

开发时可用现有编译镜像断网运行 Git 专项测试，包含真实 root / UID 1000 的双向缓存复用：

```bash
sudo docker run --rm --network none --user 0:0 -e HOME=/root \
  -v "$PWD:/workspace:ro" -w /workspace \
  192.168.10.35/xnet/xran-compile:latest \
  python3 -m unittest discover -s tests -p test_git_cache.py -v
```
