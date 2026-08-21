# Cyber 鲁班小车控制系统交接包

交接日期：2026-08-18

本包用于把目前的“电脑浏览器 → NUC → ESP32 → 小车/水泵”控制项目交给下一位开发者。代码基线为：

- ESP32：protocol 3，固件标识 3.0-last-activity-timer。
- NUC 后端：FastAPI + WebSocket + pyserial。
- 网页：WEB V4，具有 G 激活/校准按钮、W/S/A/D 按住运行、K/L 喷水控制和空格急停。
- Windows 直连工具：用于绕开 NUC，直接验证 ESP32、驱动板和水泵。

## 先做什么

1. 阅读 docs/01_项目现状与系统总览.md，区分“已经完成”和“仍需现场确认”的内容。
2. 按 docs/02_部署更新与日常操作.md 检查 NUC 上实际运行的版本。
3. 在车轮架空、水泵独立断电的条件下，执行 docs/04_测试与验收清单.md。
4. 后续修改前先读 docs/03_协议代码与安全设计.md 和 docs/05_故障排查与已知风险.md。

## 目录

~~~text
code/
  cyberluban-nuc-control/       NUC 服务、网页、ESP32 V3 固件、测试和部署脚本
  pc-esp32-car-controller/      Windows 电脑直连 ESP32 的测试控制程序
docs/
  01_项目现状与系统总览.md
  02_部署更新与日常操作.md
  03_协议代码与安全设计.md
  04_测试与验收清单.md
  05_故障排查与已知风险.md
  06_后续开发建议.md
references/
  PWM切换组合参数使用说明_厂家.md
tools/
  collect_nuc_diagnostics.sh    NUC 只读诊断信息采集
  verify_nuc_release.sh         检查服务、串口和 WEB V4 标识
VERSION_MANIFEST.md             版本、现场信息和关键文件说明
CHECKSUMS.sha256                包内文件完整性校验
~~~

## 最重要的安全规则

- 首次测试和每次改动后的测试必须架空驱动轮。
- 软件急停不能代替物理急停和电源断路开关。
- 水泵测试前先断开水泵主电源，只观察继电器输入和触点状态。
- 驱动板 12/15 V 不得接入 ESP32 GPIO、3.3 V 或 5 V。
- ESP32、驱动板控制地和继电器控制地需要按电路要求共地；电机和水泵的浪涌、反灌与电磁干扰必须做硬件隔离和抑制。
- 只允许在可信局域网或 VPN 内访问 8000 端口，不要映射到公网。
- 本包不包含 NUC 登录密码或控制口令。历史聊天中出现过的口令应视为已泄露并更换。

## 当前现场信息不是固定配置

曾确认 NUC 用户为 haoyu、主机名为 haoyu-NUC14MNK-B2、IPv4 为 172.20.10.6、ESP32 为 /dev/ttyUSB0。这些值可能随网络、重启和 USB 插拔变化，不能硬编码。交接后应在 NUC 上重新执行：

~~~bash
hostname
whoami
hostname -I
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
~~~

控制电脑访问的是 http://NUC当前IP:8000；127.0.0.1 只代表浏览器所在的那台电脑。
