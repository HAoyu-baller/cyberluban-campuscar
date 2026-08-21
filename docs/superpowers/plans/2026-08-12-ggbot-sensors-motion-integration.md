# GreenGuardian Bot 传感器与闭环运控实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 NUC 的 Ubuntu 22.04 + ROS 2 Humble 上，依次跑通 RTK、激光雷达、相机，并建立“定位/感知 → 路径跟踪 → 安全仲裁 → `/cmd_vel` → ESP32 PWM → 差速底盘”的可验证闭环。

**Architecture:** 传感器驱动只负责发布原始 ROS 话题；定位节点把 RTK 经纬度转换成校园局部 `map` 米制坐标，并用 IMU/里程计提供航向和高频运动状态；路径跟踪节点只输出 `Twist` 意图，不直接操作串口。安全仲裁节点统一处理定位丢失、雷达障碍、急停和超时，最后由底盘桥接节点把 `linear.x/angular.z` 转成 ESP32 当前控制链路能接受的持续串口命令。

**Tech Stack:** Ubuntu 22.04、ROS 2 Humble、Python 3、`rclpy`、`sensor_msgs`、`geometry_msgs`、`nav_msgs`、`tf2_ros`、`nmea_navsat_driver`、`pyserial`、`rosbridge_suite`、RViz2、V4L2/相机厂商 ROS 2 驱动；可选 `robot_localization` 和 `twist_mux`。

## Global Constraints

- 当前真实底盘链路是 **NUC/主机 → USB 串口 → ESP32 → 两路 50 Hz RC PWM → hoverboard/小车控制器**，不是 PPT 中的 8 字节 `0xABCD` 二进制协议；两条链路不能混用。
- 当前 [esp32_pwm_gesture.ino](../../../esp32_pwm_gesture.ino) 只处理 `g`、`w/s/a/d`、`x/0` 单字符命令，不处理 [ESP32.md](../../../ESP32.md) 中的 `S <speed> <steering>`；在固件统一前，现有 Python 控制器不能直接作为 ROS2 驱动使用。
- 当前 ESP32 代码在收到运动命令后没有独立的 500 ms 主机通信看门狗；运动闭环接入前必须补上“命令超时自动回中”。
- `PWM切换组合参数使用说明.md` 规定：50 Hz、1000–2000 µs、1500 µs 中位、组合手势顺序为速度上/下/转向左/右；最后“右”识别后必须立即回中。
- hoverboard 固件在两路长期中位约 10 s 后会切回 USART；不能把“PWM 中位”当作长期任务的无动作保活方案。优先确认是否能改 hoverboard 固件；不能改时，必须把该限制作为任务暂停/重新接管的设计条件，禁止用危险的非中位脉冲伪造保活。
- 上位机每 50 ms（20 Hz）发送一次运动命令；串口断开、命令年龄超过 500 ms、ROS 节点退出、定位无效、雷达触发或急停时，最终输出必须为零。
- RTK 经纬度绝不直接发送给电机；RTK 只参与得到 `map` 中的位姿，控制器最终发送的是 `v`（m/s）和 `ω`（rad/s）对应的速度意图。
- 单天线 RTK 在静止时不能提供车体航向；航向必须来自 IMU/里程计、双天线 RTK，或一次受控直行后的 RTK 航迹方向。
- 第一版相机目标是“稳定采集、时间戳、标定和可视化”；不把虫卵识别或视觉喷洒决策加入两个月内的基础闭环。
- 所有落地测试均在空旷区域、低速、有人负责物理急停的条件下进行；先架空轮子，再牵引/推行，再低速自主。
- 当前硬件型号尚未在文档中完整给出；在型号、接口、电压、坐标轴和供电确认前，不安装猜测的驱动，也不接通大功率水泵或电机。

---

## 一、现状结论与必须先解决的矛盾

### 已有条件

- NUC 已有 Ubuntu 22.04、ROS 2 Humble、`nmea_navsat_driver`、`rosbridge_suite` 和 `u2r_r2u_bridge`。
- 项目已经确定差速底盘、预设路径、RTK→局部坐标→UE 的总体方向。
- ESP32 的物理 PWM 引脚已经明确：GPIO25=CH1/转向，GPIO26=CH2/速度，共地，50 Hz。
- [esp32_pwm_controller.py](../../../esp32_pwm_controller.py) 已经表达了较好的主机侧安全意图：20 Hz 发送、`STOP`、串口状态读取、运动前确认；但它依赖另一个带 `ARMED` 和文本协议的 ESP32 固件。

### 当前不可直接联调的地方

1. [esp32_pwm_gesture.ino](../../../esp32_pwm_gesture.ino) 和 [ESP32.md](../../../ESP32.md) 不是同一个协议版本。
2. [stm32_pwm_src/main.cpp](../../../stm32_pwm_src/main.cpp) 是 PA0/PA1 的另一套 STM32 PWM 方案，不能和 ESP32 GPIO25/26 方案同时作为“当前真相”。
3. 现有文档中的 `/cmd_vel`→左右轮 RPM 公式适用于能接收左右轮输入的底盘；当前 RC PWM 方案实际上暴露的是“转向 + 速度”两通道，不能假装拥有左右轮反馈。
4. 没有编码器反馈时不能按计划可靠发布 `/odom`；仅靠 1 Hz RTK 不能承担 20 Hz 的平滑控制和车体航向估计。
5. “雷达”与“相机”型号、ROS 驱动、安装坐标和实际话题名尚未确认，因此必须先做设备盘点。

### 统一后的最小接口合同

```text
RTK/NMEA                    -> /fix                 sensor_msgs/NavSatFix
IMU                         -> /imu/data            sensor_msgs/Imu
底盘编码器/控制器反馈        -> /odom                nav_msgs/Odometry
激光雷达                    -> /scan                sensor_msgs/LaserScan
相机                        -> /camera/image_raw    sensor_msgs/Image
相机标定                    -> /camera/camera_info  sensor_msgs/CameraInfo
RTK+IMU/odom 定位           -> /pose_est             geometry_msgs/PoseStamped (frame_id=map)
任务执行器                  -> /target_path         nav_msgs/Path (frame_id=map)
路径跟踪器                  -> /cmd_vel_path        geometry_msgs/Twist
安全仲裁器                  -> /cmd_vel             geometry_msgs/Twist
ESP32 桥接                  -> S <speed> <steering> 20 Hz 串口文本
```

