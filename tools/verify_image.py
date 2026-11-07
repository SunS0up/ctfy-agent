# -*- coding: utf-8 -*-
"""匿名核验一个已发布的 agent 镜像：平台能不能拉、拉下来会执行什么。

用法：
    python tools/verify_image.py                          # 默认本项目的镜像
    python tools/verify_image.py ghcr.io/you/agent:v1     # 指定镜像

不需要任何凭据 —— 只用公开仓库的匿名 token，等价于平台的拉取视角。
纯 stdlib，和容器本身的依赖策略一致。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

DEFAULT_IMAGE = "ghcr.io/yzqcyl/ctfy-agent-pub:v1"
EXPECT_ENTRYPOINT = ["python", "-m", "ctfy_agent", "agent"]
EXPECT_PYTHONPATH = "/ctfy/src"
ACCEPT = ("application/vnd.oci.image.index.v1+json,"
          "application/vnd.docker.distribution.manifest.list.v2+json,"
          "application/vnd.oci.image.manifest.v1+json,"
          "application/vnd.docker.distribution.manifest.v2+json")


def split_ref(ref: str) -> tuple[str, str, str]:
    """ghcr.io/owner/name:tag → (registry, repo_path, tag)。"""
    registry, _, rest = ref.partition("/")
    if not rest:
        raise SystemExit(f"镜像引用格式不对：{ref}（应形如 ghcr.io/owner/name:tag）")
    tag = "latest"
    if ":" in rest.rsplit("/", 1)[-1]:
        rest, _, tag = rest.rpartition(":")
    return registry, rest, tag


def raw(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main(argv: list[str]) -> int:
    image = argv[1] if len(argv) > 1 else DEFAULT_IMAGE
    registry, repo, tag = split_ref(image)
    base = f"https://{registry}"
    print(f"镜像   : {image}")
    print(f"解释为 : registry={registry} repo={repo} tag={tag}")
    print("凭据   : 无（匿名，等价于平台拉取视角）\n")

    status, body = raw(f"{base}/token?scope=repository:{repo}:pull&service={registry}")
    print(f"[1] 匿名 token   HTTP {status}")
    if status != 200:
        print("    ✗ 拿不到匿名 token —— 包不是 public，平台拉不动")
        print("   ", body[:200].decode("utf-8", "replace"))
        return 1
    token = json.loads(body.decode())["token"]

    status, body = raw(f"{base}/v2/{repo}/manifests/{tag}",
                       {"Authorization": f"Bearer {token}", "Accept": ACCEPT})
    print(f"[2] manifest     HTTP {status}")
    if status != 200:
        print("    ✗ manifest 读不到")
        return 1
    manifest = json.loads(body.decode())
    layers = manifest.get("layers") or []
    size_mb = sum((layer.get("size") or 0) for layer in layers) / 1048576
    print(f"    {len(layers)} 层，传输体积 {size_mb:.1f} MB")

    config_digest = (manifest.get("config") or {}).get("digest")
    if not config_digest:
        print("    ✗ manifest 没有 config digest")
        return 1
    status, body = raw(f"{base}/v2/{repo}/blobs/{config_digest}",
                       {"Authorization": f"Bearer {token}"})
    print(f"[3] config blob  HTTP {status}")
    if status != 200:
        print("    ✗ config blob 读不到")
        return 1
    config = json.loads(body.decode()).get("config") or {}
    env = dict(item.split("=", 1) for item in (config.get("Env") or []) if "=" in item)

    print("\n—— 平台会执行的内容 ——")
    print(f"  Entrypoint : {config.get('Entrypoint')}")
    print(f"  Cmd        : {config.get('Cmd')}")
    print(f"  WorkingDir : {config.get('WorkingDir')}")
    print(f"  User       : {config.get('User') or 'root'}")
    print(f"  PYTHONPATH : {env.get('PYTHONPATH')}")

    problems = []
    if config.get("Entrypoint") != EXPECT_ENTRYPOINT:
        problems.append(f"Entrypoint 不是 {EXPECT_ENTRYPOINT}")
    if config.get("Cmd") not in (None, []):
        problems.append("Cmd 非空会让平台传的命令覆盖不生效")
    if env.get("PYTHONPATH") != EXPECT_PYTHONPATH:
        problems.append(f"PYTHONPATH 应为 {EXPECT_PYTHONPATH}")
    # 相对路径的 entrypoint 会被平台判为 relative_entrypoint
    for part in config.get("Entrypoint") or []:
        if re.match(r"^[\w.-]+\.py$", str(part)):
            problems.append(f"Entrypoint 含相对脚本路径 {part!r}（relative_entrypoint）")

    if problems:
        print("\n[结论] ✗ 有问题：")
        for item in problems:
            print("   -", item)
        return 1
    print("\n[结论] ✓ 平台可匿名拉取，且运行契约符合 CTFY-0001 v2")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
