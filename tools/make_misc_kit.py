#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「misc 模块外包 kit」：给接单方的最小可跑子集 + 自测说明 + 泄密断言。

用法（在项目根目录执行）::

    python tools/make_misc_kit.py                 # 生成 dist/misc-kit/
    python tools/make_misc_kit.py --verify        # 生成后立刻在 kit 里跑一遍 tests/smoke.py
    python tools/make_misc_kit.py --zip           # 额外打包 dist/misc-kit.tar.gz
    python tools/make_misc_kit.py --out D:/kit    # 指定输出目录
    python tools/make_misc_kit.py --verify --zip  # 推荐：一条命令出成品

为什么要有这个脚本（而不是一条 cp 命令）
--------------------------------------
v1 工单里手敲的 `cp -r ... HANDOFF.md` 把内部文档也打了进去：`competition_id`、平台内部结论、
历史判断全在里面，而工单 §6 明令「不能给外包方这些」。手敲命令没法防止这种手滑，
所以把「该给什么 / 不该给什么」写成代码里的断言，一次写对，每次都用。

三条硬保证
----------
1. kit 里**不含** ``.env`` / ``runs/`` / ``HANDOFF.md`` / ``PROGRESS.md`` / ``docs/mcp-tools.json``；
2. kit 里**不含**任何形如 ``CTFY_TOKEN=`` / ``*_API_KEY=`` 的赋值（扫文本文件，命中即失败）；
3. ``--verify`` 会在 kit 目录里实跑 ``tests/smoke.py``，必须 ``0 failed`` 才允许继续打包。

生成后 kit 是**自包含且可跑**的：接单方只需要一个 Python 3.12，不需要 token、不需要联网。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "dist" / "misc-kit"

# 拷进 kit 的白名单（目录 + 单文件）；不在表里的一律不进 kit
COPY_DIRS = ("src", "tests", "config", "roles")
COPY_FILES = ("Dockerfile", "README.md", "docs/misc-module-brief.md", "docs/report-ctfy-rules.md")

# 绝不进 kit 的路径片段 / 文件名（双保险：即便白名单写错也拦住）
FORBIDDEN_NAMES = {".env", ".env.example", "HANDOFF.md", "PROGRESS.md", "mcp-tools.json", "state.json"}
FORBIDDEN_DIRS = {"runs", "__pycache__", ".git", ".tools", ".ref", ".workbuddy", "dist"}

# 泄密扫描：只认「带引号的字面量」或「原始形态的密钥」，避免把代码里的取值表达式误判成密钥。
# 踩过的坑：第一版用 CTFY_TOKEN\s*=\s*\S+，结果把
#   config.py: llm_api_key=os.environ.get("DEEPSEEK_API_KEY", "")
#   llm.py:    api_key = api_key
#   brief:     `CTFY_TOKEN=` / `*_API_KEY=` 赋值
# 全判成泄密 —— 那是代码与文档，不是密钥。
SECRET_RES = (
    re.compile(r"""(?i)\b(?:CTFY_TOKEN|DEEPSEEK_API_KEY|[A-Z0-9_]{0,32}API_KEY)\s*=\s*["'][^"'\s]{16,}["']"""),
    re.compile(r"""(?i)\bCTFY_TOKEN\s*=\s*[A-Za-z0-9+/=_-]{24,}\s*$""", re.MULTILINE),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bpf_[A-Za-z0-9]{24,}\b"),
)

# 命中这些的行视为「代码取值」或「文档提及」，跳过（含反引号说明）
SECRET_SKIP_MARKERS = ("os.environ", "getenv", "environ.get", "`", "self.", "config.")