`/cmd_vel_path` 与最终 `/cmd_vel` 必须分开，避免路径跟踪节点和避障节点同时向同一个话题抢写。

---

## 二、RTK 得到位置后，如何发送运控指令

这条链路是本项目最关键的理解：**位置不是电机指令，中间必须经过坐标转换、航向估计、目标点控制和安全门控。**

```text
RTK 经纬度(lat, lon)
    ↓ 过滤定位状态、时间戳、跳点
局部 map 坐标(x, y)，单位米
    + IMU/里程计 yaw（车头朝向）
    ↓ 与目标航点比较
距离误差、航向误差、横向误差
    ↓ path_follower
Twist(linear.x=v, angular.z=ω)
    ↓ safety_mux
最终安全 Twist 或零速度
    ↓ cmd_vel_pwm_bridge
speed = v / v_max * 1000
steering = ω / ω_max * 1000
    ↓ 20 Hz 文本串口
S <speed> <steering>\n
    ↓ ESP32
CH1 转向 PWM + CH2 速度 PWM
    ↓
底盘控制器和电机
```

### 2.1 经纬度转校园局部坐标

在校园这样的小范围内，先使用局部切平面 ENU 近似。原点必须是现场固定并记录的 RTK 平均点，而不是随手取的一帧：

```python
from math import cos, radians

EARTH_RADIUS_M = 6378137.0

def latlon_to_enu(lat, lon, origin_lat, origin_lon):
    east = EARTH_RADIUS_M * cos(radians(origin_lat)) * radians(lon - origin_lon)
    north = EARTH_RADIUS_M * radians(lat - origin_lat)
    return east, north
```

第一版约定：`map.x = east`、`map.y = north`、单位为米。若 UE 地图方向不同，只在 UE 映射层或统一的 `map_rotation_rad` 中处理，不在每个节点里各自旋转。

带地图旋转时统一使用：

```python
from math import cos, sin

def rotate_map(east, north, map_rotation_rad):
    x = cos(map_rotation_rad) * east - sin(map_rotation_rad) * north
    y = sin(map_rotation_rad) * east + cos(map_rotation_rad) * north
    return x, y
```

### 2.2 从当前位置和目标点算 `v/ω`

对目标航点 `(tx, ty)` 和当前位姿 `(x, y, yaw)`：

```python
from math import atan2, cos, hypot, sin, pi

def wrap_pi(angle):
    return (angle + pi) % (2.0 * pi) - pi

def target_twist(x, y, yaw, tx, ty, v_max=0.20, omega_max=0.60):
    dx, dy = tx - x, ty - y
    distance = hypot(dx, dy)
    desired_yaw = atan2(dy, dx)
    heading_error = wrap_pi(desired_yaw - yaw)

    # 初期调试阶段：车头偏差太大时原地转，避免倒着冲向航点。
    if abs(heading_error) > 1.0472:  # 60 degrees
        v = 0.0
    else:
        v = min(v_max, 0.8 * distance * cos(heading_error))
        v = max(0.0, v)

    omega = max(-omega_max, min(omega_max, 1.5 * heading_error))
    return v, omega, distance, heading_error
```

到达判断建议为 `distance <= 0.20 m`，需要对准车头的航点再额外判断 `abs(heading_error) <= 10°`。控制循环可以 20 Hz 运行，但位姿必须有新鲜数据；RTK 只有 1 Hz 时，不能假装有 20 Hz 定位，应使用 IMU/里程计补足中间状态，并在定位年龄超过 1 s 时减速或停车。

### 2.3 `Twist` 如何变成当前 ESP32 的命令

当前 PWM 方案不是 PPT 的左右轮二进制帧。ROS2 桥接节点采用以下可校准映射：

```python
MAX_LINEAR_MPS = 0.20       # 初次自主测试，不超过 0.20 m/s
MAX_ANGULAR_RPS = 0.60
STEERING_SIGN = 1           # 现场校准；反向时改为 -1

def twist_to_pwm_command(linear_x, angular_z):
    speed = round(max(-1.0, min(1.0, linear_x / MAX_LINEAR_MPS)) * 1000)
    steering = round(
        max(-1.0, min(1.0, angular_z / MAX_ANGULAR_RPS))
        * 1000 * STEERING_SIGN
    )
    return speed, steering
```

然后以 20 Hz 发送：

```text
S 300 0\n       # v=0.06 m/s，直行（按 MAX_LINEAR_MPS=0.20）
S 300 250\n     # 同样速度，同时产生左/右转向，正负方向须现场确认
STOP\n          # 立即双通道回中
```

这段映射只代表“上位机意图到 RC 通道”的初始标定，不代表厘米级运动精度。必须先测出 `speed=+100` 的实际方向、`steering=+100` 的实际转向方向和底盘转弯响应，再决定 `STEERING_SIGN`、端点和 `MAX_*` 参数。若需要真正的左右轮 RPM 闭环，必须改走可回传编码器的 UART/CAN 控制链路，不能从两路 RC PWM 反推出可靠 `/odom`。

---

## 三、文件与模块分工

当前目录是文档和 ESP32 原型目录；ROS2 节点应在 NUC 的 `~/campuscar_ws/src/` 中创建。建议文件结构如下：

```text
~/campuscar_ws/src/
├── ggbot_bringup/
│   ├── launch/sensors.launch.py
│   ├── launch/ggbot_full.launch.py
│   └── config/sensors.yaml
├── ggbot_localization/
│   ├── ggbot_localization/rtk_localizer.py
│   ├── ggbot_localization/origin_manager.py
│   ├── config/localization.yaml
│   └── test/test_rtk_localizer.py
├── ggbot_perception/
│   ├── ggbot_perception/lidar_safety.py
│   ├── ggbot_perception/camera_health.py
│   ├── config/perception.yaml
│   └── test/
├── ggbot_control/
│   ├── ggbot_control/waypoint_follower.py
│   ├── ggbot_control/cmd_vel_pwm_bridge.py
│   ├── ggbot_control/safety_mux.py
│   ├── config/drive_limits.yaml
│   └── test/
└── ggbot_mission/
    ├── ggbot_mission/task_executor.py
    ├── config/mission_schema.json
    └── test/
```

现有工作区文件的处理原则：

