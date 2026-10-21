# 风扇控制工具

CPU 风扇与内存风扇的独立命令行控制、实时转速显示、温度自动调速。

| 文件 | 作用 |
|---|---|
| `fanctl.py` | 设置风扇档位（自动 / 手动百分比 / 满速），两个风扇可分开设置 |
| `fanspeed.py` | 实时显示两个风扇转速与当前档位，每秒刷新，关窗口退出 |
| `fanguard.py` | 按温度自动调速的守护进程，**以 Windows 服务形式常驻**（CPU / 内存温度驱动） |
| `fanlib.py` | 公共模块（WMI 接口 + UAC 提权 + 服务状态查询），被上面几个脚本引用 |
| `vendor/LibreHardwareMonitor/` | LibreHardwareMonitor 程序集（10 个文件，约 2 MB） |


配套的 `.bat` 脚本：

| 脚本 | 作用 |
|---|---|
| `status.bat` | **实时**显示：每秒刷新一次转速、当前档位、服务状态（Ctrl+C 或关窗口退出） |
| `set-auto.bat` / `set-max.bat` | 手动把两个风扇设成 AUTO / 满速 |
| `fanguard-install.bat` | 安装并启动 fanguard 服务（开机自启） |
| `fanguard-uninstall.bat` | 停止并卸载 fanguard 服务 |

---

## 安装

```powershell
pip install pywin32 pythonnet
```

需要**管理员权限**。非管理员运行时会自动弹 UAC 提权，并把提权子进程的输出回显到当前终端。

退出码：`0` 成功 / `1` 失败 / `2` 权限不足。

### 装成系统服务（推荐）

```powershell
fanguard-install.bat
```

装完之后服务会**随系统自动启动**，以 LocalSystem 身份运行，不需要登录任何用户：

```powershell
net stop fa880-fan      # 停止（风扇交回固件 AUTO）
net start fa880-fan     # 启动
sc  query fa880-fan     # 看状态
fanguard-uninstall.bat # 卸载
```

也可以在「服务」管理器（`services.msc`）里控制它，显示名是 `FA880 Fan Governor`。
服务崩了会自动重启（5 秒 / 10 秒 / 30 秒各一次）。

服务没有控制台，它做的事都记在**事件日志**里（事件查看器 → 应用程序 → `fa880-fan`）：
每次启停约 8 条（启动、锁、传感器绑定、两条曲线、退出、交回 AUTO、停止），
出错时再有一条错误。正常运行期间不会写任何日志文件。

> 装服务时不用关掉正在跑的旧守护，但建议先结束旧进程——服务启动时会发现锁被占用并拒绝运行。

---

## 用法

### fanctl —— 设置档位

```powershell
fanctl                              # 只报告当前状态，不做任何修改
fanctl --cpu max                    # 只改 CPU 风扇
fanctl --mem auto                   # 只改内存风扇
fanctl --cpu auto --mem max         # 分别设置两个风扇
fanctl --cpu 45 --mem max
fanctl --dump                       # 打印 WMI 方法签名
```

模式取值：`auto`（交回固件曲线）、`max`（满速）、`35`–`100`（手动占空比 %）。
也接受中文 `自动` / `最大`。

**不写 `--cpu` / `--mem` 的那一侧完全保持现状**，不会被顺带改动；
两个都不写时只做只读报告。

### fanspeed —— 实时转速

```powershell
fanspeed            # 每秒刷新，Ctrl+C 或关窗口退出（status.bat 就是它）
fanspeed --interval 2
fanspeed --status   # 一次性打印状态然后退出（不刷新画面）
```

只读，不改任何设置。

```
  Fan speed   2026-10-19 18:31:35   (+4s)
  instance: ACPI\PNP0C14\IP3POWERSWITCH_0

  CPU fan      [#########...................]  1599 RPM   of 4800  ?
  Memory fan   [######################......]  1985 RPM   of 2500  ?

  raw FanDuty: 0x07C1063F   (fan1 = low 16 bits, fan2 = high 16)
  setting: unknown -- the firmware reports RPM only, and no governor is publishing the gear it applied
  service fa880-fan: RUNNING
```

刷新采用**原地重写**（`ESC[nA` 上移 + `ESC[J` 清除到屏幕底）而不是整屏清空，
所以终端不会随着运行时间增长而堆积滚动记录。
顶部那行 `Watching fan speed...` 是静态标题，会一直保留。

进度条按**各风扇自己的实测上限**缩放（CPU 4800、内存 2500），不是共用一条刻度。
最后一行是服务状态（`RUNNING` / `STOPPED` / `not installed`），普通用户也能查。

