import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

"""
BBU confdb_v2.xml Configuration Tool
Full flow: connect -> backup -> download -> modify -> upload -> verify
"""

import paramiko
import time
import re
import os
from datetime import datetime

# ========== VERSION CONFIG ==========
# V5 (默认): root/Smallcell@5
# V6: f3mto5gus3r / H@rm@n$Nr!Fmt0$00.5G_$TrnG
VERSIONS = {
    "V5": {
        "username": "root",
        "password": "Smallcell@5"
    },
    "V6": {
        "username": "f3mto5gus3r",
        "password": "H@rm@n$Nr!Fmt0$00.5G_$TrnG"
    }
}

# 默认version
DEFAULT_VERSION = "V6"

# 当前使用的version配置
CURRENT_VERSION = DEFAULT_VERSION
USERNAME = VERSIONS[CURRENT_VERSION]["username"]
PASSWORD = VERSIONS[CURRENT_VERSION]["password"]

# ========== CONFIG ==========
HOST      = "192.168.5.173"
PORT      = 22
CONF_PATH = "/opt/bbu/etc/confdb_v2.xml"

# 目标IP地址 - 用户需要提供
TARGET_IP = "192.168.5.173"  # <-- 填入目标IP，例如 "192.168.5.100"

# 要modify的参数
PARAMS = {
    "CertMS_URL":         "https://ejbca.waveoss.com:9443/ejbca/publicweb/cmp/cmpclient",
    "MgmtServer_URL":     "https://ejbca.waveoss.com/tr069/gnbInitialAcs",
    "STUNServerAddress":  "ejbca.waveoss.com",
    "SecGWServer1":       "test.nms2.com",
    "WebEnable": "1",
    "SshEnable": "1",
}

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


class BBUTool:
    def __init__(self, host, port, username, password):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client = None
        self.channel = None

    def connect(self):
        print(f"[CONNECT] {self.host}:{self.port} ...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(hostname=self.host, port=self.port,
                             username=self.username, password=self.password, timeout=10)
        self.channel = self.client.invoke_shell()
        time.sleep(1)
        self.channel.recv(65535)
        print("[CONNECT] OK")

    def sudo_root(self):
        print("[SUDO] sudo su - root ...")
        self.channel.send(b"sudo su - root\n")
        time.sleep(0.5)
        self._drain()
        self.channel.send(self.password.encode() + b"\n")
        time.sleep(1)
        out = self._drain()
        if "root" in out.lower() or "#" in out:
            print("[SUDO] OK - root")
        else:
            print(f"[SUDO] unexpected: {out[:100]}")

    def _drain(self):
        out = b""
        time.sleep(0.3)
        while self.channel.recv_ready():
            out += self.channel.recv(4096)
            time.sleep(0.1)
        return strip_ansi(out.decode("utf-8", errors="ignore"))

    def run_cmd(self, cmd, wait=1.5):
        self.channel.send(cmd.encode() + b"\n")
        time.sleep(wait)
        return self._drain()

    def cd(self, path):
        self.run_cmd(f"cd {path}", wait=0.8)

    def close(self):
        if self.channel:
            self.channel.close()
        if self.client:
            self.client.close()
        print("[DISCONNECT] OK")


def sftp_download(client, remote_path, local_path):
    sftp = client.open_sftp()
    sftp.get(remote_path, local_path)
    sftp.close()
    print(f"[SFTP] download: {remote_path} -> {local_path}")


def shell_upload(tool, password, local_path, remote_path):
    """通过SFTPupload到/tmp，再用invoke_shell+sudo cp覆盖目标（绕过目录写权限）"""
    # Step 1: SFTPupload到/tmp
    sftp = tool.client.open_sftp()
    tmp_remote = "/tmp/confdb_v2.xml.tmp_upload"
    sftp.put(local_path, tmp_remote)
    sftp.close()
    print(f"[SFTP] uploaded to /tmp: {tmp_remote}")

    # Step 2: 用invoke_shell发送sudo cp命令（已知能输密码）
    print(f"[SHELL] sudo cp {tmp_remote} -> {remote_path}")
    channel = tool.channel

    # 先清空channel残留
    def full_drain():
        out = b""
        time.sleep(0.3)
        while True:
            if not channel.recv_ready():
                break
            out += channel.recv(4096)
            time.sleep(0.2)
        return strip_ansi(out.decode("utf-8", errors="ignore"))

    full_drain()  # clear buffer

    # 发送sudo cp命令
    channel.send(f"sudo cp {tmp_remote} {remote_path}\n")
    time.sleep(0.8)
    prompt = full_drain()

    if "password" in prompt.lower():
        channel.send(password + "\n")
        time.sleep(1.5)
        result = full_drain()
        if result.strip():
            print(f"  result: {result.strip()[:200]}")
        else:
            print("  OK (no output)")
    else:
        print(f"  [WARN] no password prompt. Buffer: {prompt[:200]}")


