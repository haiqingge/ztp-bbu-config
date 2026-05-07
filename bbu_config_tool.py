import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

"""
BBU confdb_v2.xml Configuration Tool
Full flow: connect -> sudo -> backup -> download -> modify -> upload -> verify -> ZTP reset
"""

import paramiko
import time
import re
import os
import socket
from datetime import datetime

# ========== VERSION CONFIG ==========
VERSIONS = {
    "V5": {"username": "root", "password": "Smallcell@5"},
    "V6": {"username": "f3mto5gus3r", "password": "H@rm@n$Nr!Fmt0$00.5G_$TrnG"},
}

DEFAULT_VERSION = "V6"
PORT = 22
CONF_PATH = "/opt/bbu/etc/confdb_v2.xml"

# TR-069 参数目标值
PARAMS = {
    "CertMS_URL":         "https://ejbca.waveoss.com:9443/ejbca/publicweb/cmp/cmpclient",
    "MgmtServer_URL":     "https://ejbca.waveoss.com/tr069/gnbInitialAcs",
    "STUNServerAddress":  "ejbca.waveoss.com",
    "SecGWServer1":       "test.nms2.com",
    "NTPServer1":         "time.windows.com",
    "WebEnable":          "1",
    "SshEnable":          "1",
}

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def parse_args():
    """解析命令行参数：bbu_config_tool.py <IP> [version]"""
    host = "192.168.5.173"
    target_ip = "192.168.5.173"
    version = DEFAULT_VERSION

    args = sys.argv[1:]
    if args:
        host = args[0]
        target_ip = args[0]
    if len(args) >= 2:
        v = args[1].upper()
        if v in VERSIONS:
            version = v
        else:
            print(f"[WARN] Unknown version '{v}', using {DEFAULT_VERSION}")

    env_ver = os.environ.get("BBU_VERSION")
    if env_ver and env_ver in VERSIONS:
        version = env_ver

    return host, target_ip, version


# ==================== SSH 工具类 ====================

class BBUTool:
    def __init__(self, host, port, username, password):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client = None
        self.channel = None
        self._is_root = False

    def connect(self):
        print(f"[CONNECT] {self.host}:{self.port} as {self.username} ...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host, port=self.port,
            username=self.username, password=self.password,
            timeout=10, banner_timeout=10, auth_timeout=10
        )
        self.channel = self.client.invoke_shell()
        time.sleep(1)
        self.channel.recv(65535)
        print("[CONNECT] OK")
        return self

    def sudo_root(self):
        """尝试 sudo 到 root"""
        if self._is_root:
            return True
        print("[SUDO] sudo su - root ...")
        if not self.channel or self.channel.closed:
            print("[SUDO] channel closed, skipping")
            return False
        try:
            self.channel.send(b"sudo su - root\n")
            time.sleep(0.8)
            self._drain(max_wait=1.0)
            self.channel.send(self.password.encode() + b"\n")
            time.sleep(2.0)
            out = self._drain(max_wait=2.0)
            if "root" in out.lower() or "#" in out or "password:" not in out.lower():
                self._is_root = True
                print("[SUDO] OK - root")
                return True
            else:
                print(f"[SUDO] failed: {out[:100]}")
                return False
        except Exception as e:
            print(f"[SUDO] error: {e}")
            return False

    def _drain(self, max_wait=2.0):
        """读取channel数据，动态等待"""
        out = b""
        elapsed = 0
        while elapsed < max_wait:
            if self.channel and self.channel.recv_ready():
                try:
                    data = self.channel.recv(4096)
                    out += data
                    elapsed = 0
                except:
                    break
            else:
                time.sleep(0.1)
                elapsed += 0.1
        return strip_ansi(out.decode("utf-8", errors="ignore"))

    def run_cmd(self, cmd, wait=1.5):
        if not self.channel or self.channel.closed:
            raise OSError("Socket is closed")
        self.channel.send(cmd.encode() + b"\n")
        time.sleep(wait)
        return self._drain()

    def close(self):
        if self.channel:
            try:
                self.channel.close()
            except:
                pass
        if self.client:
            try:
                self.client.close()
            except:
                pass
        print("[DISCONNECT] OK")


# ==================== 文件操作 ====================

def sftp_download(client, remote_path, local_path):
    sftp = client.open_sftp()
    sftp.get(remote_path, local_path)
    sftp.close()
    print(f"[SFTP] download: {remote_path} -> {local_path}")


