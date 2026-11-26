@echo off
rem ===========================================================================
rem  build_exe.bat -- build the standalone CaMa_Floodcast.exe with PyInstaller
rem
rem    build_exe.bat          windowed app  -> dist\CaMa_Floodcast\CaMa_Floodcast.exe
rem    build_exe.bat debug    same app with a console window that shows tracebacks
rem                           -> dist\CaMa_Floodcast_debug\CaMa_Floodcast_debug.exe
rem
rem  Uses "python" from PATH. To use another interpreter, first run e.g.
rem    set PYTHON=C:\Users\me\AppData\Local\Programs\Python\Python313\python.exe
rem  That interpreter needs the app's own dependencies -- pip install -e ".[gui]",
rem  plus whichever optional extras (geo, netcdf, download) the exe should support:
rem  PyInstaller bundles what is installed, and the exe falls back exactly as the
rem  source does for what is not. PyInstaller itself is installed if missing.
rem
rem  The result is a folder, not a single file: QtWebEngine (the map basemap) ships
rem  a helper process and resources a one-file exe would re-extract on every start.
rem  Move or zip the whole dist\CaMa_Floodcast folder, not just the .exe, and keep
rem  it at a path under ~170 characters: its deepest DLLs sit ~80 characters inside
rem  it, and Windows will not load a DLL past its 260-character path limit.
rem  bin\win64 (the Fortran domain/downscale tools) is copied into it. The model
rem  itself (MAIN_cmf.exe) is found in ..\src while the folder stays inside this
rem  package; anywhere else, set it once via Simulation -> Model binary.
rem ===========================================================================
setlocal EnableExtensions

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

if not defined PYTHON set "PYTHON=python"
set "NAME=CaMa_Floodcast"
set "MODE=--windowed"
if /i "%~1"=="debug" set "NAME=CaMa_Floodcast_debug"
if /i "%~1"=="debug" set "MODE=--console"
set "ICON=%ROOT%\camafloodcast\resources\camafloodcast.ico"
set "RC=0"

echo === Building %NAME% ===
echo.

rem ---- 1. interpreter and the app's own dependencies ------------------------
"%PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 || goto :err_python
"%PYTHON%" -c "import PyQt6.QtWidgets, numpy, matplotlib" >nul 2>&1 || goto :err_deps
"%PYTHON%" -c "import PyQt6.QtWebEngineWidgets" >nul 2>&1 || echo   note: PyQt6-WebEngine is not installed - the exe will use the native map, without a basemap.

"%PYTHON%" -c "import PyInstaller" >nul 2>&1 && goto :have_pyinstaller
echo [1/4] Installing PyInstaller...
"%PYTHON%" -m pip install "pyinstaller>=6.0" || goto :err_pyinstaller
:have_pyinstaller
echo [1/4] Python and dependencies OK.

rem ---- 2. application icon --------------------------------------------------
if exist "%ICON%" goto :have_icon
echo [2/4] Rendering the application icon...
"%PYTHON%" "%ROOT%\packaging\make_icon.py" || goto :err_icon
:have_icon
echo [2/4] Icon: camafloodcast\resources\camafloodcast.ico

rem ---- 3. freeze ------------------------------------------------------------
rem A running copy locks its own files, and PyInstaller would fail half-way
rem through replacing them.
tasklist /fi "imagename eq %NAME%.exe" 2>nul | find /i "%NAME%.exe" >nul && goto :err_running

echo [3/4] Running PyInstaller (this takes a few minutes)...
rem rasterio (GeoTIFF import/export, the geo extra) has no PyInstaller hook: its
rem compiled modules import siblings PyInstaller cannot see, and it looks for its
rem own gdal_data/proj_data folders (proj.db) beside itself -- so collect both.
rem
rem Excluded: packages the app never imports, which would otherwise be pulled in
rem through optional imports inside its dependencies. The big one is Earth Engine's
rem table converter (ee.table_converter -> geopandas), which on its own drags in
rem pyarrow, dask, numba/llvmlite, fiona, pyogrio, h5py, zarr and even sphinx --
rem roughly half the build -- for GeoDataFrame output this app never asks for.
"%PYTHON%" -m PyInstaller --noconfirm --clean --noupx --log-level WARN %MODE% ^
    --name "%NAME%" ^
    --icon "%ICON%" ^
    --add-data "%ROOT%\camafloodcast\resources;camafloodcast\resources" ^
    --paths "%ROOT%" ^
    --distpath "%ROOT%\dist" ^
    --workpath "%ROOT%\build\pyinstaller" ^
    --specpath "%ROOT%\build" ^
    --collect-submodules rasterio ^
    --collect-data rasterio ^
    --exclude-module geopandas ^
    --exclude-module pyarrow ^
    --exclude-module dask ^
    --exclude-module distributed ^
    --exclude-module numba ^
    --exclude-module llvmlite ^
    --exclude-module fiona ^
    --exclude-module pyogrio ^
    --exclude-module h5py ^
    --exclude-module zarr ^
    --exclude-module numcodecs ^
    --exclude-module sphinx ^
    --exclude-module babel ^
    --exclude-module scipy ^
    --exclude-module pandas ^
    --exclude-module xarray ^
    --exclude-module pyqtgraph ^
    --exclude-module tkinter ^
    --exclude-module IPython ^
    --exclude-module ipykernel ^
    --exclude-module jupyter_client ^
    --exclude-module zmq ^
    --exclude-module notebook ^
    --exclude-module pytest ^
    --exclude-module _pytest ^
    --exclude-module PyQt5 ^
    --exclude-module PySide2 ^
    --exclude-module PySide6 ^
    "%ROOT%\packaging\camafloodcast_launcher.py" || goto :err_build

rem Only a debug build of Qt loads this (72 MB); the release Qt shipped here uses
rem qtwebengine_devtools_resources.pak beside it, which stays.
del /q "%ROOT%\dist\%NAME%\_internal\PyQt6\Qt6\resources\qtwebengine_devtools_resources.debug.pak" 2>nul

rem ---- 4. companion tools ---------------------------------------------------
if not exist "%ROOT%\bin\win64" goto :no_tools
echo [4/4] Copying bin\win64 (Fortran domain/downscale tools)...
xcopy "%ROOT%\bin\win64" "%ROOT%\dist\%NAME%\bin\win64\" /e /i /y /q >nul || goto :err_copy
goto :done
:no_tools
echo [4/4] note: bin\win64 not found - Domain Builder and Downscaling will need their tools.

:done
echo.
echo Build complete:  %ROOT%\dist\%NAME%\%NAME%.exe
goto :end

rem ---- errors ---------------------------------------------------------------
:err_python
echo ERROR: Python 3.10+ not found as "%PYTHON%". Install it, or set PYTHON to its python.exe.
goto :failed
:err_deps
echo ERROR: the app's dependencies are missing from "%PYTHON%". Run:
echo            "%PYTHON%" -m pip install -e ".[gui]"
goto :failed
:err_pyinstaller
echo ERROR: could not install PyInstaller. Install it by hand: pip install pyinstaller
goto :failed
:err_icon
echo ERROR: could not render the icon (packaging\make_icon.py needs PyQt6 and Pillow).
goto :failed
:err_running
echo ERROR: %NAME%.exe is running. Close it, then build again.
goto :failed
:err_build
echo ERROR: PyInstaller failed - see the messages above.
goto :failed
:err_copy
echo ERROR: could not copy bin\win64 into dist\%NAME%.
goto :failed

:failed
set "RC=1"
echo.
echo BUILD FAILED

:end
rem Keep the window open when started by double-click, so the result can be read.
echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause
endlocal & exit /b %RC%
