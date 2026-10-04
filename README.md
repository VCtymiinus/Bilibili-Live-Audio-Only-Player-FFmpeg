# bililive v1.0 —— 只听哔哩哔哩直播的声音(完全DSH制作)

一个只在后台播放 B 站直播**声音**的本地小工具。不开浏览器、不解码视频、常驻挂机。

```
python -m bililive 26774400                 # 播放
python -m bililive 26774400 --volume 60     # 指定音量
python -m bililive --version                # 查看版本
```

发行包在 `bililive-portable-v1.0/`（见文末「打包」）。

---

## 它是怎么做到的

核心结论：**B 站能直接给出纯音频流。**

请求 `getRoomPlayInfo` 时带上 `only_audio=1`，返回的 FLV 里
**只有 audio tag，video tag 为 0**。实测数据：

```
FLV 头部     : audio=True video=False
音频参数     : AAC-LC 48000Hz 2ch
tag 统计     : audio=685  video=0
码率         : 约 150 kbps（随房间不同，实测范围 150~250 kbps）
```

没有视频轨，所以 CPU 占用接近于零。这正是「挂后台听」要的。

链路：

```
房间号 ──room_init──> 真实 room_id
   └─getRoomPlayInfo(only_audio=1)─> 纯音频 FLV 地址
        （约 1 小时有效，一次给 12 条跨 CDN 候选）
        └─ffplay 直连 URL，自己取流/解码/出声
             （Python 只管拿地址、续期、重连、清理）
```

---

## 流量：比看直播省约 78%（单次实测）

同一个房间，分别计 10 秒实际接收字节：

| | 实测码率 | 每小时 | 每天 | 每月 |
|---|---|---|---|---|
| **纯音频**（本工具） | **150 kbps** | 65 MB | 1.51 GB | 45 GB |
| **看视频**（点开直播间） | **692 kbps** | 297 MB | 6.96 GB | 209 GB |

关键证据：纯音频那条流实测 `audio_tag=222`、**`video_tag=0`** ——
视频数据一个字节都没传，不是「传了但不用」。

**这个数字的局限（如实说明）**：来自**单次 10 秒抽样的两点外推**，
样本很小；692 kbps 是 B 站**默认档位**，不是你平时实际观看的档位。
方向（纯音频确实省得多）可靠，具体倍数会随房间和画质变化。

复现：`python probe/measure_traffic.py <房间号> 10`

---

## 长时间挂机的设计

| 问题 | 处理方式 |
|---|---|
| 流地址约 1 小时过期 | **提前 10 分钟主动换新** |
| CDN 建连慢且不稳定 | 一次接口拿 **12 条候选**，失败自动轮换到下一条 |
| 读流超时 | 超时设 30 秒 —— 曾设 15 秒，比某些 CDN 的 17 秒建连还短，会永远连不上 |
| ffplay 异常退出 | 指数退避（2s → 60s 封顶）；有备用候选时立即换，不退避 |
| 关窗口后还在响 | **Job Object**：父进程无论怎么死，内核连带终止 ffplay |
| 主播未开播 | 轻量查询开播状态，每 30 秒轮询，一开播就自动开始 |
| 房间号打错 | 识别错误码 60004，立即退出而不是无限重试 |

### 各种情况下的实际行为（均已实测）

**直播间没开播** —— 不报错退出，而是等你：

```
[02:24:25] 房间 1 -> room_id=5440
[02:24:30] 该直播间当前没有开播（live_status=2）。
[02:24:30] 工具会每 30 秒自动检查一次，一开播就会开始播放。
```

**房间号打错** —— 直接退出：

```
[02:24:09] 房间号有问题: room_init(999999999) 失败: code=60004 msg=房间不存在
[02:24:09] 请确认房间号是否正确，然后重新运行。
```

**关掉窗口** —— 声音立刻停。靠 Windows Job Object（内核级进程组），
而不是 `atexit`/`finally` —— 后者在进程被强杀时根本不执行。

---

## 音量怎么调

```
python -m bililive 26774400 --volume 60
```

`--volume` 取 **0 到 100**：`0` 静音，`100` 最大（默认）。
**范围外的值会被夹紧**，不是放大 —— 实测 ffplay 自身行为：