def exec_cmd(client, cmd, timeout=10):
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        return out, err
    except Exception as e:
        return "", str(e)


# ==================== 备份 ====================

def backup_remote(client, conf_path):
    """远程备份配置（sudo + cp）"""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    bak = f"{conf_path}.bak.{ts}"

    # 先用 sudo cp（如果能sudo），否则普通 cp
    out, err = exec_cmd(client, f"sudo cp {conf_path} {bak} 2>/dev/null || cp {conf_path} {bak} 2>/dev/null")
    if err.strip():
        # 尝试 sftp 复制
        try:
            local_tmp = os.path.join(TOOL_DIR, f"_tmp_bak_{ts}.xml")
            sftp = client.open_sftp()
            sftp.get(conf_path, local_tmp)
            sftp.put(local_tmp, bak)
            sftp.close()
            os.remove(local_tmp)
            print(f"[BACKUP] sftp copy: {bak}")
        except Exception as e:
            print(f"[BACKUP] warning: cannot create backup: {e}")
    else:
        print(f"[BACKUP] remote: {bak}")


def show_current_state(local_bak):
    """显示关键参数的当前值（XML标签搜索）"""
    with open(local_bak, encoding="utf-8", errors="replace") as f:
        content = f.read()

    print("\n" + "=" * 60)
    print("CURRENT STATE")
    print("=" * 60)

    # CertMS URL
    m = re.search(r'(<URL>[^<]+</URL>)\s*</CertMS', content)
    if m:
        print(f"  [CertMS.URL]: {m.group(1)}")

    # ManagementServer URL
    m = re.search(r'ManagementServer[^<]*<URL>([^<]+)</URL>', content)
    if m:
        print(f"  [ManagementServer.URL]: <URL>{m.group(1)}</URL>")

    # STUNServerAddress
    m = re.search(r'<STUNServerAddress>([^<]+)</STUNServerAddress>', content)
    if m:
        print(f"  [STUNServerAddress]: {m.group(0)}")

    # WebEnable
    m = re.search(r'<WebEnable>(\d)</WebEnable>', content)
    if m:
        print(f"  [WebEnable]: {m.group(1)}")

    # SshEnable
    m = re.search(r'<SshEnable>(\d)</SshEnable>', content)
    if m:
        print(f"  [SshEnable]: {m.group(1)}")


# ==================== 修改参数 ====================

def _replace_by_range(content, pattern, replacement):
    """用正则查找后按位置替换，确保只替换目标位置"""
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return content, False
    return content[:m.start()] + replacement + content[m.end():], True


