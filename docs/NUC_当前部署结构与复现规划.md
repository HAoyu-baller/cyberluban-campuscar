# NUC 当前部署结构与复现规划

更新时间：2026-08-21

本文档描述从 NUC 实机同步回来的当前代码、仓库中的本地开发代码、两套 ROS 2 工作区，以及开机自启服务之间的关系。目标是让后续开发、回滚和重新部署都有明确入口。

## 1. 当前实机来源

本次从 NUC `10.3.132.132` 读取并同步了以下路径：

```text
/home/haoyu/campuscar_ws/src
/home/haoyu/livox_ws/src
/opt/cyberluban-control
/home/haoyu/mediamtx.yml
/home/haoyu/mediamtx/mediamtx.yml
/etc/systemd/system/campuscar-*.service
/etc/systemd/system/cyberluban-control.service
```

NUC 当前使用 Ubuntu 22.04、ROS 2 Humble，代码分为两个 ROS 2 工作区：

```text
campuscar_ws  -> RTK、NMEA、UE/CampusBrain、视觉、校园指令
livox_ws      -> Livox Mid-360 驱动、点云、雷达安全节点
```

ESP32 串口的唯一拥有者是：

```text
/opt/cyberluban-control
```

ROS 节点不会直接打开 ESP32 串口，而是通过本机 HTTP API 将运动或喷水请求交给控制服务。这样可以集中执行串口看门狗、控制权仲裁和急停逻辑。

## 2. 仓库结构与职责

```text
CyberLuban/
├── nuc_deployment/
│   ├── cyberluban-control/              # NUC 控制网页和 ESP32 串口服务镜像
│   ├── u2r_r2u_bridge/                  # NUC 当前 RTK/校园指令 ROS 2 包
│   ├── livox_ws/src/livox_ros_driver2/  # NUC 当前 Livox ROS 2 驱动源码
│   ├── camera/                          # MediaMTX 实际配置和相机辅助程序
│   ├── scripts/                         # NUC 诊断、抓包和安装辅助脚本
│   └── systemd/                         # NUC 六个开机服务单元
├── nuc_lidar_overlay/
│   ├── campuscar_lidar_bringup/         # Mid-360 外参、点云和雷达安全包
│   └── systemd/                         # 雷达服务模板
├── vision/
│   ├── campuscar_lawn_vision/           # NUC 当前 SegFormer ROS 2 包
│   └── lawn_detection/                  # 离线图片/视频检测原型
├── nuc_camera_stream/systemd/           # 相机服务文档副本
├── cyberluban-handoff-2026-08-18/      # 运控交接、协议、测试和 ESP32 代码
├── RTK/                                 # RTK 厂商手册和历史参考资料
├── docs/                                # 系统方案和工程计划
└── infra/                               # 网络服务配置模板
```

### 代码来源约定

1. `nuc_deployment/` 是当前 NUC 运行版本的部署镜像，优先用于复现 NUC。
2. `vision/campuscar_lawn_vision/` 和 `nuc_lidar_overlay/` 是已同步的 ROS 2 开发副本，便于在普通 ROS 2 工作区中编译和测试。
3. `cyberluban-handoff-2026-08-18/` 保存历史交接版本，不能直接覆盖当前 NUC 版本。
4. `nuc_snapshots/` 是本地临时下载快照，已加入 `.gitignore`，不作为仓库源码提交。

后续修改部署功能时，先修改对应的 `nuc_deployment/` 或已同步的 ROS 包，再更新 NUC。不要直接修改 NUC 后不回传仓库，否则仓库会与实机再次分叉。

## 3. 运行链路

### 3.1 RTK 与 UE/CampusBrain

```text
RTK 4G 板
  └─ NMEA 串口
      └─ serial_reader_node
          └─ /gps/nmea_sentence
              └─ nmea_topic_driver
                  └─ /fix
                      └─ bridge_node
                          └─ /R2UTopic_Pos
```

校园指令链路为：

```text
校园大脑/UE
  └─ TCP/BSON :9090 或 ROS topic /U2RTopic_Command
      └─ campus_command_bridge
          └─ POST 127.0.0.1:8000/api/ros-command
              └─ cyberluban-control
                  └─ ESP32 串口
```

当前 `TargetPosition` 仍会返回 `UNSUPPORTED`，不会直接驱动车辆。方向/移动指令只接受有上限的时间窗口，并由控制服务自动停车。真正的地理目标导航还需要地图、定位融合、Nav2、轮速里程计和障碍物代价地图。

### 3.2 Mid-360 与急停

```text
Livox Mid-360
  └─ /livox/lidar
      └─ radar_safety_node
          └─ /radar/safety_stop + /radar/status
          └─ 127.0.0.1:8000/api/radar-safety/state
              └─ cyberluban-control
                  └─ 拦截运动并发送停止指令
```

雷达节点不打开 ESP32 串口。雷达开关由控制网页授权，默认关闭；启用后点云断流会按失败安全策略停车。

当前 NUC 实际参数位于 `nuc_lidar_overlay/campuscar_lidar_bringup/config/radar_safety.yaml`：

```text
雷达安装高度：0.58 m
检测半径：0.5 m
检测高度：离地 1.0 至 2.0 m
最少点数：30
最少方位分区：3
车体过滤半长：0.42 m
车体过滤半宽：0.24 m
```

这组参数是当前实机配置记录，不等于最终验收参数。需要在无动力、低速和有人/灌木/墙体等场景分别测试误报与漏报。

### 3.3 相机与草坪识别

```text
/dev/video0
  └─ ffmpeg
      └─ MediaMTX /robot_cam
          └─ lawn_segformer_node
              ├─ /vision/lawn/result
              ├─ /vision/lawn/debug/compressed
              ├─ RTSP /robot_cam_ai
              └─ 127.0.0.1:8000/api/vision-spray
                  └─ cyberluban-control
                      └─ ESP32 喷水命令
```

