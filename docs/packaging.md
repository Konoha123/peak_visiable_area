# 打包指南

将本项目打包为**免安装、双击即用**的发行包：用户解压后双击启动脚本，浏览器自动打开
应用页面。本指南面向打包者，不假定操作系统——任何装有**基础 conda** 的
Windows / macOS / Linux 机器均可按同一流程操作。

> 本文所有步骤均已在 Ubuntu 24.04 + conda 24.11 环境上实测通过；Windows / macOS 步骤
> 与之等价，仅路径与归档命令不同，文中差异处已单独标注。

---

## 1. 产物形态与总体思路

发行包是一个自包含目录的压缩包，结构如下：

```
pva-<版本>-<平台>-<架构>/        ← 发行包根目录（解压后即此结构）
├── env/                # conda-pack 导出的完整 Python 运行环境（约 570 MB 解压后）
├── app/                # 后端源码
├── static/             # 前端（Leaflet 免构建）
├── run.bat             # Windows 启动脚本（双击）
├── run.command         # macOS 启动脚本（双击）
├── run.sh              # Linux 启动脚本（双击或终端运行）
└── 使用说明.md          # 用户使用说明（复制自 docs/user-guide.md）
```

总体思路：

1. 用仓库根的 `environment-dist.yml`（仅运行时依赖，不含 pytest/ruff）创建一个干净的
   conda 环境；
2. 用 **conda-pack** 把该环境导出为可搬迁的 tar.gz；
3. 组装发行目录（环境 + 代码 + 启动脚本 + 使用说明）；
4. 冒烟验证后压缩归档。

**重要**：conda-pack 导出的环境与构建时的操作系统和 CPU 架构绑定。要产出 Windows /
macOS / Linux 的包，需**分别在对应系统的机器上**执行本流程（每台机器只要求基础
conda）。x86_64 与 arm64（Apple Silicon）亦不可混用。

## 2. 打包环境要求

- 任意操作系统 + **基础 conda**（Miniconda / Miniforge / Anaconda 均可），无需其他前置；
- 磁盘空间 ≥ 3 GB（构建环境 + tar 包 + 发行目录）；
- 外网可达（conda-forge、PyPI）；
- 仓库工作区干净（建议先 `git status` 确认无未提交的临时改动）。

## 3. 一次性准备：conda-pack 工具环境

```bash
conda create -y -n pva-pack-tools -c conda-forge conda-pack
```

后续所有打包命令统一通过该环境执行。

> **务必调用 `conda-pack` 可执行程序（带连字符），不要用 `conda pack` 子命令形式。**
> 若基础环境（base）里残留旧版 conda-pack（如 0.6.0），`conda pack` 子命令会经由
> base 的 conda 插件机制分派到旧版本，其 site-packages 探测存在缺陷，会对完全正常的
> 环境误报 "Files managed by conda were found to have been deleted/overwritten"
> （报错路径特征是 `lib/python3.1/...` 这类被截断的目录名）。本项目打包流程实测踩过
> 此坑。检查方式：`conda list -n base | grep conda-pack`，若存在旧版本，坚持使用
> 下文的 `conda run -n pva-pack-tools conda-pack ...` 形式即可完全绕开。

## 4. 标准打包流程

以下命令均在**仓库根目录**执行（Windows 下在 "Anaconda Prompt" 或已初始化 conda 的
PowerShell 中执行，路径分隔符按习惯替换）。

### 4.1 构建打包环境

```bash
conda env create -f environment-dist.yml
```

创建名为 `pva-dist-env` 的环境（python 3.13 + GDAL + requirements.txt 中的运行时
pip 依赖）。首次执行约需 3–10 分钟。

### 4.2 导出环境（conda-pack）

```bash
conda run -n pva-pack-tools conda-pack -n pva-dist-env -o build/pva-dist-env.tar.gz
```

产物约 200 MB。该过程将环境整体搬迁路径做了前缀占位处理，目标机器解压后由启动脚本
的 conda-unpack 步骤修复（见第 5 节）。

### 4.3 组装发行目录