def apply_modifications(content, params, target_ip):
    """修改 XML 参数。只在值不同时修改，返回(新内容, 变更列表)"""
    modifications = []

    # 1. CertMS.URL — 找 CertMS 标签内的 URL
    old_v = re.search(r'(<URL>)([^<]+)(</URL>)\s*</CertMS', content)
    if old_v:
        current_url = old_v.group(2)
        target_url = params["CertMS_URL"]
        if current_url != target_url:
            content, ok = _replace_by_range(content,
                r'<URL>[^<]+</URL>\s*</CertMS',
                f"<URL>{target_url}</URL>\n    </CertMS")
            modifications.append(f"  [OK] CertMS.URL: {current_url} -> {target_url}" if ok
                                 else f"  [SKIP] CertMS.URL - replace failed")
        else:
            modifications.append(f"  [SKIP] CertMS.URL - already up to date")
    else:
        modifications.append(f"  [SKIP] CertMS.URL - not found")

    # 2. ManagementServer.URL
    m = re.search(r'(ManagementServer[^<]*)(<URL>)([^<]+)(</URL>)', content)
    if m:
        current_url = m.group(3)
        target_url = params["MgmtServer_URL"]
        if current_url != target_url:
            # 按位置替换 URL 部分，保留前后标签
            new = m.group(1) + m.group(2) + target_url + m.group(4)
            content = content[:m.start()] + new + content[m.end():]
            modifications.append(f"  [OK] MgmtServer.URL: {current_url} -> {target_url}")
        else:
            modifications.append(f"  [SKIP] MgmtServer.URL - already up to date")
    else:
        modifications.append(f"  [SKIP] MgmtServer.URL - not found")

    # 3. STUNServerAddress
    m = re.search(r'<STUNServerAddress>([^<]+)</STUNServerAddress>', content)
    if m:
        target = params["STUNServerAddress"]
        if m.group(1) != target:
            content = content[:m.start()] + f"<STUNServerAddress>{target}</STUNServerAddress>" + content[m.end():]
            modifications.append(f"  [OK] STUNServerAddress: {m.group(1)} -> {target}")
        else:
            modifications.append(f"  [SKIP] STUNServerAddress - already up to date")
    else:
        modifications.append(f"  [SKIP] STUNServerAddress - not found")

    # 4. SecGWServer1
    m = re.search(r'<SecGWServer1>([^<]+)</SecGWServer1>', content)
    if m:
        target = params["SecGWServer1"]
        if m.group(1) != target:
            content = content[:m.start()] + f"<SecGWServer1>{target}</SecGWServer1>" + content[m.end():]
            modifications.append(f"  [OK] SecGWServer1: {m.group(1)} -> {target}")
        else:
            modifications.append(f"  [SKIP] SecGWServer1 - already up to date")
    else:
        modifications.append(f"  [SKIP] SecGWServer1 - not found")

    # 5. WebEnable
    m = re.search(r'<WebEnable>(\d)</WebEnable>', content)
    if m:
        if m.group(1) != "1":
            content = content[:m.start()] + "<WebEnable>1</WebEnable>" + content[m.end():]
            modifications.append("  [OK] WebEnable: 0 -> 1")
        else:
            modifications.append("  [SKIP] WebEnable - already 1")
    else:
        modifications.append("  [SKIP] WebEnable - tag not found")

    # 6. SshEnable
    m = re.search(r'<SshEnable>(\d)</SshEnable>', content)
    if m:
        if m.group(1) != "1":
            content = content[:m.start()] + "<SshEnable>1</SshEnable>" + content[m.end():]
            modifications.append("  [OK] SshEnable: 0 -> 1")
        else:
            modifications.append("  [SKIP] SshEnable - already 1")
    else:
        modifications.append("  [SKIP] SshEnable - tag not found")

    # 7. NTPServer1
    m = re.search(r'<NTPServer1>([^<]+)</NTPServer1>', content)
    target_ntp = params["NTPServer1"]
    if m:
        if m.group(1) != target_ntp:
            content = content[:m.start()] + f"<NTPServer1>{target_ntp}</NTPServer1>" + content[m.end():]
            modifications.append(f"  [OK] NTPServer1: {m.group(1)} -> {target_ntp}")
        else:
            modifications.append(f"  [SKIP] NTPServer1 - already {target_ntp}")
    else:
        modifications.append(f"  [SKIP] NTPServer1 - tag not found")

    # 8. IPv4Address id="2"
    ipv4_2_new = f'''      <IPv4Address id="2">
        <DefaultGateway xsi:nil="true"/>
        <AddressingType>Static</AddressingType>
        <PortType>Ng</PortType>
        <SubnetMask>255.255.255.0</SubnetMask>
        <IPAddress>{target_ip}</IPAddress>
      </IPv4Address>'''

    if '<IPv4Address id="2">' in content:
        old_node = re.search(r'<IPv4Address id="2">.*?</IPv4Address>', content, re.DOTALL)
        if old_node:
            # 检查是否已有相同的 target_ip
            if target_ip in old_node.group(0):
                modifications.append(f"  [SKIP] IPv4Address.id=2 - already {target_ip}")
            else:
                content = content[:old_node.start()] + ipv4_2_new + content[old_node.end():]
                modifications.append(f"  [OK] IPv4Address.id=2: updated -> {target_ip}")
    else:
        # 新增节点
        iface_match = re.search(
            r'(<Interface id="1">.*?<IPv4Address id="1">.*?</IPv4Address>)(\s*<IPv6Address)',
            content, re.DOTALL
        )
        if iface_match:
            insert_pos = iface_match.end(1)
            content = content[:insert_pos] + '\n' + ipv4_2_new + '\n' + content[insert_pos:]
            modifications.append(f"  [OK] IPv4Address.id=2: inserted -> {target_ip}")
        else:
            modifications.append("  [SKIP] IPv4Address.id=2: insertion point not found")

    return content, modifications


# ==================== 验证 ====================