RUN_TESTS = """# 先跑这个（三步，全部离线，不需要 token / 联网 / Docker）

```bash
python -m compileall -q src tests      # 期望：无输出（0 error）
python tests/smoke.py                  # 期望：51 passed, 0 failed（基线；只允许变多不允许变少）
python tests/smoke.py 2>&1 | grep -i misc_contract   # 契约检查：产物齐全时打印「全部通过（7 项）」
```

其中 `misc_contract` 这个用例是**交付之后的验收器**：把你交的 `roles/misc.json`、
新增工具、`tests/fixtures/misc/*` 自动校验一遍（工具注册/签名/输出上限、夹具的 `solve.py`
能否复现 `expected.txt`、账本去重是否生效）。基线仓库已内置一套 misc 模块，所以它
会打印「全部通过（7 项）」；如果你清空了 `roles/` 与 `tests/fixtures/misc/` 再跑，它会
打印「跳过」——两者都算 PASS，底线是这条永远不能 FAIL。

## 你要交回的东西

| 产物 | 位置 |
|---|---|
| misc 角色配置片段 | `roles/misc.json` |
| misc 系统提示词全文 | `roles/misc.system.txt` |
| （可选）新本地工具增量补丁 | `patches/tools_local.patch` |
| 3 道夹具题（含 `solve.py` 参考解） | `tests/fixtures/misc/*/` |
| 新增测试用例 | `tests/smoke.py` 里加 `case_misc_*` 并注册到 `cases` |
| 变更说明 + 回滚方式 | `misc-CHANGELOG.md` |

## 自测夹具（每个夹具目录都要能这样跑通）

```bash
cd tests/fixtures/misc/01-xxx && python solve.py    # stdout 必须包含 expected.txt 里那一行 FLAG{...}
```

## 不许做

- 不要整文件覆盖现有源码（只交 diff 或新增文件）
- 不要索取 `.env` / token / 比赛实例地址
- 不要在 `solve.py` 里直接读 `expected.txt` 打印
- 不要把「执行任意命令」包装成新工具
"""


def _iter_files(base: Path):
    for path in base.rglob("*"):
        if path.is_file():
            yield path


def _copy_tree(dest: Path, verbose: bool = True) -> None:
    """按白名单拷目录，顺带跳过 forbidden 名字/目录。"""
    for name in COPY_DIRS:
        src = ROOT / name
        if not src.is_dir():
            raise SystemExit(f"[kit] 缺少必需目录：{src}")
        target = dest / name
        skipped: list[str] = []

        def _ignore(directory: str, entries: list[str]) -> set[str]:
            out: set[str] = set()
            for entry in entries:
                full = Path(directory) / entry
                if entry in FORBIDDEN_NAMES or entry in FORBIDDEN_DIRS:
                    out.add(entry)
                    skipped.append(str(full.relative_to(ROOT)))
                elif full.is_file() and full.suffix == ".pyc":
                    out.add(entry)
            return out

        shutil.copytree(src, target, ignore=_ignore, dirs_exist_ok=True)
        if verbose:
            count = sum(1 for _ in _iter_files(target))
            print(f"[kit] {name}/ -> {count} 个文件" + (f"（跳过 {len(skipped)}）" if skipped else ""))

    for name in COPY_FILES:
        src = ROOT / name
        if not src.is_file():
            print(f"[kit] 警告：缺少 {name}（跳过）")
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        if verbose:
            print(f"[kit] {name} -> {target.relative_to(dest)}")


def _write_extras(dest: Path) -> None:
    (dest / "RUN-TESTS.md").write_text(RUN_TESTS, encoding="utf-8")
    fixtures = dest / "tests" / "fixtures" / "misc"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "README.md").write_text(
        "# 夹具目录（接单方放这里）\n\n"
        "每道题一个子目录，必须有四个文件：\n\n"
        "- `task.md`：题面\n"
        "- `files/`：附件（单文件 <100KB）\n"
        "- `expected.txt`：预期 flag，单行 `FLAG{...}`\n"
        "- `solve.py`：确定性参考解（不联网、不调 LLM、只用 stdlib，stdout 打印 flag）\n\n"
        "自测：`cd <夹具目录> && python solve.py`，输出里必须包含 `expected.txt` 那一行。\n",
        encoding="utf-8",
    )
    (dest / "roles").mkdir(exist_ok=True)
    (dest / "roles" / "README.md").write_text(
        "# 角色产物目录（接单方放这里）\n\n"
        "- `misc.json`：可直接并入 `config/agent.json` 的 roles 片段\n"
        "- `misc.system.txt`：misc 系统提示词全文\n",
        encoding="utf-8",
    )
    print("[kit] 写入 RUN-TESTS.md / roles/README.md / tests/fixtures/misc/README.md")


