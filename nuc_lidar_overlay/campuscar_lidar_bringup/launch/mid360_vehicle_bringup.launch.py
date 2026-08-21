import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    livox_share = get_package_share_directory('livox_ros_driver2')
    safety_share = get_package_share_directory('campuscar_lidar_bringup')
    config = os.path.join(livox_share, 'config', 'MID360_campus_config.json')
    safety_config = os.path.join(safety_share, 'config', 'radar_safety.yaml')

    return LaunchDescription([
        Node(
            package='livox_ros_driver2',
            executable='livox_ros_driver2_node',
            name='mid360_driver',
            output='screen',
            parameters=[{
                'xfer_format': 0,
                'multi_topic': 0,
                'data_src': 0,
                'publish_freq': 10.0,
                'output_data_type': 0,
                'frame_id': 'livox_frame',
                'lvx_file_path': '/home/haoyu/mid360_test.lvx',
                'cmdline_input_bd_code': 'livox0000000001',
                'user_config_path': config,
            }],
        ),
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='vehicle_to_livox_tf',
            output='screen',
            arguments=[
                '--x', '0.0',
                '--y', '0.0',
                '--z', '0.58',
                '--roll', '0.0',
                '--pitch', '0.0',
                '--yaw', '0.0',
                '--frame-id', 'base_link',
                '--child-frame-id', 'livox_frame',
            ],
        ),
        Node(
            package='campuscar_lidar_bringup',
            executable='radar_safety_node',
            name='radar_safety',
            output='screen',
            respawn=True,
            respawn_delay=2.0,
            parameters=[safety_config],
        ),
    ])
