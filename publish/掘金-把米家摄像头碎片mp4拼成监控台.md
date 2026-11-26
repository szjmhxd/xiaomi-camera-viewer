# 把米家摄像头碎片 mp4 拼成监控台：格式分析与实现

> 本文介绍把米家摄像头导出的碎片录像（每小时一个目录、每分钟一个 mp4）组织成可拖动时间轴回放的自托管方案，覆盖文件名解析、时间轴构建、服务端转码缓存三个核心实现，以及 Docker / 飞牛 fnOS / Windows exe 三种部署形态的落地经验。

## 背景：导出目录长什么样

米家摄像头的本地录像导出后大致是这样：

```
xiaomi_camera_videos/
└── b88880054a3f/          # 设备 ID
    └── 2026112009/        # 按小时分目录
        ├── 0M12S_1763600412.mp4
        ├── 1M12S_1763600472.mp4
        └── …              # 每分钟一个
```

三个决定设计的事实：

| 事实 | 后果 |
| --- | --- |
| 文件名带 unix 时间戳（`M…S…_<ts>.mp4`） | 不用读文件内容就能建索引 |
| 编码是 HEVC + G.711 a-law | Chrome / Edge 默认两项都解不了，必须服务端转码 |
| 单文件恒定一分钟、VFR | 不能按固定帧率拼接，时间轴要按真实时长算 |

样本库规模：26 天、106 GB、8702 个片段。任何「打开文件夹翻一翻」的用法都会在第一周被放弃。

## 总体设计

服务端是一个零 pip 依赖的 Python 单文件（Python 3.9+ 与系统 ffmpeg），前端是三个静态文件。数据流：

```
扫描（后台线程）→ 按天/摄像头索引 → 时间轴（可播区间）
                                      ↓
                            按需转码 → 本地缓存（LRU）
                                      ↓
                          播放 / 倍速 / 区间导出单文件
```

## 关键实现

### 1. 文件名即时间戳

```python
NAME_RE = re.compile(r"^(\d{1,3})M(\d{1,2})S_(\d{9,11})\.mp4$", re.I)
```

解析出起始时间戳后，按天归档。扫描放在后台线程，前端轮询进度——网络盘上扫一万个文件要几十秒，同步扫描会让页面看起来像卡死。

### 2. 时间轴：空档不是「跳过」，是「不存在」

把当天所有片段的区间合并成可播区间。时间轴上的刻度只落在可播区间内，无录像的时间点不可点击，点击直接拦截不起播。这条约束是产品需求而不是优化：监控回放里点到空档弹一句「暂无录像」是不合格的体验。

### 3. 转码与缓存：只转一次

HEVC + G.711 在浏览器里基本无解（Edge 硬解 HEVC 视显卡，音频仍然没有解码器），所以服务端统一转 H.264 + AAC：

```bash
ffmpeg -i input.mp4 -c:v libx264 -crf 26 -vf scale=-2:720 \
       -r 15 -c:a aac output.mp4
```

三个参数是可调的（`CAM_HEIGHT` / `CAM_FPS` / `CAM_CRF`），CPU 弱的 NAS 可以降到 480p。转码结果写进本地缓存目录，带容量上限（默认 20 GB，LRU 淘汰），第二次播放直接走 Range 请求精确拖动，不再碰解码器。

缓存刻意不放录像目录：录像通常在 NAS 或写满的存储卡上，又慢又没有剩余空间。

### 4. 跨片段连播与导出

播放器维护一个「当天播放列表」，当前片段结束自动切下一个，倍速状态保持。导出反向利用同一个列表：圈定时间轴区间后服务端生成 concat 清单，一次 ffmpeg 调用拼成单个文件。

## 部署：一个服务，三种形态

| 形态 | 产物 | 说明 |
| --- | --- | --- |
| Docker | `ghcr.io/szjmhxd/xiaomi-camera-viewer` | 镜像自带 ffmpeg，amd64 + arm64 |
| 飞牛 fnOS | `.fpk` 应用包 | 应用中心安装，走官方「授权目录」挂载 |
| Windows | PyInstaller 单文件 exe | `ffmpeg.exe` 放同目录或进 PATH |

### 实战记录一：ghcr.io 国内拉取用前缀替换

国内直连 ghcr.io 常见 `connection reset`，`daemon.json` 的 `registry-mirrors` 官方只保证对 docker.io 生效。可靠做法是前缀替换 + 打回原名：

```bash
docker pull ghcr.1ms.run/szjmhxd/xiaomi-camera-viewer:1.3.4
docker tag ghcr.1ms.run/szjmhxd/xiaomi-camera-viewer:1.3.4 \
           ghcr.io/szjmhxd/xiaomi-camera-viewer:1.3.4
```

compose 文件一行都不用改。

### 实战记录二：飞牛 fpk 读授权目录，compose 不改写

飞牛应用要读用户已有的录像目录，官方机制是「授权目录」：用户在应用设置里勾选文件夹，平台把路径放进系统变量 `TRIM_DATA_ACCESSIBLE_PATHS`（冒号分隔，且可能为空）。Docker 应用的 compose 由应用自己提供，系统只管启停——授权路径不会自动进容器。

可行的做法是 compose 静态声明 + 变量替换：

```yaml
volumes:
  - "${NAS_DIR:-/var/apps/myapp/share/myapp/videos}:/nas:ro"
```

生命周期脚本把授权路径写进 compose 同目录的 `.env`（`NAS_DIR=…`），再 `docker compose up -d --force-recreate` 让挂载生效。compose 文件本身永远不被脚本改写——改写用户的 compose 挂载段不是官方机制，升级和排障都会出问题。平台没下发变量时用应用数据目录里持久化的上次值恢复。

### exe 打包两处适配

PyInstaller onefile 模式下 `__file__` 指向临时解压目录，静态资源要从 `sys._MEIPASS` 读：

```python
if getattr(sys, "frozen", False):
    WEB_DIR = Path(sys._MEIPASS) / "web"
else:
    WEB_DIR = Path(__file__).resolve().parent / "web"
```

另外让 `which("ffmpeg")` 回退到 exe 同目录，用户把 `ffmpeg.exe` 丢在旁边就能用，不用配 PATH。

## 安全

服务默认无鉴权，局域网内任何设备打开地址都能看录像。启动时打印可访问地址并给出提醒，只本机用加 `--host 127.0.0.1`。公网部署必须自己加反代和鉴权，仓库文档不提供「开箱暴露公网」的路径。

## 总结

碎片录像的核心问题不是画质，是缺少时间维度的组织。文件名里有时间戳，索引成本就趋近于零；时间轴按可播区间建模，空档问题自然消失；转码结果按 LRU 缓存，慢 NAS 也能精确拖动。三个部署形态共用同一个服务，差别只在「ffmpeg 从哪来」和「录像目录怎么挂」。

源码与安装包：https://github.com/szjmhxd/xiaomi-camera-viewer
