# GreenGuardian Bot 算法组 & 运控组 完整技术方案

## 0. 项目背景

GreenGuardian Bot——校园绿植消杀灭蚊虫、浇灌一体机器人。5 人团队（机械 2 + 运控 2 + 算法 1），2 个月周期，基于学校提供的差速底盘和标准开发套件。

**核心功能**：预设路径巡航 → 无差别喷雾消杀 + 雾化浇灌 → 简易返航。

**当前状态**：NUC 已装 Ubuntu 22.04 + ROS2 Humble + nmea_navsat_driver + rosbridge_suite + u2r_r2u_bridge，WiFi SSH 可用。

---

## 1. 系统整体架构（基于 PPT 第二次培训）

### 1.1 数据全链路

```
┌─────────────────────────────────────────────────────────┐
│                      机器人侧 (NUC)                       │
│                                                         │
│  RTK ──→ nmea_navsat_driver ──→ /fix ──→ u2r_r2u_bridge│
│                                          │               │
│  IMU ───────────────────────→ /imu       │   JSON        │
│  Odom(运控组) ──────────────→ /odom      ↓               │
│  LiDAR ─────────────────────→ /scan   /R2UTopic_Pos     │
│  土壤湿度 ───────────────────→ /soil   (std_msgs/String) │
│                                          │               │
│  ┌─ 算法节点 ─────────────────────────┐  │               │
│  │ dead_reckoning  (航迹推演)         │  │               │
│  │ path_follower   (路径跟踪+PID)     │  │               │
│  │ obstacle_avoider (激光避障)        │  │               │
│  │ moisture_processor (湿度决策)      │  │               │
│  │ return_home     (路径回溯返航)     │  │               │
│  └────────────────────────────────────┘  │               │
│                    ↓                      │               │
│            /cmd_vel, /spray_cmd          rosbridge :9090 │
│                    ↓                      ↓               │
│              ┌── 运控组 ──┐         WebSocket            │
│              │ STM32/ESP32 │              │               │
│              │ 电机/水泵    │              │               │
│              └─────────────┘              │               │
└───────────────────────────────────────────┼───────────────┘
                                            ↓
┌───────────────────────────────────────────────────────────┐
│                    校园大脑 (远程 UE5)                      │
│                                                           │
│  rosbridge ← /R2UTopic_Pos (位置JSON) → 地图渲染           │
│  rosbridge → /U2RTopic_Command (任务指令) → 机器人          │
│                                                           │
│  职责：地图管理、路径规划、坐标转换、状态展示、异常告警        │
└───────────────────────────────────────────────────────────┘
```

### 1.2 关键设计原则（PPT 核心共识）

- **联调对象不是单个程序，是一套位置数据合同**
- **先看 /fix → 再看 /R2UTopic_Pos → 最后看 UE 显示**（逐层排查）
- **算法组**：RTK、/fix、JSON 格式、坐标转换
- **UE 工程师**：连接、解析、坐标映射、模型显示
- **运控组**：运动、速度、朝向、状态反馈
- **机械组**：天线位置、安装稳定、供电固定

---

## 2. 算法组任务（阮浩宇，1 人）

### 2.1 ROS2 节点清单

| # | 节点名 | 订阅 | 发布 | 功能 |
|---|--------|------|------|------|
| 1 | `dead_reckoning` | /imu, /odom | /pose_est | IMU+里程计航迹推演，融合输出当前位姿估计 |
| 2 | `path_follower` | /pose_est, /target_path | /cmd_vel | 接收目标路径点序列，PID 闭环跟踪 |
| 3 | `obstacle_avoider` | /scan | /cmd_vel_override | 激光雷达检测障碍 → 紧急停车/绕行 |
| 4 | `moisture_processor` | /soil_moisture | /spray_cmd | 湿度阈值判断 → 喷雾开关决策 |
| 5 | `task_executor` | /U2RTopic_Command, /pose_est | /target_path, /task_state | 解析校园大脑指令，编排路径点，管理任务状态机 |

### 2.2 核心算法逻辑

#### 2.2.1 航迹推演（dead_reckoning）

```
输入：/imu (角速度+加速度), /odom (轮速)
输出：/pose_est (x, y, yaw)

融合策略（互补滤波）：
- 短期：IMU 角速度积分 → yaw
- 长期：里程计位置增量 → x, y
- 权重：IMU 短期可靠，里程计长期可靠，互补消除漂移
```