- 修改 [esp32_pwm_gesture.ino](../../../esp32_pwm_gesture.ino)：保留手势接管逻辑，增加行协议、`STOP`、`STATUS`、500 ms 看门狗；不要把 ROS2 代码塞进 ESP32 固件。
- 更新 [ESP32.md](../../../ESP32.md)：把实际固件协议和限制写成唯一操作说明；删除或标明与当前源码不一致的旧代码段。
- 保留 [PWM切换组合参数使用说明.md](../../../PWM切换组合参数使用说明.md) 作为 hoverboard 手势和电气安全依据。
- [stm32_pwm_src/main.cpp](../../../stm32_pwm_src/main.cpp) 仅作为备用 STM32 PWM 方案，除非实物确认使用 STM32 PA0/PA1，否则不进入主链路。
- [esp32_pwm_controller.py](../../../esp32_pwm_controller.py) 改为与统一后的 ESP32 行协议一致，并作为桥接节点的串口发送逻辑参考；不直接从 RTK 节点调用它。

---

## 四、分阶段实施任务

### Task 0：冻结硬件拓扑和安全边界

**Files:**
- Modify: `docs/superpowers/plans/2026-08-12-ggbot-sensors-motion-integration.md`
- Record: `docs/hardware-inventory.md`

**Interfaces:**
- Consumes: 现有项目文档、PPT、实物和线束标签。
- Produces: 一份带照片/型号/接口/电压/坐标轴的硬件清单，决定主底盘链路是 ESP32 PWM 还是可反馈的 UART/CAN。

- [ ] **Step 1: 记录五类硬件的实际型号**

```text
RTK：模块型号、USB/UART、波特率、是否单天线/双天线、NMEA字段
雷达：型号、USB/串口/网口、供电、扫描频率、ROS驱动
相机：USB/V4L2/RealSense/网络、分辨率、帧率、是否深度
底盘：hoverboard控制器型号、CH1/CH2含义、是否能读编码器
主机链路：NUC USB串口设备、ESP32板型、供电降压、共地位置
```

- [ ] **Step 2: 在断电状态画出接线图并标记危险电源线**

```text
ESP32 GPIO25 -> 控制器 CH1 信号
ESP32 GPIO26 -> 控制器 CH2 信号
ESP32 GND    -> 控制器 GND
控制器 12/15 V/VCC 不得接 ESP32 3V3、5V、VIN
```

- [ ] **Step 3: 建立现场停止规则**

在任何自主命令测试前，必须同时具备物理急停、轮子架空或空旷区域、低速上限、串口/ROS 看门狗和一名观察员。未满足任一项，测试结果只能算“软件仿真通过”。

- [ ] **Step 4: 形成接口决策**

若能从底盘获得编码器和状态回传，优先使用 UART/CAN 直接控制并发布 `/odom`；若只能使用当前两路 RC PWM，则先完成位置展示和低速航点演示，把 `/odom` 标为不可用，禁止声称已完成高精度闭环巡航。

**验证：** `docs/hardware-inventory.md` 中每个设备均有实际值；电机和水泵仍处于断电状态。

---

### Task 1：统一 ESP32 PWM 控制协议并验证停车

**Files:**
- Modify: `esp32_pwm_gesture.ino`
- Modify: `esp32_pwm_controller.py`
- Modify: `ESP32.md`
- Test: `tests/test_pwm_mapping.py`

**Interfaces:**
- Consumes: 当前 `g` 手势接管和 GPIO25/26 PWM 输出。
- Produces: `g`（接管）、`S <speed> <steering>`（持续运动）、`STOP`（回中）、`STATUS`（状态）四个稳定命令；运动范围 `[-1000,1000]`。

- [ ] **Step 1: 先为主机映射写失败测试**

```python
# tests/test_pwm_mapping.py
from ggbot_control.cmd_vel_pwm_bridge import twist_to_pwm

def test_forward_maps_to_speed_channel():
    assert twist_to_pwm(0.10, 0.0, 0.20, 0.60) == (500, 0)

def test_limits_are_applied():
    assert twist_to_pwm(1.0, -1.0, 0.20, 0.60) == (1000, -1000)

def test_zero_is_center():
    assert twist_to_pwm(0.0, 0.0, 0.20, 0.60) == (0, 0)
```

- [ ] **Step 2: 运行失败测试**

```bash
cd ~/campuscar_ws
colcon test --packages-select ggbot_control --event-handlers console_direct+
```

预期：因 `twist_to_pwm` 尚未存在而失败；不要在失败前接电机。

- [ ] **Step 3: 在 ESP32 中加入行协议和看门狗**

保留 `startGesture()` 的原有时序；将串口处理扩展为下面的状态逻辑，所有错误都先回中：

```cpp
constexpr uint32_t COMMAND_TIMEOUT_MS = 500;
uint32_t lastMotionCommandMs = 0;

void neutralAndReport(const char *reason) {
  setCenter();
  Serial.print("STOP ");
  Serial.println(reason);
}

void handleLine(String line) {
  line.trim();
  if (line == "g" || line == "G") {
    startGesture();
    return;
  }
  if (line == "STOP" || line == "x" || line == "0") {
    cancelGestureAndStop();
    lastMotionCommandMs = millis();
    Serial.println("OK STOP");
    return;
  }
  if (line == "STATUS") {
    Serial.printf("STATE GESTURE=%d SPEED=%d STEERING=%d AGE_MS=%lu\\n",
                  gestureRunning ? 1 : 0, 0, 0,
                  millis() - lastMotionCommandMs);
    return;
  }
  if (!line.startsWith("S ")) {
    neutralAndReport("BAD_FORMAT");
    return;
  }

  int first = line.indexOf(' ');
  int second = line.indexOf(' ', first + 1);
  if (first < 0 || second < 0) {
    neutralAndReport("BAD_FORMAT");
    return;
  }
  int speed = line.substring(first + 1, second).toInt();
  int steering = line.substring(second + 1).toInt();
  if (speed < -1000 || speed > 1000 || steering < -1000 || steering > 1000) {
    neutralAndReport("OUT_OF_RANGE");
    return;
  }
  if (gestureRunning) {
    neutralAndReport("GESTURE_RUNNING");
    return;
  }
  setDrive(steering, speed);
  lastMotionCommandMs = millis();
  Serial.printf("OK SPEED=%d STEERING=%d\\n", speed, steering);
}
```

在 `loop()` 中逐行读取并增加：

```cpp
while (Serial.available() > 0) {
  String line = Serial.readStringUntil('\n');
  handleLine(line);
}
if (!gestureRunning && millis() - lastMotionCommandMs > COMMAND_TIMEOUT_MS) {
  setCenter();
}
```