```bash
# 以下以 bash 语法给出；Windows 用等价命令（robocopy / 资源管理器复制）即可
VERSION=0.1.0            # 建议取 git describe --tags 或手动指定
PLATFORM=$(conda info --json | python -c "import json,sys; print(json.load(sys.stdin)['platform'])")
DIST="build/pva-$VERSION-$PLATFORM"

rm -rf "$DIST" && mkdir -p "$DIST/env"
tar -xzf build/pva-dist-env.tar.gz -C "$DIST/env"

# 应用代码与前端（排除 __pycache__）
rsync -a --exclude='__pycache__' app static "$DIST/"

# 启动脚本（仓库中维护，打包时原样取用，见第 5 节）
cp packaging/run.bat packaging/run.command packaging/run.sh "$DIST/"

# 用户使用说明
cp docs/user-guide.md "$DIST/使用说明.md"
```

`rsync` 在 Windows 不可用时，用 `xcopy /E /I app build\...\app`（先删除 `__pycache__`）
或任意复制工具均可，关键是**不要把 `__pycache__`、`.pytest_cache`、`.git` 等开发
残留带进发行包**。

### 4.4 冒烟验证（必做）

在发行目录里以"用户身份"启动并验证。Linux/macOS：

```bash
cd "$DIST"
chmod +x run.sh run.command
PVA_NO_BROWSER=1 ./run.sh &        # PVA_NO_BROWSER=1：不自动开浏览器（适配无头验证）
```

看到 `服务已就绪：http://127.0.0.1:8000` 后，逐项验证：

| 检查项 | 命令 / 操作 | 预期 |
| --- | --- | --- |
| 健康检查 | `curl http://127.0.0.1:8000/api/health` | `{"status":"ok"}` |
| 页面加载 | 浏览器打开 `http://127.0.0.1:8000` | 地图与侧栏正常显示 |
| 可视域（自研引擎） | 页面输入一个坐标并【确认输入】 | 曲率圆 + 可视域叠加出现 |
| 可视域（GDAL 引擎） | 高级设置切到 GDAL Viewshed 后重算 | 正常出图（此项验证 `gdal_viewshed` 子进程与 GDAL/PROJ 数据路径） |
| 点选测高 | 地图内点击任意点 | 抬升高度 + 剖面图 + 测地线返回 |
| 停止行为 | 关闭启动窗口（或 `kill` 启动脚本进程） | 服务进程一并退出，端口释放 |

无浏览器环境可省略页面项，但 API 三项（health / viewshed 两引擎 / lift）必须全过。
验证通过后停止服务（结束 `run.sh` 进程或关闭其终端）。

Windows 的等价验证：双击 `run.bat`（或 `set PVA_NO_BROWSER=1` 后命令行运行），其余
同上（`curl` 可用系统自带 `curl.exe`）。

### 4.5 归档

按目标平台选择归档格式（兼顾"保留可执行权限"与"系统原生解压"）：

```bash
# Windows 目标：.zip（资源管理器双击即可解压；.bat 不需要可执行位）
cd build && powershell Compress-Archive -Path "pva-$VERSION-win-64" -DestinationPath "pva-$VERSION-win-64.zip"
#   （在 Linux 上打 Windows 包时改用：zip -r pva-$VERSION-win-64.zip pva-$VERSION-win-64/）

# macOS / Linux 目标：.tar.gz（保留 +x 权限）
tar -czf "pva-$VERSION-$PLATFORM.tar.gz" -C build "pva-$VERSION-$PLATFORM"
```

产物命名规范：`pva-<版本>-<平台>-<架构>.<zip|tar.gz>`，平台取值如 `win-64`、
`osx-arm64`、`osx-64`、`linux-64`（与 conda 的 platform 字串一致，见 4.3 的
`PLATFORM` 变量）。

> 不要用 `python -m zipfile` 制作归档：实测其不保留 Unix 可执行位，会导致
> `run.sh`/`run.command` 解压后不可执行。

### 4.6 清理（可选）

```bash
conda env remove -n pva-dist-env        # 移除构建环境（pva-pack-tools 建议保留复用）
rm -rf build                            # 移除中间产物
```

## 5. 启动脚本说明

三个启动脚本由仓库 `packaging/` 目录维护，打包时**原样复制**进发行包根目录，不在
打包时生成或改写。统一行为逻辑：

1. 自我定位到发行包根目录，校验 `env/`、`app/`、`static/` 完整性；
2. **首次运行**执行 conda-unpack 修复打包机路径前缀（成功后写标记文件
   `env/.pva-unpacked`，之后跳过；该步骤约数十秒）；
