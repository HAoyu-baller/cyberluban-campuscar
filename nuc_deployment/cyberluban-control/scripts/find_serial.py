from serial.tools import list_ports


ports = list(list_ports.comports())
if not ports:
    print("No serial ports found.")
else:
    for port in ports:
        print(
            f"{port.device}\n"
            f"  description: {port.description}\n"
            f"  manufacturer: {port.manufacturer}\n"
            f"  VID:PID: {port.vid!s}:{port.pid!s}"
        )
