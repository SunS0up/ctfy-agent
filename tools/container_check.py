# -*- coding: utf-8 -*-
"""容器镜像验收：一条命令跑完「构建 → 体积 → 洁净度 → 契约冒烟」。

平台是怎么启动我们的（CTFY-0001 v2）：
    docker create -w /ctfy/work -e CTFY_WORK_DIR=/ctfy/work ... <image>
注意**后面不跟任何 command**，全靠镜像自带 ENTRYPOINT。本脚本的 run 也一样不传 command。

验收项（全部不联网、不烧 LLM token）：
  1. 构建成功；镜像体积在上限内（主办方要求：太大传不上去）
  2. 镜像内容洁净：只有 /ctfy/src 与 /ctfy/config，没有 .env / tests / runs / docs
  3. 非 root：Config.User 必须声明且 uid != 0（平台协议检查硬性要求，root 直接判失败）
  4. cwd 无关性：在 /tmp 下也能 import ctfy_agent（PYTHONPATH 生效）
  5. 五条契约路径都必须：退出码 0 + 写出合法 result.json
       5.1 dry_run            → stop_reason=dry_run
       5.2 缺 task.json       → stop_reason=halted
       5.3 run_mode 不支持    → stop_reason=refused
       5.4 缺凭证             → stop_reason=refused
       5.5 无 MCP 端点        → stop_reason=halted
     （这五条覆盖了「任务不合法/环境不全时也不许崩」——崩了就是 0 分）

用法：
    python tools/container_check.py                 # 构建 + 全部验收
    python tools/container_check.py --no-build      # 复用已有镜像
    python tools/container_check.py --image mytag
    python tools/container_check.py --max-mb 400    # 体积上限（默认 400 MB）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "dist" / "_container_check"
DEFAULT_IMAGE = "ctfy-agent:check"
MAX_RESULT_BYTES = 4 * 1024 * 1024


class Fail(Exception):
    """某条验收没通过。"""


def sh(cmd: list[str], *, timeout: int = 1800, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise Fail(f"命令失败（exit {proc.returncode}）：{' '.join(cmd)}\n{proc.stderr[-1500:]}")
    return proc


def require_docker() -> str:
    if not shutil.which("docker"):
        raise Fail(
            "未找到 docker。请先安装 Docker Desktop 并启动（Windows 需开启 WSL2 后端），"
            "确认 `docker version` 能看到 Server 端后重跑。"
        )
    proc = sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=120)
    if proc.returncode != 0:
        raise Fail(
            "docker 装了但守护进程连不上（Server 未启动）：\n"
            + (proc.stderr or proc.stdout)[-800:]
            + "\n请启动 Docker Desktop，等托盘图标变绿后重跑。"
        )
    return proc.stdout.strip()


def build(tag: str) -> None:
    print(f"[构建] docker build -t {tag} .")
    # 只把构建的尾部给用户看，失败时全给
    proc = sh(["docker", "build", "-t", tag, "."], timeout=3600)
    tail = (proc.stdout or "").strip().splitlines()[-15:]
    for line in tail:
        print("       " + line)
    if proc.returncode != 0:
        raise Fail(f"镜像构建失败：\n{(proc.stderr or proc.stdout)[-3000:]}")


def image_size_mb(tag: str) -> float:
    proc = sh(["docker", "image", "inspect", tag, "--format", "{{.Size}}"], check=True)
    return int(proc.stdout.strip()) / (1024 * 1024)


def check_clean(tag: str) -> None:
    """镜像里不该有源码之外的东西（尤其 .env 里的凭据）。"""
    proc = sh(["docker", "run", "--rm", "--entrypoint", "ls", tag, "-la", "/ctfy"], check=True)
    listing = proc.stdout
    print("[洁净度] /ctfy 内容：")
    for line in listing.strip().splitlines():
        print("       " + line)
    for forbidden in (".env", "tests", "runs", "docs", ".git", "misc-bank"):
        if forbidden in listing.split():
            raise Fail(f"镜像里混进了 {forbidden!r}（不该进镜像）")
    for needed in ("src", "config"):
        if needed not in listing.split():
            raise Fail(f"镜像缺 {needed!r}")


def check_cwd_independent(tag: str) -> None:
    """平台强制 cwd=/ctfy/work；换个目录也要能 import，证明靠 PYTHONPATH 而非相对路径。"""
    code = "import ctfy_agent, sys; print('ok', ctfy_agent.__file__); print(sys.version.split()[0])"
    proc = sh(["docker", "run", "--rm", "-w", "/tmp", "--entrypoint", "python", tag, "-c", code], check=True)
    print("[cwd 无关] " + " | ".join(proc.stdout.strip().splitlines()))
    if "ok " not in proc.stdout:
        raise Fail(f"在 /tmp 下 import 失败：{proc.stdout}{proc.stderr}")


def check_nonroot(tag: str) -> None:
    """镜像必须声明非 root USER，且实际运行 uid != 0。

    平台在「注册并检查」的协议检查里直接判这一点（v2 镜像就是这里挂的：
    Dockerfile 没写 USER → 以 root 运行 → 「协议检查：发现问题 / 试运行：失败」）。
    静态看 Config.User，动态再 `id -u` 实测一次——只声明不生效的镜像同样是坑。
    """
    proc = sh(["docker", "image", "inspect", tag, "--format", "{{.Config.User}}"], check=True)
    declared = (proc.stdout or "").strip()
    proc = sh(["docker", "run", "--rm", "--entrypoint", "id", tag, "-u"], check=True)
    running = (proc.stdout or "").strip()
    print(f"[非 root] Config.User={declared!r} 运行时 uid={running}")
    uid = declared.split(":")[0].strip()
    if not declared or uid in ("", "0", "root"):
        raise Fail(
            f"镜像没有声明非 root USER（Config.User={declared!r}）：平台协议检查会直接判失败。"
            "请在 Dockerfile 末尾加 `RUN useradd -u 10001 ... && USER app`。"
        )
    if running in ("", "0"):
        raise Fail(f"容器实际以 uid={running!r} 运行：平台要求非 root（uid != 0）。")


def read_result(host_dir: Path) -> dict:
    path = host_dir / "result.json"
    if not path.is_file():
        raise Fail(f"没有写出 result.json（{path}）")
    if path.is_symlink():
        raise Fail("result.json 是符号链接（平台 O_NOFOLLOW 会拒读）")
    raw = path.read_bytes()
    if len(raw) > MAX_RESULT_BYTES:
        raise Fail(f"result.json 超 4 MiB（{len(raw)} B）")
    leftovers = list(host_dir.glob("result.json.*"))
    if leftovers:
        raise Fail(f"残留临时文件：{[p.name for p in leftovers]}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise Fail(f"result.json 不是合法 JSON：{exc}") from None
    if not isinstance(data, dict):
        raise Fail(f"result.json 顶层不是对象：{type(data)}")
    return data


def run_case(tag: str, name: str, task: dict | None, env_extra: dict, expect_reason: str,
             local: bool = False) -> None:
    """跑一个契约用例：不传 command（与平台一致），只给 -w 和 CTFY_WORK_DIR。"""
    host_dir = WORK / name
    if host_dir.exists():
        shutil.rmtree(host_dir)
    host_dir.mkdir(parents=True)
    # 平台把 /ctfy/work 以 0o777 的 staging 目录 `docker cp` 进容器（harness.py），
    # 镜像现在跑非 root 用户；本地用 bind mount 时必须同样放开，否则契约用例会因
    # 「写不出 result.json」假失败（这是本地验收与平台的已知差异）。
    try:
        host_dir.chmod(0o777)
    except OSError:
        pass
    if task is not None:
        (host_dir / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

    if local:
        # 无 Docker 时先验契约逻辑本身：同样的 task.json / env / cwd，直接跑适配层。
        # 只给最小环境（不继承宿主机的真实凭据），否则 no_creds 用例会假通过。
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "CTFY_WORK_DIR": str(host_dir),
            **env_extra,
        }
        proc = subprocess.run(
            [sys.executable, "-m", "ctfy_agent", "agent"],
            cwd=str(host_dir), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
    else:
        cmd = [
            "docker", "run", "--rm",
            "-w", "/ctfy/work",
            "-e", "CTFY_WORK_DIR=/ctfy/work",
            "-v", f"{host_dir}:/ctfy/work",
        ]
        for key, value in env_extra.items():
            cmd += ["-e", f"{key}={value}"]
        cmd.append(tag)
        proc = sh(cmd, timeout=600)

    if proc.returncode != 0:
        raise Fail(
            f"[{name}] 退出码 {proc.returncode}（契约要求必须是 0）\n"
            f"stdout:\n{(proc.stdout or '')[-2000:]}\nstderr:\n{(proc.stderr or '')[-2000:]}"
        )
    data = read_result(host_dir)
    reason = data.get("stop_reason")
    if reason != expect_reason:
        raise Fail(f"[{name}] stop_reason={reason!r}，期望 {expect_reason!r}；result={json.dumps(data, ensure_ascii=False)[:600]}")
    if data.get("schema_version") != 2:
        raise Fail(f"[{name}] schema_version={data.get('schema_version')!r}，期望 2")
    ans = data.get("answers")
    if not isinstance(ans, dict):
        raise Fail(f"[{name}] answers 不是对象：{ans!r}")
    print(f"[契约] {name:<22} exit=0 stop_reason={reason:<9} answers={len(ans)} 字节={len(json.dumps(data))}")
    shutil.rmtree(host_dir, ignore_errors=True)


def contract_cases(tag: str, local: bool = False) -> None:
    inst = {"schema_version": 2, "run_mode": "instance", "challenge": {"id": "ctf-smoke"}}
    # 4.1 dry run：最便宜的注册门
    run_case(tag, "dry_run", {**inst, "dry_run": True}, {"CTFY_EVAL_DRY_RUN": "1"}, "dry_run", local)
    # 4.2 task.json 缺失、且没有任何 env 兜底
    run_case(tag, "no_task", None, {}, "halted", local)
    # 4.3 不支持的 run_mode（本适配器只做 instance）
    run_case(tag, "bad_run_mode", {"schema_version": 2, "run_mode": "qa", "challenge": {"id": "x"}}, {}, "refused", local)
    # 4.4 凭证缺失：必须 fail loudly，不许烧钱
    run_case(tag, "no_creds", inst, {"LLM_FLAVOR": "openai"}, "refused", local)
    # 4.5 没有 MCP 端点（没有 mcp.json，也没给 CTFY_PLATFORM_URL）
    run_case(
        tag, "no_mcp",
        inst,
        {"LLM_FLAVOR": "openai", "CTFY_TOKEN": "dummy-token", "LLM_API_KEY": "dummy-key"},
        "halted", local,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="容器镜像验收（B1 契约）")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"镜像标签（默认 {DEFAULT_IMAGE}）")
    parser.add_argument("--no-build", action="store_true", help="跳过构建，复用已有镜像")
    parser.add_argument("--local", action="store_true",
                        help="不碰 Docker：直接在本机跑那五条契约路径（验证脚本预期与适配层行为一致）")
    parser.add_argument("--max-mb", type=float, default=400.0, help="镜像体积上限 MB（默认 400）")
    args = parser.parse_args(argv)

    try:
        if args.local:
            print("[本地] 无 Docker 模式：只验五条契约路径（不验镜像本身）")
            contract_cases(args.image, local=True)
            print("\n契约路径全部通过 ✓（镜像本身仍需在 Docker 下用不带 --local 跑一遍）")
            return 0

        version = require_docker()
        print(f"[docker] Server {version}")
        if not args.no_build:
            build(args.image)
        else:
            sh(["docker", "image", "inspect", args.image], check=True)

        size = image_size_mb(args.image)
        print(f"[体积] {args.image} = {size:.1f} MB（上限 {args.max_mb:.0f} MB）")
        if size > args.max_mb:
            raise Fail(f"镜像 {size:.1f} MB 超过上限 {args.max_mb:.0f} MB")

        check_clean(args.image)
        check_nonroot(args.image)
        check_cwd_independent(args.image)
        contract_cases(args.image)

        print("\n全部通过 ✓ 镜像可以直接上传。")
        return 0
    except Fail as exc:
        print(f"\n验收失败 ✗\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