3. 设置环境二进制路径与 GDAL/PROJ 数据路径（`GDAL_DATA`、`PROJ_LIB`——平时由
   `conda activate` 注入，启动脚本手动指认，缺省时 GDAL 引擎与离线 GeoTIFF 读取会
   报 `Open of .../share/proj failed`）；
4. 从 8000 起探测可用端口（8000–8009），避免端口冲突；
5. 后台启动 `python -m uvicorn app.main:app --host 127.0.0.1 --port <N>`（仅绑定
   本机回环地址，不触发系统防火墙弹窗）；
6. 轮询 `/api/health` 就绪（最多 60 秒）后**自动打开默认浏览器**；失败时打印
   `pva-server.log` 最后 20 行日志；
7. 窗口保持显示运行状态，关闭窗口（或 Ctrl+C）即停止服务。

各平台差异：

| | 文件 | 双击启动 | 停止方式 | 备注 |
| --- | --- | --- | --- | --- |
| Windows | `run.bat` | 支持 | 关闭 cmd 窗口 | UTF-8 编码 + `chcp 65001`；GDAL DLL 经 `env\Library\bin` 注入 PATH |
| macOS | `run.command` | 支持（Terminal 自动打开） | 关闭 Terminal 窗口 | 首次运行可能遇 Gatekeeper 拦截，处理方式见使用说明 |
| Linux | `run.sh` | 取决于文件管理器（多数支持"在终端中运行"） | 关闭终端窗口 | 需可执行位（tar.gz 归档已保留；否则 `chmod +x run.sh`） |

环境变量开关：`PVA_NO_BROWSER=1`——启动后不自动打开浏览器（无头/自动化场景）。

**维护约束**：启动脚本中固化了应用启动形态（`app.main:app` 入口、`static/` 布局、
端口策略 8000–8009、日志文件名 `pva-server.log`）。若这些约定发生变化，必须同步
修改 `packaging/run.*` 三个脚本并重打包。

## 6. 常见问题（打包侧）

- **conda-pack 报环境不一致（pip 文件被覆盖）**：先确认不是第 3 节所述旧版
  conda-pack 误报（看报错路径是否为 `lib/python3.1/...` 截断形式）。若确为真实不一致，
  处理方式是**重建环境**（`conda env remove -n pva-dist-env` 后重新 4.1 起步），不要
  在不一致环境上做 `conda remove` 局部修补——实测可能触发解算器破坏性卸载（连带删除
  python/GDAL 等），且打出的包是残缺环境。
- **GDAL 引擎 502 / `Open of .../share/proj failed`**：启动脚本的 `PROJ_LIB`/
  `GDAL_DATA` 注入失效（env 目录结构变化或脚本被改写），核对第 5 节第 3 步。
- **打包体积**：环境 tar.gz 约 200 MB，解压后 env 约 570 MB，发行包整体（zip/tar.gz）
  约 200 MB。属正常量级（CPython + GDAL + NumPy）。
- **在线源首次验证慢**：DEM 瓦片与陆海掩膜数据在目标用户机上首次使用需联网下载并
  缓存（`~/.cache/pva/`），属预期行为；冒烟验证时首次请求可能耗时 20–60 秒。
- **Apple Silicon**：在 arm64 机器上按同一流程打包，产物命名 `osx-arm64`；不要把
  x86_64 包发给 Apple Silicon 用户（Rosetta 下未验证）。

## 7. 版本管理与维护提醒

- 发行版本号建议取 `git describe --tags`（如 `v0.1.0`），无 tag 时用日期
  （如 `0.1.0-20260911`）。
- 依赖变更同步规则（详见仓库 `AGENTS.md` 依赖记录条款）：
  - 新增**运行时**依赖 → 只改 `requirements.txt`（两个 environment*.yml 经
    `-r` 自动继承）；
  - 新增**开发**依赖 → 只改 `environment.yml`；
  - 升级 **conda 层**（python / GDAL 版本）→ `environment.yml` 与
    `environment-dist.yml` 两处手动同步。
- `environment-dist.yml` 必须保持在仓库根目录（conda 对 `-r` 相对路径按该文件所在
  目录解析）。
- 启动脚本仅存于 `packaging/`，修改后无需提交构建产物（`build/` 与 `dist` 均已在
  `.gitignore` 中）。
- 使用说明的唯一源文件是 `docs/user-guide.md`，打包时复制为发行包内 `使用说明.md`；
  不要在发行包内单独维护副本。
