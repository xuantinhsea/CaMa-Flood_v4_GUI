@echo off
rem ===========================================================================
rem  build_installer.bat -- build the Windows installer
rem                         dist\installer\CaMa_Floodcast_Setup_<version>.exe
rem
rem    build_installer.bat           full build (freezes the app first)
rem    build_installer.bat noexe     reuse the existing dist\CaMa_Floodcast
rem
rem  Steps:
rem    1. build_exe.bat                    freeze the app -> dist\CaMa_Floodcast
rem    2. packaging\md_to_docx.py          USER_MANUAL.md -> USER_MANUAL.docx
rem       packaging\update_docx_fields.ps1 fill in its contents page (needs Word;
rem                                        skipped with a note otherwise)
rem    3. packaging\stage_installer.py     example projects (paths made relative,
rem                                        cleaned), maps, forcing, MinGW runtime,
rem                                        docs -> build\installer_stage
rem    4. Inno Setup 6 (ISCC.exe)          packaging\CaMa_Floodcast.iss
rem
rem  Needs: the Python that build_exe.bat uses (with the app's dependencies, plus
rem  python-docx and pefile), the MinGW runtime the Fortran tools were built with
rem  (gfortran on PATH or C:\msys64\ucrt64\bin), and Inno Setup 6 -- found on PATH,
rem  in its default per-user or per-machine folder, or via set ISCC=...\ISCC.exe.
rem  The example data is read from ..\map and ..\inp of this CaMa-Flood package.
rem ===========================================================================
setlocal EnableExtensions

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"
if not defined PYTHON set "PYTHON=python"
set "RC=0"

rem ---- 0. tools ---------------------------------------------------------------
if defined ISCC if exist "%ISCC%" goto :have_iscc
for %%I in (ISCC.exe) do if not "%%~$PATH:I"=="" set "ISCC=%%~$PATH:I"
if defined ISCC goto :have_iscc
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC goto :have_iscc
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if defined ISCC goto :have_iscc
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC goto :err_iscc
:have_iscc
"%PYTHON%" -c "import docx, pefile" >nul 2>&1 || goto :err_pydeps
rem "call" keeps cmd from stripping the quotes around %PYTHON% inside for /f.
for /f "delims=" %%V in ('call "%PYTHON%" -c "import camafloodcast; print(camafloodcast.__version__)"') do set "VERSION=%%V"
if not defined VERSION goto :err_version
echo === Building the CaMa_Floodcast %VERSION% installer ===
echo.

rem ---- 1. frozen application --------------------------------------------------
if /i "%~1"=="noexe" goto :skip_exe
echo [1/4] Freezing the application (build_exe.bat)...
call "%ROOT%\build_exe.bat" < nul || goto :err_exe
:skip_exe
if not exist "%ROOT%\dist\CaMa_Floodcast\CaMa_Floodcast.exe" goto :err_exe
echo [1/4] Application: dist\CaMa_Floodcast

rem ---- 2. user manual ---------------------------------------------------------
echo [2/4] Generating USER_MANUAL.docx...
"%PYTHON%" "%ROOT%\packaging\md_to_docx.py" || goto :err_manual
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\packaging\update_docx_fields.ps1" "%ROOT%\USER_MANUAL.docx" ^
    || echo   note: Microsoft Word not available - the manual's contents page fills in when it is first opened.

rem ---- 3. stage examples, runtime and docs -----------------------------------
echo [3/4] Staging example projects, model runtime and documentation...
"%PYTHON%" "%ROOT%\packaging\stage_installer.py" || goto :err_stage

rem ---- 4. compile -------------------------------------------------------------
echo [4/4] Compiling the installer (Inno Setup; compression takes several minutes)...
"%ISCC%" /Qp /DAppVersion=%VERSION% "%ROOT%\packaging\CaMa_Floodcast.iss" || goto :err_iscc_build

echo.
echo Installer complete:  %ROOT%\dist\installer\CaMa_Floodcast_Setup_%VERSION%.exe
goto :end

rem ---- errors -------------------------------------------------------------------
:err_iscc
echo ERROR: Inno Setup 6 (ISCC.exe) not found. Install it from https://jrsoftware.org/isdl.php
echo        or set ISCC to the full path of ISCC.exe.
goto :failed
:err_pydeps
echo ERROR: "%PYTHON%" needs python-docx and pefile:  "%PYTHON%" -m pip install python-docx pefile
goto :failed
:err_version
echo ERROR: could not read camafloodcast.__version__ with "%PYTHON%".
goto :failed
:err_exe
echo ERROR: build_exe.bat did not produce dist\CaMa_Floodcast\CaMa_Floodcast.exe.
goto :failed
:err_manual
echo ERROR: could not generate USER_MANUAL.docx.
goto :failed
:err_stage
echo ERROR: staging failed - see the messages above.
goto :failed
:err_iscc_build
echo ERROR: Inno Setup could not compile packaging\CaMa_Floodcast.iss.
goto :failed

:failed
set "RC=1"
echo.
echo BUILD FAILED

:end
echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause
endlocal & exit /b %RC%
