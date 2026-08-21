import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    share = get_package_share_directory('livox_ros_driver2')
    config = os.path.join(share, 'config', 'MID360_campus_config.json')
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
    ])