#### 2.2.2 路径跟踪（path_follower）

```
输入：当前位置 (x,y,yaw)，目标路径点序列 [(x1,y1), (x2,y2)...]
输出：/cmd_vel (linear.x, angular.z)

PID 控制器：
- 横向偏差 → angular.z 修正（P 项为主）
- 距离偏差 → linear.x 调节（接近目标点减速）
- 到达阈值：0.2m 内视为到达当前路点，切换下一个
```

#### 2.2.3 任务状态机（task_executor）

```
状态转换：
IDLE → RECEIVED_PATH → MOVING → (避障触发) → OBSTACLE → (避障解除) → MOVING
                                  ↓
                             SPRAYING → (路径点带 action:spray)
                                  ↓
                            PATH_DONE → RETURNING → IDLE
    
异常处理：
- 定位丢失(status=-1) → WAITING，发送告警到 /task_state
- 电池低 → RETURNING，优先返航
- 超时(60s无指令) → IDLE，停车等待
```

### 2.3 预设路径数据格式

校园大脑下发 `/U2RTopic_Command`（std_msgs/String，内含 JSON）：

```json
{
  "task_id": "zone_a_20260620_001",
  "action": "start_mission",
  "path": [
    {"x": 0.0, "y": 0.0, "yaw": 0.0, "speed": 0.3, "action": "none"},
    {"x": 10.0, "y": 0.0, "yaw": 0.0, "speed": 0.5, "action": "spray"},
    {"x": 10.0, "y": 5.0, "yaw": 1.57, "speed": 0.3, "action": "spray"},
    {"x": 0.0, "y": 5.0, "yaw": 3.14, "speed": 0.5, "action": "spray"},
    {"x": 0.0, "y": 0.0, "yaw": 0.0, "speed": 0.3, "action": "stop_spray"}
  ]
}
```

机器人的执行反馈 `/R2UTopic_Status`（std_msgs/String，内含 JSON）：

```json
{
  "task_id": "zone_a_20260620_001",
  "state": "MOVING",
  "current_wp": 2,
  "progress": 0.4,
  "battery": 85.2,
  "error": null
}
```

### 2.4 坐标转换（PPT 关键）

```
RTK 经纬度(lat/lon) 
  → 参考原点(origin_lat, origin_lon，由算法组+UE工程师共同确定)
  → 局部 ENU 米制坐标(x_east, y_north)
  → UE 世界坐标(X_ue, Y_ue)（由 UE 工程师负责 scale + rotate + offset）
```

**必须与 UE 工程师提前确认**：
- 校园地图原点在哪里
- 经度对应 UE 的 X 还是 Y
- 是否需要旋转角度/偏航补偿
- 米制坐标到 UE 单位的比例
- 小车模型朝向如何定义
- 无定位时 UE 怎么显示

---

## 3. 运控组任务（钟艺丹、钱艺文，2 人）

### 3.1 底盘驱动层（PPT 框架）

| 序号 | 任务 | 详细说明 |
|------|------|----------|
| 1 | 串口通信 | NUC↔STM32/ESP32 的 UART/CAN，确认波特率和线序 |
| 2 | /cmd_vel 解析 | geometry_msgs/Twist → 差速解算 → left/right RPM |
| 3 | 控制帧发送 | 8字节协议帧，小端序，0xABCD帧头+XOR校验 |
| 4 | 反馈帧解析 | 18字节回传：左右轮速、电池电压(÷100)、温度(÷10)、LED |
| 5 | /odom 发布 | 编码器脉冲 → 轮速 → 航迹推演 → nav_msgs/Odometry |
| 6 | 外设控制 | PWM/MOS管：喷雾水泵、灯带、蜂鸣器、急停 |
| 7 | 安全保护 | 急停中断优先、cmd_vel超时停车(500ms)、限速 |

### 3.2 差速解算公式（PPT 简洁版）

```
left  = linear.x - k * angular.z      → 映射到 -1000 ~ 1000 PWM
right = linear.x + k * angular.z

直走：左右相等 | 转弯：产生差值 | 原地：左右相反
k = 轮距/2，具体值需实测标定
```

### 3.3 调试优先级（PPT 强调）