实际合入时应将 `toInt()` 替换为严格整数解析，拒绝 `S abc 1`、多余字段和空字段；测试先用小范围命令。

- [ ] **Step 4: 给主机端增加纯函数**

```python
# ggbot_control/cmd_vel_pwm_bridge.py

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

def twist_to_pwm(linear_x: float, angular_z: float,
                 max_linear: float, max_angular: float,
                 steering_sign: int = 1) -> tuple[int, int]:
    if max_linear <= 0 or max_angular <= 0:
        raise ValueError("速度上限必须为正数")
    speed = round(clamp(linear_x / max_linear, -1.0, 1.0) * 1000)
    steering = round(
        clamp(angular_z / max_angular, -1.0, 1.0) * 1000 * steering_sign
    )
    return speed, steering
```

- [ ] **Step 5: 先不接底盘，验证串口行为**

```bash
python3 esp32_pwm_controller.py --list-ports
python3 esp32_pwm_controller.py --port /dev/cu.usbserial-XXX --status
python3 esp32_pwm_controller.py --port /dev/cu.usbserial-XXX --stop
```

验收：上电只输出中位；`STATUS` 有状态；错误命令不会改变 PWM；单次 `S 100 0` 后 500 ms 内回中；连续 20 Hz 命令不会超时。

- [ ] **Step 6: 架空轮测试**

依次使用 `S 100 0`、`S -100 0`、`S 0 100`、`S 0 -100`，每次不超过 2 s，确认方向后再调整 `STEERING_SIGN` 或固件反向配置。出现方向不明、异常鸣叫、通道接反时先断电。

**重要阻塞条件：** 若 hoverboard 固件在两路中位 10 s 后自动切回 USART，Task 1 不能宣称长期 PWM 控制完成。此时优先增加 hoverboard 固件的显式 PWM 模式保持/退出机制；不能修改时只能把任务设计为短动作段，并在重新执行手势后恢复，不能发送非中位保活脉冲。

---

### Task 2：建立 ROS2 传感器启动和观测基线

**Files:**
- Create: `~/campuscar_ws/src/ggbot_bringup/launch/sensors.launch.py`
- Create: `~/campuscar_ws/src/ggbot_bringup/config/sensors.yaml`
- Create: `docs/sensor-baseline.md`

**Interfaces:**
- Consumes: Task 0 的实际型号。
- Produces: `/fix`、`/scan`、相机图像和 `camera_info` 的真实话题名、频率、frame_id 记录。

- [ ] **Step 1: 枚举设备和现有 ROS 话题**

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true
v4l2-ctl --list-devices 2>/dev/null || true
ros2 topic list -t
ros2 node list
```

- [ ] **Step 2: 只启动 RTK 驱动并验证原始 NMEA**

```bash
sudo usermod -aG dialout "$USER"
# 重新登录后执行
screen /dev/ttyACM0 115200
```

验收：能看到 `$GNGGA`/`$GNRMC`；记录真实串口和波特率。退出 `screen` 后再启动 `nmea_navsat_driver`，不要让两个程序同时占用串口。

- [ ] **Step 3: 验证 `/fix`**

```bash
ros2 launch nmea_navsat_driver nmea_serial_driver.launch.py \
  port:=/dev/ttyACM0 baud:=115200
ros2 topic echo /fix --once
ros2 topic hz /fix
```

验收：室外有效定位时 `latitude/longitude` 为有限数，`status.status >= 0`，频率达到设备输出频率；室内或无卫星时必须明确显示 NO_FIX，不得把 NaN 当作坐标。

- [ ] **Step 4: 安装并验证雷达驱动**

按 Task 0 的型号只选一个对应驱动。启动后执行：

```bash
ros2 topic type /scan
ros2 topic echo /scan --once
ros2 topic hz /scan
rviz2
```

验收：类型为 `sensor_msgs/msg/LaserScan`，范围值在驱动声明的 `range_min/range_max` 内，`header.frame_id` 稳定，频率稳定；RViz2 中旋转雷达时点云/扫描方向正确。

- [ ] **Step 5: 安装并验证相机驱动**

USB 相机先用 V4L2 驱动；深度相机用厂商 ROS2 驱动。验证命令：

```bash
v4l2-ctl --device=/dev/video0 --list-formats-ext 2>/dev/null || true
ros2 topic list -t | grep -E 'image|camera_info|depth'
ros2 topic hz /camera/image_raw
ros2 run rqt_image_view rqt_image_view
```

验收：图像连续、无明显断帧，时间戳递增，能找到对应 `CameraInfo`；记录分辨率、帧率、曝光和实际 frame_id。

- [ ] **Step 6: 保存基线**

`docs/sensor-baseline.md` 必须包含设备型号、启动命令、话题、类型、频率、frame_id、供电和一份成功/失败样例。没有真实型号时，记录“未接入”并停止后续驱动安装，不写猜测值。

---

### Task 3：RTK 原点、ENU 坐标和定位健康状态

**Files:**
- Create: `ggbot_localization/rtk_localizer.py`
- Create: `ggbot_localization/origin_manager.py`
- Create: `ggbot_localization/config/localization.yaml`
- Test: `ggbot_localization/test/test_rtk_localizer.py`

**Interfaces:**
- Consumes: `/fix`，可选 `/imu/data` 和 `/odom`。
- Produces: `/pose_est` (`PoseStamped`, `frame_id=map`)、`/localization_state`、`/R2UTopic_Pos`；无效定位时保留状态但不发布可用运动位姿。

- [ ] **Step 1: 写坐标转换单元测试**

```python
def test_origin_maps_to_zero():
    assert latlon_to_enu(22.0, 113.0, 22.0, 113.0) == (0.0, 0.0)

def test_one_degree_delta_has_expected_sign():
    east, north = latlon_to_enu(22.0 + 1e-5, 113.0 + 1e-5, 22.0, 113.0)
    assert east > 0
    assert north > 0

def test_invalid_fix_is_rejected():
    assert is_usable_fix(status=-1, latitude=float('nan'), longitude=113.0) is False
