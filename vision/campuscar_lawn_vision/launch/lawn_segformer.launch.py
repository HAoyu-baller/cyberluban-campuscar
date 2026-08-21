import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    share = get_package_share_directory('campuscar_lawn_vision')
    params = os.path.join(share, 'config', 'lawn_segformer.yaml')
    venv_python = '/home/haoyu/venvs/lawn-ai/bin/python'
    executable = os.path.join(
        share, '..', '..', 'lib', 'campuscar_lawn_vision',
        'lawn_segformer_node',
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'source_url',
            default_value='rtsp://127.0.0.1:8554/robot_cam',
        ),
        ExecuteProcess(
            cmd=[
                venv_python,
                executable,
                '--ros-args',
                '--params-file', params,
                '-p',
                ['source_url:=', LaunchConfiguration('source_url')],
            ],
            output='screen',
        ),
    ])
