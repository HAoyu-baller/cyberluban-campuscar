# NUC + ROS 2 + RTK + UE5 + 底盘运控完整部署方案

> 适用项目：CampusCar / GreenGuardian Bot  
> 目标平台：NUC、Ubuntu 22.04、ROS 2 Humble  
> 编制日期：2026-08-13  
> NTRIP 服务有效期：2027-07-04  
> 本文不保存 NTRIP 账号或密码。

---

## 1. 最终目标

完成下面这条端到端链路：

```text
UE5 / 校园大脑
   │  下发目标点、任务、暂停、取消、急停
   ▼
rosbridge WebSocket :9090
   │
/U2RTopic_Command（JSON）
   ▼
task_executor / geo_goal_bridge
   │  转换经纬度目标并调用 Nav2
   ▼
Nav2 NavigateToPose
   │
/cmd_vel_nav
   ▼
twist_mux → velocity_smoother → collision_monitor
   │
/cmd_vel_safe
   ▼
唯一底盘驱动节点
   │  正式协议待实车/固件核实
   ▼
STM32 或 ESP32 / 电机驱动器
   │
   └── 若有真实编码器反馈 → /wheel/odom、电池、温度、诊断

RTK 天线 → UM98x → 4G 评估板（NTRIP + RTCM）
   │
   └── USB NMEA → nmea_navsat_driver → /fix
                                      │
IMU + 轮速里程计 ─────────────────────┼→ robot_localization
                                      │     ├── map → odom → base_link
                                      │     └── /odometry/global
                                      ▼
/R2UTopic_Pos（JSON）→ rosbridge → UE5 模型位置与状态
```

### 1.1 一句话说明每个模块的职责

- **RTK**：提供机器人在地球上的位置；不直接控制电机。
- **IMU**：提供车体姿态、角速度和航向辅助。
- **轮速里程计**：提供高频、连续的局部运动信息。
- **robot_localization**：融合 RTK、IMU、轮速，形成可供导航使用的连续位姿。
- **Nav2**：规划路线、跟踪路线并输出 `/cmd_vel`。
- **运控节点 + MCU**：把 `/cmd_vel` 转成经实机确认的底盘协议，并执行超时停车、限速和硬件保护；MCU 可能是 STM32 或 ESP32，现阶段不能预设为左右轮二进制接口。
- **UE5**：下发任务、显示位置/轨迹/状态；不应直接输出电机 PWM。
- **rosbridge**：只负责 UE 与 ROS 2 的消息传输。

---

## 2. 当前已经确认的事实

### 2.1 NUC 现状

根据现有项目清单，NUC **被记录为** 已具备：

- Ubuntu 22.04；
- ROS 2 Humble Desktop；
- `nmea_navsat_driver`；
- `rosbridge_suite`；
- 已编译的 `u2r_r2u_bridge`；
- NUC 与开发电脑之间的网络连接。

相关记录见 [GGBot_从零到UE互通_完整清单.md](../GGBot_从零到UE互通_完整清单.md)。当前工作区没有 NUC 上的 `~/campuscar_ws` 源码或实时节点图，因此这些属于项目记录，不等于本机审计已经验证；正式部署前必须在 NUC 上复核 ROS 版本、包、节点、话题和 9090 服务类型。

### 2.2 RTK 现状

当前这套 RTK 已完成并实测：

- 4G 网络注册正常；
- NTRIP 参数已持久写入 4G 评估板；
- NTRIP 服务返回 `ICY 200 OK`；
- 挂载点可以接收差分数据；
- 室外达到 `GGA quality = 5`，即 **RTK FLOAT**；
- 观测到 17～22 颗卫星；
- HDOP 约 0.7～0.8；
- 差分龄期约 1 秒；
- USB 串口波特率为 115200。

这意味着 **NUC 不需要再运行一个 NTRIP 客户端**。4G 评估板已经负责：

1. 连接运营商网络；
2. 登录 NTRIP 服务；
3. 接收 RTCM；
4. 将 RTCM 注入 UM98x；
5. 向 NUC 输出校正后的 NMEA。

### 2.3 不应直接复用的旧文件

以下资料可参考协议，但不应直接作为 NUC 的 ROS 2 生产程序：

- [RTK/Ntrip2Uart.py](../RTK/Ntrip2Uart.py)：Windows 串口示例，配置硬编码，且会重复建立 NTRIP 链路。
- [gps_ntrip_node.py](../RTK/ROS_RTK/catkin_ws/src/gps_ntrip_py/src/gps_ntrip_node.py)：使用 `rospy` 和 Catkin，属于 ROS 1 示例。
- [gps_ntrip_py/package.xml](../RTK/ROS_RTK/catkin_ws/src/gps_ntrip_py/package.xml)：明确依赖 ROS 1 `catkin`/`rospy`。

**结论：** ROS 2 侧只读取 NMEA，不重复登录 NTRIP，也不要同时运行上述两个示例，否则可能出现串口占用、重复注入 RTCM、凭据泄露和数据混流。

---

## 3. 与培训 PPT 的对应关系

本方案直接沿用 [第二次培训.pptx](../第二次培训.pptx) 中的既有接口：

| PPT 要求 | 本方案实现 |
|---|---|
| 第 20 页：RTK/GPS `/fix` | `nmea_navsat_driver` 发布 `/fix` |
| 第 20 页：UE 指令 `/U2RTopic_Command` | 保留，JSON 命令进入任务执行器 |
| 第 20 页：`/odom`、`/imu` | 进入本地 EKF 和全局 EKF |
| 第 20 页：输出 `/cmd_vel` | Nav2 输出后经过安全链再给底盘 |
| 第 20～23 页：`/R2UTopic_Pos` | 保留基础字段，并增加 RTK 与本地坐标扩展字段 |
| 第 22、25 页：rosbridge `9090` | 保留 WebSocket 服务 |
| 第 24 页：经纬度到 UE 坐标 | 统一使用校园原点、旋转、轴向和比例参数 |
| 第 13～14 页：8/18 字节底盘帧 | 作为候选主底盘协议；必须先与实车固件逐字段核实，不能与现有 ESP32 PWM 原型混用 |
| 第 27～28 页：UE 联调验收 | 纳入本文验收矩阵 |

