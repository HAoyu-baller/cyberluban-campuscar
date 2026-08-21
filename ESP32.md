# ESP32 替代 HOTRC F-06A 接收机：完整操作指南

## 0. 这套方案在做什么

HOTRC DS600 遥控系统原来的控制链路是：

```text
DS600 遥控器
    ↓ 2.4 GHz 无线
F-06A 接收机
    ↓ 两路独立 RC PWM
小车控制器
    ↓
转向与驱动电机
```

本方案不是模拟 DS600 的无线通信，而是让 ESP32 替代 **F-06A 接收机的 PWM 输出部分**：

```text
Mac / NUC
    ↓ USB 串口命令
ESP32
    ↓ CH1、CH2 两路 50 Hz RC PWM
小车控制器
    ↓
转向与驱动电机
```

F-06A 的核心输出参数：

| 参数 | 数值 |
|---|---:|
| 周期 | 20 ms |
| 频率 | 50 Hz |
| 中位脉宽 | 1500 µs |
| 最小脉宽 | 约 1000 µs |
| 最大脉宽 | 约 2000 µs |
| CH1 | 转向，连续 PWM |
| CH2 | 油门，连续 PWM |

> **重要：** 初次测试必须架空驱动轮，旁边必须有人能立即关闭小车电源。不得直接在人群、桌边或狭小空间进行落地测试。

---

## 1. 准备物品

- ESP32 DevKit 开发板一块，带 USB 串口芯片（CH340 或 CP2102）
- 支持数据传输的 USB 线一根
- 杜邦线至少三根
- 安装 Windows 或 macOS 的电脑一台
- 小车控制器及其 F-06A 接收机四线接口
- 推荐：示波器或 USB 逻辑分析仪

不需要：

- 不需要 Ubuntu 才能烧录或测试 ESP32
- 不需要将 F-06A 的无线协议写进 ESP32
- 不需要连接原接收机的 VCC 到 USB 供电的 ESP32

---

## 2. 在 Windows 或 macOS 安装 Arduino IDE

### 2.1 下载 Arduino IDE

打开：

```text
https://www.arduino.cc/en/software
```

下载并安装 Arduino IDE 2.x：

- Windows：下载 Windows Win 10 and newer 安装包
- macOS：根据 Mac 芯片选择 Apple Silicon 或 Intel 版本

### 2.2 安装 ESP32 开发板支持

1. 打开 Arduino IDE。
2. 进入 **File → Preferences**；macOS 为 **Arduino IDE → Settings**。
3. 在 **Additional Boards Manager URLs** 填入：

```text
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

4. 点击 OK。
5. 进入 **Tools → Board → Boards Manager**。
6. 搜索 `esp32`。
7. 安装 **esp32 by Espressif Systems**。

### 2.3 安装 ESP32Servo 库

1. 进入 **Tools → Manage Libraries**。
2. 搜索 `ESP32Servo`。
3. 安装 **ESP32Servo by Kevin Harrington / John K. Bennett**。

### 2.4 连接并选择 ESP32

1. 用 USB 数据线连接 ESP32 和电脑。
2. 进入 **Tools → Board → ESP32 Arduino → ESP32 Dev Module**。
3. 进入 **Tools → Port**，选择 ESP32 串口。

常见端口：

```text
Windows: COM3、COM4 等
macOS CH340: /dev/cu.usbserial-xxx
macOS CP2102: /dev/cu.SLAB_USBtoUART
```

如果端口没有出现：

1. 换一根确认能传数据的 USB 线。
2. 换电脑 USB 口。
3. Windows 安装 CH340 或 CP210x 驱动。
4. 重新插拔 ESP32。

---

## 3. ESP32 固件代码

### 3.1 串口协议

电脑向 ESP32 发送以下文本命令，每条以换行结束：

```text
S <速度> <转向>
STOP
STATUS
```

示例：

```text
S 100 0      # 小油门前进，转向居中
S -100 0     # 小油门后退，转向居中
S 0 -100     # 油门居中，小幅向一侧转向
S 0 100      # 油门居中，小幅向另一侧转向
STOP         # 立即回中停车
STATUS       # 查询当前状态
```

速度和转向范围都是：

```text
-1000 ～ 1000
```

ESP32 会返回：

```text
OK SPEED=100 STEERING=0 THROTTLE_US=1550 STEERING_US=1500
ERR BAD_FORMAT
ERR OUT_OF_RANGE
STATE ARMED=1 ...
```

### 3.2 完整代码

在 Arduino IDE 新建草图，删除原内容，粘贴以下代码：

```cpp
#include <ESP32Servo.h>

