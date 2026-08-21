# Version manifest

Release name: cyberluban-handoff-2026-08-18

Prepared: 2026-08-18, Asia/Shanghai

## Source of truth

- ESP32 protocol: 3
- ESP32 firmware string: 3.0-last-activity-timer
- Web page marker: WEB V4
- NUC service directory: /opt/cyberluban-control
- NUC environment file: /etc/cyberluban-control.env
- systemd unit: cyberluban-control.service
- Web port default: 8000
- Serial baud: 115200

## Key file SHA-256

~~~text
2767F91D1F1972E83CD01CA50983E6F728E7FE7748C6975130CBF115AA75B9B2  code/cyberluban-nuc-control/esp32/esp32_combined_controller_nuc/esp32_combined_controller_nuc.ino
7B9D46F1565CD16CF0FDF7E9F2A48D233AA2851C7ABB1E549DB2F37694327071  code/cyberluban-nuc-control/nuc/app.py
99C4A98AA4C1128FC2827E279F9F5745448B70203058B8B8D16EDE0011FB843F  code/cyberluban-nuc-control/nuc/serial_bridge.py
36E2C13FD15D2570FC7DB8E57D962C830C752EA631C3502A579AFE459A78EA58  code/cyberluban-nuc-control/nuc/static/index.html
EF2830105B311E07B470D35DBEC7C16E602EACB20DFA47D2D139DEACADE08F55  code/cyberluban-nuc-control/nuc/static/app.js
84EB0D811B4B9F102964FB8F060740CAF4D92D9D8056AA8BC29B3041F4BE5B94  code/pc-esp32-car-controller/pc_esp32_car_controller.py
1C1798AD6336E4359A1E54A8F9A29628BB9DDAA9BF360F0A569763005860F147  references/PWM切换组合参数使用说明_厂家.md
~~~

CHECKSUMS.sha256 contains hashes for every file in the final handoff folder except CHECKSUMS.sha256 itself.

## Excluded on purpose

- Python virtual environments and installed third-party packages
- __pycache__ and pyc files
- NUC login password
- CONTROL_TOKEN
- Wi-Fi password
- historical protocol 2 and obsolete ZIP packages

Dependencies are reconstructed from requirements.txt. Secrets remain only on the target NUC and must be rotated after handoff.

## Deployment status

This manifest describes the delivered source package. It does not prove that the same release is currently installed on the physical NUC or flashed to the physical ESP32.

Use tools/verify_nuc_release.sh for the NUC-side files, then stop the service and run scripts/serial_smoke_test.py to verify the ESP32 READY line.
