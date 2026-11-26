# -*- mode: python ; coding: utf-8 -*-
# 涡流阵列采集系统 v3 (毫伏协议) - PyInstaller 打包配置

import sys
import os

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
        'pyqtgraph',
        'pyqtgraph.graphicsItems.ViewBox.axisCtrlTemplate_pyqt5',
        'pyqtgraph.graphicsItems.PlotItem.plotConfigTemplate_pyqt5',
        'pyqtgraph.imageview.ImageViewTemplate_pyqt5',
        'numpy',
        'PyQt5.sip',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'pandas',
        'PIL',
        'cv2',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 正式版：GUI 模式，不显示控制台
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='涡流阵列采集系统_v3.5.2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # GUI 模式：发布版不显示黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
