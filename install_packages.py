#!/usr/bin/env python3
"""Cross-platform installer for Fusion 360 Batch Post-Processor dependencies.

Installs:
1. Isolated .venv for command-line verification and testing.
2. In-process wheels under packages/ for embedded Fusion 360 Python execution.
Works on Windows, macOS, and Linux.
"""

import argparse
import os
import platform
import subprocess
import sys


def get_default_platform_tag():
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        return "win_amd64"
    elif system == "darwin":
        if "arm" in machine or "aarch" in machine:
            return "macosx_11_0_arm64"
        return "macosx_10_9_x86_64"
    elif system == "linux":
        if "arm" in machine or "aarch" in machine:
            return "manylinux2014_aarch64"
        return "manylinux2014_x86_64"
    return "any"


def main():
    parser = argparse.ArgumentParser(description="Install dependencies for Fusion 360 Batch Post")
    parser.add_argument("--requirements", default="requirements.txt", help="Path to requirements.txt")
    parser.add_argument("--venv-dir", default=".venv", help="Directory for isolated virtualenv")
    parser.add_argument("--packages-dir", default="packages", help="Directory for Fusion embedded packages")
    parser.add_argument("--python-version", default="3.14", help="Target Python version for packages/ (e.g. 3.14, 3.12)")
    parser.add_argument("--platform-tag", default=None, help="Target platform tag for packages/ (e.g. win_amd64, macosx_11_0_arm64)")
    parser.add_argument("--skip-venv", action="store_true", help="Skip creating/updating .venv")
    parser.add_argument("--skip-packages", action="store_true", help="Skip downloading packages/ wheels")

    args = parser.parse_args()
    root_dir = os.path.dirname(os.path.abspath(__file__))
    req_path = os.path.join(root_dir, args.requirements)
    venv_dir = os.path.join(root_dir, args.venv_dir)
    pkg_dir = os.path.join(root_dir, args.packages_dir)

    platform_tag = args.platform_tag or get_default_platform_tag()

    # 1. Setup isolated .venv
    if not args.skip_venv:
        print(f"[*] Setting up isolated virtualenv in {venv_dir}...")
        if not os.path.isdir(venv_dir):
            subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)

        if os.name == "nt":
            venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
        else:
            venv_python = os.path.join(venv_dir, "bin", "python")

        print(f"[*] Upgrading pip in {venv_dir}...")
        subprocess.run([venv_python, "-m", "pip", "install", "--upgrade", "pip"], check=True)

        if os.path.isfile(req_path):
            print(f"[*] Installing requirements into {venv_dir}...")
            subprocess.run([
                venv_python, "-m", "pip", "install",
                "--only-binary=:all:", "-r", req_path
            ], check=True)

    # 2. Download target wheels into packages/
    if not args.skip_packages and os.path.isfile(req_path):
        print(f"[*] Installing Python {args.python_version} ({platform_tag}) wheels into {pkg_dir}...")
        os.makedirs(pkg_dir, exist_ok=True)

        # Use host python or venv python to execute pip
        pip_cmd = [sys.executable, "-m", "pip"]
        if not args.skip_venv:
            if os.name == "nt":
                candidate = os.path.join(venv_dir, "Scripts", "python.exe")
            else:
                candidate = os.path.join(venv_dir, "bin", "python")
            if os.path.isfile(candidate):
                pip_cmd = [candidate, "-m", "pip"]

        cmd = pip_cmd + [
            "install",
            "--target", pkg_dir,
            "--platform", platform_tag,
            "--python-version", args.python_version,
            "--implementation", "cp",
            "--only-binary=:all:",
            "--no-compile",
            "--upgrade",
            "-r", req_path,
        ]
        try:
            subprocess.run(cmd, check=True)
            print(f"[+] Packages successfully installed to {pkg_dir}")
        except subprocess.CalledProcessError as e:
            print(f"[!] Warning: pip target install failed with exit code {e.returncode}")

    print("[+] All dependencies processed successfully.")


if __name__ == "__main__":
    main()
