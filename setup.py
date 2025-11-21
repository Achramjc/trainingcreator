from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="training-creator",
    version="1.0.0",
    author="Training Creator Team",
    description="Convert SOPs and work instructions into LMS-ready training materials",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Education",
        "Intended Audience :: Information Technology",
        "Topic :: Education",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=[
        "python-docx>=0.8.11",
        "PyPDF2>=3.0.0",
        "pdfplumber>=0.10.0",
        "markdown>=3.4.0",
        "jinja2>=3.1.2",
        "lxml>=4.9.0",
        "click>=8.1.0",
        "pyyaml>=6.0.0",
    ],
    entry_points={
        "console_scripts": [
            "training-creator=src.cli:main",
        ],
    },
)