// F-06A 替代输出：CH1 转向、CH2 油门
constexpr int PIN_STEERING = 25;
constexpr int PIN_THROTTLE = 26;

// F-06A 标准 RC PWM 参数
constexpr int PWM_HZ = 50;
constexpr int PWM_MIN_US = 1000;
constexpr int PWM_NEUTRAL_US = 1500;
constexpr int PWM_MAX_US = 2000;

// 根据实际小车方向修改：false 为正常，true 为反向
constexpr bool REVERSE_STEERING = false;
constexpr bool REVERSE_THROTTLE = false;

// 上电中立解锁与通信超时
constexpr unsigned long ARMING_TIME_MS = 3000;
constexpr unsigned long COMMAND_TIMEOUT_MS = 500;

Servo steeringServo;
Servo throttleServo;

int steeringCommand = 0;
int speedCommand = 0;
int steeringPulseUs = PWM_NEUTRAL_US;
int throttlePulseUs = PWM_NEUTRAL_US;

unsigned long bootTimeMs = 0;
unsigned long lastValidCommandMs = 0;
bool armed = false;

int clampCommand(int value) {
  if (value < -1000) return -1000;
  if (value > 1000) return 1000;
  return value;
}

int commandToPulse(int value, bool reversed) {
  value = clampCommand(value);
  if (reversed) value = -value;
  return map(value, -1000, 1000, PWM_MIN_US, PWM_MAX_US);
}

void writeNeutral() {
  steeringCommand = 0;
  speedCommand = 0;
  steeringPulseUs = PWM_NEUTRAL_US;
  throttlePulseUs = PWM_NEUTRAL_US;
  steeringServo.writeMicroseconds(steeringPulseUs);
  throttleServo.writeMicroseconds(throttlePulseUs);
}

void applyCommand(int speed, int steering) {
  speedCommand = clampCommand(speed);
  steeringCommand = clampCommand(steering);

  throttlePulseUs = commandToPulse(speedCommand, REVERSE_THROTTLE);
  steeringPulseUs = commandToPulse(steeringCommand, REVERSE_STEERING);

  steeringServo.writeMicroseconds(steeringPulseUs);
  throttleServo.writeMicroseconds(throttlePulseUs);
  lastValidCommandMs = millis();

  Serial.printf(
    "OK SPEED=%d STEERING=%d THROTTLE_US=%d STEERING_US=%d\n",
    speedCommand,
    steeringCommand,
    throttlePulseUs,
    steeringPulseUs
  );
}

void printStatus() {
  Serial.printf(
    "STATE ARMED=%d SPEED=%d STEERING=%d THROTTLE_US=%d STEERING_US=%d AGE_MS=%lu\n",
    armed ? 1 : 0,
    speedCommand,
    steeringCommand,
    throttlePulseUs,
    steeringPulseUs,
    millis() - lastValidCommandMs
  );
}

bool parseInteger(const String &token, int &value) {
  if (token.length() == 0) return false;

  int start = 0;
  if (token[0] == '-' || token[0] == '+') {
    if (token.length() == 1) return false;
    start = 1;
  }

  for (int i = start; i < token.length(); ++i) {
    if (!isDigit(token[i])) return false;
  }

  value = token.toInt();
  return true;
}

