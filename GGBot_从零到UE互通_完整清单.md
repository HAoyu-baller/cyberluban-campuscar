# GreenGuardian Bot 从零到 UE 互通 —— 完整执行清单

## 目标

NUC 从裸机开始，到通过 PPT 幻灯片 27 的联调验收标准：
- 室外 RTK 发布有效 /fix
- /R2UTopic_Pos 稳定输出 JSON
- UE5 能连上 rosbridge :9090
- UE5 能打印/解析/映射收到的位置 JSON
- 真实小车移动时 UE 模型位置同步变化
- 定位异常时 UE 有异常状态显示

---

## 阶段 0：NUC 基础环境（✅ 已完成）

| 任务 | 状态 | 负责人 |
|------|------|--------|
| Ubuntu 22.04 安装 | ✅ | 阮浩宇 |
| 有线网卡驱动 (RTL8125 r8125 9.016) | ✅ | 阮浩宇 |
| Mac↔NUC 有线直连 (192.168.10.x) | ✅ | 阮浩宇 |
| ROS2 Humble Desktop 安装 | ✅ | 阮浩宇 |
| nmea_navsat_driver 安装 | ✅ | 阮浩宇 |
| rosbridge_suite 安装 | ✅ | 阮浩宇 |
| u2r_r2u_bridge 编译 | ✅ | 阮浩宇 |

---

## 阶段 1：RTK → ROS2 → /fix 打通（第 1 优先级）

这是全链路的第一环，也是最容易卡住的地方。

### 1.1 RTK 模块串口确认

```
任务：确认 RTK 模块的 USB/串口接入方式
步骤：
  1. RTK 模块 USB 线插 NUC
  2. ls /dev/ttyACM* 或 ls /dev/ttyUSB* 找串口设备
  3. sudo screen /dev/ttyACM0 115200 看是否有 $GNGGA / $GNRMC 原始 NMEA 数据
  4. 记录：串口路径、波特率、NMEA 版本
验证：能看到 $GNGGA 开头的 ASCII 数据流
负责人：阮浩宇（算法组）
```

### 1.2 nmea_navsat_driver 配置启动

```
任务：用 nmea_navsat_driver 把 NMEA 解析成 ROS2 /fix
步骤：
  1. 确认串口权限：sudo usermod -a -G dialout haoyu
  2. 创建 launch 文件 ~/campuscar_ws/src/rtk_launch.py
  3. 启动：ros2 launch nmea_navsat_driver nmea_serial_driver.launch.py port:=/dev/ttyACM0 baud:=115200
  4. 验证：ros2 topic echo /fix 看 latitude/longitude/status 是否有效
验证标准：
  - status.status >= 0（有效定位）
  - latitude/longitude 不是 NaN
  - 更新频率 >= 1Hz
负责人：阮浩宇（算法组）
```

### 1.3 u2r_r2u_bridge 修复与验证

```
任务：修复桥接节点（当前 DDS 多实例冲突问题）
步骤：
  1. 重启 NUC（清 DDS 状态）
  2. 确保只有一个 bridge_node 实例在运行
  3. 启动 rosbridge + bridge_node
  4. ros2 topic echo /R2UTopic_Pos 验证 JSON 输出
验证标准：
  - JSON 包含 status, status_name, latitude, longitude, altitude, timestamp, frame_id
  - 字段格式与 PPT 幻灯片 23 完全一致
  - 更新频率与 /fix 同步
负责人：阮浩宇（算法组）
```

---

## 阶段 2：rosbridge → UE5 打通（第 2 优先级）

### 2.1 rosbridge 启动与端口确认

```
任务：确保 rosbridge WebSocket 在 9090 端口稳定监听
步骤：
  1. 启动：ros2 launch rosbridge_server rosbridge_websocket_launch.xml
  2. 验证：ss -tlnp | grep 9090 → LISTEN
  3. 从 Mac 测试：curl http://192.168.10.2:9090/ 应返回 HTTP 响应
验证标准：
  - 端口 9090 持续监听
  - 外部可连接（Mac 端 curl 或 websocat 能连上）
负责人：阮浩宇（算法组）
```

### 2.2 UE5 侧接入测试

```
任务：UE 工程师通过 rosbridge 连接 NUC，接收位置 JSON
步骤：
  1. UE5 连接 ws://192.168.10.2:9090
  2. UE5 订阅 /R2UTopic_Pos (std_msgs/String)
  3. UE5 打印收到的原始 JSON 字符串
  4. UE5 解析字段：status, latitude, longitude, altitude, timestamp
验证标准（PPT 幻灯片 28 验收清单）：
  - UE 能打印原始 JSON ✅
  - UE 能解析所有必须字段 ✅
  - UE 坐标映射正确（经纬度 → UE X/Y）✅
  - 异常状态显示（NO_FIX / TIMEOUT / JSON_ERROR）✅
需要与 UE 工程师确认（PPT 幻灯片 24）：
  - 校园地图原点 (origin_lat, origin_lon) 是什么？
  - 经度对应 UE 的 X 还是 Y？
  - 米制到 UE 单位的比例是多少？
  - 是否需要旋转/偏航补偿？
  - 小车模型朝向如何定义？
  - 无定位时 UE 如何显示？
负责人：阮浩宇（算法组）+ UE 工程师
```

