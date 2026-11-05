# 编译依赖资料

`manifest.json` 是源码/二进制归档的版本、来源和 SHA256 清单。`archives/` 中大文件采用最多 50MiB 的普通 Git 分片，
`scripts/compile-image.py` 自动验证并还原到 `.cache/compile-sources/`，随后 Docker 在无网络条件下编译。
清理了嵌套 Git 元数据、旧构建目录和重复归档；原 ROHC `.tar.xz` 实际为未压缩 tar，已规范为 gzip 归档。
各开源组件的许可证保留在相应归档中；原 libairtracer 二进制没有随附许可证，来源单独记录。

`legacy/` 为装机脚本和源配置的原始记录，不作为镜像安装入口。
有效安装过程、修正和版本说明见 [编译镜像说明](../../docs/compile-image.md)。
WLS 和 Intel oneAPI 不安装，原提供的不兼容 etcd/OTel 预编译库不使用。
