"""Package shared images from their existing source directory without copying them."""

from setuptools import find_packages, setup


setup(packages=[*find_packages(include=["naruto_daily*"]), "naruto_daily.assets"])
