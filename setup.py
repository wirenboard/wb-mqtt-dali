from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1].split("~")[0]


setup(
    name="wb-python-service-template",
    version=get_version(),
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Wiren Board Python Service Template",
    url="https://github.com/wirenboard/wb-python-service-template",
    packages=["wb_python_service_template"],
    license="MIT",
)
