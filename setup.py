from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1].split("~")[0]


setup(
    name="wb-mqtt-dali",
    version=get_version(),
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Wiren Board MQTT DALI Bridge",
    url="https://github.com/wirenboard/wb-mqtt-dali",
    packages=["wb.mqtt_dali", "wb.mqtt_dali.device", "wb.mqtt_dali.gear"],
    license="MIT",
)