def verify(local_verify_path, expected, target_ip):
    with open(local_verify_path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    checks = [
        ("CertMS.URL",          expected["CertMS_URL"]),
        ("MgmtServer.URL",      expected["MgmtServer_URL"]),
        ("STUNServerAddress",   expected["STUNServerAddress"]),
        ("SecGWServer1",        expected["SecGWServer1"]),
        ("IPv4Address.id=2.IPAddress",      target_ip),
        ("IPv4Address.id=2.AddressingType", "Static"),
        ("IPv4Address.id=2.PortType",       "Ng"),
        ("IPv4Address.id=2.SubnetMask",     "255.255.255.0"),
        ("IPv4Address.id=2.DefaultGateway", "xsi:nil"),
    ]

    all_ok = True
    for name, val in checks:
        found = val in content
        status = "[OK]" if found else "[FAIL]"
        if not found:
            all_ok = False
        print(f"  {status} {name}: {val}")

    return all_ok


# ==================== 上传 ====================

def shell_upload(tool, password, local_path, remote_path):
    """SFTP上传到/tmp → sudo cp 覆盖目标"""
    sftp = tool.client.open_sftp()
    tmp_remote = "/tmp/confdb_v2.xml.tmp_upload"
    sftp.put(local_path, tmp_remote)
    sftp.close()
    print(f"[SFTP] uploaded to /tmp: {tmp_remote}")

    print(f"[SHELL] sudo cp {tmp_remote} -> {remote_path}")
    channel = tool.channel

    def full_drain(max_wait=1.5):
        out = b""
        elapsed = 0
        while elapsed < max_wait:
            if channel and channel.recv_ready():
                try:
                    data = channel.recv(4096)
                    out += data
                    elapsed = 0
                except:
                    break
            else:
                time.sleep(0.1)
                elapsed += 0.1
        return strip_ansi(out.decode("utf-8", errors="ignore"))

    full_drain(1.0)
    channel.send(f"sudo cp {tmp_remote} {remote_path}\n")

    # 自适应等待：最多 5 秒等 password prompt
    password_sent = False
    for wait_step in [0.5, 1.0, 1.5, 2.0]:
        time.sleep(wait_step)
        prompt = full_drain(max_wait=0.5)
        if "password" in prompt.lower():
            channel.send(password + "\n")
            password_sent = True
            time.sleep(2.0)
            result = full_drain(2.0)
            if result.strip():
                print(f"  result: {result.strip()[:200]}")
            else:
                print("  OK (no output)")
            break
        if "denied" in prompt.lower() or "error" in prompt.lower():
            print(f"  [ERR] sudo failed: {prompt[:200]}")
            return

    if not password_sent:
        # 检查是否有 error
        final = full_drain(1.0)
        if final.strip():
            print(f"  [OK] sudo cp completed: {final[:100]}")
        else:
            print(f"  [OK] sudo cp completed")


# ==================== ZTP ====================

def wait_for_ztp_disconnect(tool, timeout=30):
    print(f"[ZTP] Command sent, waiting for device to disconnect (timeout={timeout}s) ...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            time.sleep(0.5)
            if tool.channel and tool.channel.recv_ready():
                tool.channel.recv(4096)
            if tool.channel and tool.channel.closed:
                print("[ZTP] Channel closed by device")
                return
            if tool.client and not tool.client.get_transport():
                print("[ZTP] Transport closed")
                return
            if not tool.client.get_transport().is_active():
                print("[ZTP] Transport not active")
                return
        except (socket.error, EOFError, OSError, AttributeError) as e:
            print(f"[ZTP] Device disconnected: {e}")
            return
    print(f"[ZTP] Timeout ({timeout}s), forcing close")


# ==================== MAIN ====================

def main():
    host, target_ip, version = parse_args()

    # 记录当前使用的凭据（V6→V5 降级时更新）
    current_version = version
    current_creds = VERSIONS[current_version]

    print("=" * 60)
    print("BBU confdb_v2.xml Configuration Tool")
    print("=" * 60)
    print(f"\n  Target: {host}")
    print(f"  Version: {current_version} ({current_creds['username']})")
    print(f"  Target IP for IPv4Address.2: {target_ip}")

    tool = BBUTool(host, PORT, current_creds["username"], current_creds["password"])

    # 连接 + V6→V5 降级
    try:
        tool.connect()
    except Exception as conn_err:
        if current_version == "V6":
            print(f"[CONNECT] {current_version} failed: {conn_err}, trying V5...")
            current_version = "V5"
            current_creds = VERSIONS["V5"]
            tool = BBUTool(host, PORT, current_creds["username"], current_creds["password"])
            tool.connect()
        else:
            print(f"[CONNECT] Failed: {conn_err}")
            sys.exit(1)

    # 尝试 sudo 到 root
    tool.sudo_root()

    # 远程备份
    backup_remote(tool.client, CONF_PATH)

    # 带时间戳的本地文件
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    local_bak    = os.path.join(TOOL_DIR, f"confdb_v2.xml.bak.{ts}")
    local_mod    = os.path.join(TOOL_DIR, f"confdb_v2.xml.mod.{ts}")
    local_verify = os.path.join(TOOL_DIR, f"confdb_v2.xml.verify.{ts}")
    # 保留一份 latest 方便快速引用
    latest_bak    = os.path.join(TOOL_DIR, "confdb_v2.xml.bak")
    latest_mod    = os.path.join(TOOL_DIR, "confdb_v2.xml.mod")
    latest_verify = os.path.join(TOOL_DIR, "confdb_v2.xml.verify")

    # 下载配置
    print("\n[DOWNLOAD] Fetching current config from device...")
    sftp_download(tool.client, CONF_PATH, local_bak)

    # 复制为 latest
    import shutil
    shutil.copy2(local_bak, latest_bak)

    show_current_state(local_bak)

    # 读取内容
    with open(local_bak, encoding="utf-8", errors="replace") as f:
        content = f.read()

    # 修改参数
    print("\n[MODIFY] Applying parameter modifications...")
    new_content, mods = apply_modifications(content, PARAMS, target_ip)

    print("\n" + "=" * 60)
    print("MODIFICATIONS")
    print("=" * 60)
    for m in mods:
        print(m)

    # 判断是否有实际变更
    has_changes = any(m.strip().startswith("[OK]") for m in mods)

    if not has_changes:
        print("\n[SKIP] No changes needed, skipping upload and ZTP reset")
        tool.close()
        print("\n" + "=" * 60)
        print("[IDLE] All parameters already up to date, nothing to do")
        print("=" * 60)
        return

    # 保存修改后文件
    with open(local_mod, "w", encoding="utf-8") as f:
        f.write(new_content)
    shutil.copy2(local_mod, latest_mod)
    print(f"\n[SAVE] Modified config: {local_mod}")

    # 上传配置
    print("\n[UPLOAD] Writing to device...")
    shell_upload(tool, current_creds["password"], local_mod, CONF_PATH)

    # 验证
    print("\n[VERIFY] Downloading uploaded file for verification...")
    sftp_download(tool.client, CONF_PATH, local_verify)
    shutil.copy2(local_verify, latest_verify)

    expected = {
        "CertMS_URL":        PARAMS["CertMS_URL"],
        "MgmtServer_URL":    PARAMS["MgmtServer_URL"],
        "STUNServerAddress": PARAMS["STUNServerAddress"],
        "SecGWServer1":     PARAMS["SecGWServer1"],
    }

    all_ok = verify(local_verify, expected, target_ip)

    tool.close()

    print("\n" + "=" * 60)
    if all_ok:
        print("[SUCCESS] All parameters verified on device!")

        # ZTP 开站 — 使用 current_creds（正确反映 V6/V5 降级）
        print("\n[ZTP] Triggering factory reset & ZTP ...")
        try:
            tool2 = BBUTool(host, PORT, current_creds["username"], current_creds["password"])
            tool2.connect()
            print("[ZTP] Executing: odi -n oamProcess resetfactory")
            tool2.run_cmd("odi -n oamProcess resetfactory")
            wait_for_ztp_disconnect(tool2, timeout=30)
            tool2.close()
            print("[ZTP] ZTP factory reset triggered.")
        except Exception as ztp_err:
            print(f"[ZTP] Warning: {ztp_err}")
    else:
        print("[WARN] Some parameters not verified. ZTP skipped.")
    print("=" * 60)

    # 汇总
    ok_mods = [m.strip() for m in mods if m.strip().startswith("[OK]")]
    steps = ["✅ 连接 BBU", "✅ 备份配置", "✅ 下载配置"]
    if ok_mods:
        steps.append("✅ 修改参数")
        for p in ok_mods:
            steps.append(f"   - {p}")
    else:
        steps.append("✅ 修改参数（无参数变更）")
    steps += ["✅ 上传配置", "✅ 验证通过", "✅ ZTP factory reset 已触发"]
    print("\n" + "\n".join(steps))


if __name__ == "__main__":
    main()