```
-volume=150  > 100, setting to 100
-volume=-5   < 0,   setting to 0
```

想更大声用 Windows 音量合成器单独特调 `ffplay.exe`，那样也不影响其他程序。

---

## 依赖

程序本身**只用 Python 标准库**，无第三方包。Python >= 3.8。

唯一的外部程序是 `ffplay.exe`：

```
python probe/fetch_ffmpeg.py       # 下载静态 ffmpeg（含 ffplay.exe/ffprobe.exe）
python probe/extract_ffplay.py     # 从已下载的包里解出 ffplay.exe
```

产物在 `tools/`。也支持环境变量 `BILILIVE_FFPLAY` / `BILILIVE_PYTHON`。

---

## 验证状态

### 已实测通过（客观数据）

| 项目 | 结果 |
|---|---|
| 纯音频流确认 | `video tag = 0`，AAC-LC 48kHz 2ch |
| FLV 解复用 | 真实流抠出 685 帧，时间戳连续无断裂 |
| ADTS 拼装 | 21 项字节级断言全过，含「3 字节一片投喂」跨边界用例 |
| ffplay 直连 | 连续播放 14.3s / 50s 无退出，无报错 |
| Job Object | 强杀父进程后 ffplay 被自动带走，无孤儿进程 |
| 未开播路径 | 正确识别并进入轮询；坏房间号直接退出 |
| 便携性 | 复制到临时目录、从完全不同路径启动成功 |
| 故障路径 | `test_failure_paths.py` 全部通过（离线故障注入） |

### 一键复现

```
python probe/test_adts.py              # ADTS/解复用，不联网
python probe/test_waveout.py           # waveOut 记账，不联网、不出声
python probe/test_failure_paths.py     # 故障注入，不联网、不出声
python probe/measure_traffic.py 26774400 10   # 流量对比，联网
```

### 仍需你确认的

**实际听感**。开发环境无法回听 ffplay 的音频输出，音质、延迟是否合适只能由你判断。

---

## 踩过的坑（都修了，记录备查）

### 1. `hdr.dwFlags = 0` 抹掉 `WHDR_PREPARED`
`waveOutWrite` 必返 `34 = MMSYSERR_INVALPARAM`。实测：保留 flags 或显式写回
`WHDR_PREPARED` 都能成功，清零必失败。MSDN 明确说调用方不得修改 `dwFlags`。
极隐蔽——单次写就失败，跟缓冲管理毫无关系。

### 2. `READ_TIMEOUT = 15s` 短于 CDN 建连时间
实测有的 CDN 要 17 秒才建连成功，超时设 15 秒会导致在这些 CDN 上
永远连不上，表现为无限重连。已放宽到 30 秒。

### 3. 流地址拼接不能靠字符串相加，也不能用 `urljoin`
`base_url` 是纯相对路径且结尾带 `?`，`host` 是自带 scheme 的完整 URL，
`extra` 是不带前导 `&` 的签名参数。两种直觉写法都错：相加会把域名塞进
query（报 `unknown url type`），`urljoin` 会丢掉 base 里的 query。
正确顺序：`host + path + '?' + base_query + '&' + extra`。

### 4. ffplay 的 TLS 证书校验会失败
报 `Peer certificate failed verification`，必须加 `-tls_verify 0`。
Python 的 OpenSSL 能过，ffmpeg 的 gnutls 过不了。
**安全代价**：等于放弃验证服务器身份，理论上可被中间人替换音频内容。
本工具不传凭据，风险有限，但这是真实的降级。

### 5. 管道方案在 Windows 上会死锁（已放弃）
最初设计是自己读 HTTP 流 → 解 FLV → 把 ADTS 喂给 ffmpeg stdin →
读 stdout 取 PCM → 推 waveOut。这条链会挂死：ffmpeg 等读缓冲被填满，
我这边等它的输出，双方互等。命令行直接吃 ADTS 文件完全正常，
说明是管道时序问题。现改为 URL 直接交给 ffplay。

### 6. `cmd` 按 OEM 代码页读 `.bat`，中文会撕碎脚本
早期 `play.bat` 里写了中文注释，被 GBK 解码成乱码后**当成命令执行**，
整个脚本被撕碎，窗口一闪就关且没有任何错误提示。
**启动器必须纯 ASCII**，构建脚本里有断言检查。

