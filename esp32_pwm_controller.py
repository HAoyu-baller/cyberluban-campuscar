#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP32 PWM 桥串口控制程序

本程序运行在 Mac/NUC/PC 上，不运行在 ESP32 内部。
它通过 USB 串口向已经烧录了 ESP32 PWM 桥固件的开发板发送命令：

    S <speed> <steering>\n
例如：

    S 100 0\n       # 低速直行
    S 0 100\n       # 小幅转向
    STOP\n         # 回到中位
    STATUS\n       # 查询状态

重要安全说明：
1. 本程序不会自动执行“最大油门→最小油门”的通用校准序列。
   ESC 校准必须按照具体 ESC 型号的说明书单独完成。
2. 本程序假设 ESP32 使用的是“上电保持中位约 3 秒”的修正版固件。
3. 如果检测到旧版 AUTO_ACTIVATE 全行程启动序列，程序会拒绝发送运动命令。
4. ESP32 的 COMMAND_TIMEOUT_MS 应为 500 ms；本程序默认以 20 Hz 发送命令。
5. 第一次测试必须架空驱动轮，并确保可以立即切断小车主电源。

依赖：
    python3 -m pip install pyserial
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from typing import Iterable, Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # 允许 --help 仍然可以运行
    serial = None
    list_ports = None


DEFAULT_BAUD = 115200
DEFAULT_RATE_HZ = 20.0
DEFAULT_ARM_TIMEOUT_S = 8.0
DEFAULT_BOOT_WAIT_S = 0.3
DEFAULT_COMMAND_TIMEOUT_MS = 500
MIN_COMMAND_RATE_HZ = 5.0
MAX_COMMAND_RATE_HZ = 50.0


class ControllerError(RuntimeError):
    """可向操作者直接报告的控制器错误。"""



def require_pyserial() -> None:
    if serial is None or list_ports is None:
        raise ControllerError(
            "未安装 pyserial。请先执行：python3 -m pip install pyserial"
        )



def available_ports() -> list[object]:
    require_pyserial()
    return sorted(list_ports.comports(), key=lambda item: item.device)



def print_ports() -> None:
    ports = available_ports()
    if not ports:
        print("没有发现串口设备。请检查 USB 数据线、驱动和 ESP32 是否已连接。")
        return

    print("可用串口：")
    for item in ports:
        description = item.description or "未知设备"
        manufacturer = f" / {item.manufacturer}" if item.manufacturer else ""
        print(f"  {item.device}\t{description}{manufacturer}")



def choose_port(requested: Optional[str]) -> str:
    if requested:
        return requested

    ports = available_ports()
    if len(ports) == 1:
        selected = ports[0].device
        print(f"自动选择串口：{selected}")
        return selected

    if not ports:
        raise ControllerError(
            "没有发现串口设备。请使用 --list-ports 查看设备，"
            "或通过 --port 指定 ESP32 串口。"
        )

    lines = ["发现多个串口，无法安全自动选择："]
    lines.extend(f"  {item.device} ({item.description})" for item in ports)
    lines.append("请使用 --port 明确指定 ESP32，例如 --port /dev/cu.usbserial-110")
    raise ControllerError("\n".join(lines))