> rosbridge WebSocket 的标准地址写法是 `ws://<NUC_IP>:9090`。资料中也出现过 `tcp://`/BSON，但当前工作区没有 UE 插件和对应 ROS 服务端配置可验证。默认按 WebSocket 实施；只有 NUC 上确认启动了匹配的 TCP/BSON server 且 UE 插件要求如此时，才采用该模式，不能只把 URL 前缀从 `ws://` 改成 `tcp://`。

---

## 4. 分两个里程碑实施

## 4.1 里程碑 A：满足 PPT 的 RTK × UE 显示需求

只需要打通：

```text
RTK NMEA → /fix → /R2UTopic_Pos → rosbridge → UE5
```

达到以下结果即可验收：

- UE 能看到真实机器人位置；
- 推动车辆 3～5 米，UE 模型同步移动；
- UE 显示 `NO_FIX / RTK_FLOAT / RTK_FIX / TIMEOUT`；
- 坐标方向、比例和原点正确；
- 定位断开时不会继续显示“正常”。

这个里程碑 **不要求电机自动运行**。

## 4.2 里程碑 B：让 UE 下发地点后机器人自动行驶

除里程碑 A 外，还必须具备：

- 轮速编码器与可靠 `/wheel/odom`；
- IMU，至少提供角速度；最好提供经过标定的绝对航向；
- 完整 TF：`map → odom → base_link → gps_link/imu_link`；
- Nav2；
- `/cmd_vel` 底盘驱动；
- 急停、超时停车、限速；
- 可通行地图、预设安全路线或障碍物传感器；
- RTK 质量门控和地理围栏。

**重要：RTK 只解决“在哪里”，并不单独解决“车头朝哪、路线怎么走、前方有没有人、轮子怎样转”。**

---

## 5. NUC 端推荐工作区结构

建议不要修改系统包，也不要把所有代码塞进一个脚本：

```text
~/campuscar_ws/src/
├── campuscar_bringup/
│   ├── launch/
│   │   ├── rtk_bringup.launch.py
│   │   ├── localization.launch.py
│   │   └── robot_bringup.launch.py
│   ├── config/
│   │   ├── rtk.yaml
│   │   ├── ekf.yaml
│   │   ├── navsat.yaml
│   │   ├── rtk_gate.yaml
│   │   ├── ue_coordinates.yaml
│   │   └── nav2_params.yaml
│   ├── urdf/
│   │   └── campuscar.urdf.xacro
│   └── package.xml
├── campuscar_rtk_monitor/
│   ├── campuscar_rtk_monitor/
│   │   └── rtk_quality_monitor.py
│   ├── setup.py
│   └── package.xml
├── campuscar_geo_nav/
│   ├── campuscar_geo_nav/
│   │   ├── geo_goal_bridge.py
│   │   └── task_executor.py
│   ├── setup.py
│   └── package.xml
├── campuscar_base_driver/
│   ├── src/ 或 Python 包目录
│   ├── config/base_driver.yaml
│   └── package.xml
└── u2r_r2u_bridge/                 # 保留现有包，扩展字段
```

> 命名兼容说明：现有项目计划中若已经采用 `ggbot_*` 包名，应继续沿用，避免同一工作区同时出现职责重复的 `ggbot_*` 与 `campuscar_*` 包。本节名称是职责模板，不要求无意义改名。

职责分离：

- `campuscar_bringup`：参数、launch、URDF、统一启动；
- `campuscar_rtk_monitor`：解析 GGA 质量码和差分状态；
- `campuscar_geo_nav`：处理 UE 任务、地理目标和 Nav2 Action；
- `campuscar_base_driver`：底盘协议、里程计和硬件安全；
- `u2r_r2u_bridge`：只负责给 UE 发布稳定数据合同。

---

## 6. NUC 上安装 ROS 2 依赖

```bash
source /opt/ros/humble/setup.bash

sudo apt update
sudo apt install -y \
  ros-humble-nmea-navsat-driver \
  ros-humble-nmea-msgs \
  ros-humble-robot-localization \
  ros-humble-navigation2 \
  ros-humble-nav2-bringup \
  ros-humble-rosbridge-suite \
  ros-humble-twist-mux \
  ros-humble-diagnostic-updater \
  python3-serial \
  geographiclib-tools

sudo usermod -aG dialout "$USER"
```

执行 `usermod` 后注销并重新登录，然后确认：

```bash
groups
ros2 pkg list | grep -E 'nmea_navsat_driver|robot_localization|nav2|rosbridge'
```

如果以后改为 Ubuntu 24.04 + ROS 2 Jazzy，将包名前缀从 `ros-humble-` 换成 `ros-jazzy-`，但启动前仍要检查 Nav2 的 `Twist`/`TwistStamped` 参数差异。

---

## 7. 固定 RTK 串口设备名

这套 4G 板在 USB 下会产生多个复合串口，不能长期依赖 `/dev/ttyACM0`，因为重启或插拔后编号可能变化。

## 7.1 第一次在 NUC 上识别 NMEA 端口

先停止所有可能占用串口的 ROS 节点，再查看：

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

依次以 115200 打开端口，寻找类似内容：

```text
$GNGGA,044441.00,...,5,17,0.8,...,1.0,1449*7B
```

判断规则：

- 正确 NMEA 口：稳定输出 `$GNGGA`，内容是可读 ASCII；
- 调试口：可能输出 LuatOS 日志或二进制帧；
- 空闲口：没有输出；
- 不要把二进制调试口交给 `nmea_navsat_driver`。

找到正确端口后查询 USB 接口号：

```bash
udevadm info -q property -n /dev/ttyACM<N> | grep -E 'ID_VENDOR_ID|ID_MODEL_ID|ID_USB_INTERFACE_NUM|ID_SERIAL'
```

