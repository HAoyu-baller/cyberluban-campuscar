#!/usr/bin/env python3
"""Publish deterministic RTK state scenarios for UE integration tests."""

import json
import math

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


SCENARIOS = {
    'NO_FIX': {
        'status': -1,
        'status_name': 'NO_FIX',
        'latitude': None,
        'longitude': None,
        'altitude': None,
        'quality': 0,
        'satellites': 0,
        'hdop': None,
        'correction_age_s': None,
        'stale': False,
        'autonomy_allowed': False,
    },
    'RTK_FLOAT': {
        'status': 0,
        'status_name': 'RTK_FLOAT',
        'latitude': 30.000000,
        'longitude': 120.000000,
        'altitude': 10.0,
        'quality': 5,
        'satellites': 18,
        'hdop': 0.8,
        'correction_age_s': 1.0,
        'stale': False,
        'autonomy_allowed': False,
    },
    'RTK_FIX': {
        'status': 0,
        'status_name': 'RTK_FIX',
        'latitude': 30.000000,
        'longitude': 120.000000,
        'altitude': 10.0,
        'quality': 4,
        'satellites': 20,
        'hdop': 0.7,
        'correction_age_s': 1.0,
        'stale': False,
        'autonomy_allowed': True,
    },
    'TIMEOUT': {
        'status': -1,
        'status_name': 'TIMEOUT',
        'latitude': None,
        'longitude': None,
        'altitude': None,
        'quality': None,
        'satellites': None,
        'hdop': None,
        'correction_age_s': None,
        'stale': True,
        'autonomy_allowed': False,
    },
}


class PositionSimulator(Node):
    """Publish one selected position state without touching RTK hardware."""

    def __init__(self):
        super().__init__('ue_position_simulator')
        requested = self.declare_parameter(
            'scenario', 'RTK_FIX'
        ).value.upper()
        if requested not in SCENARIOS:
            choices = ', '.join(sorted(SCENARIOS))
            raise ValueError(
                f'Unknown scenario {requested}; choose one of {choices}'
            )
        self.scenario = requested
        self.topic = self.declare_parameter(
            'output_topic', '/sim/R2UTopic_Pos'
        ).value
        self.publisher = self.create_publisher(String, self.topic, 10)
        self.timer = self.create_timer(1.0, self.publish_state)
        self.get_logger().info(
            f'Publishing {self.scenario} on {self.topic}; SIMULATED DATA'
        )

    def publish_state(self):
        """Publish a deterministic, clearly marked simulated payload."""
        state = SCENARIOS[self.scenario]
        stamp = self.get_clock().now().nanoseconds * 1e-9
        payload = {
            'schema': 'campuscar.position.v1',
            'source': 'SIMULATED',
            'status': state['status'],
            'status_name': state['status_name'],
            'latitude': state['latitude'],
            'longitude': state['longitude'],
            'altitude': state['altitude'],
            'timestamp': stamp,
            'frame_id': 'gps_link',
            'fix_age_s': 0.0 if not state['stale'] else 3.0,
            'rtk': {
                'gga_quality': state['quality'],
                'satellites': state['satellites'],
                'hdop': state['hdop'],
                'correction_age_s': state['correction_age_s'],
                'station_id': 'SIM',
                'stale': state['stale'],
            },
            'vehicle': {
                'autonomy_allowed': state['autonomy_allowed'],
            },
        }
        message = String()
        message.data = json.dumps(
            payload,
            separators=(',', ':'),
            allow_nan=False,
        )
        self.publisher.publish(message)


def main(args=None):
    """Run the deterministic UE position simulator."""
    rclpy.init(args=args)
    node = PositionSimulator()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
