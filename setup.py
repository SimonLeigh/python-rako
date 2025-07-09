import os

from setuptools import find_packages, setup


def read(*parts):
    """Read file."""
    filename = os.path.join(os.path.abspath(os.path.dirname(__file__)), *parts)
    with open(filename, encoding="utf-8") as fp:
        return fp.read()


with open("README.md") as readme_file:
    readme = readme_file.read()

setup(
    name="python-rako-2025",
    version="0.2.0",
    license="MIT",
    url="https://github.com/simonleigh/python-rako",
    author="Simon Leigh",
    author_email="simonleigh@users.noreply.github.com",
    description="Asynchronous Python client for Rako Controls Lighting.",
    keywords=["rako", "controls", "api", "async", "client"],
    long_description=readme,
    long_description_content_type="text/markdown",
    packages=find_packages(include=["python_rako"]),
    zip_safe=False,
    include_package_data=True,
    platforms="any",
    package_data={
        "python_rako": ["py.typed"],
    },
    install_requires=[
        "aiohttp>=3.10.0",
        "asyncio-dgram>=2.2.0",
        "xmltodict>=0.13.0",
    ],
    classifiers=[
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Natural Language :: English",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Framework :: AsyncIO",
        "Development Status :: 4 - Beta",
    ],
    python_requires=">=3.10",
)
