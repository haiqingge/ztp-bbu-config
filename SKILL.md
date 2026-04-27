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

## 使用

```
ZTP开站 192.168.5.100
```

## 版本

| 版本 | 用户名 | 密码 |
|------|--------|------|
| V6 | f3mto5gus3r | H@rm@n$Nr!Fmt0$00.5G_$TrnG |
| V5 | root | Smallcell@5 |

## 修改的参数

- `Device.CertMS.URL`
- `Device.ManagementServer.URL`
- `Device.ManagementServer.STUNServerAddress`
- `Device.IPsec.Conn.1.Gateway.SecGWServer1`
- `Device.Ethernet.Interface.1.IPv4Address.2.*`（新增节点）
- `Device.Time.NTPServer1`（→ time.windows.com）
- `Device.SecurityManagement.WebEnable`（→ 1）
- `Device.SecurityManagement.SshEnable`（→ 1）

## 工具

- CLI: `D:\openclaw\tools\bbu_config_tool.exe`
- 源码: `C:\Users\sw\.openclaw\skills\ztp-bbu-config\bbu_config_tool.py`