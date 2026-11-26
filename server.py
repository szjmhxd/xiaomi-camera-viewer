#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xiaomi Camera Viewer
====================

把米家摄像头导出的碎片录像（每分钟一个 mp4）整合成一台可按时间轴回放的
监控台：选日期、拖时间轴、跨片段连播。

    python server.py --root "J:/xiaomi_camera_videos"

依赖：Python 3.9+ 与系统 ffmpeg / ffprobe，其余零依赖。

为什么要在服务端转码
--------------------
米家这批录像用 HEVC(H.265) + G.711 a-law 音频，Chrome/Edge 默认两项都解不了。
服务端用 x264 实时转成 H.264 + AAC 再喂给浏览器，同时把转好的片段缓存到
本地磁盘，第二次播放即可直接 Range 读取、精确拖动。

缓存为什么不放在录像目录
------------------------
录像通常放在 NAS 或写满的存储卡上，既慢又可能没有剩余空间。
默认写入用户本地缓存目录，并带容量上限（LRU 淘汰）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.3.2"

# 米家 SD 卡文件名：<当天小时>H? 实际为 <分钟内分钟>M<秒>S_<unix 起始时间戳>.mp4
NAME_RE = re.compile(r"^(\d{1,3})M(\d{1,2})S_(\d{9,11})\.mp4$", re.I)

WEB_DIR = Path(__file__).resolve().parent / "web"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def which(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    if os.name == "nt":
        for cand in (f"{name}.exe", f"{name}.bat", f"{name}.cmd"):
            p = shutil.which(cand)
            if p:
                return p
    return None


FFMPEG = which("ffmpeg")
FFPROBE = which("ffprobe")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "XiaomiCamViewer" / "cache"
    return Path.home() / ".cache" / "xiaomi-camera-viewer"


def free_bytes(path: Path) -> int:
    """目标目录可能还不存在，向上找到最近的已存在父目录再问。"""
    p = Path(path)
    for _ in range(32):
        if p.exists():
            break
        if p.parent == p:
            return -1
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return -1


def lan_addresses(port: int) -> list[str]:
    """本机在局域网里可能被访问到的地址。

    Windows 上会混进 WSL / Hyper-V / VMware 的虚拟网卡，它们的特征是私网地址
    且末位为 .1，直接过滤掉；剩下的把默认出口网卡排最前。
    """
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                ips.add(ip)
    except OSError:
        pass

    primary = None
    for probe in ("223.5.5.5", "8.8.8.8"):     # 兜底：取默认出口网卡的地址
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.3)
            sock.connect((probe, 53))
            primary = sock.getsockname()[0]
            ips.add(primary)
            break
        except OSError:
            continue
        finally:
            sock.close()

    def private(ip: str) -> bool:
        return (ip.startswith("192.168.") or ip.startswith("10.")
                or (ip.startswith("172.") and 16 <= int(ip.split(".")[1] or 0) <= 31))

    def bogus(ip: str) -> bool:
        # 198.18.0.0/15 是 Clash 之类 TUN 代理的假地址，169.254 是链路本地，都对端连不上
        return (ip.startswith("198.18.") or ip.startswith("198.19.")
                or ip.startswith("169.254."))

    ips = {ip for ip in ips if not bogus(ip)}
    if primary and bogus(primary):
        primary = None

    real = {ip for ip in ips if not (private(ip) and ip.endswith(".1"))}
    if real:
        ips = real

    def rank(ip: str) -> tuple:
        if ip == primary:
            return (0, ip)
        if ip.startswith("192.168."):
            return (1, ip)
        if ip.startswith("10."):
            return (2, ip)
        if ip.startswith("172."):
            return (4, ip)
        return (3, ip)                          # 100.x 这类 Tailscale / 其他虚拟网卡

    return [f"http://{ip}:{port}/" for ip in sorted(ips, key=rank)]


