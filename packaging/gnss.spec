from pathlib import Path
import os

root = Path(os.environ['GNSS_BUILD_SOURCE'])
icon = os.environ['GNSS_BUILD_ICON']
gui = Analysis([str(root/'tools/launch_gui_stage4_2.py')], pathex=[str(root)],
               datas=[(str(root/'gnss_logger/gui/resources'), 'gnss_logger/gui/resources')],
               excludes=['tkinter', 'pytest'])
cli = Analysis([str(root/'packaging/launch_cli.py')], pathex=[str(root)], excludes=['tkinter', 'unittest', 'pytest', 'PySide6'])
g = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name='gnss-logger-gui', console=False, upx=False, icon=icon)
c = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name='gnss-logger', console=True, upx=False, icon=icon)
coll = COLLECT(g, c, gui.binaries, gui.datas, cli.binaries, cli.datas, name='GNSSLogger', upx=False)
