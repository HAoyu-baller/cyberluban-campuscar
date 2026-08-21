/*
 * CyberLubban ESP32 drive + spray controller for NUC remote control
 *
 * Wiring (same as the verified controller):
 *   GPIO25 -> hoverboard PB10 / PWM CH1 / steering
 *   GPIO26 -> hoverboard PB11 / PWM CH2 / speed
 *   GPIO32 -> optional activation button -> GND
 *   GPIO16 -> high-level-trigger relay IN
 *   ESP32 GND, hoverboard control GND and relay GND must be common.
 *
 * Serial protocol (115200 baud, one ASCII character per command):
 *   w/s/a/d = forward/reverse/left/right heartbeat
 *   m         = stop movement only
 *   k         = spray ON heartbeat
 *   l         = spray OFF
 *   g         = activate PWM manually
 *   x or 0    = EMERGENCY STOP (movement, spray, queued work)
 *   h or ?    = print help
 *
 * Safety:
 *   - Motion stops if w/s/a/d heartbeats disappear for 600 ms.
 *   - Spray stops if k heartbeats disappear for 1500 ms.
 *   - Startup always centers PWM and turns the relay off.
 *   - Emergency stop cancels an activation gesture and all queued movement.
 */

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
constexpr uint8_t GESTURE_BUTTON_PIN = 32;
constexpr uint8_t RELAY_PIN = 16;

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
constexpr uint16_t GESTURE_LOW_US = 1100;
constexpr uint16_t GESTURE_HIGH_US = 1900;

constexpr int16_t DRIVE_POWER = 300;
constexpr uint32_t DRIVE_HEARTBEAT_TIMEOUT_MS = 600;
constexpr uint32_t SPRAY_HEARTBEAT_TIMEOUT_MS = 1500;

// The hoverboard's PWM takeover lasts about 10 seconds. Movement is stopped
// before the takeover closes and the activation gesture is only repeated after
// the old window has safely expired.
constexpr uint32_t TAKEOVER_MOVE_START_LIMIT_MS = 8500;
constexpr uint32_t TAKEOVER_SAFE_REARM_MS = 10500;

struct GestureStage {
  uint16_t steeringUs;
  uint16_t speedUs;
  uint32_t durationMs;
  const char *name;
};

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

bool takeoverCompleted = false;
uint32_t takeoverCompletedMs = 0;
bool activationRequested = false;

bool driveRunning = false;
uint32_t driveDeadlineMs = 0;
char activeDriveCommand = 0;
bool pendingDrive = false;
int16_t pendingSteering = 0;
int16_t pendingSpeed = 0;
char pendingDriveCommand = 0;
uint32_t pendingDriveDeadlineMs = 0;

bool sprayRunning = false;
uint32_t sprayDeadlineMs = 0;

bool buttonStableHigh = true;
bool buttonLastReadHigh = true;
uint32_t buttonChangedMs = 0;

void cancelMovement();

bool timeReached(uint32_t now, uint32_t target) {
  return static_cast<int32_t>(now - target) >= 0;
}

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

void stopDrive() {
  setCenter();
  if (driveRunning) {
    Serial.println("DRIVE STOPPED");
  }
  driveRunning = false;
  activeDriveCommand = 0;
}

void startOrRefreshDrive(int16_t steering, int16_t speed, char command) {
  const bool changed = !driveRunning || activeDriveCommand != command;
  setDrive(steering, speed);
  driveRunning = true;
  activeDriveCommand = command;
  driveDeadlineMs = millis() + DRIVE_HEARTBEAT_TIMEOUT_MS;

  if (changed) {
    Serial.print("DRIVE ");
    Serial.println(command);
  }
}

bool mayStartMovementNow() {
  if (!takeoverCompleted || gestureRunning) {
    return false;
  }
  return !timeReached(millis(),
                      takeoverCompletedMs + TAKEOVER_MOVE_START_LIMIT_MS);
}

bool mayStartGestureNow() {
  return !takeoverCompleted ||
         timeReached(millis(), takeoverCompletedMs + TAKEOVER_SAFE_REARM_MS);
}

void applyGestureStage(size_t index) {
  const GestureStage &stage = GESTURE_STAGES[index];
  writePwmUs(stage.steeringUs, stage.speedUs);
  gestureStageStartedMs = millis();
  Serial.print("GESTURE ");
  Serial.println(stage.name);
}

void startGestureNow() {
  if (gestureRunning) {
    return;
  }
  stopDrive();
  activationRequested = false;
  gestureRunning = true;
  gestureStageIndex = 0;
  Serial.println("GESTURE START");
  applyGestureStage(gestureStageIndex);
}

void requestActivation() {
  if (gestureRunning) {
    return;
  }
  if (mayStartMovementNow()) {
    return;
  }
  activationRequested = true;
  if (mayStartGestureNow()) {
    startGestureNow();
  }
}