当前草坪识别参数：

```text
模型：nvidia/segformer-b0-finetuned-ade-512-512
处理宽度：640
识别频率：1 Hz
草坪开启阈值：5%
草坪关闭阈值：3%
置信度阈值：0.45
连续确认帧：3
```

识别结果不会自动绕过安全开关。必须在控制网页取得视觉喷水授权，并保持授权心跳；视觉数据超时或网页断开时自动停止喷水。视觉喷水不占用校园大脑的运动控制权。

## 4. 开机自启顺序

当前已启用的服务：

```text
campuscar-mediamtx.service
campuscar-camera-publish.service
campuscar-lawn-vision.service
cyberluban-control.service
campuscar-rtk-ue.service
campuscar-mid360.service
```

依赖关系：

```text
network-online.target
  ├─ campuscar-mediamtx.service
  │   ├─ campuscar-camera-publish.service
  │   └─ campuscar-lawn-vision.service
  ├─ cyberluban-control.service
  │   └─ campuscar-mid360.service
  └─ campuscar-rtk-ue.service
```

安装或更新服务时使用 `nuc_deployment/systemd/` 中的模板。真实运行时环境文件仍只保留在 NUC：

```text
/etc/cyberluban-control.env
```

它包含控制口令和 ROS 指令口令，禁止提交到 GitHub。

## 5. 端口与话题合同

| 项目 | 当前值 | 说明 |
| --- | --- | --- |
| 控制网页 | `8000` | 手动控制、喷水开关、雷达开关 |
| TCP/BSON | `9090` | 当前 UE/校园大脑主要接入 |
| WebSocket | `9091` | rosbridge 诊断通道 |
| RTSP | `8554` | 原始和 AI 视频发布 |
| HLS | `8888` | 浏览器和校园大脑查看视频 |
| RTK 输出 | `/fix` | `sensor_msgs/msg/NavSatFix` |
| RTK 原始句子 | `/gps/nmea_sentence` | `nmea_msgs/msg/Sentence` |
| 位置输出 | `/R2UTopic_Pos` | `std_msgs/msg/String`，JSON 字符串 |
| 校园指令 | `/U2RTopic_Command` | `std_msgs/msg/String`，JSON 字符串 |
| 校园状态 | `/R2UTopic_Status` | `std_msgs/msg/String`，JSON 字符串 |
| 点云 | `/livox/lidar` | `sensor_msgs/msg/PointCloud2` |

## 6. 复现流程

### 第一步：准备 NUC 依赖

```bash
sudo apt install python3-colcon-common-extensions python3-serial \
  ros-humble-nmea-navsat-driver ros-humble-nmea-msgs \
  ros-humble-rosbridge-server ros-humble-sensor-msgs-py
```

视觉模型依赖使用 NUC 上单独的 Python 虚拟环境，不把模型缓存提交到仓库：

```text
/home/haoyu/venvs/lawn-ai
```

### 第二步：编译两个工作区

```bash
source /opt/ros/humble/setup.bash

cd ~/campuscar_ws
colcon build --symlink-install

cd ~/livox_ws
colcon build --symlink-install
```

### 第三步：配置运行时环境

```bash
sudo install -m 0600 /path/to/cyberluban-control.env \
  /etc/cyberluban-control.env
```

仓库只提供 `.env.example`，真实 Token 必须在部署机器上重新生成或从安全备份恢复。

### 第四步：安装并检查服务

```bash
sudo cp nuc_deployment/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cyberluban-control.service
sudo systemctl enable --now campuscar-mediamtx.service
sudo systemctl enable --now campuscar-camera-publish.service
sudo systemctl enable --now campuscar-lawn-vision.service
sudo systemctl enable --now campuscar-rtk-ue.service
sudo systemctl enable --now campuscar-mid360.service
```

检查：

```bash
systemctl --no-pager --full status \
  cyberluban-control campuscar-mediamtx campuscar-camera-publish \
  campuscar-lawn-vision campuscar-rtk-ue campuscar-mid360
```

## 7. 验收顺序

1. 断开电机动力或架空车轮，确认物理急停有效。
2. 确认控制网页可打开，ESP32 串口状态为 ready。
3. 单独确认 `/fix`、`/gps/nmea_sentence` 和 `/R2UTopic_Pos`。
4. 用 TCP/BSON 客户端发送订阅和测试指令，确认状态包可回传。
5. 观看 `robot_cam` 和 `robot_cam_ai` 两路视频。
6. 在喷水开关关闭时只验证草坪识别结果和比例。
7. 水泵接入后先短脉冲验证，再验证视觉喷水授权、阈值和超时停车。
8. 雷达保护关闭时只查看点云和 `/radar/status`。
9. 雷达保护开启后用静态障碍物验证停车，再做低速有人场景测试。
10. 最后才进行校园大脑、RTK、运动和喷水的联合测试。

## 8. 明确不在仓库中的内容

以下内容有意排除：

- `/etc/cyberluban-control.env` 及任何实际控制 Token；
- NTRIP 账号、密码和 NUC 登录密码；
- SSH 私钥、证书、浏览器凭据；
- SegFormer/Hugging Face 模型缓存；
- `build/`、`install/`、`log/`、`__pycache__/`、`.pyc`；
- 实时视频、点云录包和诊断日志；
- 厂商 GUI 软件和不必要的大型二进制发行包。

本地从 NUC 下载的临时快照保存在 `nuc_snapshots/`，该目录已加入 `.gitignore`。正式提交使用已合并的源码目录，不直接提交临时压缩包。