void handleLine(String line) {
  line.trim();

  if (line == "STOP" || line == "K") {
    writeNeutral();
    lastValidCommandMs = millis();
    Serial.println("OK STOP");
    return;
  }

  if (line == "STATUS") {
    printStatus();
    return;
  }

  if (!line.startsWith("S ")) {
    writeNeutral();
    Serial.println("ERR BAD_FORMAT EXPECTED=S_<speed>_<steering>");
    return;
  }

  // 正确格式：S 100 0
  int firstSpace = line.indexOf(' ');
  int secondSpace = line.indexOf(' ', firstSpace + 1);

  if (firstSpace < 0 || secondSpace < 0) {
    writeNeutral();
    Serial.println("ERR BAD_FORMAT EXPECTED=S_<speed>_<steering>");
    return;
  }

  String speedToken = line.substring(firstSpace + 1, secondSpace);
  String steeringToken = line.substring(secondSpace + 1);
  speedToken.trim();
  steeringToken.trim();

  int speed = 0;
  int steering = 0;
  if (!parseInteger(speedToken, speed) || !parseInteger(steeringToken, steering)) {
    writeNeutral();
    Serial.println("ERR BAD_NUMBER");
    return;
  }

  if (speed < -1000 || speed > 1000 || steering < -1000 || steering > 1000) {
    writeNeutral();
    Serial.println("ERR OUT_OF_RANGE RANGE=-1000..1000");
    return;
  }

  if (!armed) {
    writeNeutral();
    Serial.println("ERR NOT_ARMED WAIT_FOR_3_SECONDS");
    return;
  }

  applyCommand(speed, steering);
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(30);

  steeringServo.setPeriodHertz(PWM_HZ);
  throttleServo.setPeriodHertz(PWM_HZ);
  steeringServo.attach(PIN_STEERING, PWM_MIN_US, PWM_MAX_US);
  throttleServo.attach(PIN_THROTTLE, PWM_MIN_US, PWM_MAX_US);

  writeNeutral();
  bootTimeMs = millis();
  lastValidCommandMs = millis();

  Serial.println("BOOT PWM_BRIDGE");
  Serial.println("ARMING NEUTRAL_FOR_3000_MS");
}