---

## 阶段 3：运控组——底盘驱动与 /cmd_vel 闭环（第 3 优先级）

这是你现在也要忙的部分。

### 3.1 硬件适配（运控组）

```
任务：确认控制板、电机驱动器的型号和通信协议
需要确认的清单：
  [ ] STM32/ESP32 控制板型号？
  [ ] 通信方式：UART / CAN / USB？
  [ ] 波特率、数据位、停止位？
  [ ] 电机驱动器型号？PWM 还是 CAN 控制？
  [ ] 编码器接口？（GPIO 脉冲 / SPI / RS485）
  [ ] 急停按钮接线方式？（常闭→物理断电 还是 信号→STM32）
  [ ] 电池电压？（如 24V）
负责人：钟艺丹、钱艺文
```

### 3.2 串口通信验证（运控组）

```
任务：NUC↔STM32/ESP32 串口通信打通
步骤（PPT 幻灯片 11 调试流程）：
  1. 确认串口路径（/dev/ttyUSB0 或 /dev/ttyACM0）
  2. 发停车帧（速度=0），确认反馈帧能正确解析
  3. 小速度直行（0.1 m/s），确认轮子转动方向正确
  4. 小速度转向，确认左右轮差速方向正确
  5. 解析反馈帧（PPT 幻灯片 14：18字节协议）
安全规则（PPT 强调）：
  ❌ 绝对不要：没验证停车就测前进
  ❌ 绝对不要：没限速就联调
  ❌ 绝对不要：在人群旁边测试
  ✅ 第一次调车：低速、空旷场地、旁边有人看急停、日志持续记录
负责人：钟艺丹、钱艺文
```

### 3.3 /cmd_vel 节点开发（运控组 + 算法协助）

```
任务：ROS2 节点订阅 /cmd_vel，转换成控制帧发给 STM32
核心公式（PPT 幻灯片 12）：
  left  = v - k * ω    → 映射到 -1000 ~ 1000
  right = v + k * ω
控制帧格式（PPT 幻灯片 13，小端序）：
  0xABCD | 左/转向输入(int16 LE) | 右/速度输入(int16 LE) | XOR 校验
  实际发送 8 字节：CD AB XX XX YY YY ZZ ZZ
需要开发：
  1. cmd_vel_subscriber 节点（订阅 geometry_msgs/Twist）
  2. 差速解算函数（v, ω → left_rpm, right_rpm）
  3. 控制帧打包函数（int16 + 小端序 + XOR）
  4. 串口发送（pyserial 或 C++)
  5. 反馈帧解析（PPT 幻灯片 14 的 18 字节协议）
  6. /odom 发布（编码器脉冲 → 航迹推演 → nav_msgs/Odometry）
安全机制（PPT 幻灯片 10-11 强调）：
  [ ] 急停中断优先（物理 + ROS2 topic）
  [ ] /cmd_vel 超时保护（500ms 无数据 → 停车）
  [ ] 最大速度硬限（0.5 m/s 线速度，1.0 rad/s 角速度）
  [ ] 电池低压告警
负责人：钟艺丹、钱艺文（主），阮浩宇（协助 ROS2 框架）
```

### 3.4 /odom 里程计发布（运控组）

```
任务：从编码器反馈计算里程计，发布 /odom
步骤：
  1. 解析控制板回传的左右轮 RPM（PPT 14）
  2. RPM → 线速度（轮子周长 × RPM / 60）
  3. 左右轮速度 → 机器人线速度 + 角速度
  4. 航迹推演：累积 Δx, Δy, Δyaw
  5. 发布 nav_msgs/Odometry 到 /odom
验证：
  - 推车 1 米，/odom.pose 变化 ≈ 1 米
  - 原地转 90°，/odom 航向角变化 ≈ 1.57 rad
负责人：钟艺丹、钱艺文
```

---

## 阶段 4：算法组——定位与路径跟踪（第 4 优先级）

### 4.1 dead_reckoning 节点

```
任务：融合 /imu + /odom 输出 /pose_est
算法：互补滤波
  - IMU 角速度积分 → yaw（短期可信）
  - 里程计位置增量 → x, y（长期可信）
  - 互补：低通滤波 IMU 漂移 + 高通滤波里程计噪声
步骤：
  1. 创建 ros2 pkg：dead_reckoning（ament_python）
  2. 订阅 /imu (sensor_msgs/Imu) + /odom (nav_msgs/Odometry)
  3. 发布时间戳对齐后的融合位姿 /pose_est (geometry_msgs/PoseStamped)
  4. 无硬件时用 ros2 topic pub 模拟数据测试
验证：
  - 静止状态下 /pose_est 漂移 < 0.1m/min
负责人：阮浩宇
```

