#!/usr/bin/env python3
"""Bridge ROS GNSS messages to the UE-compatible JSON topic."""

import json
import math
import time

from nmea_msgs.msg import Sentence
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import NavSatFix
from std_msgs.msg import String


QUALITY_NAMES = {
    0: 'NO_FIX',
    1: 'GPS_FIX',
    2: 'DGPS',
    4: 'RTK_FIX',
    5: 'RTK_FLOAT',
    6: 'DEAD_RECKONING',
}


def finite_or_none(value):
    """Return finite floats and represent invalid values as JSON null."""
    return float(value) if math.isfinite(value) else None


def checksum_is_valid(sentence):
    """Validate the checksum of one NMEA sentence."""
    if not sentence.startswith('$') or '*' not in sentence:
        return False
    body, checksum = sentence[1:].split('*', 1)
    calculated = 0
    for character in body:
        calculated ^= ord(character)
    try:
        return calculated == int(checksum[:2], 16)
    except ValueError:
        return False


def parse_gga(sentence):
    """Parse quality fields from a checksum-valid GGA sentence."""
    if not checksum_is_valid(sentence):
        return None
    fields = sentence.split(',')
    if not fields or not fields[0].endswith('GGA') or len(fields) < 15:
        return None
    try:
        quality = int(fields[6] or 0)
        satellites = int(fields[7] or 0)
        hdop = float(fields[8]) if fields[8] else None
        correction_age = float(fields[13]) if fields[13] else None
    except ValueError:
        return None
    station_id = fields[14].split('*', 1)[0] or None
    return {
        'quality': quality,
        'satellites': satellites,
        'hdop': hdop,
        'correction_age_s': correction_age,
        'station_id': station_id,
    }


class RTKBridge(Node):
    """Publish backward-compatible position JSON with RTK quality."""

    def __init__(self):
        super().__init__('rtk_bridge')
        self.timeout_s = self.declare_parameter('timeout_s', 2.5).value
        self.allow_float = self.declare_parameter(
            'allow_float', False
        ).value
        self.fix_message = None
        self.gga = None
        self.fix_received_at = None
        self.gga_received_at = None
        self.last_timeout_publish = 0.0

        self.create_subscription(NavSatFix, '/fix', self.on_fix, 10)
        self.create_subscription(
            Sentence, '/gps/nmea_sentence', self.on_sentence, 10
        )
        self.publisher = self.create_publisher(
            String, '/R2UTopic_Pos', 10
        )
        self.create_timer(0.5, self.check_timeout)
        self.get_logger().info('RTK to UE bridge ready')

    def on_fix(self, message):
        """Store the latest fix and publish a position update."""
        self.fix_message = message
        self.fix_received_at = time.monotonic()
        self.publish_position()

    def on_sentence(self, message):
        """Track GGA quality without taking ownership of the serial port."""
        parsed = parse_gga(message.sentence)
        if parsed is None:
            return
        previous_quality = self.gga['quality'] if self.gga else None
        self.gga = parsed
        self.gga_received_at = time.monotonic()
        if self.fix_message is not None and parsed['quality'] != previous_quality:
            self.publish_position()

    def check_timeout(self):
        """Publish TIMEOUT while input data remains stale."""
        if self.fix_message is None:
            return
        now = time.monotonic()
        if not self.inputs_are_stale(now):
            return
        if now - self.last_timeout_publish >= 1.0:
            self.publish_position()
            self.last_timeout_publish = now

    def inputs_are_stale(self, now=None):
        """Report whether either required GNSS input is stale."""
        now = time.monotonic() if now is None else now
        if self.fix_received_at is None or self.gga_received_at is None:
            return True
        return (
            now - self.fix_received_at > self.timeout_s
            or now - self.gga_received_at > self.timeout_s
        )

    def publish_position(self):
        """Publish one UE-compatible JSON payload."""
        message = self.fix_message
        if message is None:
            return
        now = time.monotonic()
        stale = self.inputs_are_stale(now)
        quality = self.gga['quality'] if self.gga else None

        if stale:
            status_name = 'TIMEOUT'
        elif quality is not None:
            status_name = QUALITY_NAMES.get(quality, f'FIX_{quality}')
        else:
            status_name = (
                'FIX' if message.status.status >= 0 else 'NO_FIX'
            )

        fix_age = (
            now - self.fix_received_at
            if self.fix_received_at is not None
            else None
        )
        stamp = (
            float(message.header.stamp.sec)
            + message.header.stamp.nanosec * 1e-9
        )
        autonomy_allowed = (
            not stale
            and (
                quality == 4
                or (self.allow_float and quality == 5)
            )
        )
        gga = self.gga or {}
        payload = {
            'schema': 'campuscar.position.v1',
            'status': message.status.status,
            'status_name': status_name,
            'latitude': finite_or_none(message.latitude),
            'longitude': finite_or_none(message.longitude),
            'altitude': finite_or_none(message.altitude),
            'timestamp': stamp,
            'frame_id': message.header.frame_id,
            'fix_age_s': round(fix_age, 3) if fix_age is not None else None,
            'rtk': {
                'gga_quality': quality,
                'satellites': gga.get('satellites'),
                'hdop': gga.get('hdop'),
                'correction_age_s': gga.get('correction_age_s'),
                'station_id': gga.get('station_id'),
                'stale': stale,
            },
            'vehicle': {
                'autonomy_allowed': autonomy_allowed,
            },
        }
        output = String()
        output.data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
            allow_nan=False,
        )
        self.publisher.publish(output)


def main(args=None):
    """Run the RTK bridge node."""
    rclpy.init(args=args)
    node = RTKBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