def load_sources(dirpath: str) -> dict:
    """上传目录里的 sources.json：文件名 -> (起始秒, 时长)。

    手机上传的文件没有米家的 `<分>M<秒>S_<时间戳>` 命名，靠 mtime 排不出顺序，
    上传结束时按真实时长顺序写一份时间表，扫描时优先用它。
    """
    try:
        data = json.loads((Path(dirpath) / "sources.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, tuple[int, float]] = {}
    for item in data:
        try:
            out[str(item["name"])] = (int(item["start"]), float(item.get("dur") or 60.0))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---- 配置文件：记住上次打开的文件夹 ----

def load_config(cache_dir: Path) -> dict:
    try:
        return json.loads((cache_dir / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cache_dir: Path, data: dict) -> None:
    try:
        (cache_dir / "config.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass


# ---- 目录浏览（给网页里的"选择文件夹"用）----

SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", "Config.Msi",
    "Recovery", "Windows", "Program Files", "Program Files (x86)",
    "ProgramData", "$WinREAgent", "node_modules", "__pycache__",
}


# 服务器/容器里常见的一级挂载点；/proc/mounts 里的一堆系统挂载要排除掉
SYSTEM_MOUNT_HINTS = ("/videos", "/data", "/recordings", "/cache")
_SYS_MOUNT_PREFIX = (
    "/proc", "/sys", "/dev", "/run", "/tmp", "/etc", "/boot", "/usr",
    "/lib", "/lib64", "/bin", "/sbin", "/snap", "/var/lib", "/var/cache",
    "/var/log", "/var/run", "/var/tmp",
)


def list_drives() -> list[dict]:
    """列出盘符。Windows 走 GetLogicalDrives 位掩码，比逐个 exists() 快得多，
    也不会在掉线的映射盘上卡住。"""
    out: list[dict] = []
    if os.name == "nt":
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
                if not (mask >> i) & 1:
                    continue
                root = f"{letter}:\\"
                dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                out.append({
                    "path": root,
                    "name": f"{letter}:",
                    # 2=可移动 3=本地 4=网络 5=光驱 6=RAM
                    "kind": {2: "removable", 3: "local", 4: "network",
                             5: "optical", 6: "ram"}.get(dtype, "other"),
                })
        except Exception:
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                if os.path.exists(f"{letter}:\\"):
                    out.append({"path": f"{letter}:\\", "name": f"{letter}:", "kind": "other"})
    else:
        out.append({"path": "/", "name": "/", "kind": "local"})
        home = str(Path.home())
        out.append({"path": home, "name": "~", "kind": "local"})
        seen = {"/", home}
        for mnt in ("/mnt", "/media", "/Volumes"):
            try:
                for name in sorted(os.listdir(mnt)):
                    p = os.path.join(mnt, name)
                    if os.path.isdir(p) and p not in seen:
                        seen.add(p)
                        out.append({"path": p, "name": name, "kind": "local"})
            except OSError:
                pass
        # 容器里没有盘符，录像目录通常直接 bind mount 到 /videos、/data 这类一级路径。
        # 除了固定候选，再把 /proc/mounts 里非系统的挂载点列出来，页面里才有得选。
        for p in SYSTEM_MOUNT_HINTS + tuple(_linux_mount_points()):
            if p not in seen and os.path.isdir(p):
                seen.add(p)
                out.append({"path": p, "name": p, "kind": "local"})
    return out


def _linux_mount_points(path: str = "/proc/mounts") -> list[str]:
    """读 /proc/mounts，返回看起来像"数据盘"的挂载点（最多三层深）。

    Docker 容器里 bind mount 进来的目录会出现在这里，用户就能在文件夹选择器
    里直接看到 /videos 之外自己挂的其它目录。
    """
    pts: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mp = parts[1].replace("\\040", " ").replace("\\011", "\t")
                if not mp.startswith("/") or mp == "/":
                    continue
                if any(mp == p or mp.startswith(p + "/") for p in _SYS_MOUNT_PREFIX):
                    continue
                if mp.count("/") > 3:
                    continue
                if mp not in pts:
                    pts.append(mp)
    except OSError:
        pass
    return sorted(pts)


def parent_of(path: Path) -> str | None:
    p = path.parent
    return str(p) if str(p) != str(path) else None


def list_subdirs(path: Path) -> list[dict]:
    """只列子目录，不做递归统计——网络盘上逐个扫描子目录会慢到无法使用。
    "含视频"标记共享一份 IO 预算，预算耗尽后不再探测（只是不显示标记，不影响使用）。"""
    items = []
    budget = [120]
    with os.scandir(path) as it:
        for e in it:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            has_video = _quick_has_mp4(e.path, 2, budget) if budget[0] > 0 else False
            items.append({"name": e.name, "path": e.path, "video": has_video})
    items.sort(key=lambda x: (not x["video"], x["name"].lower()))
    return items


def _quick_has_mp4(dirpath: str, depth: int = 3, budget: list | None = None) -> bool:
    """在有限的 IO 预算内判断这个目录树里有没有 mp4。

    米家是三层结构（根/摄像头ID/小时/*.mp4），所以至少要往下探三层；
    同时用 budget 限制总 scandir 次数，避免在盘符根目录上扫全盘。
    """
    if budget is None:
        budget = [24]
    if depth < 0 or budget[0] <= 0:
        return False
    subdirs: list[str] = []
    try:
        with os.scandir(dirpath) as it:
            for e in it:
                budget[0] -= 1
                if budget[0] <= 0:
                    break
                if e.name.lower().endswith(".mp4"):
                    return True
                try:
                    if e.is_dir(follow_symlinks=False):
                        subdirs.append(e.path)
                except OSError:
                    continue
                if len(subdirs) >= 6:
                    break
    except OSError:
        return False
    for p in subdirs:
        if _quick_has_mp4(p, depth - 1, budget):
            return True
    return False


def parse_rel_time(text: str) -> int | None:
    """'16:20' / '16:20:13' -> 当天秒偏移。"""
    if not text:
        return None
    try:
        nums = [int(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m = nums
        s = 0
    elif len(nums) == 1:
        h, m, s = nums[0], 0, 0
    else:
        return None
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return h * 3600 + m * 60 + s


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------

class Segment:
    __slots__ = ("sid", "uid", "cam", "path", "start", "size", "dur", "day", "rel")

    def __init__(self, sid: str, cam: str, path: str, start: int, size: int, dur: float):
        self.sid = sid            # 相对路径，用于接口定位
        self.uid = os.path.normcase(path)   # 绝对路径，用于缓存键（换目录也不会撞）
        self.cam = cam
        self.path = path
        self.start = start
        self.size = size
        self.dur = dur
        dt = datetime.fromtimestamp(start)
        self.day = dt.strftime("%Y-%m-%d")
        self.rel = dt.hour * 3600 + dt.minute * 60 + dt.second

    def api(self) -> dict:
        return {"id": self.sid, "s": self.rel, "t": round(self.dur, 2), "sz": self.size}


class Index:
    """扫描录像目录并在内存中建立按日期分组的索引。

    慢速网络盘上逐次 os.path.getsize 每次要几十毫秒，上万文件会卡到不可用；
    这里统一用 os.scandir 的 DirEntry.stat()，它复用目录枚举时已有的元数据。
    """

    def __init__(self, root: Path, cache_dir: Path):
        self.root = root
        self.cache_dir = cache_dir
        self.lock = threading.RLock()
        self.segs: list[Segment] = []
        self.by_id: dict[str, Segment] = {}
        self.by_day: dict[str, list[Segment]] = {}
        self.durations: dict[str, float] = {}
        self.cams: list[str] = []
        self.skipped = 0
        self.scan_seconds = 0.0
        self.progress = 0          # 已遍历的文件数，供前端显示扫描进度
        self._load_durations()

    @property
    def _dur_file(self) -> Path:
        return self.cache_dir / "durations.json"

    def _load_durations(self) -> None:
        try:
            self.durations = json.loads(self._dur_file.read_text("utf-8"))
        except (OSError, ValueError):
            self.durations = {}

    def _save_durations(self) -> None:
        try:
            self._dur_file.write_text(json.dumps(self.durations), "utf-8")
        except OSError:
            pass

    def remember_duration(self, sid: str, dur: float) -> None:
        with self.lock:
            if abs(self.durations.get(sid, -1) - dur) < 0.05:
                return
            self.durations[sid] = round(dur, 3)
            seg = self.by_id.get(sid)
            if seg:
                seg.dur = dur
            self._save_durations()

    def refresh(self) -> None:
        t0 = time.time()
        segs: list[Segment] = []
        skipped = 0
        cams: set[str] = set()
        stack = [self.root]
        root_str = str(self.root)
        self.progress = 0

        while stack:
            d = stack.pop()
            meta = load_sources(str(d))
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        self.progress += 1
                        if e.is_dir(follow_symlinks=False):
                            if e.name in (".git", "node_modules", "#recycle", "@eaDir"):
                                continue
                            stack.append(Path(e.path))
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        if not e.name.lower().endswith(".mp4"):
                            continue
                        st = e.stat()
                        if st.st_size < 1024:          # 0 字节 / 拷贝中断的残片
                            skipped += 1
                            continue
                        m = NAME_RE.match(e.name)
                        info = meta.get(e.name)
                        if m:
                            start = int(m.group(3))
                        elif info:
                            start = info[0]
                        else:
                            start = int(st.st_mtime)
                        rel = os.path.relpath(e.path, root_str).replace("\\", "/")
                        cam = rel.split("/")[0] if "/" in rel else "default"
                        cams.add(cam)
                        dur = self.durations.get(rel) or (info[1] if info else 60.0)
                        segs.append(Segment(rel, cam, e.path, start, st.st_size, dur))
                    except OSError:
                        continue

        segs.sort(key=lambda s: s.start)
        by_day: dict[str, list[Segment]] = {}
        by_id: dict[str, Segment] = {}
        for s in segs:
            by_day.setdefault(s.day, []).append(s)
            by_id[s.sid] = s

        with self.lock:
            self.segs = segs
            self.by_id = by_id
            self.by_day = by_day
            self.cams = sorted(cams)
            self.skipped = skipped
            self.scan_seconds = time.time() - t0

    def day_list(self) -> list[dict]:
        out = []
        with self.lock:
            for day in sorted(self.by_day, reverse=True):
                items = self.by_day[day]
                out.append({
                    "date": day,
                    "count": len(items),
                    "seconds": round(sum(i.dur for i in items), 1),
                    "bytes": sum(i.size for i in items),
                })
        return out

    def day_segments(self, day: str) -> list[Segment]:
        with self.lock:
            return list(self.by_day.get(day, []))

    def get(self, sid: str) -> Segment | None:
        with self.lock:
            return self.by_id.get(sid)

    def stats(self) -> dict:
        with self.lock:
            return {
                "files": len(self.segs),
                "bytes": sum(s.size for s in self.segs),
                "days": len(self.by_day),
                "cameras": self.cams,
                "skipped": self.skipped,
                "scan_seconds": round(self.scan_seconds, 2),
                "progress": self.progress,
                "first": self.segs[0].start if self.segs else None,
                "last": self.segs[-1].start if self.segs else None,
            }


# --------------------------------------------------------------------------
# 转码 / 缓存
# --------------------------------------------------------------------------

class Encoder:
    def __init__(self, cache_dir: Path, height=720, fps=15, crf=26, max_gb=20.0):
        self.cache_dir = cache_dir
        self.vdir = cache_dir / "v"
        self.tdir = cache_dir / "thumb"
        self.vdir.mkdir(parents=True, exist_ok=True)
        self.tdir.mkdir(parents=True, exist_ok=True)
        self.height = height
        self.fps = fps
        self.crf = crf
        self.max_bytes = int(max_gb * 1024 ** 3)
        self._jobs: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max(1, min(3, (os.cpu_count() or 2) // 2)))

    # -- 路径 -------------------------------------------------------------
    def video_path(self, sid: str) -> Path:
        return self.vdir / (hashlib.md5(sid.encode("utf-8")).hexdigest()[:20] + ".mp4")

    def thumb_path(self, sid: str, t: float) -> Path:
        h = hashlib.md5(f"{sid}@{t:.1f}".encode("utf-8")).hexdigest()[:20]
        return self.tdir / (h + ".jpg")

    def is_cached(self, sid: str) -> bool:
        p = self.video_path(sid)
        try:
            return p.is_file() and p.stat().st_size > 4096
        except OSError:
            return False

    # -- 命令 -------------------------------------------------------------
    def _cmd(self, src: str, ss: float | None, dur: float | None) -> list[str]:
        cmd = [FFMPEG or "ffmpeg", "-hide_banner", "-v", "error", "-nostdin"]
        if ss and ss > 0:
            cmd += ["-ss", f"{ss:.3f}"]
        cmd += ["-i", src]
        if dur and dur > 0:
            cmd += ["-t", f"{dur:.3f}"]
        cmd += [
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"scale=-2:{self.height}:force_original_aspect_ratio=decrease",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.crf),
            "-pix_fmt", "yuv420p", "-g", str(max(15, self.fps * 3)),
            "-fps_mode", "cfr", "-r", str(self.fps),
            "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000",
        ]
        return cmd

    def stream(self, src: str, ss: float | None, dur: float | None) -> subprocess.Popen:
        """fMP4 流式输出，首字节约 1 秒，用于立即起播。"""
        cmd = self._cmd(src, ss, dur)
        cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4", "pipe:1"]
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

    def cache(self, sid: str, src: str) -> Path | None:
        out = self.video_path(sid)
        if self.is_cached(sid):
            return out
        tmp = out.with_name(out.stem + f".{os.getpid()}.part")
        cmd = self._cmd(src, None, None) + ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
        with self._sem:
            try:
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=CREATE_NO_WINDOW, timeout=900)
                if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 4096:
                    tmp.unlink(missing_ok=True)
                    return None
                tmp.replace(out)
                self.enforce_limit()
                return out
            except (OSError, subprocess.SubprocessError):
                tmp.unlink(missing_ok=True)
                return None

    def enforce_limit(self) -> None:
        """按 mtime 淘汰最旧缓存，保留最近 10 分钟写的不动。"""
        if self.max_bytes <= 0:
            return
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p)
                     for p in self.vdir.glob("*.mp4")]
        except OSError:
            return
        total = sum(f[1] for f in files)
        if total <= self.max_bytes:
            return
        now = time.time()
        for mtime, size, p in sorted(files):
            if total <= self.max_bytes:
                break
            if now - mtime < 600:
                continue
            try:
                p.unlink()
                total -= size
            except OSError:
                pass

    def schedule_cache(self, seg: Segment) -> None:
        if self.max_bytes == 0 or self.is_cached(seg.uid):
            return
        with self._lock:
            if seg.uid in self._jobs:
                return
            th = threading.Thread(target=self._bg, args=(seg,), daemon=True)
            self._jobs[seg.uid] = th
            th.start()

    def _bg(self, seg: Segment) -> None:
        try:
            self.cache(seg.uid, seg.path)
        finally:
            with self._lock:
                self._jobs.pop(seg.uid, None)

    def thumb(self, src: str, sid: str, t: float) -> Path | None:
        out = self.thumb_path(sid, t)
        if out.is_file() and out.stat().st_size > 512:
            return out
        cmd = [FFMPEG or "ffmpeg", "-hide_banner", "-v", "error", "-nostdin",
               "-ss", f"{max(0.0, t):.2f}", "-i", src,
               "-frames:v", "1", "-vf", "scale=-2:180", "-q:v", "6",
               "-f", "image2", "-y", str(out)]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=CREATE_NO_WINDOW, timeout=30)
            if r.returncode == 0 and out.is_file() and out.stat().st_size > 512:
                return out
            out.unlink(missing_ok=True)
        except (OSError, subprocess.SubprocessError):
            out.unlink(missing_ok=True)
        return None

    def probe_duration(self, src: str) -> float | None:
        if not FFPROBE:
            return None
        try:
            r = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", src],
                capture_output=True, text=True, timeout=30,
                creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return None

    def cache_stats(self) -> dict:
        try:
            files = list(self.vdir.glob("*.mp4"))
            total = sum(p.stat().st_size for p in files)
        except OSError:
            files, total = [], 0
        return {"items": len(files), "bytes": total, "limit": self.max_bytes}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class App:
    """运行期状态：当前打开的录像目录、索引、转码器。

    目录可以在运行中切换，所以 index 允许为空（还没选目录）。
    扫描在网络盘上要几十秒，放到后台线程跑，前端轮询 /api/scan。
    """

    def __init__(self, cache_dir: Path, height, fps, crf, max_gb, root: Path | None = None):
        self.cache_dir = cache_dir
        self.encoder = Encoder(cache_dir, height, fps, crf, max_gb)
        self.index: Index | None = None
        self.root: Path | None = None
        self.started = time.time()
        self._lock = threading.Lock()
        self.scanning = False
        self.scan_error = ""
        cfg = load_config(cache_dir)
        self.recent: list[str] = [p for p in cfg.get("recent", []) if isinstance(p, str)]
        if root is not None:
            self.set_root(root, wait=True)

    # -- 切换目录 ---------------------------------------------------------
    def set_root(self, path, wait: bool = False) -> dict:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        # 不做 resolve()：映射盘会被解析成 \\\\host\\share 的 UNC 形式，显示难看，
        # 而且同一个目录会出现两种写法，缓存键对不上。
        target = Path(os.path.normpath(str(target)))
        if not target.is_dir():
            raise NotADirectoryError(str(target))

        with self._lock:
            if self.scanning:
                raise RuntimeError("正在扫描上一个目录，请稍候")
            self.root = target
            self.index = Index(target, self.cache_dir)
            self.scanning = True
            self.scan_error = ""

        idx = self.index

        def work():
            try:
                idx.refresh()
                self._remember(target)
            except Exception as exc:
                self.scan_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.scanning = False

        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True, name="scan").start()
        return {"root": str(target), "scanning": self.scanning}

    def _remember(self, path: Path) -> None:
        s = str(path)
        self.recent = [s] + [p for p in self.recent if p.lower() != s.lower()]
        self.recent = self.recent[:8]
        cfg = load_config(self.cache_dir)
        cfg["root"] = s
        cfg["recent"] = self.recent
        save_config(self.cache_dir, cfg)

    # -- 状态 -------------------------------------------------------------
    def status(self) -> dict:
        idx = self.index
        return {
            "root": str(self.root) if self.root else None,
            "name": self.root.name if self.root else None,
            "scanning": self.scanning,
            "error": self.scan_error,
            "stats": idx.stats() if idx else None,
            "recent": self.recent,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = f"XiaomiCamViewer/{VERSION}"
    protocol_version = "HTTP/1.1"
    app: App

    # -- 输出 -------------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _serve_file(self, path: Path, ctype: str, download: str | None = None,
                    range_ok=True):
        try:
            size = path.stat().st_size
        except OSError:
            return self._err(404, "文件不存在")

        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")
        if range_ok and rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].strip().partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
                partial = True
            except ValueError:
                partial = False

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{urllib.parse.quote(download)}")
        self.end_headers()

        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- 路由 -------------------------------------------------------------
    def do_HEAD(self):
        self.route(head=True)

    def do_GET(self):
        self.route(head=False)

    def do_POST(self):
        try:
            u = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(u.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if path == "/api/upload":
                return self._api_upload(q)
            if path == "/api/upload/finish":
                return self._api_upload_finish(q)
            return self._err(404, "未知路径")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # 顶层兜底
            self.close_connection = True
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

    # -- 上传（手机/其他设备上的视频文件）----------------------------------
    def _api_upload(self, q):
        raw = q.get("name", "").strip()
        batch = q.get("batch", "").strip()
        if not raw:
            return self._err(400, "缺少 name 参数")
        if not re.fullmatch(r"\d{8}-\d{6}", batch):
            return self._err(400, "batch 需为 YYYYMMDD-HHMMSS")
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.basename(raw)).strip(" .")
        name = name[:120] or f"video-{int(time.time())}.mp4"
        if not name.lower().endswith((".mp4", ".mov", ".m4v", ".mkv", ".avi")):
            name += ".mp4"
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if total <= 0:
            return self._err(411, "需要 Content-Length")

        target_dir = self.app.cache_dir / "uploads" / batch
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._err(500, f"无法创建上传目录：{exc}")

        target = target_dir / name
        n = 1
        while target.exists():
            target = target_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1

        wrote = 0
        try:
            with open(target, "wb") as f:
                left = total
                while left > 0:
                    chunk = self.rfile.read(min(1048576, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
                    wrote += len(chunk)
        except OSError as exc:
            target.unlink(missing_ok=True)
            self.close_connection = True
            return self._err(500, f"写盘失败：{exc}")

        if wrote < total:                     # 客户端断流，别留下半截文件
            target.unlink(missing_ok=True)
            self.close_connection = True
            return self._err(400, "上传中断")

        self._json({"ok": True, "name": target.name, "bytes": wrote,
                    "dir": str(target_dir)})

    def _api_upload_finish(self, q):
        """上传结束：探测每段真实时长，按顺序写时间表，避免时间轴重叠。"""
        batch = q.get("batch", "").strip()
        if not re.fullmatch(r"\d{8}-\d{6}", batch):
            return self._err(400, "batch 需为 YYYYMMDD-HHMMSS")
        target_dir = self.app.cache_dir / "uploads" / batch
        if not target_dir.is_dir():
            return self._err(404, "上传目录不存在")
        try:
            files = [p for p in target_dir.iterdir()
                     if p.is_file() and p.suffix.lower() in
                     (".mp4", ".mov", ".m4v", ".mkv", ".avi")
                     and p.stat().st_size >= 1024]
        except OSError as exc:
            return self._err(500, str(exc))
        files.sort(key=lambda p: p.name.lower())
        if not files:
            return self._err(400, "没有可用的视频文件")

        start = int(files[0].stat().st_mtime)
        items = []
        for p in files:
            dur = self.app.encoder.probe_duration(str(p)) or 60.0
            items.append({"name": p.name, "start": start, "dur": round(dur, 3)})
            start += int(round(dur)) + 1
        try:
            (target_dir / "sources.json").write_text(
                json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
        except OSError as exc:
            return self._err(500, f"写入时间表失败：{exc}")
        self._json({"ok": True, "dir": str(target_dir), "files": len(items),
                    "seconds": round(sum(i["dur"] for i in items), 1)})

    def route(self, head=False):
        try:
            u = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(u.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[8:])
            api = {
                "/api/config": self._api_config,
                "/api/scan": self._api_scan,
                "/api/days": self._api_days,
                "/api/day": self._api_day,
                "/api/status": self._api_status,
                "/api/prepare": self._api_prepare,
                "/api/stream": self._api_stream,
                "/api/media": self._api_media,
                "/api/thumb": self._api_thumb,
                "/api/export": self._api_export,
                "/api/reindex": self._api_reindex,
                "/api/fs/drives": self._api_fs_drives,
                "/api/fs/list": self._api_fs_list,
                "/api/fs/open": self._api_open,
            }.get(path)
            if api:
                return api(q)
            return self._err(404, "未知路径")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # 顶层兜底，单请求出错不影响服务
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

    def _static(self, rel):
        rel = rel.lstrip("/")
        base = WEB_DIR.resolve()
        target = (base / rel).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            return self._err(404, "not found")
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".png": "image/png",
                 ".ico": "image/x-icon"}.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- API --------------------------------------------------------------
    def _api_config(self, q):
        enc = self.app.encoder
        st = self.app.status()
        try:
            bound_host, bound_port = self.server.server_address[:2]
        except Exception:
            bound_host, bound_port = "127.0.0.1", 8765
        lan = lan_addresses(bound_port) if bound_host in ("0.0.0.0", "", "::") else []
        self._json({
            "version": VERSION,
            "root": st["root"],
            "name": st["name"],
            "scanning": st["scanning"],
            "error": st["error"],
            "lan": lan,
            "lan_enabled": bool(lan),
            "stats": st["stats"] or {
                "files": 0, "bytes": 0, "days": 0, "cameras": [],
                "skipped": 0, "scan_seconds": 0, "first": None, "last": None,
            },
            "recent": st["recent"],
            "cache": enc.cache_stats(),
            "encoder": {"height": enc.height, "fps": enc.fps, "crf": enc.crf,
                        "ffmpeg": bool(FFMPEG)},
            "now": int(time.time()),
        })

    def _api_scan(self, q):
        self._json(self.app.status())

    def _api_days(self, q):
        idx = self.app.index
        self._json(idx.day_list() if idx else [])

    # -- 目录浏览 ---------------------------------------------------------
    def _api_fs_drives(self, q):
        self._json(list_drives())

    def _api_fs_list(self, q):
        raw = q.get("path", "")
        if not raw:
            return self._json({"path": "", "parent": None, "dirs": [], "root": True})
        p = Path(raw)
        if not p.is_dir():
            return self._err(404, f"目录不存在或无权访问：{raw}")
        try:
            dirs = list_subdirs(p)
        except OSError as exc:
            return self._err(403, f"无法读取该目录：{exc.strerror or exc}")
        self._json({"path": str(p), "parent": parent_of(p), "dirs": dirs, "root": False})

    def _api_open(self, q):
        """切换录像目录，扫描在后台跑，前端轮询 /api/scan。"""
        raw = q.get("path", "").strip()
        if not raw:
            return self._err(400, "缺少 path 参数")
        try:
            res = self.app.set_root(raw, wait=False)
        except NotADirectoryError:
            return self._err(400, f"不是一个有效目录：{raw}")
        except RuntimeError as exc:
            return self._err(409, str(exc))
        self._json({"ok": True, **res})

    def _api_reindex(self, q):
        if not self.app.root:
            return self._err(400, "还没有选择录像目录")
        try:
            self.app.set_root(self.app.root, wait=False)
        except RuntimeError as exc:
            return self._err(409, str(exc))
        self._json({"ok": True, "scanning": True})

    def _seg(self, q):
        """取片段：统一处理"还没选目录"和"片段不存在"。"""
        idx = self.app.index
        if not idx:
            self._err(400, "还没有选择录像目录")
            return None
        seg = idx.get(q.get("id", ""))
        if not seg:
            self._err(404, "片段不存在")
            return None
        return seg

    def _api_day(self, q):
        day = q.get("date", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return self._err(400, "date 需为 YYYY-MM-DD")
        idx = self.app.index
        if not idx:
            return self._json({"date": day, "count": 0, "seconds": 0, "segments": []})
        segs = idx.day_segments(day)
        enc = self.app.encoder
        items = []
        for s in segs:
            d = s.api()
            d["cached"] = enc.is_cached(s.uid)
            items.append(d)
        self._json({
            "date": day,
            "count": len(items),
            "seconds": round(sum(i["t"] for i in items), 1),
            "segments": items,
        })

    def _api_status(self, q):
        seg = self._seg(q)
        if not seg:
            return
        self._json({"id": seg.sid, "cached": self.app.encoder.is_cached(seg.uid),
                    "dur": seg.dur, "size": seg.size})

    def _api_prepare(self, q):
        seg = self._seg(q)
        if not seg:
            return
        self.app.encoder.schedule_cache(seg)
        self._json({"queued": True, "cached": self.app.encoder.is_cached(seg.uid)})

    def _api_thumb(self, q):
        seg = self._seg(q)
        if not seg:
            return
        try:
            t = float(q.get("t", "1"))
        except ValueError:
            t = 1.0
        p = self.app.encoder.thumb(seg.path, seg.uid, min(t, max(0.0, seg.dur - 0.5)))
        if not p:
            return self._err(500, "缩略图生成失败")
        self._serve_file(p, "image/jpeg")

    def _api_media(self, q):
        seg = self._seg(q)
        if not seg:
            return
        enc = self.app.encoder
        if not enc.is_cached(seg.uid):
            if not enc.cache(seg.uid, seg.path):
                return self._err(500, "转码失败")
        self._serve_file(enc.video_path(seg.uid), "video/mp4")

    def _api_stream(self, q):
        """实时转码边转边播：首字节约 1 秒，适合立即起播。"""
        seg = self._seg(q)
        if not seg:
            return
        if not FFMPEG:
            return self._err(500, "未找到 ffmpeg，无法转码")

        try:
            ss = max(0.0, min(float(q.get("ss", "0")), max(0.0, seg.dur - 0.2)))
        except ValueError:
            ss = 0.0
        try:
            want = float(q.get("t", "0"))
        except ValueError:
            want = 0.0
        left = max(0.3, seg.dur - ss)
        dur = min(left, want) if want > 0 else left

        proc = self.app.encoder.stream(seg.path, ss, dur)

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.close_connection = True
        self.end_headers()

        sent = 0
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdout.close()   # type: ignore[union-attr]
            except Exception:
                pass
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if sent > 8192:
                self.app.encoder.schedule_cache(seg)   # 播过的顺手转好，下次秒开

    def _api_export(self, q):
        day = q.get("date", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return self._err(400, "date 需为 YYYY-MM-DD")
        segs = self.app.index.day_segments(day) if self.app.index else []
        if not segs:
            return self._err(404, "该日期没有录像")
        if not FFMPEG:
            return self._err(500, "未找到 ffmpeg")

        lo = parse_rel_time(q.get("from", ""))
        hi = parse_rel_time(q.get("to", ""))
        lo = 0 if lo is None else lo
        hi = 86399 if hi is None else hi
        if hi <= lo:
            return self._err(400, "结束时间需晚于开始时间")

        picked = [s for s in segs if lo <= s.rel <= hi]
        if not picked:
            return self._err(404, "该区间没有录像")
        if len(picked) > 720:
            return self._err(400, "区间过大，请缩小到 6 小时以内")

        enc = self.app.encoder
        listfile = enc.cache_dir / f"concat-{os.getpid()}-{int(time.time() * 1000)}.txt"
        lines = ["ffconcat version 1.0"]
        for s in picked:
            lines.append("file '" + s.path.replace("'", r"'\''") + "'")
        listfile.write_text("\n".join(lines) + "\n", "utf-8")

        cmd = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
               "-f", "concat", "-safe", "0", "-i", str(listfile),
               "-map", "0:v:0", "-map", "0:a:0?",
               "-vf", f"scale=-2:{enc.height}:force_original_aspect_ratio=decrease",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", str(enc.crf),
               "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(enc.fps),
               "-c:a", "aac", "-b:a", "96k",
               "-movflags", "frag_keyframe+empty_moov+default_base_moof",
               "-f", "mp4", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)

        name = f"{day}_{q.get('from', '0000')}-{q.get('to', '2400')}.mp4".replace(":", "")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.close_connection = True
        self.end_headers()
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            listfile.unlink(missing_ok=True)

    def log_message(self, fmt, *args):
        if "/api/stream" in self.path or "/api/media" in self.path:
            return
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="xiaomi-camera-viewer",
        description="把米家摄像头碎片录像整合成可拖时间轴回放的监控台",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--root", default=None,
                   help="录像根目录；不填则启动后在网页里选择（会记住上次选的）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="0.0.0.0",
                   help="监听地址；默认 0.0.0.0 表示同一局域网的手机也能访问，"
                        "只想本机用就填 127.0.0.1")
    p.add_argument("--cache", default=None, help="转码缓存目录，默认用户本地缓存")
    p.add_argument("--cache-max-gb", type=float, default=20.0,
                   help="缓存上限，超出按最久未用淘汰；0 表示不缓存")
    p.add_argument("--height", type=int, default=720, help="转码输出高度")
    p.add_argument("--fps", type=int, default=15, help="转码输出帧率")
    p.add_argument("--crf", type=int, default=26, help="x264 质量，越小越清晰")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)   # type: ignore[attr-defined]
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    if not FFMPEG:
        print("[!] 未找到 ffmpeg，请先安装并加入 PATH：https://ffmpeg.org", file=sys.stderr)
        return 2

    cache_dir = Path(args.cache).expanduser().resolve() if args.cache else default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if free_bytes(cache_dir) < 1024 ** 3:
        print(f"[!] 缓存目录所在磁盘剩余空间不足 1GB: {cache_dir}", file=sys.stderr)
        return 2

    print(f"[*] 缓存目录 : {cache_dir}")

    app = App(cache_dir, args.height, args.fps, args.crf, args.cache_max_gb)

    # 初始目录：命令行 --root 优先，其次上次打开过的
    candidates: list[Path] = []
    if args.root:
        candidates.append(Path(args.root).expanduser())
    last = load_config(cache_dir).get("root")
    if last:
        candidates.append(Path(last))

    chosen: Path | None = None
    for cand in candidates:
        try:
            if cand.is_dir():
                chosen = cand
                break
            if args.root:
                print(f"[!] 目录不存在，忽略：{cand}", file=sys.stderr)
        except OSError:
            continue

    if chosen:
        print(f"[*] 录像目录 : {chosen}")
        print("[*] 扫描索引 ...", end="", flush=True)
        app.set_root(chosen, wait=True)
        st = app.index.stats() if app.index else None
        if st:
            print(f"\r[*] 索引完成 : {st['files']} 片段 / {human_size(st['bytes'])} / "
                  f"{st['days']} 天 / 摄像头 {len(st['cameras'])} 个 / "
                  f"{st['scan_seconds']}s / 跳过损坏 {st['skipped']}")
            if not st["files"]:
                print("[!] 该目录里没有可识别的 mp4，请在网页里换一个目录。",
                      file=sys.stderr)
        avail = free_bytes(chosen)
        if 0 <= avail < 1024 ** 3:
            print(f"[i] 提示：录像所在磁盘仅剩 {human_size(avail)}，只读取不写入。")
    else:
        print("[i] 尚未选择录像目录，启动后在网页里点「选择文件夹」。")

    handler = type("BoundHandler", (Handler,), {"app": app})
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        print(f"[!] 端口 {args.port} 无法监听: {exc}", file=sys.stderr)
        return 2
    httpd.daemon_threads = True

    url = f"http://127.0.0.1:{args.port}/"
    print(f"[*] 本机访问 : {url}")
    if args.host in ("0.0.0.0", "", "::"):
        lan = lan_addresses(args.port)
        if lan:
            print("[*] 手机访问 : " + "   ".join(lan))
            print("[i] 同一 Wi-Fi 下的手机/平板打开上面的地址即可，"
                  "文件夹列表就是这台电脑的磁盘。")
        print("[!] 注意：服务没有密码，局域网内任何人都能看这些录像。"
              "不想开放时用 --host 127.0.0.1 重开。")
    else:
        print(f"[*] 仅监听 {args.host}，其他设备无法访问")
    print("[*] Ctrl+C 停止")

    if not args.no_browser:
        try:
            import webbrowser
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
