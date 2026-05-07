---
name: ztp-bbu-config
description: BBU设备 ZTP 开站 + TR-069 参数配置工具。自动识别 V5/V6 版本，支持修改所有关键参数并触发 factory reset。
metadata:
  {
    "openclaw":
      {
        "requires": {},
        "install": []
      }
  }
---

# ZTP BBU Configuration

BBU设备 ZTP 开站 + TR-069 参数配置工具。

## 参数

| 参数 | 说明 |
|------|------|
| host | (必填) BBU IP地址 |
| version | (可选) V5 或 V6，默认自动检测 |

## 使用

```bash
# 指定IP
ZTP开站 192.168.5.100

# 指定IP + 版本
ZTP开站 192.168.5.100 V5

# 环境变量指定版本
BBU_VERSION=V5 ZTP开站 192.168.5.100
```

## 支持版本

| 版本 | 用户名 | 密码 |
|------|--------|------|
| V6（默认） | f3mto5gus3r | H@rm@n$Nr!Fmt0$00.5G_$TrnG |
| V5 | root | Smallcell@5 |

V6 连接失败会自动降级 V5。

## 修改的参数

- `Device.CertMS.URL`
- `Device.ManagementServer.URL`
- `Device.ManagementServer.STUNServerAddress`
- `Device.IPsec.Conn.1.Gateway.SecGWServer1`
- `Device.Ethernet.Interface.1.IPv4Address.2.*`（新增/更新节点）
- `Device.Time.NTPServer1`（→ time.windows.com）
- `Device.SecurityManagement.WebEnable`（→ 1）
- `Device.SecurityManagement.SshEnable`（→ 1）

## 执行流程

1. SSH 连接 BBU（支持命令行传 IP）
2. 备份远程配置（`confdb_v2.xml.bak.<timestamp>`）
3. 下载当前配置
4. 修改 TR-069 参数（基于 XML 标签搜索，不依赖行号）
5. 上传新配置
6. 下载验证（9 项全检）
7. 触发 ZTP factory reset（设备自动重启）

## Bug修复记录

- [FIX] 修复重复 `tool.connect()` 导致的 Socket is closed
- [FIX] 修复 hardcoded 行号 → 改为 XML 标签搜索
- [FIX] 新增命令行参数支持（`python bbu_config_tool.py <IP> [V5/V6]`）
- [FIX] 修复 _drain() 固定 sleep → 动态等待
- [FIX] 修复 ZTP 等待循环 → 主动检测通道状态，提前退出
- [FIX] 参数搜索改为正则匹配，不依赖行号

## 工具

- 源码: `skills/ztp-bbu-config/bbu_config_tool.py`
- CLI: `D:\openclaw\tools\bbu_config_tool.exe`
- GUI: `D:\openclaw\tools\bbu_config_gui.exe`
