# 机器人摄像头 → UE5 视频通信方案

## 方案选型对比

| 指标 | 方案一：ROS Bridge raw Image | 方案二：MJPEG HTTP | **推荐：RTSP / H.264** |
|------|-----------------------------|--------------------|----------------------|
| 带宽（640×480 30fps） | ~36 MB/s（Base64膨胀后） | ~2 MB/s | **~0.3 MB/s** |
| 延迟 | 高（序列化+每帧new纹理） | 200~500 ms | **< 100 ms（zerolatency）** |
| UE5 CPU 占用 | 高 | 低 | **极低（硬件解码）** |
| 实现复杂度 | 已有代码但有性能问题 | 简单 | **简单** |
| ROS 元数据 | 有 | 无 | 无 |

**结论：** 仅需在 UE5 显示机器人视角画面，选 RTSP/H.264 方案。  
若需在 UE5 内处理图像数据（触发游戏逻辑、CV 分析），才保留方案一并做性能优化。

---

## 推荐方案：GStreamer RTSP/H.264 → UE5 Media Player

### 架构图

```
机器人摄像头
    │  /dev/video0 或 ROS topic
    ▼
NUC（Ubuntu）
  GStreamer Pipeline
  + mediamtx RTSP Server
    │  rtsp://NUC_IP:8554/robot_cam
    ▼  （局域网）
Windows PC
  UE5 Media Player
  → Media Texture → 材质/UI Widget
```

---

## 一、NUC 侧配置

### 1. 安装依赖

```bash
# GStreamer 完整安装
sudo apt update
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  libgstreamer1.0-dev

# 验证
gst-launch-1.0 --version
```

### 2. 安装 mediamtx（轻量 RTSP 服务器）

```bash
# 下载最新版（https://github.com/bluenviron/mediamtx/releases）
wget https://github.com/bluenviron/mediamtx/releases/download/v1.9.0/mediamtx_v1.9.0_linux_amd64.tar.gz
tar -xzf mediamtx_v1.9.0_linux_amd64.tar.gz
sudo mv mediamtx /usr/local/bin/
```

mediamtx 默认配置即可，监听 `8554` 端口，无需修改配置文件。

### 3. 启动 RTSP 推流

**方式 A：直接从 USB 摄像头推流（不经过 ROS）**

```bash
# 先启动 mediamtx
mediamtx &

# 推流（640×480，30fps，H.264，超低延迟）
gst-launch-1.0 -v \
  v4l2src device=/dev/video0 \
  ! video/x-raw,width=640,height=480,framerate=30/1 \
  ! videoconvert \
  ! x264enc tune=zerolatency bitrate=1000 speed-preset=ultrafast key-int-max=30 \
  ! rtph264pay config-interval=1 pt=96 \
  ! rtspclientsink location=rtsp://127.0.0.1:8554/robot_cam
```

**方式 B：从 ROS topic 推流（保留 ROS 集成）**

```bash
# 安装 ros-noetic-gscam（或对应 ROS 版本）
sudo apt install ros-noetic-gscam

# 或者用 Python 脚本桥接 ROS Image → GStreamer
# 见下方 ros_to_rtsp.py
```

`ros_to_rtsp.py` 示例：

```python
#!/usr/bin/env python3
"""将 ROS sensor_msgs/Image 转为 GStreamer appsrc 推 RTSP"""
import rospy
import cv2
import numpy as np
import subprocess
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

bridge = CvBridge()
ffmpeg_proc = None

def image_callback(msg):
    global ffmpeg_proc
    frame = bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
    if ffmpeg_proc is None:
        h, w = frame.shape[:2]
        cmd = [
            'ffmpeg', '-y',
            '-f', 'rawvideo', '-vcodec', 'rawvideo',
            '-pix_fmt', 'bgr24', '-s', f'{w}x{h}', '-r', '30',
            '-i', '-',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-b:v', '1000k', '-f', 'rtsp',
            '-rtsp_transport', 'tcp',
            'rtsp://127.0.0.1:8554/robot_cam'
        ]
        ffmpeg_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    ffmpeg_proc.stdin.write(frame.tobytes())

rospy.init_node('ros_to_rtsp')
rospy.Subscriber('/camera/image_raw', Image, image_callback)
rospy.spin()
```

### 4. 开机自启（systemd）