```

- [ ] **Step 2: 运行失败测试**

```bash
cd ~/campuscar_ws
colcon test --packages-select ggbot_localization --event-handlers console_direct+
```

预期：转换函数和状态函数尚未实现时失败。

- [ ] **Step 3: 实现原点采集**

原点采集器在车静止、空旷室外、RTK 固定后收集 60 个有效样本，用中位数而不是单帧值写入 YAML：

```yaml
origin:
  latitude: 22.00000000
  longitude: 113.00000000
  altitude: 10.0
  map_rotation_rad: 0.0
  source: averaged_60_samples
```

实际采集命令应输出样本数、状态、标准差和最终原点；若有效样本不足 60 或水平散布超过 0.5 m，退出并要求重新选点。

- [ ] **Step 4: 实现定位健康门**

每帧 `/fix` 至少检查：状态可用、经纬度有限、时间戳未过期、与上一帧的跳变不超过配置阈值。状态枚举固定为 `FIX`、`FLOAT`、`NO_FIX`、`STALE`、`JUMP`；无法从驱动区分 FIX/FLOAT 时，原始 `status.status` 和 `position_covariance` 必须进入日志，不能伪造 RTK_FIXED。

- [ ] **Step 5: 实现位姿发布**

位置使用 ENU 转 `map.x/y`；姿态不要从单天线静态经纬度推断。优先顺序：双天线 heading > IMU/odom 融合 yaw > 受控直行初始化 yaw。没有有效 yaw 时发布状态但让控制层进入 `WAITING_HEADING`。

- [ ] **Step 6: 运行现场验证**

```bash
ros2 param set /rtk_localizer origin_file ~/campuscar_ws/src/ggbot_localization/config/origin.yaml
ros2 topic echo /pose_est
ros2 topic echo /localization_state
ros2 topic echo /R2UTopic_Pos
ros2 topic hz /R2UTopic_Pos
```

验收：车静止时局部坐标在小范围内变化；断开 RTK 后状态转为 `NO_FIX`/`STALE`，不会继续输出“可用 FIX”；JSON 必须包含 `status/status_name/latitude/longitude/altitude/timestamp/frame_id`，扩展字段不改变基础字段类型。

---

### Task 4：确认航向来源并完成 IMU/里程计基线

**Files:**
- Create: `ggbot_localization/yaw_initializer.py`
- Create: `ggbot_localization/pose_fusion.py`
- Test: `ggbot_localization/test/test_yaw_initializer.py`
- Record: `docs/odom-calibration.md`

**Interfaces:**
- Consumes: `/imu/data`、`/odom`，以及受控移动期间的连续 `/fix`。
- Produces: `yaw` 和 `/pose_est` 使用的统一 ROS 弧度制航向。

- [ ] **Step 1: 判定实际航向方案**

```text
双天线 RTK 有 heading：直接读取厂商 heading，记录天线基线方向。
单天线 RTK + IMU：启动时由 IMU/里程计给 yaw，RTK 只修正位置。
单天线 RTK 无 IMU：先人工设定车头方向，禁止自主航点；只能做位置展示。
单天线 RTK + 受控直行：直行至少 1.5 m 后用连续 RTK 点估算初始方向。
```

- [ ] **Step 2: 写直行航向测试**

```python
def heading_from_displacement(east_delta, north_delta):
    if east_delta * east_delta + north_delta * north_delta < 1.5 * 1.5:
        raise ValueError("直行距离不足 1.5 m，不能初始化航向")
    return math.atan2(north_delta, east_delta)
```

- [ ] **Step 3: 标定里程计**

推行 1 m、原地转 90°，分别记录 `/odom.pose.pose`；通过轮径、轮距、编码器每圈脉冲修正参数。若当前 PWM 控制器没有编码器回传，明确记录 `/odom unavailable`，不要用命令值代替反馈。

- [ ] **Step 4: 运行静止和移动检查**

```bash
ros2 topic hz /imu/data
ros2 topic hz /odom
ros2 topic echo /odom --once
ros2 run tf2_tools view_frames
```

验收：IMU/里程计时间戳递增、frame_id 与 TF 一致；静止时 yaw 不快速发散；没有真实反馈时，控制器默认状态为 `DEGRADED_POSITION_ONLY`，速度上限锁定为 0.10 m/s。

---

### Task 5：实现航点跟踪器，只输出意图速度

**Files:**
- Create: `ggbot_control/waypoint_follower.py`
- Create: `ggbot_control/config/drive_limits.yaml`
- Test: `ggbot_control/test/test_waypoint_follower.py`

**Interfaces:**
- Consumes: `/pose_est`、`/target_path`。
- Produces: `/cmd_vel_path`，并发布当前航点、到达事件和跟踪状态；不直接打开串口。

- [ ] **Step 1: 写核心控制测试**

```python
def test_target_ahead_drives_forward():
    v, omega = controller.compute_pose(0.0, 0.0, 0.0, 1.0, 0.0)
    assert v > 0.0
    assert abs(omega) < 1e-9

def test_target_behind_stops_and_turns():
    v, omega = controller.compute_pose(0.0, 0.0, 0.0, -1.0, 0.0)
    assert v == 0.0
    assert omega != 0.0

def test_waypoint_reached_within_20_cm():
    assert controller.reached(0.0, 0.0, 0.19) is True
    assert controller.reached(0.0, 0.0, 0.21) is False
```

- [ ] **Step 2: 实现最小 P 控制**

```python
distance = math.hypot(dx, dy)
desired_yaw = math.atan2(dy, dx)
heading_error = wrap_pi(desired_yaw - yaw)
omega = clamp(1.5 * heading_error, -omega_max, omega_max)
v = 0.0 if abs(heading_error) > math.radians(60) else clamp(
    0.8 * distance * math.cos(heading_error), 0.0, v_max
)
```

先用 P 控制，不在没有实测数据时加入积分项；只有出现稳定、可复现的系统性横向偏差，才增加 D 或积分，并给积分限幅。

- [ ] **Step 3: 加入新鲜度和定位状态门**

当 `/pose_est` 年龄超过 1 s、状态不是 `FIX/FLOAT_ALLOWED`、航向无效、路径为空或任务被取消时，发布零 `Twist`。`FLOAT_ALLOWED` 只能在低速模式开启时使用，默认关闭。

- [ ] **Step 4: 定义路径消息和航点动作**

内部统一使用 `nav_msgs/Path`，每个 Pose 在 `map` 坐标中；速度和 `spray` 动作放在任务执行器的并行元数据中。旧 JSON 的 `path[].x/y/yaw/speed/action` 在入口处转换为同一内部结构，不让跟踪器解析 JSON。

- [ ] **Step 5: 仿真验证**

用 `ros2 topic pub` 发布矩形 `nav_msgs/Path` 和假位姿，验证直线、转角、到点切换；全程不连接 ESP32、不发布真实电机命令。验收：目标在前方时 `linear.x>0`，大角度偏差时 `linear.x=0`，航点 0.2 m 内切换。

---

### Task 6：实现雷达安全层和统一速度仲裁

**Files:**
- Create: `ggbot_perception/lidar_safety.py`
- Create: `ggbot_control/safety_mux.py`
- Create: `ggbot_perception/config/perception.yaml`
- Test: `ggbot_perception/test/test_lidar_safety.py`
- Test: `ggbot_control/test/test_safety_mux.py`

**Interfaces:**
- Consumes: `/scan`、`/cmd_vel_path`、定位状态、急停状态。
- Produces: 唯一最终 `/cmd_vel`；任何安全条件不满足时为零。

- [ ] **Step 1: 写障碍测试**

```python
def test_front_obstacle_forces_stop():
    scan = make_scan(front_distance=0.35)
    assert limiter.output(scan, requested_v=0.15).linear.x == 0.0