void loop() {
  if (!armed && millis() - bootTimeMs >= ARMING_TIME_MS) {
    armed = true;
    lastValidCommandMs = millis();
    Serial.println("ARMED");
  }

  while (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    handleLine(line);
  }

  if (armed && millis() - lastValidCommandMs > COMMAND_TIMEOUT_MS) {
    if (speedCommand != 0 || steeringCommand != 0) {
      writeNeutral();
      Serial.println("FAILSAFE TIMEOUT NEUTRAL");
    }
  }

  delay(5);
}
```

> 旧版代码中的 `S300 0` 解析存在错误：代码要求找到第二个空格，但命令只有一个空格，因此命令不会执行。新版统一使用 `S 300 0`，并正确解析三个字段。

---

## 4. 编译和烧录

1. Arduino IDE 选择 **ESP32 Dev Module**。
2. 选择 ESP32 对应串口。
3. 点击 **Verify**，确认代码编译成功。
4. 点击 **Upload**。
5. 如果控制台长时间停在 `Connecting...`：
   - 按住 ESP32 的 BOOT 键；
   - 等上传开始后松开；
   - 如果仍失败，保持 BOOT 按下并重新点击 Upload。
6. 看到 `Hard resetting via RTS pin...` 或上传完成提示后，烧录成功。

---

## 5. 第一次验证：暂时不要连接小车控制器

### 5.1 打开串口监视器

1. 打开 **Tools → Serial Monitor**。
2. 波特率选择 `115200`。
3. 行结束符选择 **Newline**。
4. 按一下 ESP32 的 EN/RST 键，观察：

```text
BOOT PWM_BRIDGE
ARMING NEUTRAL_FOR_3000_MS
ARMED
```

### 5.2 验证命令解析

依次发送：

```text
STATUS
S 100 0
STATUS
STOP
STATUS
```

预期回执示例：

```text
STATE ARMED=1 SPEED=0 STEERING=0 THROTTLE_US=1500 STEERING_US=1500 ...
OK SPEED=100 STEERING=0 THROTTLE_US=1550 STEERING_US=1500
OK STOP
```

如果发送 `S100 0` 或 `S 100`，应返回 `ERR`。这证明错误命令不会误驱动车辆。

### 5.3 验证超时保护

只发送一次：

```text
S 100 0
```

停止发送。约 500 ms 后应看到：

```text
FAILSAFE TIMEOUT NEUTRAL
```

这是正常行为。真实控制时必须以 20 Hz 左右持续发送运动命令。

---

## 6. 验证 PWM 波形

推荐使用示波器或逻辑分析仪：

| 测试命令 | GPIO25 / CH1 | GPIO26 / CH2 |
|---|---:|---:|
| `STOP` | 1500 µs | 1500 µs |
| 持续 `S 100 0` | 1500 µs | 1550 µs |
| 持续 `S -100 0` | 1500 µs | 1450 µs |
| 持续 `S 0 100` | 1550 µs | 1500 µs |
| 持续 `S 0 -100` | 1450 µs | 1500 µs |

所有波形周期都应约为 20 ms，也就是 50 Hz。

万用表只能观察平均电压变化，不能证明周期和脉宽正确。3.3 V PWM 的近似平均电压：

| 脉宽 | 占空比 | 近似平均电压 |
|---:|---:|---:|
| 1000 µs | 5% | 0.165 V |
| 1500 µs | 7.5% | 0.248 V |
| 2000 µs | 10% | 0.330 V |

---

## 7. 连接小车控制器

### 7.1 先确认 F-06A 接收机四根线

典型四线定义：

```text
GND / 黑线
VCC / 红线
CH1 / 转向信号
CH2 / 油门信号
```

不能只根据线色盲接。若控制器或线束有标签，应以标签为准；必要时使用万用表确认 GND 和 VCC。

### 7.2 正确接线

先关闭小车电源，再拔掉 F-06A 接收机：

```text
ESP32 GPIO25 → 控制器原 F-06A CH1 信号
ESP32 GPIO26 → 控制器原 F-06A CH2 信号
ESP32 GND    → 控制器原 F-06A GND
```

不要连接：

```text
控制器接收机 VCC  ×  ESP32 5V/VIN
控制器接收机 VCC  ×  ESP32 3V3
```

ESP32 用 USB 单独供电。ESP32 和控制器只连接两路信号及 GND。

### 7.3 为什么必须共地

PWM 电压必须有共同参考点。如果 ESP32 GND 没有连接控制器 GND，即使万用表能看到某些电压变化，控制器也可能无法可靠识别高低电平。

---

## 8. 安全试车流程

### 8.1 准备

- 架空四个驱动轮，确保车辆不会突然冲出。
- 原 F-06A 接收机必须拔掉。
- 现场人员将手放在小车主电源旁，随时准备断电。
- ESP32 USB 已连接电脑，串口监视器已打开。

### 8.2 上电解锁

1. 先给 ESP32 上电。
2. 等串口出现 `ARMED`。
3. 确认两个输出为 1500 µs。
4. 再打开小车主电源。
5. 保持至少 3 秒，不发送运动命令。
6. 确认车轮不动。

如果小车控制器需要在自身上电后重新检测中立，可按以下顺序重试：

1. ESP32 先上电并保持中立；
2. 等 ESP32 显示 `ARMED`；
3. 再打开小车控制器电源；
4. 再等待 3～5 秒；
5. 才开始发送小行程命令。

### 8.3 小行程测试

串口监视器手动发送一次命令会在 500 ms 后自动停车，因此最适合先检查是否有轻微反应。

依次测试：

```text
S 100 0
S -100 0
S 0 100
S 0 -100
STOP
```

确认方向后，才能逐渐增大到：

```text
S 200 0
S 300 0
```

不得一开始发送 `S 1000 0`。

---

## 9. 使用 Mac 持续发送测试命令

### 9.1 找到 ESP32 串口

在 Mac 终端执行：

```bash
ls /dev/cu.*
```

CH340 常见端口示例：

```text
/dev/cu.usbserial-110
```

### 9.2 安装 pyserial

```bash
python3 -m pip install pyserial
```

### 9.3 持续发送低速前进 2 秒，然后停车

将串口路径改成实际值：

```bash
python3 - <<'PY'
import serial
import time

PORT = "/dev/cu.usbserial-110"

with serial.Serial(PORT, 115200, timeout=0.2) as ser:
    time.sleep(3.5)  # 等待 ESP32 上电中立解锁
    ser.reset_input_buffer()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        ser.write(b"S 100 0\n")
        time.sleep(0.05)  # 20 Hz

    ser.write(b"STOP\n")
    time.sleep(0.2)
    print(ser.read_all().decode(errors="replace"))
PY
```

### 9.4 紧急停止

```bash
python3 - <<'PY'
import serial
with serial.Serial("/dev/cu.usbserial-110", 115200, timeout=0.2) as ser:
    ser.write(b"STOP\n")