```ini
# /etc/systemd/system/robot-rtsp.service
[Unit]
Description=Robot Camera RTSP Stream
After=network.target

[Service]
ExecStartPre=/usr/local/bin/mediamtx
ExecStart=/bin/bash -c 'gst-launch-1.0 v4l2src device=/dev/video0 \
  ! video/x-raw,width=640,height=480,framerate=30/1 \
  ! videoconvert \
  ! x264enc tune=zerolatency bitrate=1000 speed-preset=ultrafast key-int-max=30 \
  ! rtph264pay config-interval=1 pt=96 \
  ! rtspclientsink location=rtsp://127.0.0.1:8554/robot_cam'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable robot-rtsp
sudo systemctl start robot-rtsp
```

### 5. 防火墙放行

```bash
sudo ufw allow 8554/tcp
sudo ufw allow 8554/udp
```

---

## 二、UE5 侧配置

### 1. 启用插件

在 UE5 编辑器菜单 **Edit → Plugins** 中搜索并启用：

- `Media Framework Utilities`（内置，确认已启用）
- `WMF Media`（Windows Media Foundation，内置，用于 H.264 硬件解码）

> UE5.3 默认已内置上述插件，无需额外安装第三方插件。

### 2. 创建 Media Player Asset

1. Content Browser 右键 → **Media → Media Player**
2. 弹窗勾选 **"Create Media Texture"**，自动生成配套的 `MediaTexture` Asset
3. 命名例如：`MP_RobotCam`、`MT_RobotCam`

### 3. 创建材质

1. 新建 Material，命名 `M_RobotCam`
2. 拖入 `MT_RobotCam` 作为 Texture Sample 节点
3. 连接到 Emissive Color（不需要光照影响）
4. 将材质赋给场景中的显示平面或 UI 的 Image 控件

### 4. 蓝图：开始播放

在关卡蓝图或 Actor 蓝图的 `BeginPlay` 事件中：

```
Event BeginPlay
    │
    ├─ [Get] MP_RobotCam (Media Player Object Reference)
    │
    └─ Open URL
           Media Player: MP_RobotCam
           URL: "rtsp://192.168.1.100:8554/robot_cam"
                 ↑ 替换为实际 NUC IP
```

节点路径：`Media Player → Open URL`

### 5. 蓝图：UMG Widget 显示（可选）

若要在 HUD 上显示：

1. 创建 Widget Blueprint，添加 `Image` 控件
2. Image 的 Brush → Texture 选择 `MT_RobotCam`
3. 在 BeginPlay 中 `Create Widget` + `Add to Viewport`

### 6. 延迟优化设置

在 `MP_RobotCam` 的 Details 面板中：

- **Play on Open**：勾选
- **Loop**：勾选
- **Native Audio Out**：不勾选（无音频）

在 `DefaultEngine.ini` 中添加（降低 Media Player 内部缓冲）：

```ini
[/Script/WmfMedia.WmfMediaSettings]
LowLatencyMode=True
```

---

## 三、网络要求

| 参数 | 建议值 |
|------|--------|
| 网络类型 | 有线局域网（千兆）或 5GHz WiFi |
| 带宽占用 | ~1 Mbps（1000kbps bitrate） |
| 延迟目标 | < 100 ms（端到端） |
| NUC IP | 建议设为静态 IP，避免 DHCP 变化 |

---

## 四、调试验证

### NUC 侧验证推流是否正常

```bash
# 用 VLC 或 ffplay 在 NUC 本机测试
ffplay rtsp://127.0.0.1:8554/robot_cam

# 查看推流状态
gst-launch-1.0 ... 2>&1 | grep -i error
```

### Windows 侧验证（UE5 之前先用 VLC 测试）

```
VLC → 媒体 → 打开网络串流
URL: rtsp://192.168.1.100:8554/robot_cam
```

能在 VLC 看到画面，UE5 Media Player 就一定能播放。

---

## 五、常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| UE5 黑屏 | WMF 插件未启用 | Edit → Plugins 启用 WMF Media |
| 连接超时 | 防火墙拦截 8554 | NUC 执行 `ufw allow 8554` |
| 画面卡顿 | WiFi 信号差 | 换有线或降低 bitrate |
| 延迟高 | 缓冲区过大 | 开启 `LowLatencyMode=True` |
| 摄像头找不到 | 设备路径错误 | `ls /dev/video*` 确认设备号 |

---

## 六、转流：RTSP → HLS（解决 UE5 WMF 不支持 RTSP 的问题）

### 背景

UE5 在 Windows 上默认使用 WMF（Windows Media Foundation）处理媒体，WMF 不支持 RTSP 协议，会报错：
```
WinRT originate error - 0xC00D426A : '从网络中读取时出错。'
```
启用 ElectraPlayer 插件后，WMF 仍会优先拦截 `rtsp://` URL，Electra 无法接管。

