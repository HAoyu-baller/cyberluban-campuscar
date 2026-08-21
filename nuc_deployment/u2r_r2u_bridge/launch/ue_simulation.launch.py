"""Launch a loopback-only UE position simulation environment."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import AnyLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch.substitutions import PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    """Create an isolated simulator and local rosbridge."""
    scenario = LaunchConfiguration('scenario')
    port = LaunchConfiguration('rosbridge_port')
    rosbridge_launch = PathJoinSubstitution([
        FindPackageShare('rosbridge_server'),
        'launch',
        'rosbridge_websocket_launch.xml',
    ])
    return LaunchDescription([
        DeclareLaunchArgument(
            'scenario',
            default_value='RTK_FIX',
            description='NO_FIX, RTK_FLOAT, RTK_FIX, or TIMEOUT',
        ),
        DeclareLaunchArgument(
            'rosbridge_port',
            default_value='19090',
            description='isolated loopback test port',
        ),
        Node(
            package='u2r_r2u_bridge',
            executable='ue_position_simulator',
            name='ue_position_simulator',
            output='screen',
            parameters=[{
                'scenario': scenario,
                'output_topic': '/sim/R2UTopic_Pos',
            }],
        ),
        IncludeLaunchDescription(
            AnyLaunchDescriptionSource(rosbridge_launch),
            launch_arguments={
                'address': '127.0.0.1',
                'port': port,
            }.items(),
        ),
    ])
