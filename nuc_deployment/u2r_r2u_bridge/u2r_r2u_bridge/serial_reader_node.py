#!/usr/bin/env python3
"""Publish decoded NMEA sentences from the RTK serial port."""

import time

from nmea_msgs.msg import Sentence
import rclpy
from rclpy.node import Node
import serial


class SerialReader(Node):
    """Own the GNSS serial port and publish complete ASCII sentences."""

    def __init__(self):
        super().__init__('rtk_serial_reader')
        self.port = self.declare_parameter(
            'port',
            '/dev/serial/by-id/'
            'usb-AirM2M_AirM2M_Compo_000000000001-if06',
        ).value
        self.baud = self.declare_parameter('baud', 115200).value
        self.frame_id = self.declare_parameter(
            'frame_id', 'gps_link'
        ).value
        self.reconnect_delay = self.declare_parameter(
            'reconnect_delay_s', 2.0
        ).value

        self.publisher = self.create_publisher(
            Sentence, 'nmea_sentence', 10
        )
        self.serial_port = None
        self.buffer = bytearray()
        self.next_reconnect = 0.0
        self.timer = self.create_timer(0.02, self.poll)

    def connect(self):
        """Open the serial port when the reconnect delay has elapsed."""
        now = time.monotonic()
        if now < self.next_reconnect:
            return False
        try:
            self.serial_port = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=0,
                exclusive=True,
            )
            self.buffer.clear()
            self.get_logger().info(
                f'Connected to {self.port} at {self.baud}'
            )
            return True
        except (OSError, serial.SerialException) as error:
            self.next_reconnect = now + self.reconnect_delay
            self.get_logger().warning(
                f'Cannot open {self.port}: {error}; retrying'
            )
            return False

    def disconnect(self):
        """Close the current serial connection."""
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except (OSError, serial.SerialException):
                pass
        self.serial_port = None
        self.buffer.clear()
        self.next_reconnect = time.monotonic() + self.reconnect_delay

    def publish_line(self, raw_line):
        """Decode and publish one complete NMEA line."""
        line = raw_line.strip(b'\r')
        if not line.startswith(b'$'):
            return
        try:
            text = line.decode('ascii')
        except UnicodeDecodeError:
            self.get_logger().warning('Discarded non-ASCII serial data')
            return

        message = Sentence()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = self.frame_id
        message.sentence = text
        self.publisher.publish(message)

    def poll(self):
        """Read available bytes without blocking the ROS executor."""
        if self.serial_port is None and not self.connect():
            return
        try:
            waiting = self.serial_port.in_waiting
            if waiting <= 0:
                return
            self.buffer.extend(self.serial_port.read(min(waiting, 4096)))
            if len(self.buffer) > 65536:
                self.get_logger().warning('Serial buffer overflow; resetting')
                self.buffer.clear()
                return
            while b'\n' in self.buffer:
                raw_line, _, remainder = self.buffer.partition(b'\n')
                self.buffer = bytearray(remainder)
                self.publish_line(raw_line)
        except (OSError, serial.SerialException) as error:
            self.get_logger().error(f'Serial connection lost: {error}')
            self.disconnect()

    def destroy_node(self):
        """Close the serial device before node shutdown."""
        self.disconnect()
        return super().destroy_node()


def main(args=None):
    """Run the serial reader node."""
    rclpy.init(args=args)
    node = SerialReader()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
