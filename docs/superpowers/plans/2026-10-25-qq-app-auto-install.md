# QQ 辅助应用自动补装实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** QQ 任务启动雷电后自动补装缺少的辅助应用，失败继续原扫码尝试。

**Architecture:** 私有 OSS 固定版本 + Cloudflare 摘要配置；父进程生成带 Date 的固定 GET 下载签名，子进程下载缓存并调用严格绑定的 ADB 安装方法。已有包、原扫码时限和微信流程保持原行为。

**Tech Stack:** Python、标准库 HTTPS 下载、oss2、ADB、pytest。

**Spec:** ../specs/2026-10-25-qq-app-auto-install-design.md

## Global Constraints

- 只安装缺失包；不加 `-r`、不卸载，不自动处理签名冲突。
- 下载不持全局实例锁；安装前后复核实例绑定和目标包。
- 不把下载签名、OSS 密钥写入日志或公开任务 JSON。
- 不延长任务截止时间；下载或安装失败继续原 QQ 扫码尝试。
- 微信和仅二维码模式不新增辅助应用操作。

## Review Focus

- 同时两个任务首次下载时，不读取半文件或无限等待缓存锁。
- 已有包在检查与安装之间出现，不能覆盖；实例已被换主不能操作。
- 下载超时、内容损坏及大文件不被安装，错误不泄漏签名 URL。
- 只安装成功一款或探测失败时，准确回退原 QQ 尝试。
- 两包齐全时零下载，Cloudflare 缺配置仍兼容旧部署。

## 任务

- [x] 下载模块与回归测试：`capture_apks.py` 定义固定包名、受限下载缓存与安全错误；先验证失败，再实现摘要缓存、失败清理和并发原子发布。
- [x] 雷电安装方法与测试：`ldplayer.py` 增加严格缺失列表和仅缺失安装方法，保留原布尔探测兼容；先验证失败，再实现复核绑定、ADB安装和复测。
- [x] 配置与父子传递：`config.py`、`environment.py`、`storage.py`、`runtime.py` 添加 `QQ_APK_RELEASE_PREFIX`、`QQ_CAPTURE_APK_SHA256`、`QQ_LOGIN_APK_SHA256`；下载 URL、签名头和摘要使用内部 `QQ_APK_DOWNLOADS` 环境变量，不从本地回退。
- [x] Worker 接入与矩阵：`ldplayer_worker.py` 在QQ分支探测缺包后补装；失败尝试旧扫码；覆盖双包、单包、无包、失败、过期和微信场景。
- [x] 发布并回读两环境私有OSS对象及摘要，写入Cloudflare三项配置并回读；不重启常驻任务。
- [x] 运行相关登录、环境与存储测试并独立审查，完成中文提交及 main MR 的交付准备。上次全量已确认的四项无关失败不重复扩大排查。

## 验证记录

- 登录服务全部测试、环境隔离与 Cloudflare 配置：1554 项通过，1 项第三方弃用提示。
- 正式、测试环境的两款私有 APK 均完成从零下载、SHA256 校验及二次缓存复用验证；OSS 对象 ACL 和 Cloudflare 三项配置均已回读核对。
- 多轮独立审查覆盖中断/过期、实例归属、请求头签名范围、安装不覆盖、失败回退及缓存并发；修复后并发下载单项连续 30 轮通过。
- 安装命令、包复查和四种缺包组合已通过模拟 ADB 边界测试；未卸载现有应用，也未使用空白雷电实例进行实装验收。未重启正式服务。
- 部署方式：合并 MR，拉取代码后重启常驻 worker。既有应用不自动升级；缺包时才使用已发布的版本。
