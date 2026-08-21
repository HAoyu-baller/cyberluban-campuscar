/**
 * STM32F103 PWM Bridge - NUC → 小車控制器
 *
 * 接線:
 *   PA0 (TIM2_CH1) → 控制器 CH1 信號 (轉向)
 *   PA1 (TIM2_CH2) → 控制器 CH2 信號 (油門)
 *   GND           → 控制器 GND
 *
 * 串口協議 (115200bps via USART1 - PA9/PA10 or USB VCP):
 *   "S<spd> <steer>\n"  例: "S300 0\n"  PWM=1650us前進
 *   "STOP\n"            緊急停車
 *
 * PWM: 50Hz, 1000-2000us (標準 RC 舵機信號)
 *   中位: 1500us, 前進: 1500→2000, 後退: 1000→1500
 */
#include <Arduino.h>

#define PIN_STEER PA0
#define PIN_THROTTLE PA1

// PWM 參數 (TIM2, 72MHz)
HardwareTimer *pwmTimer = new HardwareTimer(TIM2);

volatile uint32_t steer_pulse = 1500;    // us
volatile uint32_t throttle_pulse = 1500; // us

unsigned long last_cmd_ms = 0;
const unsigned long TIMEOUT_MS = 500; // 500ms 無指令自動停車

void setup() {
  Serial.begin(115200);
  // 如果有 USB VCP, 也用上
  Serial1.begin(115200); // USART1 for potential direct serial

  // TIM2: 50Hz PWM on PA0, PA1
  pwmTimer->setPrescaleFactor(72);     // 72MHz / 72 = 1MHz
  pwmTimer->setOverflow(20000);        // 1MHz / 20000 = 50Hz (20ms周期)
  pwmTimer->setMode(1, TIMER_OUTPUT_COMPARE_PWM1, PIN_STEER);
  pwmTimer->setMode(2, TIMER_OUTPUT_COMPARE_PWM1, PIN_THROTTLE);

  // 初始中立
  pwmTimer->setCaptureCompare(1, 1500); // CH1 1500us
  pwmTimer->setCaptureCompare(2, 1500); // CH2 1500us
  pwmTimer->resume();

  last_cmd_ms = millis();
}

void loop() {
  // 讀 USB 串口
  while (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line == "STOP") {
      steer_pulse = 1500;
      throttle_pulse = 1500;
    }
    else if (line.startsWith("S")) {
      int s1 = line.indexOf(' ');
      if (s1 > 1) {
        int raw_spd = line.substring(1, s1).toInt();
        int raw_str = line.substring(s1 + 1).toInt();
        // raw (-1000~1000) → us (1000~2000)
        throttle_pulse = map(raw_spd, -1000, 1000, 1000, 2000);
        steer_pulse    = map(raw_str, -1000, 1000, 1000, 2000);
        last_cmd_ms = millis();
      }
    }
  }

  // 超時保護
  if (millis() - last_cmd_ms > TIMEOUT_MS) {
    steer_pulse = 1500;
    throttle_pulse = 1500;
  }

  // 輸出 PWM
  pwmTimer->setCaptureCompare(1, steer_pulse);
  pwmTimer->setCaptureCompare(2, throttle_pulse);

  delay(10);
}