当前设备的 USB VID/PID 已知为：

```text
VID = 19d1
PID = 0001
```

但 Linux 下的 `ID_USB_INTERFACE_NUM` 必须在 NUC 上实测，不要只根据 macOS 端口尾号猜测。

## 7.2 创建 udev 规则

创建 `/etc/udev/rules.d/99-campuscar-rtk.rules`：

```udev
ACTION=="add", SUBSYSTEM=="tty", ATTRS{idVendor}=="19d1", ATTRS{idProduct}=="0001", IMPORT{builtin}="usb_id", ENV{ID_USB_INTERFACE_NUM}=="<实测接口号>", SYMLINK+="rtk_nmea", GROUP="dialout", MODE="0660"
```

应用规则：

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
ls -l /dev/rtk_nmea
```

以后所有配置统一使用：

```text
/dev/rtk_nmea
```

而不是 `/dev/ttyACM0`。

---

## 8. RTK NMEA 驱动与 ROS 2 话题

## 8.1 推荐话题

| 话题 | 类型 | 来源 | 用途 |
|---|---|---|---|
| `/fix` | `sensor_msgs/msg/NavSatFix` | NMEA 驱动 | 保持 PPT 和现有 UE 桥兼容 |
| `/gps/vel` | `geometry_msgs/msg/TwistStamped` | `nmea_topic_driver` | 若模块输出 RMC/VTG，可提供地速 |
| `/gps/nmea_sentence` | `nmea_msgs/msg/Sentence` | `nmea_topic_serial_reader` | 保留原始句子，供解析 GGA 质量 |
| `/rtk/status` | `diagnostic_msgs/msg/DiagnosticArray` | 质量监控节点 | 系统诊断 |
| `/rtk/status_json` | `std_msgs/msg/String` | 质量监控节点 | 便于桥接与日志 |
| `/rtk/autonomy_allowed` | `std_msgs/msg/Bool` | 质量门控 | 是否允许自动导航 |

## 8.2 launch 节点示例

因为质量监控必须读取原始 GGA，推荐把串口读取和 NMEA 解析拆成两个节点。ROS 2 Humble 的 `nmea_serial_driver` 会直接解析串口，但**不会**发布 `nmea_sentence`；原始句子应由 `nmea_topic_serial_reader` 发布，再由 `nmea_topic_driver` 解析。串口仍只有一个 owner。

`rtk_bringup.launch.py` 的核心应类似：

```python
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        Node(
            package='nmea_navsat_driver',
            executable='nmea_topic_serial_reader',
            name='rtk_nmea_reader',
            output='screen',
            respawn=True,
            respawn_delay=2.0,
            parameters=[{
                'port': '/dev/rtk_nmea',
                'baud': 115200,
                'frame_id': 'gps_link',
            }],
            remappings=[
                ('nmea_sentence', '/gps/nmea_sentence'),
            ],
        ),
        Node(
            package='nmea_navsat_driver',
            executable='nmea_topic_driver',
            name='rtk_nmea_parser',
            output='screen',
            parameters=[{
                'frame_id': 'gps_link',
                'time_ref_source': 'gps',
                'useRMC': False,
            }],
            remappings=[
                ('nmea_sentence', '/gps/nmea_sentence'),
                ('fix', '/fix'),
                ('vel', '/gps/vel'),
                ('heading', '/gps/heading'),
                ('time_reference', '/gps/time_reference'),
            ],
        ),
        Node(
            package='campuscar_rtk_monitor',
            executable='rtk_quality_monitor',
            name='rtk_quality_monitor',
            output='screen',
            parameters=['config/rtk_gate.yaml'],
            remappings=[
                ('nmea_sentence', '/gps/nmea_sentence'),
            ],
        ),
    ])
```

> `use_GNSS_time` 不是 Humble 2.0.1 版该驱动的参数。可用的是 `time_ref_source` 和 `useRMC`；系统时间仍应由 chrony/NTP 等独立机制维护。

首次排障时可先只验证直接解析路径：

```bash
ros2 run nmea_navsat_driver nmea_serial_driver --ros-args \
  -p port:=/dev/rtk_nmea -p baud:=115200 -p frame_id:=gps_link
```

需要原始 GGA 监控时，再改用上述 reader → topic driver 组合。不要同时启动两条路径，否则会争抢同一个串口。

验证：

```bash
ros2 topic echo /fix --once
ros2 topic hz /fix
ros2 topic echo /gps/nmea_sentence --once
```

最低要求：

- `/fix` 更新频率不低于 1 Hz；
- 纬度、经度不是 NaN；
- `header.frame_id` 为 `gps_link`；
- 时间戳持续更新；
- 原始 GGA 校验和有效。

---

## 9. 必须单独解析 RTK FLOAT / FIX

`sensor_msgs/NavSatFix.status` 不能可靠地区分所有厂家的 GGA `4` 和 `5`，因此不能只看 `/fix.status` 判断厘米级状态。

`rtk_quality_monitor` 应从 `$GNGGA` 解析：

| GGA 字段 | 含义 |
|---|---|
| 字段 6 | 定位质量码 |
| 字段 7 | 使用卫星数 |
| 字段 8 | HDOP |
| 字段 9 | 高程 |
| 字段 13 | 差分龄期，秒 |
| 字段 14 | 差分站 ID |

建议统一状态名称：

| GGA quality | 状态名称 | 自动驾驶策略 |
|---:|---|---|
| 0 | `NO_FIX` | 立即停止/拒绝新目标 |
| 1 | `GPS_FIX` | 仅显示，不允许自动导航 |
| 2 | `DGPS` | 可按项目策略低速，但不用于近障碍精确到点 |
| 4 | `RTK_FIX` | 正常自动导航 |
| 5 | `RTK_FLOAT` | 仅限开阔区域、降速、放宽到达阈值 |
| 6 | `DEAD_RECKONING` | 不作为独立全球定位依据 |

推荐初始门控参数：

```yaml
rtk_quality_monitor:
  ros__parameters:
    timeout_s: 2.0
    min_satellites: 12
    max_hdop: 1.5
    max_correction_age_s: 3.0
    allow_float: true
    float_max_speed_mps: 0.25
    fixed_max_speed_mps: 0.50
    no_fix_action: stop_and_cancel
