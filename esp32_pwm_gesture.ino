// ESP32 dual RC-PWM generator for the current hoverboard firmware.
//
// Wiring (current VARIANT_USART + CONTROL_PWM_RIGHT firmware):
//   ESP32 GPIO25 -> hoverboard PB10 / PWM CH1 / steering
//   ESP32 GPIO26 -> hoverboard PB11 / PWM CH2 / speed
//   ESP32 GND    -> hoverboard GND
//
// Do not connect the hoverboard connector's 12/15 V rail directly to ESP32.
// Power the ESP32 through a suitable regulator and always connect grounds.
// Test with the drive wheels raised off the ground.

#include <Arduino.h>

#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif

#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 2
#endif

namespace {

constexpr uint8_t STEERING_PIN = 25;
constexpr uint8_t SPEED_PIN = 26;
constexpr uint8_t GESTURE_BUTTON_PIN = 32;  // Optional button from GPIO32 to GND.

constexpr uint32_t PWM_FREQUENCY_HZ = 50;
constexpr uint8_t PWM_RESOLUTION_BITS = 16;
constexpr uint32_t PWM_PERIOD_US = 1000000UL / PWM_FREQUENCY_HZ;
constexpr uint32_t PWM_MAX_DUTY = (1UL << PWM_RESOLUTION_BITS) - 1UL;

#if ESP_ARDUINO_VERSION_MAJOR < 3
constexpr uint8_t STEERING_LEDC_CHANNEL = 0;
constexpr uint8_t SPEED_LEDC_CHANNEL = 1;
#endif

constexpr uint16_t PWM_MIN_US = 1000;
constexpr uint16_t PWM_CENTER_US = 1500;
constexpr uint16_t PWM_MAX_US = 2000;

// Firmware gesture thresholds are strict: up/right > 1800 us,
// down/left < 1200 us. These values leave useful margin.
constexpr uint16_t GESTURE_LOW_US = 1100;
constexpr uint16_t GESTURE_HIGH_US = 1900;

struct GestureStage {
  uint16_t steeringUs;
  uint16_t speedUs;
  uint32_t durationMs;
  const char *name;
};

// Center is inserted between directions for a clear and reproducible gesture.
// Each next direction arrives well inside the firmware's approximately 2 s
// per-step timeout. The final RIGHT pulse is deliberately short because PWM
// control becomes active as soon as that step is recognized.
constexpr GestureStage GESTURE_STAGES[] = {
    {PWM_CENTER_US, PWM_CENTER_US, 400, "ARM CENTER"},
    {PWM_CENTER_US, GESTURE_HIGH_US, 120, "UP"},
    {PWM_CENTER_US, PWM_CENTER_US, 100, "CENTER"},
    {PWM_CENTER_US, GESTURE_LOW_US, 120, "DOWN"},
    {PWM_CENTER_US, PWM_CENTER_US, 100, "CENTER"},
    {GESTURE_LOW_US, PWM_CENTER_US, 120, "LEFT"},
    {PWM_CENTER_US, PWM_CENTER_US, 100, "CENTER"},
    {GESTURE_HIGH_US, PWM_CENTER_US, 60, "RIGHT"},
    {PWM_CENTER_US, PWM_CENTER_US, 100, "FINAL CENTER"},
};

constexpr size_t GESTURE_STAGE_COUNT =
    sizeof(GESTURE_STAGES) / sizeof(GESTURE_STAGES[0]);

bool gestureRunning = false;
size_t gestureStageIndex = 0;
uint32_t gestureStageStartedMs = 0;

bool buttonStableHigh = true;
bool buttonLastReadHigh = true;
uint32_t buttonChangedMs = 0;

uint32_t pulseUsToDuty(uint16_t pulseUs) {
  pulseUs = constrain(pulseUs, PWM_MIN_US, PWM_MAX_US);
  return (static_cast<uint32_t>(pulseUs) * PWM_MAX_DUTY + PWM_PERIOD_US / 2) /
         PWM_PERIOD_US;
}

void writePulseUs(uint8_t pin, uint8_t legacyChannel, uint16_t pulseUs) {
  const uint32_t duty = pulseUsToDuty(pulseUs);
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  (void)legacyChannel;
  ledcWrite(pin, duty);
#else
  (void)pin;
  ledcWrite(legacyChannel, duty);
#endif
}

void writePwmUs(uint16_t steeringUs, uint16_t speedUs) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  writePulseUs(STEERING_PIN, 0, steeringUs);
  writePulseUs(SPEED_PIN, 0, speedUs);
#else
  writePulseUs(STEERING_PIN, STEERING_LEDC_CHANNEL, steeringUs);
  writePulseUs(SPEED_PIN, SPEED_LEDC_CHANNEL, speedUs);
#endif
}

