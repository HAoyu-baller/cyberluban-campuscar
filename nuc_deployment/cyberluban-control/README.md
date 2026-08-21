# Cyber 鲁班：NUC → ESP32 → 小车远程控制

本项目将控制链路改为：

```text
电脑浏览器 --局域网/Wi-Fi--> NUC --USB 串口--> ESP32 --> 小车和喷水继电器
```

键盘操作：按住 `W/S/A/D` 运动、松键停止运动，`K` 开始喷水、`L` 停止喷水，空格强制停止运动、喷水和待执行动作。网页按钮也支持鼠标和触摸操作。

## 1. 安全设计

- 浏览器每 150 ms 向 NUC 报告当前状态。
- 浏览器 800 ms 没有心跳，NUC 自动急停并释放控制权。
- NUC 每 150 ms 刷新运动指令；ESP32 超过 600 ms 没收到运动指令就停车。
- NUC 每 400 ms 刷新喷水指令；ESP32 超过 1500 ms 没收到喷水指令就关闭继电器。
- 页面失焦、隐藏、关闭、WebSocket 断开、串口重连或服务退出时发送急停。
- ESP32 串口断开或重新连接后会释放网页控制权，必须重新点击“取得控制权”，防止旧按键状态自动恢复。
- 同一时间只允许一个网页取得控制权；任何已认证页面都可以急停。
- ESP32 上电时强制 PWM 回中、喷水继电器关闭。

软件急停不能替代物理急停。首次测试必须架空车轮，并准备可直接断开电机和水泵电源的物理开关。

## 2. NUC 所需环境

推荐环境：

- Ubuntu 22.04/24.04 LTS，64 位
- Python 3.10 或更高版本
- NUC 和控制电脑处于同一个可信局域网
- NUC 有固定 IP，或在路由器中设置 DHCP 地址保留
- ESP32 通过可传输数据的 USB 线连接 NUC
- 登录用户属于 `dialout` 组，能够访问 `/dev/ttyUSB0` 或 `/dev/ttyACM0`
- TCP 8000 端口只向本地局域网开放，不要直接映射到公网

安装脚本会安装 Python 环境、依赖、串口权限和 systemd 服务，并生成随机控制口令。

## 3. 先烧录 ESP32 固件

打开：

```text
esp32/esp32_combined_controller_nuc/esp32_combined_controller_nuc.ino
```

在 Arduino IDE 中选择实际 ESP32 开发板和串口，确认编译通过后上传。串口波特率为 `115200`。

新协议：

| 命令 | 功能 |
|---|---|
| `w/s/a/d` | 运动心跳 |
| `m` | 只停止运动，不停止喷水 |
| `k` | 打开/刷新喷水心跳 |
| `l` | 关闭喷水 |
| `g` | 手动执行 PWM 激活手势 |
| `x` 或 `0` | 强制停止全部活动 |
| `h` 或 `?` | 帮助信息 |

## 4. 将项目复制到 NUC

### 方法 A：U 盘

把整个 `cyberluban-nuc-control` 文件夹复制到 U 盘，再复制到 NUC 用户主目录。

### 方法 B：从 Windows 电脑使用 SCP

先在 NUC 上确认 SSH 已启用：

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
hostname -I
```

在 Windows PowerShell 中执行，替换用户名、IP 和压缩包路径：

```powershell
scp .\cyberluban-nuc-control.zip 用户名@NUC_IP:/home/用户名/
```

然后在 NUC 上：

```bash
cd ~
unzip cyberluban-nuc-control.zip
cd cyberluban-nuc-control
chmod +x scripts/install_nuc.sh
sudo ./scripts/install_nuc.sh
```

安装结束会显示：

```text
Control URL: http://NUC_IP:8000
Control token: 一串随机口令
```

保存这串口令。如果忘记，可以在 NUC 上查看：

```bash
sudo grep '^CONTROL_TOKEN=' /etc/cyberluban-control.env
```

首次加入 `dialout` 组后，建议重启 NUC：

```bash
sudo reboot
```

## 5. 检查 ESP32 串口

ESP32 接到 NUC 后执行：

```bash
cd /opt/cyberluban-control
.venv/bin/python scripts/find_serial.py
```

通常会看到 `/dev/ttyUSB0` 或 `/dev/ttyACM0`。如果自动识别了错误设备，编辑：

```bash
sudo nano /etc/cyberluban-control.env
```

把：

```text
SERIAL_PORT=auto
```

改成：

```text
SERIAL_PORT=/dev/ttyUSB0
```

然后重启服务：

```bash
sudo systemctl restart cyberluban-control
```

安全串口测试只发送 `x` 和 `h`：

```bash
sudo systemctl stop cyberluban-control
cd /opt/cyberluban-control
.venv/bin/python scripts/serial_smoke_test.py /dev/ttyUSB0
sudo systemctl start cyberluban-control
```

不要让串口测试程序和控制服务同时打开同一个串口。

## 6. 电脑端操作

1. 电脑和 NUC 连接到同一个 Wi-Fi/局域网。
2. 在电脑浏览器打开 `http://NUC_IP:8000`。
3. 输入安装时生成的控制口令，点击“取得控制权”。
4. 确认顶部显示“网页已连接”“ESP32 /dev/ttyUSB0”和“正在控制”。
5. 先把车轮架空，短按 `W/S/A/D`，确认方向正确。
6. 按 `K` 开始喷水，按 `L` 停止喷水。
7. 任何异常立即按空格，必要时使用物理断电开关。

方向键必须保持网页处于焦点。切换窗口会自动急停，这是预期的安全行为。

## 7. 服务维护

查看状态：

```bash
sudo systemctl status cyberluban-control
```

查看实时日志：

```bash
sudo journalctl -u cyberluban-control -f
```

重启：

```bash
sudo systemctl restart cyberluban-control
```

停止：

```bash
sudo systemctl stop cyberluban-control
```

检查网页服务：

```bash
curl http://127.0.0.1:8000/health
```

## 8. 不连接真实 ESP32 的网页测试

编辑 `/etc/cyberluban-control.env`，临时设置：

```text
SERIAL_PORT=mock
```

重启服务后即可测试网页、键盘和控制权逻辑。完成后改回 `auto` 或真实串口。

## 9. 网络建议

不要在路由器上把 8000 端口转发到互联网。需要跨网络控制时，应先建立 VPN，再通过 VPN 地址访问 NUC。远程视频控制还需要单独评估时延、视野、制动距离和失联风险。