class SerialSession:
    """封装串口收发，并持续观察 ESP32 的安全状态。"""

    def __init__(self, port: str, baud: int, quiet: bool = False) -> None:
        require_pyserial()
        self.port = port
        self.quiet = quiet
        self._rx_buffer = bytearray()
        self.legacy_activation_detected = False
        self.ready_detected = False
        self.last_error_lines: list[str] = []

        try:
            # 打开 USB 串口时部分 ESP32 开发板会自动复位，
            # 因此后续必须重新等待固件的中位保持阶段。
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=0,
                write_timeout=0.5,
                dsrdtr=False,
                rtscts=False,
            )
        except Exception as exc:
            raise ControllerError(f"无法打开串口 {port}: {exc}") from exc

    def __enter__(self) -> "SerialSession":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "ser", None) is not None and self.ser.is_open:
            self.ser.close()

    def send(self, line: str) -> None:
        if not self.ser.is_open:
            raise ControllerError("串口已经关闭")

        clean_line = line.strip()
        if not clean_line:
            return

        try:
            self.ser.write((clean_line + "\n").encode("ascii"))
            self.ser.flush()
        except Exception as exc:
            raise ControllerError(f"发送串口命令失败 {clean_line!r}: {exc}") from exc

    def poll(self) -> list[str]:
        """读取当前已经到达的完整串口行，不阻塞主控制循环。"""
        if not self.ser.is_open:
            return []

        try:
            waiting = self.ser.in_waiting
            if waiting:
                self._rx_buffer.extend(self.ser.read(waiting))
        except Exception as exc:
            raise ControllerError(f"读取串口失败：{exc}") from exc

        lines: list[str] = []
        while True:
            newline_index = self._rx_buffer.find(b"\n")
            if newline_index < 0:
                break

            raw_line = bytes(self._rx_buffer[:newline_index])
            del self._rx_buffer[: newline_index + 1]
            line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace").strip()
            if not line:
                continue

            lines.append(line)
            self._observe(line)

        return lines

    def read_for(self, seconds: float) -> list[str]:
        deadline = time.monotonic() + max(0.0, seconds)
        lines: list[str] = []
        while time.monotonic() < deadline:
            lines.extend(self.poll())
            time.sleep(0.01)
        lines.extend(self.poll())
        return lines

    def _observe(self, line: str) -> None:
        # 这些标记属于用户最开始的旧版代码：开机自动发送前进、后退、
        # 左转、右转。检测到它时，不能把后续的 ARMED 当成安全解锁。
        legacy_markers = (
            "PWM_BRIDGE_AUTO_ACTIVATION",
            "ACTIVATION START",
            "ACTIVATION FORWARD",
            "ACTIVATION REVERSE",
        )
        if any(marker in line for marker in legacy_markers):
            if not self.legacy_activation_detected:
                print(
                    "[安全停止] 检测到旧版自动全行程激活固件。"
                    "请先烧录‘只保持中位’的修正版 ESP32 固件。"
                )
            self.legacy_activation_detected = True

        if (
            line == "ARMED"
            or "ARMING COMPLETE" in line
            or "READY SOFTWARE_ONLY" in line
            or "ARMED=1" in line
        ):
            self.ready_detected = True

        if line.startswith("ERR"):
            self.last_error_lines.append(line)

        # 运动命令每次都会返回 OK；quiet 模式只隐藏重复的速度回执，
        # 仍然显示启动信息、错误和停车回执。
        repetitive_ok = line.startswith("OK SPEED=")
        if not (self.quiet and repetitive_ok):
            print(f"< ESP32 {line}")

    def clear_errors(self) -> None:
        self.last_error_lines.clear()



def send_safe_stop(session: SerialSession, repeats: int = 3) -> None:
    """尽力重复发送 STOP；失败时不掩盖原始错误。"""
    for index in range(max(1, repeats)):
        try:
            session.send("STOP")
        except ControllerError as exc:
            if index == 0:
                print(f"[警告] STOP 发送失败：{exc}", file=sys.stderr)
            break
        time.sleep(0.05)

    try:
        session.read_for(0.15)
    except ControllerError as exc:
        print(f"[警告] 读取 STOP 回执失败：{exc}", file=sys.stderr)



def is_ready_line(line: str) -> bool:
    return (
        line == "ARMED"
        or "ARMING COMPLETE" in line
        or "READY SOFTWARE_ONLY" in line
        or "ARMED=1" in line
    )