// Normalized commands use the same convention as the firmware:
// -1000 -> 1000 us, 0 -> 1500 us, +1000 -> 2000 us.
void setDrive(int16_t steering, int16_t speed) {
  steering = constrain(steering, -1000, 1000);
  speed = constrain(speed, -1000, 1000);
  writePwmUs(PWM_CENTER_US + steering / 2, PWM_CENTER_US + speed / 2);
}

void setCenter() {
  setDrive(0, 0);
}

bool attachPwmOutputs() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  const bool steeringOk =
      ledcAttach(STEERING_PIN, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS);
  const bool speedOk =
      ledcAttach(SPEED_PIN, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS);
  return steeringOk && speedOk;
#else
  ledcSetup(STEERING_LEDC_CHANNEL, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS);
  ledcSetup(SPEED_LEDC_CHANNEL, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS);
  ledcAttachPin(STEERING_PIN, STEERING_LEDC_CHANNEL);
  ledcAttachPin(SPEED_PIN, SPEED_LEDC_CHANNEL);
  return true;
#endif
}

void applyGestureStage(size_t index) {
  const GestureStage &stage = GESTURE_STAGES[index];
  writePwmUs(stage.steeringUs, stage.speedUs);
  gestureStageStartedMs = millis();

  Serial.print("Gesture: ");
  Serial.print(stage.name);
  Serial.print("  CH1=");
  Serial.print(stage.steeringUs);
  Serial.print(" us, CH2=");
  Serial.print(stage.speedUs);
  Serial.println(" us");
}

void startGesture() {
  if (gestureRunning) {
    return;
  }

  gestureRunning = true;
  gestureStageIndex = 0;
  Serial.println("Starting PWM takeover gesture...");
  applyGestureStage(gestureStageIndex);
}

void updateGesture() {
  if (!gestureRunning) {
    return;
  }

  const GestureStage &stage = GESTURE_STAGES[gestureStageIndex];
  if (millis() - gestureStageStartedMs < stage.durationMs) {
    return;
  }

  gestureStageIndex++;
  if (gestureStageIndex < GESTURE_STAGE_COUNT) {
    applyGestureStage(gestureStageIndex);
    return;
  }

  gestureRunning = false;
  setCenter();
  Serial.println("Gesture complete; PWM takeover should now be active.");
  Serial.println("Send a drive command within 10 s or firmware returns to USART.");
}

void cancelGestureAndStop() {
  gestureRunning = false;
  setCenter();
  Serial.println("PWM centered. Firmware returns to USART after about 10 s.");
}

void handleSerialCommand(char command) {
  if (command == '\r' || command == '\n') {
    return;
  }

  switch (command) {
    case 'g':
    case 'G':
      startGesture();
      break;
    case '0':
    case 'x':
    case 'X':
      cancelGestureAndStop();
      break;
    case 'w':
    case 'W':
      if (!gestureRunning) setDrive(0, 300);
      break;
    case 's':
    case 'S':
      if (!gestureRunning) setDrive(0, -300);
      break;
    case 'a':
    case 'A':
      if (!gestureRunning) setDrive(-300, 0);
      break;
    case 'd':
    case 'D':
      if (!gestureRunning) setDrive(300, 0);
      break;
    default:
      Serial.println("Commands: g=gesture, w/s/a/d=drive, x or 0=center");
      break;
  }
}

void updateGestureButton() {
  const bool readHigh = digitalRead(GESTURE_BUTTON_PIN) == HIGH;
  if (readHigh != buttonLastReadHigh) {
    buttonLastReadHigh = readHigh;
    buttonChangedMs = millis();
  }

  if (millis() - buttonChangedMs < 30 || readHigh == buttonStableHigh) {
    return;
  }

  buttonStableHigh = readHigh;
  if (!buttonStableHigh) {
    startGesture();
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(GESTURE_BUTTON_PIN, INPUT_PULLUP);

  if (!attachPwmOutputs()) {
    Serial.println("Failed to attach ESP32 LEDC outputs.");
    while (true) {
      delay(1000);
    }
  }

  // Always begin at center. This also lets the firmware validate both channels
  // and arm its gesture detector without requesting any motor movement.
  setCenter();
  delay(500);

  Serial.println("ESP32 hoverboard PWM generator ready.");
  Serial.println("Press GPIO32 button or send 'g' to request PWM takeover.");
  Serial.println("Commands: g=gesture, w/s/a/d=drive, x or 0=center");
}

void loop() {
  updateGesture();
  updateGestureButton();

  while (Serial.available() > 0) {
    handleSerialCommand(static_cast<char>(Serial.read()));
  }

  delay(1);
}
