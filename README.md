# Bilibili Live Audio-Only Player — FFmpeg（完全dsh制作）

只听 B 站直播声音的小工具。给定直播间号，在后台把直播音频放出来，不开浏览器、不拉视频轨。

```
python -m bililive 房间号
python -m bililive 房间号 --volume 60
```

## 目录

- [安装](#安装)
- [使用](#使用)
- [挂机时的行为](#挂机时的行为)
- [流量占用](#流量占用)
- [实现](#实现)
- [代码结构](#代码结构)
- [开发](#开发)
- [已知限制](#已知限制)

## 安装

需要 Python 3.8 以上。程序只用标准库，没有第三方依赖。

唯一的外部程序是 `ffplay.exe`，用仓库里的脚本获取，Releases文件里已包含ffplay：

```
python probe/fetch_ffmpeg.py      # 下载静态 ffmpeg 包（含 ffplay / ffprobe）
python probe/extract_ffplay.py    # 从包里解出 ffplay.exe
```

产物在 `tools/`。如果你已经有 ffplay，用环境变量 `BILILIVE_FFPLAY` 指向它即可，跳过上面两步。

**免安装版**：`bililive-portable/` 目录自带 `ffplay.exe`，双击 `start.bat` 按提示输入房间号。目标机器仍需装 Python（3.8+），但不需要单独准备 ffmpeg。
**桌面版**；`bililive-1.3-win64` 是一个有前端的版本,可以直接填入房间号使用，并且随意调整声音

## 使用

```
python -m bililive <房间号> [--volume 0-100] [-v]
```

| 参数 | 说明 |
|---|---|
| `<房间号>` | 直播间号，短号和真实房间号都能用 |
| `--volume` | 音量，0 到 100，默认 100。0 为静音 |
| `-v` | 输出详细日志，排查问题时使用 |

音量超出 0-100 会被夹紧而不是放大，这是 ffplay 自身的行为：

```
-volume=150  > 100, setting to 100
-volume=-5   < 0,   setting to 0
```

要更大的声音，用 Windows 音量合成器单独调 `ffplay.exe`，不影响其他程序。

## 挂机时的行为

**流地址过期** —— 每条地址有效期约 1 小时。工具会在剩余 10 分钟时主动换新，不等它真的断掉。

**CDN 不通** —— 一次接口返回 12 条候选，分布在不同 CDN。工具在同一优先级层内并行探测，取最先返回数据的那条；播放中途失败则自动换下一条，不需要你干预。

**主播没开播** —— 不会退出，每 30 秒查一次，一开播自动开始：

```
[02:24:30] 该直播间当前没有开播（live_status=2）。
[02:24:30] 工具会每 30 秒自动检查一次，一开播就会开始播放。
```

**房间号打错** —— 直接退出并说明原因，不会无限重试：

```
[02:24:09] 房间号有问题: room_init(999999999) 失败: code=60004 msg=房间不存在
[02:24:09] 请确认房间号是否正确，然后重新运行。
```

**关掉窗口** —— 声音立刻停，不会留下后台进程继续放音。

**ffplay 崩溃** —— 指数退避重试，2 秒起步、60 秒封顶；如果还有备用候选地址，则立即换一条。

## 流量占用

同一房间统计 10 秒内实际接收的字节：

| | 实测码率 | 每小时 | 每天 | 每月 |
|---|---|---|---|---|
| 纯音频（本工具） | 150 kbps | 65 MB | 1.51 GB | 45 GB |
| 看视频（点开直播间） | 692 kbps | 297 MB | 6.96 GB | 209 GB |

复现：`python probe/measure_traffic.py <房间号> 10`

样本说明：以上来自单次 10 秒抽样，样本较小；692 kbps 是 B 站默认档位，不代表你平时的观看画质。省流量的方向是确定的，具体倍数会随房间和画质变化。

## 实现

### 接口

全程不需要登录，用两个 B 站接口取流。

```
GET https://api.live.bilibili.com/room/v1/Room/room_init?id=<房间号>
    -> data.room_id        真实房间号（短号会重定向，以这个为准）
    -> data.live_status    0 未开播 / 1 直播中 / 2 轮播

GET https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo
    ?room_id=<room_id>&protocol=0,1&format=0,1,2&codec=0,1&qn=10000
    &platform=web&only_audio=1&only_video=0
    -> data.playurl_info.playurl.stream[].format[].codec[].url_info[]
```

关键是第二个接口的 `only_audio=1`。带这个参数时，服务端返回的 FLV 中**只包含音频标签**，实测 `audio tag=685 / video tag=0`——视频数据不下发，而不是下发后丢弃。流参数：

| 项 | 值 |
|---|---|
| 容器 | FLV |
| 音频编码 | AAC-LC |
| 采样率 / 声道 | 48000 Hz / 2ch |
| 码率 | 约 150 kbps（随房间与档位浮动） |

不含视频轨，解码侧没有视频开销，常驻进程的 CPU 占用接近 0。

### 地址构造与有效期

`getRoomPlayInfo` 返回的每条候选是**三段拼接**，不是完整 URL：

```
host       自带 scheme 的完整域名，如 https://cn-xxx.bilivideo.com
base_url   相对路径，结尾带 '?'，如 /live-bvc/xxx.flv?
extra      不带前导 '&' 的签名参数
```

拼接顺序必须是 `host + base_url + extra`。直接字符串相加会把域名写进 query（报 `unknown url type`），`urljoin` 会丢掉 `base_url` 里的 query——两种写法都不可用。

一次请求返回 12 条候选，分布在不同 CDN，来自 `format`/`codec` 的组合，优先级按实测定序（`flv` 优先于 `hls`）。每条地址带 `expires` 时间戳，有效期约 1 小时。

### 播放

取流、解码、音频输出全部交给 ffplay，启动参数：

```
ffplay -hide_banner -loglevel info -nostats -nodisp -vn -autoexit
       -volume <0-100> -infbuf -probesize 32 -analyzeduration 0
       -tls_verify 0 -headers "Referer: https://live.bilibili.com/\r\n" <url>
```

几个关键参数：

- `-nodisp -vn`：不开窗口，丢弃视频轨。
- `-infbuf`：取消输入缓冲上限，避免缓冲不足导致断续。不能与 `-fflags nobuffer` 同时使用，两者目标相反，同时给会使直播延迟只增不减。
- `-probesize 32 -analyzeduration 0`：跳过默认探测，把 ffplay 识别音频参数的时间从 5.91s 降到 0.92s。这只影响探测阶段，从启动到真正出声还取决于 CDN 首批数据的到达时间。
- `-tls_verify 0`：原因见 [已知限制](#已知限制)。
- `-headers`：必须带 `Referer: https://live.bilibili.com/`，否则 CDN 拒绝请求。

Python 侧不参与数据通路，只负责请求接口、拼接地址、启动并监视 ffplay 进程、在地址过期前换新、进程异常退出时决定重试策略。

## 代码结构

```
bililive/
  __init__.py   版本与架构说明
  biliapi.py    房间号 -> 纯音频流地址
  ffplay.py     ffplay 进程管理、输出收集、退出判定
  play.py       主循环：候选轮换、续期、退避、退出分类
  jobobject.py  Windows Job Object 封装
  __main__.py   python -m bililive 入口

  # 早期的管道方案，已弃用，保留作参考，不在发行包里
  flvdemux.py   增量 FLV 解析 + ADTS 拼装
  stream.py     收流引擎
  player.py     ffmpeg 管道解码
  waveout.py    winmm.waveOut 流式播放
  cli.py        管道版 CLI

probe/          开发期的验证与排障脚本
```

## 开发

`probe/` 下是开发过程中攒下的脚本。可以离线跑的测试：

```
python probe/test_adts.py            # FLV 解复用与 ADTS 拼装，21 项字节级断言
python probe/test_waveout.py         # waveOut 缓冲记账（静音，不占声卡）
python probe/test_failure_paths.py   # 故障注入：各种异常退出路径
```

另有流量对比脚本 `measure_traffic.py` 和一批 `diag_*.py` 排障脚本。

构建免安装版：

```
python probe/build_portable.py
```

脚本会检查：启动器是纯 ASCII、产物里没有开发机的绝对路径、弃用模块未混入、版本号一致。

## 已知限制

- **依赖 B 站私有接口**，属于逆向实现。对方改协议就得跟着改。
- **不登录只能拿默认音质档位。** 更高音质需要 cookie，而 cookie 会过期，目前没有处理。
- **续期时约有 0.3~1 秒断音。** 地址过期需要重启 ffplay。
- **音量只能在启动时指定**，运行时调节要用系统音量合成器。
- **TLS 证书校验已关闭。** B 站 CDN 的证书链用 gnutls 校验会失败（`Peer certificate failed verification`），必须加 `-tls_verify 0` 才能连接。代价是放弃验证服务器身份，理论上可被中间人替换音频内容。本工具不传凭据、只听公开直播，风险有限，但这是真实的降级。
- 只走 `http_stream/flv` 这一条流。
- 请仅用于自己收听，不要录播转发。
