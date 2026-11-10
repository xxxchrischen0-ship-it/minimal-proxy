# 极简代理

基于 GOST 与 CustomTkinter 开发的极简 Windows SOCKS5 代理客户端。采用简洁的白色界面，在后台静默运行 GOST，支持一键接管系统代理，并在停止或关闭时恢复原有系统代理设置。内置实时折线图，显示整机上传和下载速率。

## 安装与运行

1. 在 Windows 上安装 Python 3.12，然后在项目目录打开 PowerShell，安装依赖。

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. 从 GOST 官方发布页下载适合电脑架构的 Windows 版 GOST v3。解压后将可执行文件命名为 `gost.exe`，放在 `proxy_gui.pyw` 同一目录。[GOST Releases](https://github.com/go-gost/gost/releases)
3. 在项目目录运行客户端。

   ```powershell
   python proxy_gui.pyw
   ```

4. 填写自己的 SOCKS5 节点 IP、端口、用户名和密码，点击「启动代理」。没有本地配置时，四项均为空。点击「停止代理」或关闭窗口会停止客户端启动的 GOST 进程，并尝试恢复原来的系统代理设置。

## 打包为单文件 EXE

在 Windows 的项目目录执行以下命令。打包前先按上文安装依赖，并把 `gost.exe` 放到位。[PyInstaller 命令参考](https://pyinstaller.org/en/stable/usage.html)

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx --name MinimalProxy --collect-all customtkinter --collect-all psutil --add-binary "gost.exe:." proxy_gui.pyw
```

产物为 `dist\MinimalProxy.exe`，内含 GOST 和 Python 依赖。若分发包含 GOST 的构建产物，请同时提供上游许可证及版权声明。[GOST 许可证](https://github.com/go-gost/gost/blob/master/LICENSE)

## 本地配置与提交

点击「启动代理」后，客户端会尝试把节点配置保存到程序旁边的 `config.json`，下次打开时自动回填。密码在界面中显示为星号，配置文件中的密码以明文保存。请仅在本地保留该文件。

提交源码时应包含 `proxy_gui.pyw`、`requirements.txt`、`README.md` 和 `.gitignore`。`config.json`、`gost.exe`、构建产物和 Python 缓存均为本地文件，已列入 `.gitignore`。
