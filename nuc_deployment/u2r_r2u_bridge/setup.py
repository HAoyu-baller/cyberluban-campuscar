from glob import glob
import os

from setuptools import find_packages, setup


package_name = 'u2r_r2u_bridge'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        (
            'share/ament_index/resource_index/packages',
            ['resource/' + package_name],
        ),
        ('share/' + package_name, ['package.xml']),
        (
            os.path.join('share', package_name, 'launch'),
            glob('launch/*.launch.py'),
        ),
        (
            os.path.join('share', package_name, 'config'),
            glob('config/*.yaml'),
        ),
        (
            os.path.join('share', package_name, 'scripts'),
            glob('scripts/*.py'),
        ),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='haoyu',
    maintainer_email='haoyu@todo.todo',
    description='RTK to ROS 2 and UE5 bridge package',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'bridge_node = u2r_r2u_bridge.bridge_node:main',
            'serial_reader_node = '
            'u2r_r2u_bridge.serial_reader_node:main',
            'ue_position_simulator = '
            'u2r_r2u_bridge.ue_position_simulator:main',
            'campus_command_bridge = '
            'u2r_r2u_bridge.campus_command_bridge:main',
        ],
    },
)