```
1. 串口回环 → 2. 发送停车帧(速度=0) → 3. 确认反馈帧解析正确
→ 4. 小速度直行 0.1m/s → 5. 小速度转向 → 6. 接入/cmd_vel
→ 7. 加超时与急停 → 8. 联调算法
```

**绝对不要**：没验证停车就测前进；没限速就联调；在人群旁测试。

---

## 4. 算法-运控-校园大脑 Topic 接口

### 4.1 算法 → 运控

| Topic | 类型 | 频率 | 说明 |
|-------|------|------|------|
| /cmd_vel | geometry_msgs/Twist | 20Hz | 速度指令 |
| /spray_cmd | std_msgs/Bool | 1Hz | True=喷雾开 |
| /led_cmd | std_msgs/Int32 | 1Hz | 0=待机, 1=作业, 2=故障 |

### 4.2 运控 → 算法

| Topic | 类型 | 频率 | 说明 |
|-------|------|------|------|
| /odom | nav_msgs/Odometry | 20Hz | 轮式里程计 |
| /battery | sensor_msgs/BatteryState | 1Hz | 电压+电量百分比 |
| /motor_state | std_msgs/String(JSON) | 5Hz | 左右轮速、温度、故障码 |

### 4.3 算法 ↔ 校园大脑（通过 rosbridge :9090）

| Topic | 方向 | 类型 | 说明 |
|-------|------|------|------|
| /R2UTopic_Pos | → UE | std_msgs/String | 位置 JSON（/fix 桥接） |
| /R2UTopic_Status | → UE | std_msgs/String | 任务状态 JSON |
| /U2RTopic_Command | ← UE | std_msgs/String | 任务指令 JSON（路径+动作） |

---

## 5. 硬件清单（待确认）

### 算法组依赖
- [ ] RTK/GPS 模块 — USB/UART，型号？波特率？NMEA 版本？
- [ ] IMU — I2C/SPI/UART，型号？数据频率？内置还是外置？
- [ ] 激光雷达 — 型号？360° or 单线？通信接口？
- [ ] 土壤湿度传感器 — 模拟量(AI)/数字量(RS485/I2C)？供电？

### 运控组依赖
- [ ] STM32/ESP32 控制板 — 型号？固件 SDK？（STM32CubeIDE/Arduino）
- [ ] 电机驱动器 — 型号？PWM/CAN 控制？
- [ ] 喷雾水泵 — 电压？MOS管驱动？流量？
- [ ] 电池组 — 电压？容量？（如 24V/10Ah）
- [ ] 急停按钮 — 常闭？接线到 STM32 还是物理断总电源？
- [ ] 线束 — 供电线、信号线、接口定义

**建议**：从机械组获取完整 BOM，尽快补齐上述信息。

---

## 6. 开发时间线（2 个月，8 周）

| 阶段 | 周次 | 算法组（阮浩宇） | 运控组（钟艺丹、钱艺文） |
|------|------|------------------|--------------------------|
| **P0 基础环境** | W1 | NUC 环境验证（✅已做完），RTK→/fix→/R2UTopic_Pos 链路打通 | 控制板串口通信确认，停车帧验证 |
| **P1 闭环巡航** | W2-3 | dead_reckoning + path_follower + task_executor 开发 | 差速解算 + 控制帧/反馈帧 + /odom 发布 |
| **P2 功能补齐** | W4-5 | 避障 + 湿度处理 + 返航逻辑 + UE 指令解析 | 外设控制（水泵/LED）+ 安全机制 + 限速 |
| **P3 联调测试** | W6-7 | 全链路：RTK→定位→路径→控制→UE，三个分区路径测试 | 底盘稳定性调试、异常处理、电池续航测试 |
| **P4 演示** | W8 | 三个校园分区完整作业演示 + UE 实时追踪 | 最终验收 + 文档 |

---

## 7. 开发路径建议

```
现阶段 ──→ 先写 task_executor（状态机骨架）
         → 再写 dead_reckoning（无传感器时用假数据）
         → 再写 path_follower（纯仿真循线）
         → 拿到硬件后联调 IMU/里程计
         → 最后加避障和湿度
```

**优先做**：状态机 + 预设路径跟踪 → 这两个有了就能跑基础巡航，其他是增强功能。