```

状态判定建议：

```text
HARD_STOP：
  超过 2 秒无新数据，或 quality=0，或经纬度无效

DEGRADED：
  quality=5，且卫星/HDOP/差分龄期满足要求
  → 允许低速，目标容差建议 1～2 米

NORMAL：
  quality=4，且各质量指标满足要求
  → 可使用正常限速，目标容差可逐步降到 0.2～0.5 米
```

不要将“卫星数量多”直接等同于“厘米级”。必须同时看 quality、HDOP、差分龄期、数据新鲜度以及位置是否突跳。

---

## 10. TF 与传感器安装

推荐 TF 树：

```text
earth（可选）
└── map
    └── odom
        └── base_link
            ├── base_footprint
            ├── gps_link
            ├── imu_link
            ├── lidar_link
            └── camera_link
```

职责必须唯一：

- 全局 EKF 发布 `map → odom`；
- 本地 EKF 发布 `odom → base_link`；
- URDF/robot_state_publisher 发布 `base_link → gps_link/imu_link/...`；
- 任何一条 TF 都不能由两个节点同时发布。

## 10.1 RTK 天线外参

测量并写入 URDF：

- 天线相位中心相对 `base_link` 的前后偏移 `x`；
- 左右偏移 `y`；
- 高度 `z`。

例如：

```xml
<joint name="base_to_gps" type="fixed">
  <parent link="base_link"/>
  <child link="gps_link"/>
  <origin xyz="0.20 0.00 0.65" rpy="0 0 0"/>
</joint>
```

示例数值必须替换为实测值。天线应位于车体高处、朝上、无遮挡、牢固安装，并远离电机电源线和大块金属遮挡。这与 PPT 第 4 页要求一致。

## 10.2 单天线不等于有可靠航向

当前资料同时包含 UM980（单天线定位）和 UM982（双天线定向）手册，尚不能仅凭资料确认实物型号与天线数量。实施前应查看模块标签、天线接口和实际输出：

- 若实物为单天线方案，静止时 RTK 只能给位置，不能给可靠车头方向；
- RMC/VTG 的 course over ground 只有车辆移动后才有意义；
- 低速、倒车、原地转向时不能把 course 当车体 yaw；
- 自动导航必须融合 IMU 和轮速；
- 若实物为 UM982 类并正确安装两根天线，才可接入其 heading 输出；必须同时记录天线基线、输出 frame、真北/磁北约定和协方差；
- 若当前为单天线而又要求静止即得到高精度真航向，后续可升级双天线 GNSS。

IMU 必须确认：

- 输出坐标符合 ROS ENU 约定；
- 安装方向与 `imu_link` TF 一致；
- 陀螺零偏已标定；
- 磁力计若受电机干扰，不能直接作为绝对航向；
- covariance 不能全部错误地填 0。

---

## 11. robot_localization 融合架构

使用“双 EKF + navsat_transform_node”：

```text
/wheel/odom + /imu/data
        │
        ▼
EKF Local（world_frame=odom）
        │
        ├── /odometry/local
        └── TF odom → base_link

/fix + /imu/data + /odometry/global（全局 EKF 的当前预测）
        │
        ▼
navsat_transform_node
        │
        └── /odometry/gps

/wheel/odom + /imu/data + /odometry/gps
        │
        ▼
EKF Global（world_frame=map）
        │
        ├── /odometry/global
        └── TF map → odom
```

## 11.1 本地 EKF

目标：在 RTK 的 1 Hz 更新之间，仍以 30～50 Hz 提供平滑运动。

```yaml
ekf_local:
  ros__parameters:
    frequency: 30.0
    sensor_timeout: 0.2
    two_d_mode: true
    publish_tf: true

    map_frame: map
    odom_frame: odom
    base_link_frame: base_link
    world_frame: odom

    odom0: /wheel/odom
    odom0_config: [false, false, false,
                   false, false, false,
                   true,  false, false,
                   false, false, true,
                   false, false, false]
    odom0_differential: false

    imu0: /imu/data
    imu0_config: [false, false, false,
                  false, false, true,
                  false, false, false,
                  false, false, true,
                  true,  false, false]
    imu0_remove_gravitational_acceleration: true
```

上述 IMU 配置假设其 yaw 已经可用。若绝对 yaw 未标定，先只融合 `angular_velocity.z`，完成标定后再开启 orientation yaw。

## 11.2 navsat_transform_node

```yaml
navsat_transform:
  ros__parameters:
    frequency: 10.0
    delay: 1.0
    magnetic_declination_radians: 0.0   # 按现场和 IMU 类型实配
    yaw_offset: 0.0                     # 使校正后 yaw=0 表示东向
    zero_altitude: true
    publish_filtered_gps: true
    broadcast_utm_transform: false
    use_odometry_yaw: false
    wait_for_datum: true
    datum: [<origin_lat>, <origin_lon>, <origin_heading_rad>]