### 4.2 path_follower 节点（核心）

```
任务：PID 闭环跟踪预设路径，输出 /cmd_vel
算法：
  - 横向偏差 (cross-track error) → angular.z (P term)
  - 距离偏差 (along-track error) → linear.x (P term)
  - 到达路点阈值：0.2m
步骤：
  1. 创建 ros2 pkg：path_follower
  2. 订阅 /pose_est（当前位置） + /target_path（目标路径点序列）
  3. 发布 /cmd_vel (geometry_msgs/Twist)
  4. PID 参数：初期 P=1.0, I=0.01, D=0.1 → 实测调优
验证：
  - 给定一条直线路径，偏差 < 0.2m
  - 给定矩形路径，能完成四边遍历
负责人：阮浩宇
```

### 4.3 task_executor 状态机

```
任务：管理任务状态，接收校园大脑指令，编排路径执行
状态转换：
  IDLE → RECEIVED_PATH → MOVING → (OBSTACLE) → SPRAYING → PATH_DONE → RETURNING → IDLE
步骤：
  1. 创建 ros2 pkg：task_executor
  2. 订阅 /U2RTopic_Command（校园大脑下发的 JSON 路径任务）
  3. 发布 /target_path（转换后的路径点列表）
  4. 发布 /spray_cmd（喷雾开关，依赖 moisture_processor）
  5. 发布 /R2UTopic_Status（任务状态反馈 JSON）
验证：
  - 收到 start_mission 指令 → 状态变为 MOVING → 发布 target_path
  - 路径执行完毕 → 状态变为 RETURNING
  - 收到紧急停止 → 立即 IDLE
负责人：阮浩宇
```

---

## 阶段 5：联调验收（第 5 优先级）

### 5.1 静止联调测试

```
环境：室外、RTK 有效信号、所有设备已上电
步骤（PPT 幻灯片 25）：
  1. 确认 RTK 串口有 $GNGGA 数据
  2. ros2 topic echo /fix → 确认 lat/lon/status 有效
  3. ros2 topic hz /R2UTopic_Pos → 确认频率稳定
  4. ss -tlnp | grep 9090 → 确认 rosbridge 在监听
  5. UE 打印原始 JSON → 确认字段完整
  6. UE 模型显示在正确位置 → 确认坐标映射正确
原则：先静止测试，逐层确认。
口号：先看 /fix，再看 /R2UTopic_Pos，最后看 UE 显示。
```

### 5.2 移动联调测试

```
环境：室外空旷区域，推车/遥控移动 3-5 米
步骤（PPT 幻灯片 27）：
  1. 静止 → 记录 /fix 和 UE 模型位置
  2. 推动小车 3-5 米 → 观察 /fix 变化
  3. 观察 /R2UTopic_Pos 变化
  4. 观察 UE 模型移动
  5. 记录轨迹点 → 对比移动方向和距离
  6. 检查偏移量
原则：先追求字段稳定、坐标方向正确、异常状态可见。
```

### 5.3 验收标准 Checklist（PPT 幻灯片 27）

```
[ ] 室外 RTK 能发布有效 /fix
[ ] /R2UTopic_Pos 能稳定输出 JSON（频率 ≥ 1Hz）
[ ] UE 能连接 NUC 的 rosbridge（tcp://<NUC_IP>:9090）
[ ] UE 能打印收到的位置 JSON
[ ] UE 能把经纬度转换成校园地图位置
[ ] 真实小车移动时，UE 模型位置同步变化
[ ] 小车方向与 UE 模型朝向一致
[ ] 定位异常时（NO_FIX / TIMEOUT），UE 有异常状态显示
[ ] 坐标转换参数已文档化（origin_lat/lon, scale, axis_mapping）
```

---

## 阶段 6：后续增强（验收后）

```
[ ] obstacle_avoider：LiDAR 避障节点
[ ] moisture_processor：土壤湿度 → 喷雾决策
[ ] return_home：路径回溯返航
[ ] UE 远程下发路径：/U2RTopic_Command 完备实现
[ ] 三个校园分区路径文件（YAML）
[ ] 夜晚作业调度（定时启动/停止）
[ ] 日志与数据导出（JSON/CSV）
```

---

## 当前最紧急的三件事

1. **重启 NUC，修复桥接** —— 清 DDS 多实例问题，验证 `/fix → /R2UTopic_Pos` JSON 链路
2. **联系 UE 工程师确认坐标参数** —— origin_lat/lon, scale, axis_mapping，这是联调前提
3. **联系运控组确认硬件型号** —— STM32 型号、通信协议、编码器接口，准备写 /cmd_vel 节点