def _purge_caches(dest: Path) -> int:
    """删掉 kit 里的 __pycache__ / *.pyc。

    必须在 `--verify` **之后**执行：verify 会在 kit 内 import src，从而现生成 pyc。
    第一版把泄密检查放在 verify 之前，结果这些后生成的 pyc 直接进了 tar.gz。
    """
    removed = 0
    for path in sorted(dest.rglob("__pycache__"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    for path in dest.rglob("*.pyc"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _assert_no_secrets(dest: Path) -> None:
    """硬断言 1+2：不该有的路径、不该出现的密钥形态。命中即失败（不是警告）。"""
    problems: list[str] = []

    for path in _iter_files(dest):
        rel = path.relative_to(dest)
        if path.name in FORBIDDEN_NAMES:
            problems.append(f"kit 里出现禁用文件：{rel}")
        if FORBIDDEN_DIRS & set(rel.parts):
            problems.append(f"kit 里出现禁用目录：{rel}")
        if path.stat().st_size > 2_000_000:
            continue  # 大二进制不扫
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in SECRET_SKIP_MARKERS):
                continue
            for pattern in SECRET_RES:
                match = pattern.search(line)
                if match:
                    problems.append(f"疑似密钥：{rel}:{lineno} 命中 {match.group(0)[:24]}…")
                    break

    if problems:
        print("\n[kit] ❌ 泄密检查失败，已中止：", file=sys.stderr)
        for item in problems:
            print("  -", item, file=sys.stderr)
        raise SystemExit(2)
    print("[kit] 泄密检查通过（无 .env / runs / HANDOFF / 密钥形态）")


def _verify(dest: Path) -> None:
    """硬断言 3：在 kit 里实跑单测，必须 0 failed。"""
    smoke = dest / "tests" / "smoke.py"
    print("[kit] 在 kit 内实跑 tests/smoke.py …")
    proc = subprocess.run(
        [sys.executable, str(smoke)],
        cwd=str(dest),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()][-3:]
    for line in tail:
        print("   ", line)
    if proc.returncode != 0 or "0 failed" not in (proc.stdout or ""):
        print("[kit] ❌ kit 内自测未通过，已中止。", file=sys.stderr)
        print((proc.stderr or "")[-2000:], file=sys.stderr)
        raise SystemExit(3)
    print("[kit] ✅ kit 内自测通过")


def _zip(dest: Path) -> Path:
    archive = dest.parent / f"{dest.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(dest, arcname=dest.name)
    size = archive.stat().st_size / 1024
    print(f"[kit] 打包：{archive}（{size:.0f} KB）")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 misc 模块外包 kit")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 dist/misc-kit）")
    parser.add_argument("--verify", action="store_true", help="生成后在 kit 内实跑 tests/smoke.py")
    parser.add_argument("--zip", action="store_true", help="额外打成 tar.gz")
    parser.add_argument("--keep", action="store_true", help="输出目录已存在时不删除（默认先清空）")
    args = parser.parse_args(argv)

    dest = Path(args.out).resolve()
    if dest.exists():
        if not args.keep:
            shutil.rmtree(dest)
        else:
            print(f"[kit] 复用已有目录：{dest}")
    dest.mkdir(parents=True, exist_ok=True)

    print(f"[kit] 源仓库：{ROOT}")
    print(f"[kit] 输出：{dest}\n")
    _copy_tree(dest)
    _write_extras(dest)

    if args.verify:
        _verify(dest)
    else:
        print("[kit] 未跑 --verify（建议加上：能立刻发现 kit 是否自包含）")

    purged = _purge_caches(dest)
    if purged:
        print(f"[kit] 清掉 {purged} 个 __pycache__/*.pyc（verify 时现生成的）")
    _assert_no_secrets(dest)

    if args.zip:
        _zip(dest)

    manifest = {
        "source": str(ROOT),
        "kit": str(dest),
        "baseline": "tests/smoke.py 期望 51 passed / 0 failed",
        "verified": bool(args.verify),
        "brief": "docs/misc-module-brief.md (v2)",
    }
    (dest / "KIT-MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n[kit] 完成。把整个目录交给接单方即可；他们第一步读 RUN-TESTS.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