```

launch 中必须显式连接：

```python
remappings=[
    ('imu/data', '/imu/data'),
    ('gps/fix', '/fix'),
    ('odometry/filtered', '/odometry/global'),
    ('odometry/gps', '/odometry/gps'),
    ('gps/filtered', '/gps/filtered'),
]
```

要求：

- `origin_lat/origin_lon` 使用校园统一参考原点；
- `datum` 第三个数是**弧度制 ENU 航向**，`0` 表示东向，不是高程；
- 该 datum 与 UE 校园模型必须使用同一个基准；
- `use_odometry_yaw=false` 时，IMU 必须能提供地球参考的绝对航向；只有角速度、磁航向未标定或受电机干扰时，不能据此初始化全局 ENU 朝向；
- 不可在轮速里程计没有地球参考航向时随意设置 `use_odometry_yaw=true`；
- `zero_altitude=true` 适合二维地面车，可避免 GNSS 高程噪声影响导航；
- `/fromLL` 服务即使已经出现，也不代表 datum 转换已经初始化。至少等待 `/odometry/global`、所需 TF 和 `/odometry/gps` 正常，再用已知点做一次转换自检后接受 UE 目标；未初始化或返回全零/非有限值时必须拒绝任务。

## 11.3 全局 EKF

```yaml
ekf_global:
  ros__parameters:
    frequency: 30.0
    sensor_timeout: 0.5
    two_d_mode: true
    publish_tf: true

    map_frame: map
    odom_frame: odom
    base_link_frame: base_link
    world_frame: map

    odom0: /wheel/odom
    odom0_config: [false, false, false,
                   false, false, false,
                   true,  false, false,
                   false, false, true,
                   false, false, false]

    odom1: /odometry/gps
    odom1_config: [true,  true,  false,
                   false, false, false,
                   false, false, false,
                   false, false, false,
                   false, false, false]

    imu0: /imu/data
    imu0_config: [false, false, false,
                  false, false, true,
                  false, false, false,
                  false, false, true,
                  false, false, false]
```

需要根据实车噪声设置 pose/twist rejection threshold，避免单次 GNSS 突跳导致 `map → odom` 猛跳。

---

## 12. 坐标系统与 UE 映射

## 12.1 统一坐标所有权

建议由 ROS 2 维护唯一的校园地理配置：

```yaml
campus_origin:
  latitude: <origin_lat>
  longitude: <origin_lon>
  altitude: <origin_alt>

map_alignment:
  rotation_deg: <地图北向相对 ENU 的旋转>

ue_mapping:
  scale_cm_per_m: 100.0
  axis_mapping: <例如 X=east,Y=north；最终由 UE 工程确认>
  rotation_offset_deg: <模型旋转补偿>
  x_offset_cm: <校园模型偏移>
  y_offset_cm: <校园模型偏移>
  z_offset_cm: <高度偏移>
  heading_offset_deg: <模型车头补偿>
```

不要让 ROS、UE 和离线脚本各自保存不同的原点。

## 12.2 推荐的数据策略

`/R2UTopic_Pos` 同时发送：

1. 原始经纬度，便于调试和地图服务；
2. ROS `map` 坐标（米），作为机器人导航的权威本地坐标；
3. UE 需要的坐标转换参数版本。

这样 UE 可以：

- 第一阶段继续按 PPT 自己做经纬度转换；
- 稳定后优先使用 ROS 提供的 `map.x_m/map.y_m`，只做米到 UE 厘米及轴向转换；
- 避免两边使用不同地球模型导致位置逐渐偏离。

## 12.3 UE 单位

UE 默认世界单位通常为厘米：

```text
1 ROS 米 = 100 UE 单位
```

但轴向和旋转不能猜，必须现场确认：

- 东向对应 UE X 还是 Y；
- 北向是否需要取负；
- 校园模型自身是否有旋转；
- 车头 mesh 的正方向是 UE X 还是其他方向。

---

## 13. `/R2UTopic_Pos` 数据合同

保持 PPT 要求的基础字段，扩展字段只新增、不随意改名。

推荐 JSON：

```json
{
  "schema": "campuscar.position.v1",
  "status": 0,
  "status_name": "RTK_FLOAT",
  "latitude": 0.0,
  "longitude": 0.0,
  "altitude": 0.0,
  "timestamp": 0.0,
  "frame_id": "gps_link",
  "rtk": {
    "gga_quality": 5,
    "satellites": 19,
    "hdop": 0.8,
    "correction_age_s": 1.0,
    "station_id": "1449",
    "stale": false
  },
  "map": {
    "frame_id": "map",
    "x_m": 0.0,
    "y_m": 0.0,
    "z_m": 0.0,
    "yaw_rad": 0.0
  },
  "vehicle": {
    "linear_speed_mps": 0.0,
    "angular_speed_rps": 0.0,
    "task_state": "IDLE",
    "autonomy_allowed": true
  }
}
```

注意：现有 bridge 的 `status` 可能使用 `NavSatFix.status`。不要直接改变其语义。推荐保留原字段兼容旧 UE，并通过 `status_name` 和 `rtk.gga_quality` 明确表达 `FLOAT/FIX`。

发布要求：

- 频率：与 `/fix` 同步，至少 1 Hz；融合位姿可另以 10 Hz 发布；
- 时间戳：统一 Unix 秒或 ROS time，写入接口文档；
- 超过 2 秒无 RTK 数据时：`status_name=TIMEOUT`、`stale=true`；
- JSON 序列化失败：bridge 不得崩溃，应发布诊断；
- 不向 UE 发送 NTRIP 账号、密码或完整认证日志。

---

## 14. UE 下发目标的数据合同

保留 `/U2RTopic_Command`，类型可继续使用 `std_msgs/msg/String`，其 `data` 为 JSON。

推荐命令：

```json
{
  "schema": "campuscar.command.v1",
  "command_id": "20260813-0001",
  "timestamp": 0.0,
  "command": "navigate_to_geo",
  "target": {
    "latitude": 0.0,
    "longitude": 0.0,
    "altitude": 0.0,
    "yaw_deg": 0.0
  },
  "constraints": {
    "max_speed_mps": 0.25,
    "require_rtk_fixed": false,
    "goal_tolerance_m": 1.0
  }
}
```

同时支持：

```text
navigate_to_geo   经纬度目标
navigate_to_map   ROS map 坐标目标
navigate_to_poi   预登记安全地点 ID
pause             暂停任务并停车
resume            恢复任务
cancel            取消当前 Nav2 Action
estop             软件急停；不能替代物理急停
clear_estop       仅在本地安全条件满足后允许解除
```

## 14.1 推荐优先使用 POI

生产运行时，UE 最好发送：

```json
{
  "command": "navigate_to_poi",
  "poi_id": "GREENHOUSE_A_ENTRANCE"
}
```

NUC 在本地 `waypoints.yaml` 中保存经过人工验收的目标点。这样比允许 UE 任意发送经纬度更安全，也便于地理围栏和权限控制。

## 14.2 task_executor 接收命令后的检查顺序

1. JSON schema 和字段类型正确；
2. `command_id` 未重复；
3. 时间戳未过期；
4. 目标在地理围栏内；
5. 当前没有物理急停；
6. RTK/IMU/odom 数据没有超时；
7. 当前定位质量满足任务要求；
8. 目标可转换到 `map`；
9. Nav2 lifecycle 处于 active；
10. 接受目标后发布带 `command_id` 的 ACK。

状态反馈使用 `/R2UTopic_Status`：

```json
{
  "command_id": "20260813-0001",
  "accepted": true,
  "state": "MOVING",
  "reason": "",
  "distance_remaining_m": 12.4,
  "timestamp": 0.0
}
```

---

## 15. 经纬度目标如何交给 Nav2

推荐 `geo_goal_bridge`：

1. 接收纬度、经度；
2. 确认定位融合和 datum 已初始化；
3. 调用 Humble `robot_localization` 的 `/fromLL`（服务类型 `robot_localization/srv/FromLL`）转换成 `map` 坐标；
4. 拒绝非有限、未初始化或明显异常的转换结果，并用本地 ENU/geofence 逻辑核验；
5. 构造 `geometry_msgs/PoseStamped`；
6. 调用 Nav2 `NavigateToPose` Action；
7. 反馈 accepted、running、succeeded、failed、canceled。

不要直接把经纬度数值填进 `PoseStamped.position.x/y`。纬度和经度是角度，Nav2 的 `map` 坐标是米。

Humble 下建议自己实现这个轻量桥接层，避免依赖只在较新 Nav2 版本中完整提供的 GPS waypoint 接口。

---

## 16. Nav2 与底盘控制链

推荐话题链：

```text
Nav2 controller
  └── /cmd_vel_nav