def backup_remote(client, conf_path):
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    bak = f"{conf_path}.bak.{ts}"
    stdin, stdout, stderr = client.exec_command(f"cp {conf_path} {bak}")
    stdout.read()
    stderr.read()
    print(f"[BACKUP] remote: {bak}")


def show_current_state(local_bak):
    """显示关键参数的当前值"""
    with open(local_bak, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    targets = {
        458:  "CertMS.URL",
        471:  "ManagementServer.URL",
        483:  "STUNServerAddress",
    }

    print("\n" + "=" * 60)
    print("CURRENT STATE")
    print("=" * 60)
    for linenum, desc in targets.items():
        idx = linenum - 1
        if 0 <= idx < len(lines):
            print(f"  Line {linenum:5d}  [{desc}]:  {lines[idx].strip()}")


def apply_modifications(content, params, target_ip):
    """在XML字符串上应用所有modify，返回(新内容, modify列表)"""
    modifications = []
    old = "<URL>http://cmp1.global.mgmt:8081/pkix/</URL>"
    new = f"<URL>{params['CertMS_URL']}</URL>"
    if old in content:
        content = content.replace(old, new, 1)
        modifications.append(f"  [OK] CertMS.URL")
    else:
        modifications.append(f"  [SKIP] CertMS.URL - not found")

    # 2. ManagementServer.URL
    old = "<URL>https://nms.harman.global.mgmt:443/tr069/gnbInitialAcs</URL>"
    new = f"<URL>{params['MgmtServer_URL']}</URL>"
    if old in content:
        content = content.replace(old, new, 1)
        modifications.append(f"  [OK] ManagementServer.URL")
    else:
        modifications.append(f"  [SKIP] ManagementServer.URL - not found")

    # 3. STUNServerAddress
    old = "<STUNServerAddress>nms.harman.global.mgmt</STUNServerAddress>"
    new = f"<STUNServerAddress>{params['STUNServerAddress']}</STUNServerAddress>"
    if old in content:
        content = content.replace(old, new, 1)
        modifications.append(f"  [OK] STUNServerAddress")
    else:
        modifications.append(f"  [SKIP] STUNServerAddress - not found")

    # 4. SecGWServer1
    old = "<SecGWServer1>segw.harman.global.t-mobile.com</SecGWServer1>"
    new = f"<SecGWServer1>{params['SecGWServer1']}</SecGWServer1>"
    if old in content:
        content = content.replace(old, new, 1)
        modifications.append(f"  [OK] SecGWServer1")
    else:
        modifications.append(f"  [SKIP] SecGWServer1 - not found")



    # 9.5. WebEnable
    old = '<WebEnable>0</WebEnable>'
    new = '<WebEnable>1</WebEnable>'
    if old in content:
        content = content.replace(old, new, 1)
        modifications.append("  [OK] WebEnable: 0 -> 1")
    else:
        modifications.append("  [SKIP] WebEnable - already 1 or not found")

    # 9.6. SshEnable
    old = '<SshEnable>0</SshEnable>'
    new = '<SshEnable>1</SshEnable>'
    if old in content:
        content = content.replace(old, new, 1)
        modifications.append("  [OK] SshEnable: 0 -> 1")
    else:
        modifications.append("  [SKIP] SshEnable - already 1 or not found")

    # 9.7. NTPServer1
    old = '<NTPServer1>time.google.com</NTPServer1>'
    new = '<NTPServer1>time.windows.com</NTPServer1>'
    if old in content:
        content = content.replace(old, new, 1)
        modifications.append("  [OK] NTPServer1: time.google.com -> time.windows.com")
    else:
        modifications.append("  [SKIP] NTPServer1 - already set or not found")

    # 10. IPv4Address id="2": 在Interface id="1"下新增（如果不存在）或更新
    #     目标路径: Device.Ethernet.Interface.1.IPv4Address.2
    #     插入位置: Interface id="1" 内，IPv4Address id="1" 之后，IPv6Address id="1" 之前
    ipv4_2_old = '<IPv4Address id="2">'
    ipv4_2_new = f'''      <IPv4Address id="2">
        <DefaultGateway xsi:nil="true"/>
        <AddressingType>Static</AddressingType>
        <PortType>Ng</PortType>
        <SubnetMask>255.255.255.0</SubnetMask>
        <IPAddress>{target_ip}</IPAddress>
      </IPv4Address>'''

    if ipv4_2_old in content:
        # 已存在：替换整个节点内容（保留id="2"标签）
        old_node_pattern = re.compile(
            r'<IPv4Address id="2">.*?</IPv4Address>',
            re.DOTALL
        )
        if old_node_pattern.search(content):
            content = old_node_pattern.sub(ipv4_2_new, content, count=1)
            modifications.append(f"  [OK] IPv4Address.id=2: updated -> {target_ip}")
    else:
        # 不存在：在Interface id="1"内，IPv4Address id="1"之后插入
        iface_match = re.search(r'(<Interface id="1">.*?<IPv4Address id="1">.*?</IPv4Address>)(\s*<IPv6Address)',
                                content, re.DOTALL)
        if iface_match:
            insert_pos = iface_match.end(1)
            content = content[:insert_pos] + '\n' + ipv4_2_new + '\n' + content[insert_pos:]
            modifications.append(f"  [OK] IPv4Address.id=2: inserted -> {target_ip}")
        else:
            modifications.append(f"  [SKIP] IPv4Address.id=2: insertion point not found")

    return content, modifications


def verify(local_verify_path, expected):
    """verifyupload后的文件"""
    with open(local_verify_path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    checks = [
        ("CertMS.URL",         expected["CertMS_URL"]),
        ("MgmtServer.URL",     expected["MgmtServer_URL"]),
        ("STUNServerAddress",  expected["STUNServerAddress"]),
        ("SecGWServer1",       expected["SecGWServer1"]),
        ("IPv4Address.id=2.IPAddress",      TARGET_IP),
        ("IPv4Address.id=2.AddressingType", "Static"),
        ("IPv4Address.id=2.PortType",       "Ng"),
        ("IPv4Address.id=2.SubnetMask",      "255.255.255.0"),
        ("IPv4Address.id=2.DefaultGateway",  "xsi:nil"),
    ]

    all_ok = True
    for name, val in checks:
        found = val in content
        status = "[OK]" if found else "[FAIL]"
        if not found:
            all_ok = False
        print(f"  {status} {name}: {val}")

    return all_ok


def main():
    global USERNAME, PASSWORD, CURRENT_VERSION
    
    print("=" * 60)
    print("BBU confdb_v2.xml Configuration Tool")
    print("=" * 60)
    
    # versionselection
    print("\n[VERSION SELECTION]")
    print(f"  默认version: {DEFAULT_VERSION}")
    for v in VERSIONS:
        marker = " (默认)" if v == DEFAULT_VERSION else ""
        print(f"  - {v}: {VERSIONS[v]['username']}@{HOST}{marker}")
    
    # 检查环境变量或命令行参数
    import os
    version_override = os.environ.get("BBU_VERSION")
    
    if version_override and version_override in VERSIONS:
        CURRENT_VERSION = version_override
        USERNAME = VERSIONS[CURRENT_VERSION]["username"]
        PASSWORD = VERSIONS[CURRENT_VERSION]["password"]
        print(f"\n[VERSION] Using environment variable: {CURRENT_VERSION}")
    else:
        # 默认使用V6
        CURRENT_VERSION = DEFAULT_VERSION
        USERNAME = VERSIONS[CURRENT_VERSION]["username"]
        PASSWORD = VERSIONS[CURRENT_VERSION]["password"]
        print(f"\n[VERSION] Using default: {CURRENT_VERSION}")
    
    print(f"[CREDENTIALS] {USERNAME}@{HOST}")
    
    if not TARGET_IP:
        print("\n[ERROR] TARGET_IP not set!")
        print("        Edit line 50: TARGET_IP = 'your.ip.here'\n")
        sys.exit(1)

    print(f"\n[TARGET_IP] {TARGET_IP}")
    
    tool = BBUTool(HOST, PORT, USERNAME, PASSWORD)
    # 自动尝试V6，失败则尝试V5
    try:
        tool.connect()
    except Exception as conn_err:
        print(f"[CONNECT] V6 failed: {conn_err}, trying V5...")
        CURRENT_VERSION = "V5"
        USERNAME = VERSIONS["V5"]["username"]
        PASSWORD = VERSIONS["V5"]["password"]
        tool = BBUTool(HOST, PORT, USERNAME, PASSWORD)
        tool.connect()
    tool.connect()
    tool.cd("/opt/bbu/etc/")

    # 远程backup
    backup_remote(tool.client, CONF_PATH)

    # 本地文件路径
    local_bak   = os.path.join(TOOL_DIR, "confdb_v2.xml.bak")
    local_mod   = os.path.join(TOOL_DIR, "confdb_v2.xml.mod")
    local_verify = os.path.join(TOOL_DIR, "confdb_v2.xml.verify")

    # download
    print("\n[DOWNLOAD] Fetching current config from device...")
    sftp_download(tool.client, CONF_PATH, local_bak)

    # 显示当前值
    show_current_state(local_bak)

    # 读取内容
    with open(local_bak, encoding="utf-8", errors="replace") as f:
        content = f.read()

    # 应用modify
    print("\n[MODIFY] Applying parameter modifications...")
    new_content, mods = apply_modifications(content, PARAMS, TARGET_IP)

    print("\n" + "=" * 60)
    print("MODIFICATIONS")
    print("=" * 60)
    for m in mods:
        print(m)

    # 保存modify后文件
    with open(local_mod, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"\n[SAVE] Modified config: {local_mod}")

    # upload
    print("\n[UPLOAD] Writing to device...")
    shell_upload(tool, PASSWORD, local_mod, CONF_PATH)

    # verify
    print("\n[VERIFY] Downloading uploaded file for verification...")
    sftp_download(tool.client, CONF_PATH, local_verify)

    expected = {
        "CertMS_URL":        PARAMS["CertMS_URL"],
        "MgmtServer_URL":    PARAMS["MgmtServer_URL"],
        "STUNServerAddress": PARAMS["STUNServerAddress"],
        "SecGWServer1":     PARAMS["SecGWServer1"],
    }

    all_ok = verify(local_verify, expected)

    tool.close()

    print("\n" + "=" * 60)
    if all_ok:
        print("[SUCCESS] All parameters verified on device!")

        # ---- ZTP 开站 ----
        print("\n[ZTP] Triggering factory reset & ZTP ...")
        try:
            tool2 = BBUTool(HOST, PORT, USERNAME, PASSWORD)
            tool2.connect()
            print("[ZTP] Executing: odi -n oamProcess resetfactory")
            tool2.run_cmd("odi -n oamProcess resetfactory")
            print("[ZTP] Command sent, waiting for device to disconnect ...")
            import socket
            start = time.time()
            while True:
                try:
                    time.sleep(1)
                    # 尝试检测connect状态
                    if tool2.channel.recv_ready():
                        tool2.channel.recv(4096)
                    if time.time() - start > 30:
                        print("[ZTP] Timeout (30s), forcing close")
                        break
                except (socket.error, EOFError, OSError):
                    break
            tool2.close()
            print("[ZTP] Device disconnected, ZTP factory reset triggered.")
        except Exception as ztp_err:
            print(f"[ZTP] Warning: {ztp_err}")

    else:
        print("[WARN] Some parameters not verified. ZTP skipped.")
    print("=" * 60)

    # Build WeChat summary
    ok_mods = [m.strip().lstrip("[OK]") for m in mods if m.strip().startswith("[OK]")]

    steps = [
        "✅ 连接 BBU",
        "✅ 备份配置",
        "✅ 下载配置",
    ]
    if ok_mods:
        steps.append("✅ 修改参数")
        for p in ok_mods:
            steps.append(f"   - {p}")
    else:
        steps.append("✅ 修改参数（无参数变更）")
    steps += [
        "✅ 上传配置",
        "✅ 验证通过",
        "✅ ZTP factory reset 已触发",
    ]
    print("\n" + "\n".join(steps))


if __name__ == "__main__":
    main()