PY
```

---

## 10. 方向和端点校准

### 10.1 油门方向相反

如果 `S 100 0` 导致后退，将：

```cpp
constexpr bool REVERSE_THROTTLE = false;
```

改为：

```cpp
constexpr bool REVERSE_THROTTLE = true;
```

重新烧录。

### 10.2 转向方向相反

如果正转向命令方向相反，将：

```cpp
constexpr bool REVERSE_STEERING = false;
```

改为：

```cpp
constexpr bool REVERSE_STEERING = true;
```

不要通过交换 GPIO25 与 GPIO26 解决方向相反。交换通道会把油门和转向功能互换，不是反向设置。

### 10.3 控制器端点不是完整 1000～2000 µs

先从保守范围开始，例如：

```cpp
constexpr int PWM_MIN_US = 1100;
constexpr int PWM_NEUTRAL_US = 1500;
constexpr int PWM_MAX_US = 1900;
```

确认正常后再逐渐扩展。不要在小车落地状态下校准端点。

---

## 11. 常见故障排查

### 11.1 串口回执正确，但小车完全不动

按顺序检查：

1. ESP32 与控制器是否共地。
2. CH1、CH2 是否接在原 F-06A 的信号位置，而不是 VCC。
3. 小车是否在 ESP32 已稳定输出中立后才上电。
4. 控制器是否需要保持中立 3～5 秒解锁。
5. 示波器是否确认周期 20 ms、脉宽 1500/1550 µs。
6. 控制器是否把 CH3 作为“接管/使能”通道。

如果原系统必须切换 DS600 的 CH3 接管键才能行驶，则 ESP32 还需要第三路 PWM 模拟 CH3。可增加：

```cpp
constexpr int PIN_ENABLE = 27;
```

然后让 GPIO27 输出实测的 CH3 使能脉宽。未确认 CH3 行为前，不要盲目固定为 1000 或 2000 µs。

### 11.2 万用表电压变化，但控制器不响应

万用表只能测平均电压。必须进一步确认：

- 周期是否确实为 20 ms；
- 高脉冲是否为 1～2 ms；
- ESP32 GND 是否和控制器 GND 连接；
- 3.3 V 高电平是否达到控制器输入阈值。

如果控制器不能可靠识别 3.3 V，可以在信号线上增加 74HCT125 或 74HCT244，将 ESP32 3.3 V 逻辑转换为 5 V 逻辑。不要直接把 ESP32 GPIO 拉到 5 V。

### 11.3 车轮上电立刻转动

立即断电，然后检查：

- CH1/CH2 是否接反；
- 控制器中立值是否不是 1500 µs；
- F-06A 原系统是否开启了通道微调或反向；
- ESP32 上电时是否先输出了 1500 µs。

必要时测量 F-06A 实际中立脉宽，并将 `PWM_NEUTRAL_US` 改成实测值。

### 11.4 命令执行约半秒后自动停车

这是 500 ms 超时保护正常工作。真实控制程序必须持续以 10～20 Hz 发送运动命令，而不是只发送一次。

---

## 12. 与第二次培训 PPT 的关系

培训 PPT 介绍的控制帧：

```text
0xABCD + 两个 int16 输入 + XOR 校验
```

属于 NUC 与某个底盘控制固件之间的**二进制串口协议示例**。

本指南处理的是另一层：

```text
ESP32 → 模拟 HOTRC F-06A → 两路物理 RC PWM → 小车控制器
```

两者不能混为同一种协议。第一阶段采用易观察的文本串口命令，是为了先可靠验证 ESP32 能否替代 F-06A。验证成功后，可以在 NUC 上增加 ROS2 节点：

```text
/cmd_vel
    ↓
速度与转向限幅
    ↓
持续发送 S <speed> <steering>
    ↓ USB 串口
ESP32
    ↓ 50 Hz RC PWM
小车控制器
```

后续也可以将 ESP32 串口输入升级为 PPT 的 8 字节二进制帧，但这不是首次试车的必要条件。

---

## 13. 最终验收清单

- [ ] Arduino IDE 能识别 ESP32 串口。
- [ ] ESP32 固件编译和烧录成功。
- [ ] 上电后先输出中立，并在 3 秒后返回 `ARMED`。
- [ ] `STATUS` 能返回当前命令和 PWM 脉宽。
- [ ] `S 100 0` 返回 `THROTTLE_US=1550`、`STEERING_US=1500`。
- [ ] 停止发送超过 500 ms 后自动返回中立。
- [ ] GPIO25、GPIO26 的周期均为 20 ms。
- [ ] ESP32 与小车控制器已经共地。
- [ ] 架空车轮时，小幅前进、后退、左右转向均可控制。
- [ ] `STOP` 和通信超时均能可靠停车。
- [ ] 完成架空测试后，再以最低速度进行空旷场地落地测试。