UE 手动遥控
  └── /cmd_vel_teleop

安全停车/急停
  └── /cmd_vel_stop 或锁止信号

上述输入
  ▼
twist_mux（优先级：急停 > 手动 > Nav2）
  ▼
nav2_velocity_smoother
  ▼
nav2_collision_monitor
  ▼
/cmd_vel_safe
  ▼
campuscar_base_driver
```

底盘驱动不得直接订阅未经安全处理的多个 `/cmd_vel` 来源。

初次实车参数建议：

```yaml
limits:
  max_linear_mps: 0.25
  max_angular_rps: 0.6
  max_linear_accel_mps2: 0.25
  max_angular_accel_rps2: 0.8
  command_timeout_s: 0.5
```

完成低速测试和制动距离测量后，才逐步放宽。

---

## 17. 底盘驱动节点

### 17.1 先冻结唯一正式协议

当前资料中的底盘接口并不一致：

- PPT 给出 8 字节控制帧和 18 字节反馈帧；
- 当前可审计 ESP32 固件按**单字符**解析 `g/w/s/a/d/x/0`；
- 主机 Python 原型期望整行 `S <speed> <steering>`、`STOP`、`STATUS`；
- STM32 原型又使用 `S<speed> <steering>`、`STOP`。

这些协议不能混用。特别是向当前 ESP32 单字符固件发送 `S 100 0`，首字符大写 `S` 会被立即解释为固定后退，存在实车危险。在完成以下事项前，禁止把最终 `/cmd_vel` 接到实车：

1. 读取当前 MCU 型号、烧录固件和真实接线；
2. 选定唯一协议并完成双向回环测试；
3. MCU 内实现 500 ms 命令 watchdog，超时回到安全零输出；
4. 先验证 `STOP`、非法帧、断串口和物理急停；
5. 车轮架空或在封闭空旷场地进行首轮运动测试。

### 17.2 仅当正式底盘确认为左右轮差速接口时

根据 PPT 第 12～14 页，差速底盘换算应使用实车轮距和轮半径：

```text
v_left  = v - ω × track_width / 2
v_right = v + ω × track_width / 2

rpm_left  = v_left  / (2π × wheel_radius) × 60
rpm_right = v_right / (2π × wheel_radius) × 60
```

之后再将 RPM 映射为固件要求的 `-1000～1000` 控制量。

8 字节控制帧：

```text
0xABCD | left/int16 | right/int16 | checksum/uint16
```

小端序发送，校验建议按 PPT 约定：

```text
checksum = 0xABCD XOR (left & 0xFFFF) XOR (right & 0xFFFF)
```

Python 打包形式可采用：

```python
payload = struct.pack(
    '<HhhH',
    0xABCD,
    left_command,
    right_command,
    checksum,
)
```

但在接真车前必须与实际 MCU 固件确认；在确认之前，上述帧和 XOR 公式都只属于 PPT 候选合同，不能视为已实现协议：

- left/right 字段究竟代表左右轮，还是转向/油门；
- 字段有符号性；
- 映射比例；
- 校验覆盖范围；
- 反馈帧的左右轮顺序；
- 轮速正方向；
- 看门狗超时时间。

只有解析到真实左右轮反馈、完成轮径/轮距标定并传播合理 covariance 后，才能发布 `/wheel/odom`；不得用发出的 `/cmd_vel` 或 PWM 命令伪造里程计。底盘节点至少发布：

| 话题 | 类型 |
|---|---|
| `/wheel/odom` | `nav_msgs/msg/Odometry` |
| `/battery_state` | `sensor_msgs/msg/BatteryState` |
| `/base/status` | `diagnostic_msgs/msg/DiagnosticArray` |
| `/base/estop` | `std_msgs/msg/Bool` |

安全要求：

- 500 ms 无新命令必须由 NUC 和 STM32 **双重停车**；
- 节点退出时发送停车帧；
- 串口断开时不得保持最后速度；
- 物理急停优先级高于任何 ROS 指令；
- 初次转轮测试必须架空车轮或在空旷区域低速进行。

---

## 18. 地图、路线和避障要求

如果目标只是“沿着已确认的校园道路去某个点”，第一版可以采用：

- RTK + IMU + 轮速定位；
- 人工录制的安全路点；
- 地理围栏；
- 低速跟踪；
- 人工手持急停监督。

如果目标是“在有行人、车辆和临时障碍的校园自主行驶”，还必须增加：

- LiDAR、深度相机或可靠的障碍物检测；
- Nav2 local costmap；
- collision monitor；
- 最小停车距离验证；
- 行人和道路规则策略。

**只有 RTK 而没有障碍物感知，不能视为可安全自主导航系统。**

---

## 19. 定位质量降级策略

建议状态机：

```text
INIT
  └── 等待 /fix、/imu、/wheel/odom、TF