### 7. `py.exe -3` 不能塞进变量再展开
`cmd` 会把 `"C:\...\py.exe -3"` 当成**一个带空格的命令名**，报 9009。
必须拆成两个变量分别调用。这个 bug 会让便携版在任何机器上都无法启动。

### 8. `atexit`/`finally` 救不了被强杀的进程
关 cmd 窗口时 `cmd → python → ffplay` 断在最上层，ffplay 变孤儿继续放音。
必须用内核级 Job Object 兜底。

---

## 代码审查修订记录（v1.0）

`CODE_REVIEW.md` 提出的问题已逐条核实并处理。

### 确认属实，已修

| 编号 | 问题 | 说明 |
|---|---|---|
| P0-1 | 退避是死代码 | `stop()` 后读 `exit_code` 恒为 `None`、`uptime` 恒为 `0.0`，判断永远为假 |
| P0-2 | 候选轮换没实现 | 永远只用 `cands[0]`，README 承诺的「失败就轮换」是空话 |
| P1-1 | 声明了 deflate 却不解压 | 改为只声明 gzip，另留 deflate 兼容分支 |
| P1-2 | 中文子串匹配过宽 | 去掉 `PERMANENT_HINTS`，只认错误码 60004 |
| P1-3 | `-infbuf` 与 `nobuffer` 对冲 | 两参数目标相反，去掉 `nobuffer` 保留 `-infbuf` |
| P1-4 | reader 线程竞态 | 线程改为绑定进程句柄；`stop()` 里 `join()` 旧线程 |
| P2-6 | 丢弃 ffplay 诊断 | loglevel 改 `info`，保留 60 行，错误特征更全 |
| P2-1 | 死代码进发行包 | 发行包只含 6 个在用模块，弃用的 5 个明确排除 |
| P2-2 | `__init__.py` 描述旧架构 | 重写，版本号 0.1.0 → 1.0 |
| P2-3 | 文档数字矛盾 | 统一为 150 kbps，并标注样本局限 |
| P2-4 | 零工程化文件 | 补 `requirements.txt`、`LICENSE` |
| P2-5 | 构建脚本不可移植、校验提前报 OK | `ROOT` 改为相对推导，校验改为先收集后打印 |

### 审查未发现、本次新查出的问题

**ffplay 在彻底连不上时退出码是 0，不是非零。** 实测：

```
http://127.0.0.1:1/nope.flv               -> exit_code=0  "Error number -138 occurred"
http://nonexistent-host-xyz.invalid/a.flv -> exit_code=0  "I/O error"
```

这意味着即使按 P0-1 修好了「读取时机」，`rc not in (0, None)` **依然恒为假**，
退避照样不会触发。审查只发现了「读的时机错」，没发现「这个信号本身是坏的」。

最终改为：失败判定以 **stderr 的错误特征为主**（`FfplayPlayer.had_error`），
退出码只作补充。判定逻辑抽成纯函数 `classify_exit()` 以便离线测试。

另外实测澄清两点，与审查意见不同：

- **`-headers` 含空格不构成风险**。实测 ffplay 正确解析该参数，
  稳定播放 12 秒。之前担心的参数拆分问题不存在。
- **P1-4 的竞态被高估**。代码上确有隐患（旧线程用 `self._proc` 动态取管道），
  但实测旧线程会及时退出，未出现「两线程读同一管道」。仍按正确写法修掉了，
  但实际严重性低于 P0 级。

### 未采纳

- 审查建议「换 openssl 后端的 ffmpeg 构建」以恢复证书校验。
  可行，但需要另找构建且体积可能更大，**收益与成本不匹配**，
  改为在文档中如实标注安全代价。如果确实需要，可以再做。

---

## 第二轮报告（ISSUES.md）的处理

第二轮提出两条，均核实为真并修复。详见 `ISSUES-FIXED.md`。

### 3. 候主流全部过期时零等待死循环

`picked is None` 分支在 `continue` 前没有等待 -> 约 2 次/秒打接口，
一天 25 万次。**且时钟偏快 10 分钟以上时重启也救不了**：
`expires_at` 是服务端绝对时间戳，减去本机时间后剩余量恒为负，
每次启动都会立刻再进这个分支。