def test_clear_scan_preserves_limited_command():
    scan = make_scan(front_distance=3.0)
    assert limiter.output(scan, requested_v=0.10).linear.x == 0.10

def test_stale_scan_stops():
    assert limiter.scan_is_safe(age_s=1.1) is False
```

- [ ] **Step 2: 实现前方扇区和动态停止距离**

扫描前方约 ±30° 的有效量程，初期使用保守停止距离；公式为：

```python
stop_distance = (speed * speed) / (2.0 * braking_accel) + safety_margin
```

无有效扫描、雷达时间戳过期、距离小于停止距离或急停触发时，输出零；不在第一版自动绕障，先实现“检测即停”。

- [ ] **Step 3: 实现唯一命令出口**

`SafetyMux` 的优先级固定为：物理急停 > 软件急停 > 控制器/串口故障 > 定位无效 > 雷达不安全 > 路径跟踪命令。只有所有门通过才转发 `/cmd_vel_path`。任何节点不得直接向最终 `/cmd_vel` 发布。

- [ ] **Step 4: 录包回放验证**

```bash
ros2 bag record /scan /cmd_vel_path /cmd_vel /localization_state
ros2 bag play <bag_dir> --clock
```

验收：回放中插入近距离障碍或删除 `/scan` 后，最终 `/cmd_vel` 在一个控制周期内归零；路径节点仍可继续运行，不绕过安全层。

---

### Task 7：把 ROS2 `Twist` 接入 ESP32，并解决 PWM 模式生命周期

**Files:**
- Create: `ggbot_control/cmd_vel_pwm_bridge.py`
- Modify: `esp32_pwm_gesture.ino`
- Modify: `esp32_pwm_controller.py`
- Test: `ggbot_control/test/test_cmd_vel_pwm_bridge.py`

**Interfaces:**
- Consumes: 最终 `/cmd_vel`、软件急停、串口参数。
- Produces: ESP32 的 `S <speed> <steering>\n`，固定 20 Hz；串口断开或输入过期时发送 `STOP` 并发布故障状态。

- [ ] **Step 1: 写映射和看门狗测试**

```python
def test_bridge_sends_twenty_hz_commands():
    bridge = FakeBridge(rate_hz=20.0)
    bridge.on_twist(0.05, 0.0)
    bridge.tick_for(0.20)
    assert 3 <= len(bridge.sent_lines) <= 5
    assert all(line.startswith("S ") for line in bridge.sent_lines)

def test_stale_twist_sends_stop():
    bridge = FakeBridge(rate_hz=20.0)
    bridge.tick_at(0.0, 0.10, 0.0)
    bridge.tick_at(0.60, None, None)
    assert bridge.sent_lines[-1] == "STOP"
```

- [ ] **Step 2: 实现桥接节点核心循环**

```python
class CmdVelPwmBridge(Node):
    def __init__(self):
        super().__init__("cmd_vel_pwm_bridge")
        self.last_twist = Twist()
        self.last_twist_time = self.get_clock().now()
        self.serial = serial.Serial(self.get_parameter("port").value, 115200,
                                     timeout=0, write_timeout=0.2)
        self.create_subscription(Twist, "/cmd_vel", self.on_twist, 10)
        self.create_timer(0.05, self.send_cycle)

    def on_twist(self, msg):
        self.last_twist = msg
        self.last_twist_time = self.get_clock().now()

    def send_cycle(self):
        age = (self.get_clock().now() - self.last_twist_time).nanoseconds / 1e9
        if age > 0.5:
            self.serial.write(b"STOP\n")
            return
        speed, steering = twist_to_pwm(
            self.last_twist.linear.x, self.last_twist.angular.z,
            self.get_parameter("max_linear_mps").value,
            self.get_parameter("max_angular_rps").value,
            self.get_parameter("steering_sign").value,
        )
        self.serial.write(f"S {speed} {steering}\n".encode("ascii"))
```

生产代码还必须捕获串口异常、关闭时发送多次 `STOP`、限制参数、发布 `/motor_state`，并保证发送线程不被日志读取阻塞。

- [ ] **Step 3: 设计接管流程**

启动顺序固定为：ESP32 输出中位 → 主机发送 `g` → 读取接管成功回执/超时 → 再开始发送 `S 0 0` → 收到第一个安全 `Twist` 后才允许非零速度。启动失败时保持 `STOP`，不自动重试无限次。

- [ ] **Step 4: 处理 hoverboard 的 10 s 中位切回限制**

做一个明确的二选一验收：

```text
方案 A（推荐）：修改 hoverboard 固件，使 PWM 模式有显式 ENABLE/KEEPALIVE 状态，
且 keepalive 不改变电机目标；增加独立 deadman 和 500 ms 信号丢失归零。

