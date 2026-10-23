"""Build the two small HUY Android apps without Gradle or Android Studio.

Needs a JDK, Android SDK Build Tools, and Android 28's android.jar. Nothing is
installed into the system. Both APKs use one local signing key, outside Git.

Examples:
    python tools/huy_android/build_apks.py --test
    python tools/huy_android/build_apks.py --test-only
    python tools/huy_android/build_apks.py --app capture --toolchain-root PATH

Pure Java tests live in common/tests/**/*Test.java and expose a main method.
They run with assertions enabled. Android APIs are available at compile time
only; tests must exercise code that does not require an Android runtime.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import zipfile


SOURCE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = SOURCE_ROOT.parents[1]
APK_NAMES = {"capture": "HUYCapture.apk", "login": "HUYLogin.apk"}
PASSWORD_ENV = "HUY_ANDROID_BUILD_KEY_PASSWORD"


class BuildError(Exception):
    """A concise build failure suitable for a command-line message."""


def run(args: list[str | Path], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if result.returncode:
        # Arguments never contain the signing password or authorization data.
        detail = (result.stdout + result.stderr).strip()
        raise BuildError(f"{Path(args[0]).name} 执行失败：\n{detail}")
    return (result.stdout + result.stderr).strip()


def find_one(root: Path, pattern: str, description: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise BuildError(f"无法唯一定位{description}，请指定工具路径。搜索位置：{root / pattern}")
    return matches[0].resolve()


class Toolchain:
    def __init__(self, args: argparse.Namespace):
        root = args.toolchain_root.resolve()
        jdk = args.jdk_home
        if jdk is None:
            jdk = find_one(root, "jdk17/*/bin/javac.exe", "JDK").parent.parent
        build_tools = args.build_tools
        if build_tools is None:
            build_tools = find_one(root, "build-tools35/*/aapt2.exe", "Android Build Tools").parent
        self.java = jdk / "bin" / ("java.exe" if os.name == "nt" else "java")
        self.javac = jdk / "bin" / ("javac.exe" if os.name == "nt" else "javac")
        self.keytool = jdk / "bin" / ("keytool.exe" if os.name == "nt" else "keytool")
        self.android = args.android_jar or find_one(root, "platform28/*/android.jar", "Android 28 编译库")
        self.aapt2 = build_tools / ("aapt2.exe" if os.name == "nt" else "aapt2")
        self.zipalign = build_tools / ("zipalign.exe" if os.name == "nt" else "zipalign")
        self.d8 = build_tools / "lib" / "d8.jar"
        self.apksigner = build_tools / "lib" / "apksigner.jar"
        for value in vars(self).values():
            if not value.is_file():
                raise BuildError(f"缺少构建工具：{value}")


def restrict_signing_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        row = next(csv.reader([run(["whoami", "/user", "/fo", "csv", "/nh"])], skipinitialspace=True))
        sid = row[1].strip()
        if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
            raise BuildError("无法确认当前 Windows 用户，未创建签名私钥。")
        run([
            "icacls", directory, "/inheritance:r", "/grant:r",
            f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
        ])
    else:
        directory.chmod(0o700)


def signing_key(tools: Toolchain, directory: Path) -> tuple[Path, dict[str, str]]:
    restrict_signing_directory(directory)
    key = directory / "huy.p12"
    password_file = directory / "password.txt"
    if key.exists() and not password_file.exists():
        raise BuildError("已有签名私钥，但缺少 password.txt；未替换原有私钥。")
    if not password_file.exists():
        with password_file.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
        if os.name != "nt":
            password_file.chmod(0o600)
    password = password_file.read_text(encoding="utf-8").strip()
    if not password or "\n" in password or "\r" in password:
        raise BuildError("签名密码文件格式不正确。")
    env = os.environ.copy()
    env[PASSWORD_ENV] = password
    if not key.exists():
        run([
            tools.keytool, "-genkeypair", "-noprompt", "-keystore", key,
            "-storetype", "PKCS12", "-storepass:env", PASSWORD_ENV,
            "-keypass:env", PASSWORD_ENV, "-alias", "huy", "-keyalg", "RSA",
            "-keysize", "3072", "-validity", "10000",
            "-dname", "CN=HUY Local Android, OU=Personal, O=HUY, C=CN",
        ], env=env)
        if os.name != "nt":
            key.chmod(0o600)
    return key, env


def java_sources(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.java")) if directory.is_dir() else []


def javac_argument_file(path: Path, arguments: list[str | Path]) -> None:
    # javac @files avoid Windows' command-line length limit. Forward slashes
    # avoid javac's escape interpretation of Windows backslashes.
    def quoted(value: str | Path) -> str:
        text = str(value).replace("\\", "/")
        return '"' + text.replace('"', '\\"') + '"'

    path.write_text("\n".join(quoted(value) for value in arguments), encoding="utf-8")


def run_tests(tools: Toolchain, source_root: Path, work: Path) -> int:
    tests = java_sources(source_root / "common" / "tests")
    apps = sorted((source_root / "apps").iterdir()) if (source_root / "apps").is_dir() else []
    for app in apps:
        tests.extend(java_sources(app / "tests"))
    entries = [test for test in tests if test.stem.endswith("Test")]
    if not entries:
        raise BuildError("common/tests 下没有带 main 方法的 *Test.java。")
    classes = work / "test-classes"
    classes.mkdir()
    argfile = work / "test-javac.args"
    javac_argument_file(argfile, [
        "--release", "8", "-encoding", "UTF-8", "-classpath", tools.android,
        "-sourcepath", os.pathsep.join(str(app / "src") for app in apps),
        "-d", classes,
        *java_sources(source_root / "common" / "src"), *tests,
    ])
    run([tools.javac, "-J-Dfile.encoding=UTF-8", "-J-Duser.language=en", "@" + str(argfile)])
    for test in entries:
        source = test.read_text(encoding="utf-8")
        match = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.MULTILINE)
        class_name = (match.group(1) + "." if match else "") + test.stem
        output = run([tools.java, "-ea", "-cp", classes, class_name])
        print(f"测试通过：{class_name}")
        if output:
            print(output)
    return len(entries)


def build_app(
    name: str, tools: Toolchain, source_root: Path, output: Path, work: Path,
    key: Path, signing_env: dict[str, str],
) -> dict[str, str | int]:
    app = source_root / "apps" / name
    manifest = app / "AndroidManifest.xml"
    if not manifest.is_file():
        raise BuildError(f"缺少应用清单：{manifest}")
    sources = java_sources(app / "src") + java_sources(source_root / "common" / "src")
    if not sources:
        raise BuildError(f"应用没有 Java 源码：{app}")
    work.mkdir()
    generated = work / "generated"
    generated.mkdir()
    classes = work / "classes"
    classes.mkdir()
    dex = work / "dex"
    dex.mkdir()
    unsigned = work / "unsigned.apk"
    link_args: list[str | Path] = [
        tools.aapt2, "link", "-o", unsigned, "--manifest", manifest,
        "-I", tools.android, "--java", generated,
        "--min-sdk-version", "28", "--target-sdk-version", "28",
    ]
    res = app / "res"
    if res.is_dir() and any(path.is_file() for path in res.rglob("*")):
        resources = work / "resources.zip"
        run([tools.aapt2, "compile", "--dir", res, "-o", resources])
        link_args.append(resources)
    assets = app / "assets"
    if assets.is_dir():
        link_args.extend(["-A", assets])
    run(link_args)
    argfile = work / "javac.args"
    javac_argument_file(argfile, [
        "--release", "8", "-encoding", "UTF-8",
        "-classpath", tools.android, "-d", classes,
        *sources, *java_sources(generated),
    ])
    run([tools.javac, "-J-Dfile.encoding=UTF-8", "-J-Duser.language=en", "@" + str(argfile)])
    classes_jar = work / "classes.jar"
    with zipfile.ZipFile(classes_jar, "w", zipfile.ZIP_DEFLATED) as archive:
        for class_file in sorted(classes.rglob("*.class")):
            archive.write(class_file, class_file.relative_to(classes).as_posix())
    run([
        tools.java, "-cp", tools.d8, "com.android.tools.r8.D8", "--release",
        "--min-api", "28", "--lib", tools.android, "--output", dex, classes_jar,
    ])
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as archive:
        for dex_file in sorted(dex.glob("*.dex")):
            archive.write(dex_file, dex_file.name)
    aligned = work / "aligned.apk"
    run([tools.zipalign, "-f", "4", unsigned, aligned])
    signed = output / APK_NAMES[name]
    run([
        tools.java, "-jar", tools.apksigner, "sign", "--ks", key,
        "--ks-key-alias", "huy", "--ks-pass", f"env:{PASSWORD_ENV}",
        "--key-pass", f"env:{PASSWORD_ENV}", "--v4-signing-enabled", "false",
        "--out", signed, aligned,
    ], env=signing_env)
    verify = run([tools.java, "-jar", tools.apksigner, "verify", "--verbose", signed])
    run([tools.zipalign, "-c", "4", signed])
    digest = hashlib.sha256(signed.read_bytes()).hexdigest()
    print(f"构建完成并通过签名、对齐检查：{signed}")
    return {"app": name, "apk": str(signed), "bytes": signed.stat().st_size,
            "sha256": digest, "signature_verification": verify}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", action="append", choices=sorted(APK_NAMES), help="默认构建两款应用；可重复。")
    parser.add_argument("--test", action="store_true", help="构建前运行纯 Java 测试。")
    parser.add_argument("--test-only", action="store_true", help="只运行纯 Java 测试。")
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=REPOSITORY_ROOT / "artifacts" / "huy-android")
    parser.add_argument("--signing-dir", type=Path, default=REPOSITORY_ROOT / ".secrets" / "huy-android-signing")
    default_tools = os.environ.get("HUY_ANDROID_TOOLCHAIN", str(Path(tempfile.gettempdir()) / "huy-android-toolchain-20261023"))
    parser.add_argument("--toolchain-root", type=Path, default=Path(default_tools))
    parser.add_argument("--jdk-home", type=Path)
    parser.add_argument("--build-tools", type=Path)
    parser.add_argument("--android-jar", type=Path)
    return parser.parse_args()


def main() -> int:
    # Windows consoles and captured PowerShell output otherwise disagree about
    # the locale encoding, making Chinese build errors unreadable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        tools = Toolchain(args)
        args.source_root = args.source_root.resolve()
        args.output_dir = args.output_dir.resolve()
        temp_parent = Path(tempfile.gettempdir()).resolve()
        with tempfile.TemporaryDirectory(prefix="huy-apk-build-", dir=temp_parent) as temporary:
            work = Path(temporary).resolve()
            if work.parent != temp_parent:
                raise BuildError("临时构建目录不在预期位置。")
            if args.test or args.test_only:
                run_tests(tools, args.source_root, work)
            if args.test_only:
                return 0
            key, env = signing_key(tools, args.signing_dir.resolve())
            args.output_dir.mkdir(parents=True, exist_ok=True)
            builds = [build_app(name, tools, args.source_root, args.output_dir, work / name, key, env)
                      for name in dict.fromkeys(args.app or APK_NAMES)]
            (args.output_dir / "build-report.json").write_text(
                json.dumps({"min_sdk": 28, "target_sdk": 28, "builds": builds}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except (BuildError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