转速读不到时显示 `?`（值未知），风扇真的停转才显示 `stopped`——两者不是一回事。

#### 不提权查看（自动判断）

`root\WMI` 的方法接口对非管理员一律返回拒绝访问（读也一样），所以
直接读必须提权。

但如果 fanguard 服务正在运行，它会把样本写进**命名共享内存**
（`Global\fa880-fan-telemetry`），fanspeed 会优先读它，完全不需要权限。
所以 `fanspeed` / `status.bat` 会自己判断：

| fanguard 状态 | 行为 |
|---|---|
| 正在运行（样本 < 20 秒） | 读共享内存，**不弹 UAC** |
| 没运行 / 样本过期 | 退回直接读 WMI，弹 UAC |
| 运行中途退出 | 自动切回 WMI 模式 |

```
  Fan speed   19:46:00   (+5s, via fanguard pid 6236)

  CPU fan      [#########...................]  1603 RPM   of 4800  AUTO
  Memory fan   [###################.........]  1737 RPM   of 2500  AUTO

  temperatures: CPU 41.4 C   memory 49.8 C   (from fanguard)
  sample age  : 0.1s
```

服务装好之后，每次看转速都不再有 UAC 弹窗。
`--force-wmi` 可强制走提权路径，`--max-age` 调整过期判定。