方案 B（临时演示）：任务不允许中位停留超过 10 s；每次重新运动前重新执行手势，
并在重新接管前保持车体静止。不得用非中位脉冲维持模式。
```

在方案 A 未通过前，状态反馈必须标记 `PWM_MODE_LIMITED`，不能承诺长时间喷洒停留或完整返航。

- [ ] **Step 5: 架空轮、系留、低速三层测试**

```text
层 1：ROS2 发布恒定 Twist，观察 ESP32 串口行协议和 PWM 脉宽。
层 2：车轮架空，speed=±100，确认前后和转向方向。
层 3：空旷场地系留，v<=0.10 m/s，验证 STOP、断串口、定位失效均停车。
```

验收：任何单点故障都不会留下持续运动命令；`/cmd_vel` 为零时 ESP32 两路为 1500 µs。

---

### Task 8：任务执行器、RTK 航点和喷水动作

**Files:**
- Create: `ggbot_mission/task_executor.py`
- Create: `ggbot_mission/config/mission_schema.json`
- Test: `ggbot_mission/test/test_task_executor.py`
- Modify: `GGBot_从零到UE互通_完整清单.md`

**Interfaces:**
- Consumes: `/U2RTopic_Command` (`std_msgs/String` JSON)、`/pose_est`、定位和安全状态。
- Produces: `/target_path`、`/spray_cmd`、`/R2UTopic_Status`。

- [ ] **Step 1: 固定任务 JSON 合同**

```json
{
  "task_id": "zone_a_001",
  "action": "start_mission",
  "frame_id": "map",
  "require_fix": true,
  "path": [
    {"x": 0.0, "y": 0.0, "yaw": 0.0, "speed": 0.10, "action": "none"},
    {"x": 3.0, "y": 0.0, "yaw": 0.0, "speed": 0.10, "action": "spray"},
    {"x": 3.0, "y": 2.0, "yaw": 1.57, "speed": 0.08, "action": "stop_spray"}
  ]
}
```

入口必须拒绝缺少 `task_id`、空路径、非有限坐标、未知动作和 `frame_id != map` 的任务；兼容旧字段 `speed`，内部统一转换为 `speed_mps`。

- [ ] **Step 2: 写状态机测试**

```python
def test_start_mission_without_fix_waits_and_stays_stopped():
    executor.receive(start_command)
    executor.update_localization("NO_FIX")
    assert executor.state == "WAITING_FIX"
    assert executor.last_twist.linear.x == 0.0

def test_fix_releases_valid_path():
    executor.receive(start_command)
    executor.update_localization("FIX")
    assert executor.state == "MOVING"

def test_cancel_publishes_stop_and_idle():
    executor.cancel()
    assert executor.state == "IDLE"
    assert executor.last_spray is False
```

- [ ] **Step 3: 实现状态转换**

```text
IDLE -> WAITING_FIX -> MOVING -> ACTION_PAUSE -> PATH_DONE -> RETURNING -> IDLE
任何状态 + cancel/emergency -> IDLE + 零速度 + 停喷
任何状态 + NO_FIX/STALE -> WAITING_FIX + 零速度 + 停喷
```

- [ ] **Step 4: 用固定 map 航点测试**

先用 3 m × 2 m 的矩形，不使用校园真实经纬度；验证每个航点到达、喷雾开关和状态 JSON 后再导入实测原点。

- [ ] **Step 5: 明确返航策略**

第一版返航只回到任务开始时保存的 `home` 航点或路径逆序列表；返航同样必须经过定位健康门、雷达安全层和最终 `/cmd_vel`，不能由任务执行器直接串口发电机命令。

---

### Task 9：UE/rosbridge 位置显示和异常合同

**Files:**
- Modify: `u2r_r2u_bridge` source on NUC
- Create: `docs/ue-position-contract.md`
- Modify: `GGBot_从零到UE互通_完整清单.md`

**Interfaces:**
- Consumes: `/fix`、`/pose_est`、任务状态。
- Produces: `/R2UTopic_Pos` 和 `/R2UTopic_Status`，供 UE5 通过 rosbridge 9090 订阅。

- [ ] **Step 1: 固定基础 JSON**

```json
{
  "status": 0,
  "status_name": "FIX",
  "latitude": 22.0,
  "longitude": 113.0,
  "altitude": 10.0,
  "timestamp": 1710000000.0,
  "frame_id": "gps",
  "vehicle": {"x": 1.2, "y": 0.4, "yaw": 0.1}
}
```

基础字段类型和名称不可随意改动；`vehicle` 为扩展字段。状态至少包括 `FIX`、`NO_FIX`、`STALE`、`JSON_ERROR`、`NETWORK_LOST`。

- [ ] **Step 2: 分层验证**

```bash
ros2 topic echo /fix --once
ros2 topic echo /R2UTopic_Pos --once
ros2 topic hz /R2UTopic_Pos
ss -tlnp | grep 9090
```

再由 UE 连接 `ws://<NUC_IP>:9090`，先打印原始 `String.data`，再解析，再应用 origin/scale/axis/rotation。UE 不直接把经纬度当世界坐标。

- [ ] **Step 3: 做四个坐标标定动作**

在地面标记原点、向东约 3 m、向北约 3 m、车头转 90°；记录 RTK、`vehicle.x/y/yaw` 和 UE X/Y/朝向，确定轴向、比例、旋转补偿和模型前向轴。每项参数写入 `docs/ue-position-contract.md`。

---

### Task 10：相机作为独立可验证链路接入

**Files:**
- Create: `ggbot_perception/camera_health.py`
- Create: `ggbot_perception/config/camera.yaml`
- Test: `ggbot_perception/test/test_camera_health.py`
- Record: `docs/camera-calibration.md`

**Interfaces:**
- Consumes: 相机厂商/USB 驱动话题。
- Produces: 图像健康状态、可选压缩图像记录；第一版不参与电机控制。

- [ ] **Step 1: 固定相机安装和 frame**

记录安装高度、俯仰角、车体坐标中的位置，安装支架必须可调、抗震且不遮挡 RTK/雷达/急停。发布静态 TF：`base_link -> camera_link -> camera_optical_frame`。

- [ ] **Step 2: 验证图像和标定**

```bash
ros2 topic hz /camera/image_raw
ros2 topic echo /camera/camera_info --once
ros2 run camera_calibration cameracalibrator --size 8x6 --square 0.025 \
  --ros-args -r image:=/camera/image_raw -r camera:=/camera
```

将得到的内参和畸变参数保存到 `camera.yaml`；验收图像时间戳、CameraInfo 和 TF 都稳定。

- [ ] **Step 3: 实现健康节点**

```python
if image_age_s > 1.0 or frame_count == 0:
    state = "CAMERA_STALE"
elif width < min_width or height < min_height:
    state = "CAMERA_BAD_FORMAT"
else:
    state = "CAMERA_OK"
```