READY_FIXED
  └── RTK FIX，允许正常自动任务

READY_FLOAT
  └── RTK FLOAT，只允许开阔场地低速任务

DEGRADED
  └── 卫星少、HDOP 大、差分龄期过高或位置异常
      → 降速、暂停接受新目标

LOST
  └── NO_FIX 或 RTK 超时
      → 立即输出 0、取消 Nav2、上报 UE

ESTOP
  └── 物理/软件急停
      → 底盘锁止，必须人工确认后恢复
```

推荐触发动作：

| 故障 | 动作 |
|---|---|
| `/fix` 超过 2 秒未更新 | 停车并取消目标 |
| GGA quality 变为 0 | 停车并上报 `NO_FIX` |
| 差分龄期超过 3～5 秒 | 转 `DEGRADED`；近障碍任务停车 |
| HDOP > 2.0 | 降速或停车 |
| 位置单帧突跳超过合理阈值 | 拒绝该测量并报警 |
| `/imu` 或 `/wheel/odom` 超时 | 停车 |
| TF 不完整 | Nav2 不允许 active |
| rosbridge 断开 | 上报本地日志；是否继续自主任务按任务策略决定 |
| 控制串口断开 | MCU 看门狗停车 |

---

## 20. rosbridge 与网络安全

启动：

```bash
ros2 launch rosbridge_server rosbridge_websocket_launch.xml
ss -tlnp | grep 9090
```

UE 推荐连接：

```text
ws://<NUC_IP>:9090
```

注意：rosbridge 默认不是安全的公网控制接口。

- 只允许受信任局域网访问 9090；
- 不要把 9090 直接映射到公网；
- UE 命令必须经过 task_executor 校验，不能直接连接底盘驱动；
- 对任意目标点做 geofence 检查；
- 命令必须带 ID、时间戳并做去重；
- 物理急停不能依赖网络；
- 摄像头视频继续使用独立 RTSP/HLS 链路，不要塞进 rosbridge。

视频方案见 [RTK/机器人-UE5视频通信方案.md](../RTK/机器人-UE5视频通信方案.md)。

---

## 21. 开机自启与断线重连

建议统一由一个 ROS 2 bringup 启动，驱动节点自身设置 `respawn`，外层再由 systemd 负责进程级恢复。

创建 `/etc/systemd/system/campuscar-robot.service`：

```ini
[Unit]
Description=CampusCar ROS 2 Robot Bringup
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=haoyu
SupplementaryGroups=dialout
Environment=ROS_DOMAIN_ID=20
Environment=RCUTILS_COLORIZED_OUTPUT=1
ExecStart=/bin/bash -lc 'source /opt/ros/humble/setup.bash && source /home/haoyu/campuscar_ws/install/setup.bash && while [ ! -e /dev/rtk_nmea ]; do sleep 1; done && exec ros2 launch campuscar_bringup robot_bringup.launch.py'
Restart=always
RestartSec=3
TimeoutStopSec=10
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable campuscar-robot.service
sudo systemctl start campuscar-robot.service
sudo systemctl status campuscar-robot.service
journalctl -u campuscar-robot.service -f
```

要求：

- RTK USB 拔插后，驱动能重新打开 `/dev/rtk_nmea`；
- rosbridge 异常退出能重启；
- 底盘驱动重启期间 MCU 看门狗保持停车；
- systemd 停止服务时，底盘节点先发送停车帧；
- NTRIP 账号不写入 systemd、launch、Git 或日志，因为 4G 评估板已经持久保存。

---

## 22. 日志与 rosbag

联调时建议录制：

```bash
ros2 bag record \
  /fix \
  /gps/nmea_sentence \
  /rtk/status \
  /imu/data \
  /wheel/odom \
  /odometry/local \
  /odometry/gps \
  /odometry/global \
  /R2UTopic_Pos \
  /R2UTopic_Status \
  /cmd_vel_safe \
  /tf /tf_static
```

日志中应记录：

- RTK 状态切换；
- 卫星数、HDOP、差分龄期；
- UE 命令 ID 与接收结果；
- Nav2 Action 状态；
- 急停、超时和取消原因；
- 串口重连；
- 地理坐标和本地坐标转换版本。

不得记录：

- NTRIP 密码；
- HTTP Basic Authorization 内容；
- 不必要的完整身份信息。

---

## 23. 分阶段验收矩阵

## 23.1 RTK 驱动

- [ ] `/dev/rtk_nmea` 插拔后名称不变；
- [ ] 115200 下持续收到 `$GNGGA`；
- [ ] `/fix` ≥ 1 Hz；
- [ ] 室外 latitude/longitude 有效；
- [ ] `/rtk/status` 能区分 FLOAT 和 FIX；
- [ ] 拔掉 RTK 后 2 秒内显示 TIMEOUT。

## 23.2 UE 显示——对应 PPT 第一版验收

- [ ] rosbridge 监听 9090；
- [ ] UE 能连接 `ws://<NUC_IP>:9090`；
- [ ] UE 能订阅 `/R2UTopic_Pos`；
- [ ] UE 能打印并解析基础 JSON；
- [ ] UE 能显示 `NO_FIX/RTK_FLOAT/RTK_FIX/TIMEOUT`；
- [ ] 静止时模型位置稳定；
- [ ] 实车移动 3～5 米时，模型方向和距离合理；
- [ ] 原点、轴向、比例、旋转补偿已写入配置文档。