def wait_until_ready(session: SerialSession, timeout_s: float) -> None:
    """等待修正版固件完成中位保持；期间绝不发送运动命令。"""
    print(f"等待 ESP32 安全中位启动，最长 {timeout_s:.1f} 秒……")

    # 先读取可能已经到达的启动日志，再发送 STOP。
    session.poll()
    if session.legacy_activation_detected:
        raise ControllerError(
            "当前 ESP32 仍在执行旧版自动激活序列；"
            "程序未发送任何运动命令，请断电并先更新 ESP32 固件。"
        )

    session.send("STOP")
    deadline = time.monotonic() + timeout_s
    next_status_time = time.monotonic() + 0.5

    while time.monotonic() < deadline:
        for line in session.poll():
            if session.legacy_activation_detected:
                raise ControllerError(
                    "检测到旧版自动全行程激活序列；"
                    "程序拒绝继续发送运动命令。"
                )
            if is_ready_line(line):
                session.ready_detected = True

        if session.ready_detected:
            print("已检测到 ESP32 软件就绪；现在才允许发送运动命令。")
            return

        # 如果启动日志已经错过，STATUS 仍可通过 ARMED=1 判断状态。
        now = time.monotonic()
        if now >= next_status_time:
            session.send("STATUS")
            # STATUS 回执可能是唯一能看到的 ARMED=1 证据；
            # 立即读取并更新 ready_detected，避免等待多余一轮。
            for status_line in session.read_for(0.05):
                if session.legacy_activation_detected:
                    raise ControllerError(
                        "检测到旧版自动全行程激活序列；"
                        "程序拒绝继续发送运动命令。"
                    )
                if is_ready_line(status_line):
                    session.ready_detected = True
            if session.ready_detected:
                print("已检测到 ESP32 软件就绪；现在才允许发送运动命令。")
                return
            next_status_time = now + 0.5

        time.sleep(0.01)

    raise ControllerError(
        "等待 ESP32 就绪超时。请确认已烧录‘中位启动’固件，"
        "并检查串口、供电和波特率。程序未发送运动命令。"
    )



def validate_motion(speed: int, steering: int, duration_s: float, rate_hz: float) -> None:
    if not -1000 <= speed <= 1000:
        raise ControllerError("speed 必须在 -1000..1000 范围内")
    if not -1000 <= steering <= 1000:
        raise ControllerError("steering 必须在 -1000..1000 范围内")
    if duration_s <= 0:
        raise ControllerError("duration 必须大于 0 秒")
    if rate_hz < MIN_COMMAND_RATE_HZ or rate_hz > MAX_COMMAND_RATE_HZ:
        raise ControllerError(
            f"rate 必须在 {MIN_COMMAND_RATE_HZ:g}..{MAX_COMMAND_RATE_HZ:g} Hz 内；"
            f"ESP32 超时保护为 {DEFAULT_COMMAND_TIMEOUT_MS} ms"
        )



def confirm_motion(speed: int, steering: int, duration_s: float, assume_yes: bool) -> None:
    print("\n即将发送运动命令：")
    print(f"  speed={speed}, steering={steering}, duration={duration_s:g}s")
    print("  安全要求：驱动轮架空，现场可以立即切断主电源。")

    if assume_yes:
        return

    if not sys.stdin.isatty():
        raise ControllerError("非交互终端必须显式添加 --yes 才能执行运动命令")

    answer = input("确认测试条件安全？请输入大写 YES，其他输入取消：").strip()
    if answer != "YES":
        raise ControllerError("已取消运动命令")



def stream_motion(
    session: SerialSession,
    speed: int,
    steering: int,
    duration_s: float,
    rate_hz: float,
) -> int:
    """以固定频率发送运动命令，结束后由调用者负责 STOP。"""
    validate_motion(speed, steering, duration_s, rate_hz)
    session.clear_errors()

    period_s = 1.0 / rate_hz
    deadline = time.monotonic() + duration_s
    next_send = time.monotonic()
    sent_count = 0

    print(f"开始发送，频率 {rate_hz:g} Hz；按 Ctrl-C 可立即进入停车流程。")

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now < next_send:
            time.sleep(min(next_send - now, 0.01))
            continue

        session.send(f"S {speed} {steering}")
        sent_count += 1
        response_lines = session.poll()
        if any(line.startswith("ERR") for line in response_lines):
            raise ControllerError(
                "ESP32 拒绝运动命令：" + "; ".join(session.last_error_lines[-3:])
            )

        next_send += period_s
        # 如果系统调度暂时延迟，避免连续追赶造成突发命令。
        if next_send < time.monotonic() - period_s:
            next_send = time.monotonic() + period_s

    return sent_count