> **为什么是 `Global\` 而不是 `Local\`？**
> 服务跑在 session 0，普通程序跑在用户的 session，而 `Local\` 命名空间是
> **按 session 隔离**的——两边根本看不到同一个对象。所以服务创建的内核对象
> 放在全局命名空间，并且带一个显式 DACL，让普通用户也能读
> （只有温度和转速，没有敏感数据）。

#### 档位显示（AUTO / MAX / NN%）

WMI 接口读不回占空比——`GetFanControl` 返回的是打包后的**转速**，
`PowerSwitchInterface` 也没有取回档位的方法（该类只有 `Active` 和
`InstanceName` 两个属性，没有 getter）。

所以档位只有一个来源：**设置它的那个进程**。守护进程每 tick
都会把生效档位发到共享内存总线上，`fanspeed` 直接读它：

```
  CPU fan      [#########...................]  1603 RPM   of 4800  AUTO
  Memory fan   [###################.........]  1737 RPM   of 2500  AUTO
```

**为此不需要往硬盘写任何东西。** 以前专门记档位的 `%LOCALAPPDATA%\fa880-fan\fanstate.ini`
已经取消（旧文件可以直接删掉）：守护进程运行时磁盘写入为**零**
（除非显式用 `--log` 指定日志文件），
也就不会再有档位变化时反复打开/覆写文件、把 SSD 从低功耗里唤醒的事。

没有守护进程在跑（或它卡住不再发布数据）时，档位无从得知，显示 `?` 而不是猜：

```
  setting: unknown -- the firmware reports RPM only, and no governor is publishing the gear it applied
```

`fanctl` 手动改档位同理：命令执行完就退出，没有进程再去发布这个档位，
所以之后 `fanspeed` 的档位列是 `?`（`fanctl` 自己会打印它设成了什么）。
需要精确档位时，看服务的实时画面即可。

#### 写盘清单

整套工具**常驻运行时不写磁盘**（事件日志除外，见下）。所有会落盘的地方列在这里：

| 位置 | 什么时候写 | 说明 |
|---|---|---|
| Windows 事件日志（`fa880-fan` 源） | 每次启停约 8 条信息，出错时 1 条错误 | 服务没有控制台，这是它唯一的出口。**不是每 tick 写** |
| `%LOCALAPPDATA%\fa880-fan\fanstate.ini` | — | **已取消**，不再存在 |
| `%TEMP%\fanctl-*.log` | 提权时 | 提权子进程的输出中继文件。只有在**服务没跑**、工具不得不直接读 WMI 时才出现；读完立刻删 |
| 注册表 `HKLM\...\EventLog\Application\fa880-fan` | **安装时一次** | 事件日志的来源登记；服务启动时会先查一下在不在，在就不再写 |
| `--log FILE` | 每 tick 1 行 | 只有显式指定日志文件时才有 |
| `__pycache__\*.pyc` | 源码改动后 1 次 | Python 的字节码缓存，与风扇无关；工具目录只读时 Python 会自动跳过 |

实测：服务运行 46 秒（约 15 个 tick）**写入 0 字节**，工具目录 / `%LOCALAPPDATA%` /
`%TEMP%` 都没有文件被动过。

两个要点：

- **服务模式全程不写文件**：档位走共享内存，转速走 WMI，事件只写事件日志。
- 前台窗口（`python fanguard.py`）需要把提权子进程的实时画面中继回当前控制台，
  这条路径仍然用临时文件：窗口开着期间它会缓慢增长，关掉窗口即删除。
- **风扇档位本身根本不在磁盘上**：它写在 EC / 固件里，机器断电就没了，
  服务只是每次重新设一遍。

传感器驱动（PawnIO）已经在 DriverStore 里，LibreHardwareMonitor 只是打开它，不会安装或写盘。

### fanguard —— 温度自动调速（系统服务）

```powershell
fanguard-install.bat              # 装成服务并启动（开机自启）
fanguard-uninstall.bat            # 停止并卸载

python fanguard.py --service            # 由服务控制管理器启动（不用手动跑）
python fanguard.py --install-service    # 只安装（可带参数，如 --cpu-max-at 65）
python fanguard.py --uninstall-service  # 只卸载
python fanguard.py --start-service      # 启动
python fanguard.py --stop-service       # 停止（风扇交回 AUTO）

python fanguard.py                     # 前台运行，实时画面；关窗口即停
python fanguard.py --dry-run           # 只记录决策，不动风扇
python fanguard.py --list-sensors      # 列出所有温度传感器
python fanguard.py --log fanguard.log  # 前台运行并额外留一份日志到磁盘
```

服务以 **LocalSystem** 身份运行（需要 WMI 风扇接口和 PawnIO 驱动），随系统自动启动，
不需要任何用户登录。装好后由「服务」管理器或 `net start/stop fa880-fan` 控制。

装服务时可以带上守护参数，它们会进到服务的命令行里：

```powershell
fanguard-install.bat --cpu-max-at 65 --mem-max-at 70
fanguard-install.bat          # 恢复默认参数（会重启服务）
```

装服务时的**相对路径按工具目录解析**（服务的工作目录是 `C:\Windows\System32`），
含空格的路径也没问题（安装时按 Windows 的规则加引号，末尾带反斜杠也不会坏）。
`--list-sensors` 这种一次性的诊断开关不会传给服务。

**服务停止时会把手上的风扇交回固件 AUTO**——`net stop`、`sc stop`、
服务崩溃、机器关机都会走这条路（除非显式加了 `--no-restore`）。

**传感器连续读不到也会退出（错误码 1）**，这样服务控制管理器会按失败动作把它拉起来
（5 秒 / 10 秒 / 30 秒各一次）——风扇不会悄悄停在最后一次的占空比上。

**同一时刻只允许一个实例。** 两个守护进程会互相抢着写风扇设置、往同一个共享内存里
发布数据，而且任一退出时都会把风扇设回 AUTO、覆盖另一个的设置。所以 `fanguard`
启动时用命名互斥量加锁（`Global\fa880-fan-fanguard`），第二个实例会直接报错退出：

```
ERROR: another fanguard instance is already running
Stop the running instance first (net stop fa880-fan).
```

互斥量是**内核对象**，进程一结束内核就释放，断电、崩溃、强制结束都不会留下死锁。
（判断「有没有人在管风扇」靠的是**能不能拿到这个锁**，而不是对象还在不在——
对象在但没人持有，是上一次运行留下的残骸。）
`--dry-run` 和 `--list-sensors` 是只读的，不会被运行中的守护挡住。

**怎么知道有没有在跑**：`fanspeed --status` 同时看三个信号——

```powershell
python fanspeed.py --status
# running  pid=6756  age=2.7s  cpu=42.3 C  mem=48.8 C
# service fa880-fan: RUNNING
#   CPU fan      [#########...................]  1603 RPM   of 4800  AUTO
#   Memory fan   [###################.........]  1737 RPM   of 2500  AUTO

# running  (holds the lock, no sample published yet)   ← 正在启动
# running  (holds the lock, but its last sample is 45.0s old)  ← 卡住了
# not running
# service fa880-fan: STOPPED
```

- **互斥量**是权威信号：谁持有它，谁就控制着风扇，哪怕它已经不再发布数据。
- **共享内存总线**说明它还活着并且在发布数据。
- **服务状态**直接问服务控制管理器，普通用户也能查（不需要提权）。

三者都是内核对象 / SCM 查询：不可能像磁盘上的锁文件那样留下残留。
（退出码 0 = 有守护在跑，1 = 没有。）

停止服务请用 `net stop fa880-fan`（或 `python fanguard.py --stop-service`）：
服务会在下一个 tick 之前**干净退出**并把风扇交回 AUTO。
直接 `taskkill` 会跳过这一步，风扇会停在上一次的占空比上。

> ⚠️ **服务在跑时不要用 `fanctl` 改档位。** 守护进程按自己的曲线每个 tick 都会
> 重写风扇设置，`fanctl` 改的手动档位会在下一个 tick（约 3 秒）被覆盖掉。
> 单实例锁只防「两个守护进程」，不防「守护 + fanctl」这种组合。
> `fanctl` 检测到服务在跑会给出提示；要真正手动控制，先
> `net stop fa880-fan`（风扇交回固件 AUTO），用完再 `net start fa880-fan`。

固件的 AUTO 曲线在负载高时到不了满速，CPU 会闷热掉性能。
`fanguard` 用 LibreHardwareMonitor 读温度，按温度自动升降转速。

传感器映射（本机实测）：

| 用途 | 传感器 | 实测值 |
|---|---|---|
| CPU 风扇 ← | `Core (Tctl/Tdie)` | 40 °C |
| 内存风扇 ← | `max(DIMM #0, DIMM #1)` | 48 / 46 °C |

内存温度是 DDR5 SPD 传感器实测值（`Micron CT32G56C46S5`）。
本机是核显（Radeon 780M 集成在 CPU 封装内），显存用的是共享内存，
没有独立的显存温度传感器，而且内存风扇对显存也起不到散热作用，
所以不把 GPU / 显存纳入监控。

控制逻辑：

```
温度 >= --cpu-max-at / --mem-max-at   ->  MAX (100 %)
温度在 --auto-below ~ 阈值 之间       ->  按线性比例从 --min-duty 爬到 100 %
温度 <  --auto-below                  ->  交回固件 AUTO（可自动停转降噪）
```

默认阈值：

| 风扇 | 交回 AUTO | 拉满温度 |
|---|---|---|
| CPU 风扇 | **50 °C** | **70 °C** |
| 内存风扇 | **50 °C** | **75 °C** |

CPU 风扇实际映射：

| 温度 | ≤52 | 53 | 55 | 60 | 65 | **≥70** |
|---|---|---|---|---|---|---|
| duty | AUTO | 40% | 50% | 65% | 80% | **100%** |
| 实测 RPM | AUTO | ~2210 | 2697 | 3388 | 4007 | **4739** |

参数一览：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--auto-below` | 50 | 低于此温度交回 AUTO |
| `--cpu-max-at` | 70 | CPU 风扇在此温度拉满 |
| `--mem-max-at` | 75 | 内存风扇在此温度拉满 |
| `--hysteresis` | 3 | AUTO 边界的滞环度数，避免临界抖动 |
| `--interval` | 3 s | 决策间隔 |
| `--step` | 5 | 占空比量化粒度（%） |
| `--min-duty` / `--max-duty` | 35 / 100 | 占空比上下限 |
| `--duration` | 0 | 运行多少秒后自动停止，0 = 一直跑 |
| `--max-failures` | 5 | 连续几轮读不到温度就放弃控制并恢复 AUTO（只统计本机实际存在的传感器；没有 DIMM 温度传感器的机器不受影响） |

占空比按 `--step` **向下取整**，保证只有真正达到阈值温度时才会到 MAX。

档位列显示的是**实际生效**的档位；? 表示尚未成功下发过（值未知）。
--dry-run 模式下永远不会真正下发，所以该列显示的是**决策值**（即「如果真跑会设成什么）。

---

## 接口技术参考

风扇控制走 WMI（ACPI `PNP0C14` 标准接口，Clevo / 蓝天 / 同方系游戏本通用）：

```
命名空间 : root\WMI
类       : PowerSwitchInterface
实例     : ACPI\PNP0C14\IP3POWERSWITCH_0
方法     : SetFanControl(FanNumber, FanDuty) -> ResultStatus
           GetFanControl(FanNumber)          -> FanDuty
```

FanDuty 语义：

| 值 | 模式 |
|---|---|
| `101` | **AUTO**，超出量程的哨兵值，交回 EC 固件曲线 |
| `100` | **MAX**，满速 |
| `35`–`100` | **手动**占空比 % |

FanNumber：`1` = CPU 风扇，`2` = 内存风扇。

`GetFanControl` 把两个风扇的实际转速打包进同一个 uint32（与传哪个 FanNumber 无关）：

```
FanDuty 低 16 位  ->  FanNumber 1 (CPU 风扇)    的 RPM
FanDuty 高 16 位  ->  FanNumber 2 (内存风扇)    的 RPM
```

各风扇的 MAX 平台转速（本机实测）：

| 风扇 | MAX RPM | 进度条刻度 |
|---|---|---|
| CPU（fan1） | ~4700 | 4800 |
| 内存（fan2） | ~2400 | 2500 |

---

## 实测数据

### 固件 AUTO 曲线 vs 手动档位

CPU 风扇（满载，CPU ≈ 57 °C）：

| 模式 | RPM |
|---|---|
| **固件 AUTO @57 °C** | **1619** |
| 手动 35% | 1967 |
| 手动 50% | 2697 |
| 手动 65% | 3388 |
| 手动 80% | 4007 |
| 手动 100% | 4739 |

内存风扇（空载，内存 54–58 °C）：

| 模式 | RPM |
|---|---|
| **固件 AUTO @58.5 °C** | **1744** |
| 手动 35% | 1064 |
| 手动 50% | 1505 |
| 手动 65% | 1865 |
| 手动 80% | 2186 |
| 手动 100% | 2383 |

两个风扇的 AUTO 曲线都相当「懒」：

- **CPU 风扇**的 AUTO 甚至低于手动 35%。
- **内存风扇**的 AUTO 约等于手动 55–58%。

### AUTO 对温度的响应

跑内存密集负载（18.8 GB 的 LLM 模型）期间连续采样：

| 内存温度 | 51.0 °C | 53.0 °C | 56.0 °C | 60.5 °C | 62.2 °C |
|---|---|---|---|---|---|
| AUTO 下内存风扇 | 1741 | 1736 | 1737 | 1743 | **1740** |

**内存温度涨了 11 °C，风扇一格没动。** 固件 AUTO 在内存风扇上基本不随温度变化。

而 DDR5 模块自身上报的 `Thermal Sensor High Limit` 就是 **55 °C**——
也就是说从 55 °C 起，固件曲线已经落后于模块自己的安全阈值，却毫无察觉。

按 fanguard 的内存风扇曲线（50 °C 交回 AUTO，75 °C 拉满）：

| 内存温度 | AUTO 实测 | fanguard 指令 | 对应 RPM |
|---|---|---|---|
| 51 °C | 1741 | AUTO | ~1740 |
| 56 °C | 1737 | 50% | 1505 |
| 62 °C | 1740 | 65% | 1865 |
| 75 °C | ~1740 | **100%** | **2383（比 AUTO 高 37%）** |

> 接管的瞬时温度如果正好落在 50–55 °C，两者比较接近，可能有几秒的轻微回落；
> 闭环控制会在几个 tick 内自行修正。

---

## LibreHardwareMonitor 依赖

`vendor/LibreHardwareMonitor/` 里只放了运行时真正需要的 10 个程序集
（已剔除 Aga.Controls / OxyPlot 等 GUI 组件）：

```
LibreHardwareMonitorLib.dll      主程序集
LibreHardwareMonitor.config      上述程序集的绑定重定向（缺了会加载失败）
RAMSPDToolkit-NDD.dll            DIMM / SPD 温度传感器
BlackSharp.Core.dll, HidSharp.dll
System.Buffers.dll, System.Memory.dll, System.Numerics.Vectors.dll,
System.Runtime.CompilerServices.Unsafe.dll
Microsoft.Bcl.HashCode.dll
```

换机器时从 LibreHardwareMonitor 安装目录复制以上文件即可。
**`LibreHardwareMonitor.config` 缺一个就会 `FileNotFoundException`。**

**驱动**：不需要额外安装。LibreHardwareMonitor 用的是 Windows WDK 签名的
`PawnIO` 驱动，它作为正规驱动包装在 DriverStore 里（`pawnio.inf`），
LHM 会自己按需加载。

---

## 注意事项

- 风扇模式**重启后不保留**，每次开机回到 BIOS/EC 默认值。
  装成服务（`fanguard-install.bat`）之后，风扇调节会在开机时自动恢复。
- 不要把 CPU 风扇长期压在 35% 以下低温区运行。
- `fanguard` 的退出路径（正常结束 / `Ctrl+C` / 关掉窗口 / `net stop` /
  传感器连续失败放弃）都会在 `finally` 里把两个风扇交回固件 AUTO
  （`--no-restore` 可关闭）。
  前台运行时它还会监视父进程存活，关掉启动窗口时跟着退出。
- ⚠️ **强杀进程会跳过恢复。** 用任务管理器 / `taskkill /F` 结束前台的 `fanguard`
  时，风扇会停在上一次的占空比上。想干净退出请用 `Ctrl+C`、`--stop-service`
  或直接关窗口。服务被强杀时 SCM 会按失败动作把它拉起来，风扇仍是 AUTO。
- 服务的日志在事件查看器里（应用程序 → `fa880-fan`），不在任何文件里。
- 这些脚本只操作风扇，不会碰其它硬件设置。