void updateGesture() {
  if (!gestureRunning) {
    return;
  }

  const GestureStage &stage = GESTURE_STAGES[gestureStageIndex];
  if (!timeReached(millis(), gestureStageStartedMs + stage.durationMs)) {
    return;
  }

  gestureStageIndex++;
  if (gestureStageIndex < GESTURE_STAGE_COUNT) {
    applyGestureStage(gestureStageIndex);
    return;
  }

  gestureRunning = false;
  setCenter();
  takeoverCompleted = true;
  takeoverCompletedMs = millis();
  Serial.println("GESTURE COMPLETE");

  if (pendingDrive) {
    startOrRefreshDrive(pendingSteering, pendingSpeed, pendingDriveCommand);
    pendingDrive = false;
  }
}

void queueOrRefreshDrive(int16_t steering, int16_t speed, char command) {
  if (mayStartMovementNow()) {
    pendingDrive = false;
    startOrRefreshDrive(steering, speed, command);
    return;
  }

  pendingDrive = true;
  pendingSteering = steering;
  pendingSpeed = speed;
  pendingDriveCommand = command;
  pendingDriveDeadlineMs = millis() + DRIVE_HEARTBEAT_TIMEOUT_MS;
  requestActivation();
}

void updateActivationRequest() {
  if (activationRequested && !gestureRunning && mayStartGestureNow()) {
    startGestureNow();
  }
}

void updateDrive() {
  if (pendingDrive && timeReached(millis(), pendingDriveDeadlineMs)) {
    cancelMovement();
    Serial.println("SAFETY PENDING DRIVE HEARTBEAT TIMEOUT");
    return;
  }
  if (driveRunning && timeReached(millis(), driveDeadlineMs)) {
    stopDrive();
    Serial.println("SAFETY DRIVE HEARTBEAT TIMEOUT");
  }
}

void startOrRefreshSpray() {
  if (!sprayRunning) {
    digitalWrite(RELAY_PIN, HIGH);
    sprayRunning = true;
    Serial.println("SPRAY ON");
  }
  sprayDeadlineMs = millis() + SPRAY_HEARTBEAT_TIMEOUT_MS;
}

void stopSpray() {
  digitalWrite(RELAY_PIN, LOW);
  if (sprayRunning) {
    Serial.println("SPRAY OFF");
  }
  sprayRunning = false;
}

void updateSpray() {
  if (sprayRunning && timeReached(millis(), sprayDeadlineMs)) {
    stopSpray();
    Serial.println("SAFETY SPRAY HEARTBEAT TIMEOUT");
  }
}

void cancelMovement() {
  pendingDrive = false;
  activationRequested = false;
  if (gestureRunning) {
    gestureRunning = false;
    takeoverCompleted = false;
  }
  stopDrive();
}

void emergencyStop() {
  cancelMovement();
  stopSpray();
  Serial.println("EMERGENCY STOP");
}

void printHelp() {
  Serial.println("READY protocol=2 baud=115200");
  Serial.println("w/s/a/d=drive heartbeat, m=movement stop");
  Serial.println("k=spray heartbeat, l=spray off, g=activate");
  Serial.println("x/0=emergency stop, h/?=help");
}

void handleSerialCommand(char command) {
  if (command == '\r' || command == '\n' || command == ' ' ||
      command == '\t') {
    return;
  }

  switch (command) {
    case 'w':
    case 'W':
      queueOrRefreshDrive(0, DRIVE_POWER, 'w');
      break;
    case 's':
    case 'S':
      queueOrRefreshDrive(0, -DRIVE_POWER, 's');
      break;
    case 'a':
    case 'A':
      queueOrRefreshDrive(-DRIVE_POWER, 0, 'a');
      break;
    case 'd':
    case 'D':
      queueOrRefreshDrive(DRIVE_POWER, 0, 'd');
      break;
    case 'm':
    case 'M':
      cancelMovement();
      break;
    case 'k':
    case 'K':
      startOrRefreshSpray();
      break;
    case 'l':
    case 'L':
      stopSpray();
      break;
    case 'g':
    case 'G':
      requestActivation();
      break;
    case '0':
    case 'x':
    case 'X':
      emergencyStop();
      break;
    case 'h':
    case 'H':
    case '?':
      printHelp();
      break;
    default:
      Serial.print("UNKNOWN ");
      Serial.println(command);
      break;
  }
}

void updateGestureButton() {
  const bool readHigh = digitalRead(GESTURE_BUTTON_PIN) == HIGH;
  if (readHigh != buttonLastReadHigh) {
    buttonLastReadHigh = readHigh;
    buttonChangedMs = millis();
  }

  if (!timeReached(millis(), buttonChangedMs + 30) ||
      readHigh == buttonStableHigh) {
    return;
  }

  buttonStableHigh = readHigh;
  if (!buttonStableHigh) {
    requestActivation();
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(GESTURE_BUTTON_PIN, INPUT_PULLUP);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);

  if (!attachPwmOutputs()) {
    Serial.println("FATAL LEDC ATTACH FAILED");
    while (true) {
      digitalWrite(RELAY_PIN, LOW);
      delay(1000);
    }
  }

  setCenter();
  delay(500);
  printHelp();
}

void loop() {
  updateGesture();
  updateActivationRequest();
  updateDrive();
  updateSpray();
  updateGestureButton();

  while (Serial.available() > 0) {
    handleSerialCommand(static_cast<char>(Serial.read()));
  }

  delay(1);
}