修法：等待 `max(backoff, 5.0)` 并让退避翻倍；同时**单独识别时钟偏移**，
直接提示用户校准系统时间（这种情况重启无效）。

实测（`probe/test_spin_loop.py`，离线故障注入）：

```
12 秒内请求 3 次 -> 0.25 次/秒 -> 21,600 次/天
旧实现           -> 约 2 次/秒 -> 172,800 次/天
```

### 4. 发行包混入 `__pycache__`

构建后跑自测，Python 重新生成字节码，产物被运行污染。

修法分三层，**只做第一层不够**：只设 `build_portable.py` 的
`sys.dont_write_bytecode` 后，构建过程干净了，但**用户运行程序**仍会生成。
最终同时设置：

1. `build_portable.py` 最早处 `sys.dont_write_bytecode = True`，
   并把清查挪到所有校验步骤之后
2. `bililive_main.py` 最早处 `sys.dont_write_bytecode = True`
3. `start.bat` 设 `PYTHONDONTWRITEBYTECODE=1`

**顺带好处**：现在可以从只读介质（U 盘、只读目录）直接运行。

验证：构建后连跑 3 次程序 + 一次真实播放，发行包仍无 `__pycache__`。

---

## 打包

```
python probe/build_portable.py
```

产物 `bililive-portable-v1.0/`，约 102 MB：

```
start.bat            双击启动（纯 ASCII，自动找 Python）
ffplay.exe           播放器（静态自包含）
bililive_main.py     入口
bililive/            程序包（只含 6 个在用模块）
README.txt           使用说明
没声音看这里.txt      排查清单
```

构建脚本会自动校验：启动器纯 ASCII、无开发机绝对路径、
弃用模块未混入、版本号一致。

### 关于 exe

尝试过用 PyInstaller 打包，**放弃**，原因：

1. PyPI 在当前网络下连不上（SSL 被中断），装不上 PyInstaller；
2. 更根本的是**收益很小** —— ffplay 有 102 MB，塞进单文件 exe 会让它涨到
   110 MB+，运行时还要解压到临时目录；而目标机器**仍然需要装 Python**。
   exe 只省掉「装 Python」这一件事，却省不掉。

绿色文件夹反而更简单、启动更快、更容易排错。

---

## 代码结构

```
bililive/
  __init__.py   版本与架构说明
  biliapi.py    房间号 -> 纯音频流地址（含所有接口坑的注释）
  ffplay.py     ffplay 进程管理、输出收集、退出判定
  play.py       主循环：候选轮换、续期、退避、退出分类
  jobobject.py  Windows Job Object
  __main__.py   python -m bililive 入口

  # 已弃用的管道方案（不在发行包里，保留作参考）
  flvdemux.py   增量 FLV 解析 + ADTS 拼装
  stream.py     收流引擎
  player.py     ffmpeg 管道解码
  waveout.py    winmm.waveOut 流式播放
  cli.py        管道版 CLI

probe/
  build_portable.py        组装便携包
  portable_template/       start.bat 模板
  test_adts.py             ADTS/解复用自检（离线）
  test_waveout.py          waveOut 记账自检（离线、静音）
  test_failure_paths.py    故障注入（离线、静音）
  measure_traffic.py       流量对比实测
  chk_room.py              房间可用性与候选流试读
  test_ffplay.py           ffplay 直连验证
  fetch_ffmpeg.py          多线程分块下载静态 ffmpeg
  extract_ffplay.py        从包中解出 ffplay/ffprobe
  verify_review.py         针对审查意见的核实脚本
  diag_*.py                排障脚本
```

---

## 已知限制

- **依赖 B 站私有接口**，属于逆向实现。对方改协议就得跟着改。
- **不登录只能拿默认音质档位**。更高音质需要 cookie，而 cookie 会过期。
- **续期会有约 0.3~1 秒断音**：URL 过期需重启 ffplay。这是 A 方案的固有代价。
- **音量只能在启动时指定**，运行时调节要用 Windows 音量合成器。
- **关闭了 TLS 证书校验**，详见上文坑 4。
- 只走 `http_stream/flv` 这一条流。
- 请仅用于自己收听，不要录播转发。