def open_session(args: argparse.Namespace) -> SerialSession:
    port = choose_port(args.port)
    session = SerialSession(port, args.baud, quiet=args.quiet)
    if args.boot_wait_s > 0:
        time.sleep(args.boot_wait_s)
    session.poll()
    return session



def run_motion(args: argparse.Namespace, speed: int, steering: int, duration_s: float) -> int:
    validate_motion(speed, steering, duration_s, args.rate_hz)
    confirm_motion(speed, steering, duration_s, args.yes)

    session: Optional[SerialSession] = None
    try:
        session = open_session(args)
        wait_until_ready(session, args.arm_timeout_s)
        count = stream_motion(
            session,
            speed,
            steering,
            duration_s,
            args.rate_hz,
        )
        print(f"运动阶段完成，共发送 {count} 条命令。")
        return 0
    except KeyboardInterrupt:
        print("\n检测到 Ctrl-C，进入停车流程。", file=sys.stderr)
        return 130
    finally:
        if session is not None:
            send_safe_stop(session)
            session.close()
            print("已发送 STOP，并关闭串口。")



def run_status(args: argparse.Namespace) -> int:
    session: Optional[SerialSession] = None
    try:
        session = open_session(args)
        session.send("STATUS")
        session.read_for(0.6)
        return 0
    finally:
        if session is not None:
            session.close()



def run_stop(args: argparse.Namespace) -> int:
    session: Optional[SerialSession] = None
    try:
        session = open_session(args)
        send_safe_stop(session)
        print("停车命令发送完成。")
        return 0
    finally:
        if session is not None:
            session.close()



def parse_interactive_motion(parts: list[str]) -> tuple[int, int, float]:
    if len(parts) != 4:
        raise ControllerError("格式：drive <speed> <steering> <seconds>")

    try:
        speed = int(parts[1])
        steering = int(parts[2])
        duration_s = float(parts[3])
    except ValueError as exc:
        raise ControllerError("speed、steering、seconds 必须是数字") from exc

    return speed, steering, duration_s



def print_interactive_help() -> None:
    print(
        "\n可用命令：\n"
        "  drive <speed> <steering> <seconds>  持续发送运动命令\n"
        "  stop                                发送 STOP\n"
        "  status                              查询 ESP32 状态\n"
        "  help                                显示帮助\n"
        "  quit                                停车并退出\n\n"
        "示例：drive 100 0 2\n"
        "运动命令范围：speed、steering 均为 -1000..1000。"
    )



