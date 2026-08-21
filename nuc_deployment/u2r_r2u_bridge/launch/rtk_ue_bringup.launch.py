"""Launch RTK ROS topics plus TCP/BSON and diagnostic WebSocket transports."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.actions import ExecuteProcess
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import AnyLaunchDescriptionSource
from launch.substitutions import FindExecutable
from launch.substitutions import LaunchConfiguration
from launch.substitutions import PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    config = PathJoinSubstitution([
        FindPackageShare('u2r_r2u_bridge'),
        'config',
        'rtk_ue.yaml',
    ])
    bson_script = PathJoinSubstitution([
        FindPackageShare('u2r_r2u_bridge'),
        'scripts',
        'rosbridge_bson_tcp.py',
    ])
    websocket_launch = PathJoinSubstitution([
        FindPackageShare('rosbridge_server'),
        'launch',
        'rosbridge_websocket_launch.xml',
    ])
    address = LaunchConfiguration('rosbridge_address')
    bson_port = LaunchConfiguration('rosbridge_port')
    websocket_port = LaunchConfiguration('websocket_port')

    return LaunchDescription([
        DeclareLaunchArgument(
            'rosbridge_address',
            default_value='0.0.0.0',
            description='TCP/BSON and WebSocket bind address; trusted LAN only',
        ),
        DeclareLaunchArgument(
            'rosbridge_port',
            default_value='9090',
            description='rosbridge TCP/BSON port for CampusBrain/UE',
        ),
        DeclareLaunchArgument(
            'websocket_port',
            default_value='9091',
            description='diagnostic rosbridge WebSocket port',
        ),
        Node(
            package='u2r_r2u_bridge',
            executable='serial_reader_node',
            name='rtk_serial_reader',
            output='screen',
            respawn=True,
            respawn_delay=2.0,
            parameters=[config],
            remappings=[('nmea_sentence', '/gps/nmea_sentence')],
        ),
        Node(
            package='nmea_navsat_driver',
            executable='nmea_topic_driver',
            name='rtk_nmea_parser',
            output='screen',
            respawn=True,
            respawn_delay=2.0,
            parameters=[{
                'frame_id': 'gps_link',
                'time_ref_source': 'gps',
                'useRMC': False,
            }],
            remappings=[
                ('nmea_sentence', '/gps/nmea_sentence'),
                ('fix', '/fix'),
                ('vel', '/gps/vel'),
                ('heading', '/gps/heading'),
                ('time_reference', '/gps/time_reference'),
            ],
        ),
        Node(
            package='u2r_r2u_bridge',
            executable='bridge_node',
            name='rtk_bridge',
            output='screen',
            respawn=True,
            respawn_delay=2.0,
            parameters=[config],
        ),
        Node(
            package='u2r_r2u_bridge',
            executable='campus_command_bridge',
            name='campusbrain_command_bridge',
            output='screen',
            respawn=True,
            respawn_delay=2.0,
            parameters=[{
                'command_topic': '/U2RTopic_Command',
                'status_topic': '/R2UTopic_Status',
                'control_url': 'http://127.0.0.1:8000/api/ros-command',
                'max_duration_s': 30.0,
                'http_timeout_s': 1.0,
            }],
        ),
        ExecuteProcess(
            cmd=[
                FindExecutable(name='python3'),
                bson_script,
                '--port',
                bson_port,
                '--address',
                address,
            ],
            output='screen',
            respawn=True,
            respawn_delay=2.0,
        ),
        IncludeLaunchDescription(
            AnyLaunchDescriptionSource(websocket_launch),
            launch_arguments={
                'address': address,
                'port': websocket_port,
            }.items(),
        ),
    ])