**解决方案：** 在 NUC 上用 ffmpeg 将 RTSP 流实时转为 HLS（`.m3u8`），UE5 通过 `http://` 拉取 HLS，Electra 原生支持 HLS 且 WMF 不会拦截。

---

### 架构

```
机器人摄像头
    │
    ▼
mediamtx（RTSP Server）
    │  rtsp://127.0.0.1:8554/robot_cam
    ▼
ffmpeg（RTSP → HLS 转码）
    │  写入 /tmp/hls/robot_cam.m3u8
    ▼
Python HTTP Server（端口 8888）
    │  http://NUC_IP:8888/robot_cam.m3u8
    ▼
UE5 ElectraPlayer（HLS 拉流）
```

---

### NUC 侧操作步骤

#### 1. 安装 ffmpeg

```bash
sudo apt update
sudo apt install -y ffmpeg

# 验证
ffmpeg -version
```

#### 2. 创建 HLS 输出目录

```bash
mkdir -p /tmp/hls
```

#### 3. 启动 RTSP → HLS 转流

确保 mediamtx 已在运行（`rtsp://127.0.0.1:8554/robot_cam` 可用），然后执行：

```bash
ffmpeg -rtsp_transport tcp \
  -i rtsp://127.0.0.1:8554/robot_cam \
  -c:v copy \
  -f hls \
  -hls_time 1 \
  -hls_list_size 3 \
  -hls_flags delete_segments+append_list \
  /tmp/hls/robot_cam.m3u8 \
  -loglevel warning &
```

参数说明：
- `-rtsp_transport tcp`：强制 TCP 拉取 RTSP，避免 UDP 丢包
- `-c:v copy`：直接复制 H.264 码流，不重新编码，CPU 占用极低
- `-hls_time 1`：每个 TS 分片 1 秒
- `-hls_list_size 3`：播放列表保留最近 3 个分片
- `-hls_flags delete_segments+append_list`：自动删除旧分片，保持低延迟

#### 4. 启动 HTTP 服务器提供 HLS 文件

```bash
python3 -m http.server 8888 --directory /tmp/hls &
```

#### 5. 验证转流是否正常

在 Windows 浏览器访问：
```
http://10.12.171.184:8888/robot_cam.m3u8
```
能看到以 `#EXTM3U` 开头的文本内容，说明 HLS 服务正常。

也可以用 VLC 测试：
```
VLC → 媒体 → 打开网络串流
URL: http://10.12.171.184:8888/robot_cam.m3u8
```

#### 6. 防火墙放行 8888 端口

```bash
sudo ufw allow 8888/tcp
```

---

### UE5 侧配置

#### 1. 启用 Electra 插件（必须）

Edit → Plugins，启用以下三个插件，重启编辑器并重新编译：
- **ElectraPlayer**
- **ElectraCodecs**
- **Electra Player Utilities**

#### 2. Media Player 使用 HLS URL

蓝图中 `Open URL` 填入：
```
http://10.12.171.184:8888/robot_cam.m3u8
```

> 注意：不要在 URL 后面加 `?rtsp_transport=tcp` 等参数，Electra 不支持 ffmpeg 风格的 query string。

---

### 开机自启（systemd）

将 mediamtx、ffmpeg 转流、HTTP 服务器三个进程统一管理：

```ini
# /etc/systemd/system/robot-hls.service
[Unit]
Description=Robot Camera HLS Stream
After=network.target

[Service]
Type=forking
ExecStartPre=/bin/mkdir -p /tmp/hls
ExecStart=/bin/bash -c '\
  /usr/local/bin/mediamtx & \
  sleep 3 && \
  ffmpeg -rtsp_transport tcp \
    -i rtsp://127.0.0.1:8554/robot_cam \
    -c:v copy -f hls \
    -hls_time 1 -hls_list_size 3 \
    -hls_flags delete_segments+append_list \
    /tmp/hls/robot_cam.m3u8 \
    -loglevel warning & \
  python3 -m http.server 8888 --directory /tmp/hls'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable robot-hls
sudo systemctl start robot-hls
sudo systemctl status robot-hls
```

---

### 延迟说明

HLS 协议本身有分片缓冲，延迟比 RTSP 高：

| 参数 | 典型值 |
|------|--------|
| 端到端延迟 | 2~4 秒（`hls_time=1` + 3个分片） |
| 降低延迟方法 | 将 `hls_time` 改为 `0.5`，`hls_list_size` 改为 `2` |
| 最低可达延迟 | ~1.5 秒 |

如果需要更低延迟（< 1 秒），需要改用 LL-HLS（Low Latency HLS），mediamtx 支持，但 UE5 ElectraPlayer 对 LL-HLS 支持有限，不推荐。