相机异常只在第一版上报状态并记录，不直接让相机线程阻塞 `/cmd_vel`；若后续把相机用于避障，必须像雷达一样经过独立安全测试。

---

### Task 11：全链路联调、故障注入和验收

**Files:**
- Create: `docs/field-test-checklist.md`
- Create: `config/zone_a.yaml`, `config/zone_b.yaml`, `config/zone_c.yaml`
- Create: `scripts/start_ggbot_demo.sh`

**Interfaces:**
- Consumes: 所有前置任务的稳定话题和参数。
- Produces: 可复现的现场启动顺序、rosbag、故障记录和验收结论。

- [ ] **Step 1: 静止联调**

```text
1. 检查急停和主电源断开手段。
2. 启动 RTK，确认 /fix 有效。
3. 锁定并记录 origin.yaml。
4. 启动 IMU/odom、雷达、相机，确认 frame 和频率。
5. 启动 rosbridge 和 /R2UTopic_Pos。
6. UE 打印并解析 JSON，确认静止坐标合理。
7. 启动安全层和 PWM bridge，但保持最终 /cmd_vel=0。
```

- [ ] **Step 2: 手动/推行移动 3–5 m**

先不让算法驱动电机，推车或人工控制，观察 `/fix`、`/pose_est`、`/R2UTopic_Pos` 和 UE 模型是否同向变化；完成坐标轴和 heading 校准。

- [ ] **Step 3: 架空轮闭环**

发布单个前方航点，设置 `max_linear_mps=0.05`，观察 `path_follower`、`safety_mux`、串口 `S ...` 和 PWM。测试急停、删掉 `/fix`、停止 `/scan`、拔掉串口，均应归零。

- [ ] **Step 4: 系留低速场地测试**

顺序为直线 1 m、原地转、2 m 单航点、3 点折线；每次只改一个参数。记录实际轨迹、RTK 状态、控制指令和停车原因。

- [ ] **Step 5: 雷达检测即停**

在空旷场地用软障碍物从远到近进入前方扇区，验收最终 `/cmd_vel` 先归零，随后才考虑绕障；不在人群旁测试。

- [ ] **Step 6: 喷水动作联调**

只有底盘停车、定位、雷达和急停都通过后，才接入 `/spray_cmd`。喷雾节点必须具备：无定位停喷、急停停喷、任务结束停喷、通信超时停喷；药剂需先获得学校/后勤批准并做植物安全评估。

- [ ] **Step 7: 录制证据并验收**

```bash
ros2 bag record /fix /imu/data /odom /scan /pose_est \
  /cmd_vel_path /cmd_vel /R2UTopic_Pos /R2UTopic_Status
```

最终验收必须同时满足：有效 `/fix`、稳定位置 JSON、UE 可连接/解析/映射、真实移动同步、方向正确、NO_FIX/TIMEOUT 可见、急停和超时可靠停车。没有编码器反馈时，验收文案只能写“RTK 位置跟踪演示”，不能写“完整里程计闭环”。

---

## 五、推荐两周排期

| 时间 | 目标 | 通过条件 |
|---|---|---|
| 第 1 天 | 硬件盘点、接线和安全测试 | 型号/电压/端口/坐标轴全部记录 |
| 第 2 天 | ESP32 中位、手势、行协议 | `STOP`、超时、架空小行程通过 |
| 第 3 天 | RTK 原始 NMEA 与 `/fix` | 室外有效、频率稳定 |
| 第 4 天 | 原点采集、ENU、UE JSON | 静止局部坐标和 JSON 正确 |
| 第 5 天 | 雷达驱动、TF、检测即停 | RViz 正确，近障碍最终停车 |
| 第 6 天 | 相机驱动、CameraInfo、标定 | 图像连续、内参保存 |
| 第 7 天 | IMU/odom/航向初始化 | 航向来源明确，或明确降级 |
| 第 8 天 | 航点跟踪仿真 | 直线/矩形路径不接电机通过 |
| 第 9 天 | `/cmd_vel`→ESP32 闭环 | 20 Hz、限速、断链停车 |
| 第 10 天 | 架空轮全链路 | 任务状态、喷停、急停通过 |
| 第 11–12 天 | 系留低速场测 | 1 m/折线/雷达停通过 |
| 第 13 天 | UE 和三分区任务 | 坐标、朝向、异常显示通过 |
| 第 14 天 | 录包、复盘、演示脚本 | 可复现启动和验收证据完整 |

### 每天结束必须留下的证据

```text
启动命令、ros2 topic list -t、关键 topic hz、一次 rosbag、参数版本、
已知故障、下一次测试的唯一变量、物理急停测试结果。
```

---

## 六、第一天就要问清楚的 8 个问题

1. 实物 RTK 是单天线还是双天线？是否真的输出 RTK FIX 状态和 heading？
2. 雷达具体型号和接口是什么？目标话题是否确实为 `/scan`？
3. 相机是普通 USB、深度相机还是网络相机？目标是看前方还是看地面？
4. hoverboard 固件能否修改 10 s PWM 自动切回逻辑？
5. 当前底盘是否有编码器反馈？从哪里读取？
6. ESP32 当前烧录的是否就是 `esp32_pwm_gesture.ino`，还是 [ESP32.md](../../../ESP32.md) 中的另一版固件？
7. `CH1=转向、CH2=速度` 的正负方向是否已用示波器和架空轮实测？
8. UE 地图原点、轴向、比例、旋转角和模型前向轴由谁签字确认？

**完成标准：** 这 8 个问题有实测答案后，才开始 Task 3 之后的自主移动；否则最多做传感器静态数据验证。

## 自检结果

- **覆盖性：** RTK 串口/`/fix`/ENU/航向、雷达 `/scan`/TF/检测即停、相机图像/标定、ESP32 协议/看门狗、`Twist` 映射、状态机、UE JSON、故障注入和现场验收均有独立任务。
- **接口一致性：** 所有运动内部使用 `Twist`，最终唯一出口为 `/cmd_vel`；当前 PWM 协议与 PPT 二进制协议明确分离；路径消息统一为 `nav_msgs/Path`。
- **安全性：** 任何无效定位、过期传感器、雷达异常、串口断开或急停都归零；未解决的 10 s PWM 切回限制被设为明确的验收门，不用危险脉冲绕过。
- **已知限制：** 硬件型号和编码器能力尚未从文件中确认；计划将其作为 Task 0 的决策门，而不是编造驱动参数。