def run_interactive(args: argparse.Namespace) -> int:
    session: Optional[SerialSession] = None
    try:
        session = open_session(args)
        wait_until_ready(session, args.arm_timeout_s)
        print_interactive_help()

        while True:
            try:
                raw_line = input("esp32> ")
            except EOFError:
                print("\n收到 EOF，进入停车流程。")
                break
            except KeyboardInterrupt:
                print("\n进入停车流程。")
                break

            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                parts = shlex.split(raw_line)
                command = parts[0].lower()

                if command in {"quit", "exit", "q"}:
                    break

                if command == "help":
                    print_interactive_help()
                    continue

                if command == "stop":
                    send_safe_stop(session)
                    continue

                if command == "status":
                    session.send("STATUS")
                    session.read_for(0.5)
                    continue

                if command == "drive":
                    speed, steering, duration_s = parse_interactive_motion(parts)
                    validate_motion(speed, steering, duration_s, args.rate_hz)
                    confirm_motion(speed, steering, duration_s, args.yes)
                    try:
                        count = stream_motion(
                            session,
                            speed,
                            steering,
                            duration_s,
                            args.rate_hz,
                        )
                        print(f"运动阶段完成，共发送 {count} 条命令。")
                    finally:
                        # 每个运动命令结束、报错或 Ctrl-C 后都停车。
                        send_safe_stop(session)
                    continue

                print("未知命令，请输入 help。")
            except KeyboardInterrupt:
                print("\n当前运动中断，进入停车流程。")
                send_safe_stop(session)
            except (ControllerError, ValueError) as exc:
                print(f"[错误] {exc}", file=sys.stderr)

        return 0
    finally:
        if session is not None:
            send_safe_stop(session)
            session.close()
            print("已发送 STOP，并关闭串口。")



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过 USB 串口安全控制 ESP32 两路 RC PWM。",
        epilog=(
            "示例：\n"
            "  python3 esp32_pwm_controller.py --list-ports\n"
            "  python3 esp32_pwm_controller.py --port /dev/cu.usbserial-110 --status\n"
            "  python3 esp32_pwm_controller.py --port /dev/cu.usbserial-110 --stop\n"
            "  python3 esp32_pwm_controller.py --port /dev/cu.usbserial-110 "
            "--speed 100 --steering 0 --duration 2 --yes\n"
            "  python3 esp32_pwm_controller.py --port /dev/cu.usbserial-110 --interactive"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", help="ESP32 串口；不填时仅在恰好一个串口时自动选择")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="波特率，默认 115200")
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="列出可用串口后退出",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="查询一次 ESP32 状态",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="发送停车命令并退出",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="进入交互模式；不输入 drive 命令时不会运动",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="以 speed=100、steering=0 运行 2 秒；仍需安全确认",
    )
    parser.add_argument("--speed", type=int, help="运动速度命令 -1000..1000")
    parser.add_argument("--steering", type=int, help="转向命令 -1000..1000")
    parser.add_argument("--duration", type=float, help="运动持续时间（秒）")
    parser.add_argument(
        "--rate-hz",
        type=float,
        default=DEFAULT_RATE_HZ,
        help="运动命令发送频率，默认 20 Hz",
    )
    parser.add_argument(
        "--arm-timeout-s",
        type=float,
        default=DEFAULT_ARM_TIMEOUT_S,
        help="等待 ESP32 软件就绪的最长时间，默认 8 秒",
    )
    parser.add_argument(
        "--boot-wait-s",
        type=float,
        default=DEFAULT_BOOT_WAIT_S,
        help="打开串口后等待启动日志的时间，默认 0.3 秒",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过运动前安全确认；仅适合已确认测试条件的自动化调用",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="隐藏重复的运动 OK 回执，但保留错误和安全日志",
    )
    return parser



def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list_ports:
        try:
            print_ports()
            return 0
        except ControllerError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2

    selected_modes = sum(
        bool(value) for value in (args.status, args.stop, args.interactive, args.demo)
    )
    has_motion_values = any(
        value is not None for value in (args.speed, args.steering, args.duration)
    )

    if selected_modes > 1:
        parser.error("--status、--stop、--interactive、--demo 只能选择一个")

    if args.demo and has_motion_values:
        parser.error("--demo 不能和 --speed、--steering、--duration 同时使用")

    if not args.demo and has_motion_values:
        if args.speed is None or args.steering is None or args.duration is None:
            parser.error("运动模式必须同时提供 --speed、--steering 和 --duration")
        selected_modes += 1

    if selected_modes > 1:
        parser.error("运动参数不能和其他模式同时使用")

    try:
        if args.status:
            return run_status(args)
        if args.stop:
            return run_stop(args)
        if args.demo:
            return run_motion(args, speed=100, steering=0, duration_s=2.0)
        if has_motion_values:
            # 前面的校验保证这三个值都不为 None。
            return run_motion(args, args.speed, args.steering, args.duration)

        # 没有指定一次性动作时，默认进入交互模式；启动阶段仍只保持中位。
        return run_interactive(args)
    except ControllerError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
