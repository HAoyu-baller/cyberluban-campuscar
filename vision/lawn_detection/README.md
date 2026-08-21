# Mac 草坪识别原型

这个目录提供一个不依赖海康专用 SDK 和 ROS 2 的草坪识别原型。它通过
OpenCV 使用 macOS AVFoundation 或 Linux V4L2 读取普通 UVC 摄像头，也可以读取
本地图片和视频。拿到 CZ007 后只需要确认摄像头索引；迁移到 NUC 时检测核心无需
修改，ROS 2 节点负责把图像传给 `LawnDetector` 即可。

## 当前算法

当前版本使用以下条件生成候选草坪掩码：

1. HSV 色相、饱和度和亮度符合绿色范围；
2. Excess Green（`2G - R - B`）超过阈值；
3. 形态学开闭运算去除噪声；
4. 删除过小的连通区域；
5. 草坪总覆盖率和最大连通区域同时超过阈值；
6. 连续多帧满足条件后，才把状态切换为 `stable_present=true`。

它是现场采集和阈值验证用的 MVP，不应直接作为无人值守灌溉的唯一安全信号。
树叶、绿色塑料、绿色地垫以及异常白平衡仍可能造成误检。正式部署前需要用现场
数据验证；如果传统视觉不能可靠区分，再升级为语义分割模型。

## Mac 安装

建议在这个目录创建独立虚拟环境：

```bash
cd vision/lawn_detection
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

首次使用摄像头时，macOS 可能会请求终端或 Python 的摄像头权限。可在“系统设置 →
隐私与安全性 → 摄像头”中确认权限。

## 使用方法

列出可读取的摄像头索引：

```bash
python3 lawn_detector.py --list-cameras
```

使用索引 0 的摄像头，默认请求 1280×720、30 FPS：

```bash
python3 lawn_detector.py --source 0
```

读取视频并保存带标注的视频和逐帧 JSON：

```bash
python3 lawn_detector.py \
  --source sample.mp4 \
  --output-video output/lawn_debug.mp4 \
  --output-jsonl output/lawn_results.jsonl
```

读取单张图片并保存标注图和二值掩码：

```bash
python3 lawn_detector.py \
  --source sample.jpg \
  --headless \
  --output-image output/sample_debug.jpg \
  --output-mask output/sample_mask.png
```

实时预览快捷键：

- `q` 或 `Esc`：退出；
- 空格：暂停或继续；
- `s`：保存当前标注图和掩码。

没有显示器的环境使用 `--headless`。视频模式下可以用 `--loop` 循环播放。

## 输出含义

- `raw_present`：当前单帧是否满足草坪条件；
- `stable_present`：经过连续帧确认后的稳定状态；
- `confidence`：由覆盖率和最大连通区域得到的启发式分数；
- `coverage_ratio`：ROI 中草坪掩码占比；
- `centroid_px`：掩码像素质心；
- `centroid_normalized`：归一化图像坐标，左上为 `(0, 0)`；
- `bbox_px`：草坪掩码外接矩形；
- `roi_px`：本次参与检测的图像区域。

这里的 `confidence` 不是经过数据集标定的概率，不能把 `0.9` 理解为 90% 的真实
正确率。

## 参数调整

默认参数位于 `config/default.json`。建议复制一份现场配置再修改，不要直接覆盖默认
配置：

```bash
cp config/default.json config/cz007_field.json
python3 lawn_detector.py --source 0 --config config/cz007_field.json
```

主要参数：

- `h_min` / `h_max`：OpenCV HSV 绿色色相范围，取值 0～179；
- `s_min` / `v_min`：过滤灰色、低饱和度和过暗区域；
- `exg_min`：绿色相对红、蓝通道的优势阈值；
- `min_total_coverage`：草坪总覆盖率门槛；
- `min_largest_component_ratio`：最大连续草坪区域门槛；
- `roi_top_ratio`：忽略图像顶部比例，车载俯视安装后可先试 0.2～0.4；
- `confirm_frames` / `clear_frames`：状态确认和清除需要的连续帧数。

## 测试

```bash
python3 -m unittest -v test_lawn_detector.py
```

测试使用程序生成的合成图像，不需要摄像头。它只检查处理链路和基本阈值行为，不能
替代真实草坪数据集评估。

## 后续迁移到 NUC / ROS 2

推荐保持两层结构：

```text
ROS 2 image topic / V4L2 camera
             |
             v
       LawnDetector
             |
             v
vision detection message -> task_executor -> irrigation gate
```

NUC 上可由 `v4l2_camera` 或 `usb_cam` 发布 `sensor_msgs/Image`。ROS 2 包只负责
消息转换、时间戳、TF 和状态发布，颜色分割与连续帧逻辑继续复用本文件。视觉状态必须
再经过 RTK 质量、机器人速度、目标区域和灌溉执行器反馈门控，不能直接控制阀门。