## 23.3 里程计与融合

- [ ] 推车直行 1 米，轮速里程计误差可接受；
- [ ] 原地旋转 90°，yaw 约变化 1.57 rad；
- [ ] `map → odom → base_link` 连续且无 TF 冲突；
- [ ] RTK 1 Hz 时 `/odometry/local` 仍维持 30 Hz；
- [ ] 短时遮挡 GNSS 时局部里程计连续；
- [ ] GNSS 恢复后全局定位平滑收敛，不瞬间猛跳。

## 23.4 底盘

- [ ] 物理急停有效；
- [ ] 停车帧已验证；
- [ ] 500 ms 命令超时自动停车；
- [ ] 架空状态下左右轮方向正确；
- [ ] 0.1 m/s 小速度直行；
- [ ] 小速度左右转；
- [ ] 反馈帧、轮速、电压、温度解析正确；
- [ ] 串口断开后立即停车。

## 23.5 UE 下发目标与 Nav2

- [ ] UE 下发目标后收到 ACK；
- [ ] 目标经纬度正确转换成 `map` 坐标；
- [ ] 围栏外目标被拒绝；
- [ ] 重复 `command_id` 不会执行两次；
- [ ] Nav2 能在架空/仿真条件下输出合理 `/cmd_vel`；
- [ ] 空旷场地以 ≤0.25 m/s 完成第一个短距离目标；
- [ ] FLOAT 模式采用较大到达容差；
- [ ] NO_FIX、IMU 超时、odom 超时任一发生时立即停车；
- [ ] UE 能看到 MOVING/SUCCEEDED/FAILED/CANCELED。

## 23.6 故障注入

- [ ] 运行中拔掉 RTK；
- [ ] 临时遮挡天线；
- [ ] 停止 rosbridge；
- [ ] 断开底盘串口；
- [ ] 杀死驱动节点；
- [ ] 发送过期/非法/围栏外命令；
- [ ] 按下物理急停；
- [ ] 每种故障都有确定、安全、可复现的行为。

---

## 24. 推荐实施顺序

严格按以下顺序，不要同时联调全部模块：

### 第 1 天：RTK → ROS 2

1. 在 NUC 确认 NMEA 端口；
2. 创建 `/dev/rtk_nmea` udev 规则；
3. 启动 `nmea_navsat_driver`；
4. 验证 `/fix`；
5. 实现 FLOAT/FIX 质量监控；
6. 录制第一份室外 rosbag。

### 第 2 天：ROS 2 → UE

1. 保持现有 `/R2UTopic_Pos` 基础字段；
2. 增加 RTK 状态扩展字段；
3. 启动 rosbridge；
4. UE 打印原始 JSON；
5. 确认校园原点、轴向、比例和旋转；
6. 推车 3～5 米完成 PPT 第 27 页验收。

### 第 3～4 天：底盘基础

1. 停车帧；
2. 超时停车；
3. 小速度直行/转向；
4. 反馈帧；
5. `/wheel/odom`；
6. 急停联调。

### 第 5～6 天：定位融合

1. IMU 标定和 TF；
2. local EKF；
3. navsat_transform；
4. global EKF；
5. 手推 5～20 米验证轨迹；
6. 故障和突跳测试。

### 第 7 天以后：Nav2 与 UE 目标

1. 先在仿真或架空状态验证 `/cmd_vel`；
2. 实现 `geo_goal_bridge`；
3. 实现 POI 和 geofence；
4. 开阔场地短距离低速测试；
5. 加障碍物感知；
6. 最后才做完整校园任务。

---

## 25. 当前能否满足 PPT 和自动移动需求

### 25.1 PPT 的 RTK × UE 接入需求

**可以满足。**当前 RTK 硬件和差分链路已经通过实测，NUC 只需补齐：

- 固定串口名；
- ROS 2 NMEA 驱动；
- FLOAT/FIX 质量解析；
- `/R2UTopic_Pos` 扩展；
- 校园坐标参数。

完成这些即可达到 PPT 第 27～28 页的第一版验收标准。

### 25.2 “让机器人去一个地方”

**可以实现，但不是只接入 RTK 就自动实现。**必须同时完成：

- IMU + 轮速里程计；
- TF 和 robot_localization；
- Nav2；
- `/cmd_vel` 底盘节点；
- 急停、限速、超时停车；
- 路线/地图/geofence；
- 对动态障碍安全运行时还需要 LiDAR 或深度感知。

当前 RTK FLOAT 可先用于开阔场地低速验证。若目标是精确停靠、贴近障碍或窄路行驶，应优先等待 RTK FIX，并完成整体安全验收。

---

## 26. 最终交付物清单

完成部署后应交付：

- [ ] `99-campuscar-rtk.rules`；
- [ ] `campuscar_bringup` ROS 2 包；
- [ ] `rtk_quality_monitor`；
- [ ] `ekf.yaml`、`navsat.yaml`；
- [ ] `campuscar.urdf.xacro` 中 RTK/IMU 外参；
- [ ] 更新后的 `u2r_r2u_bridge`；
- [ ] UE JSON v1 数据合同；
- [ ] `geo_goal_bridge` 和 `task_executor`；
- [ ] 底盘驱动、反馈解析和 `/wheel/odom`；
- [ ] Nav2 参数；
- [ ] POI 与 geofence 配置；
- [ ] systemd 服务；
- [ ] 静止、推车、低速自动导航三组 rosbag；
- [ ] 故障注入记录；
- [ ] NTRIP 服务于 2027-07-04 到期的续费提醒。

完成上述交付物后，系统的数据闭环为：

```text
UE 下发“去哪里”
→ ROS 2 验证并转换目标
→ Nav2 决定“怎么走”
→ 运控决定“轮子怎么转”
→ RTK + IMU + 轮速确认“实际走到哪里”
→ ROS 2 将位置和任务状态回传 UE
```
