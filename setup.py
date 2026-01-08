"""Setup script for PicoMidi."""

from setuptools import find_packages, setup

# Read the README file for the long description
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

# Read version from package
import re

with open("picomidi/__init__.py", "r", encoding="utf-8") as fh:
    version_match = re.search(r'^__version__ = ["\']([^"\']*)["\']', fh.read(), re.MULTILINE)
    if version_match:
        version = version_match.group(1)
    else:
        version = "0.1.0"

setup(
    name="picomidi",
    version=version,
    author="Mark Brooks",
    author_email="",  # Add your email if desired
    description="A lightweight, focused MIDI library for Python",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/picomidi",  # Update with your repository URL
    packages=find_packages(exclude=["tests", "*.tests", "*.tests.*", "tests.*"]),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Multimedia :: Sound/Audio :: MIDI",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=[
        # No external dependencies - pure Python library
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
            "mypy>=1.0.0",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)

